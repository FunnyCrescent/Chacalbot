"""
libs.handlers.state_cmds — auto-split from libs/bot_handlers.py.

DO NOT EDIT MANUALLY — regenerate via scripts/split_bot_handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import re as _re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from libs.ai_client import DMEngine, MASTER_PROMPT, MASTER_TOOLS, OpenAIClient
from libs.character_parser import CharacterParser, ParsedCharacter
from libs.creu import get_handler as get_creu_handler
from libs.creu.storage import init_db as init_creu_db, cleanup_stale as creu_cleanup_stale
from libs.config_legacy import (
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, OPENAI_BASE_URL, BASE_DIR, DB_PATH, LOG_PATH, CHARACTERS_DIR,
    ADMIN_CHAT_ID, PLAYER_ROLL_TIMEOUT_SECONDS, COMBAT_PC_TURN_TIMEOUT_SECONDS,
    TRANSLATOR_ENABLED, TRANSLATOR_MODEL, TRANSLATOR_TEMP, TRANSLATOR_MAX_TOKENS, TRANSLATOR_BATCH_DELAY,
    NPC_AI_ENABLED, NPC_AI_MODE, NPC_AI_MODEL, NPC_AI_TEMP, NPC_AI_MAX_TOKENS,
    ANTISPAM_ENABLED, ANTISPAM_DELETE_DELAY, ANTISPAM_DELETE_BATCH_SIZE, ANTISPAM_DELETE_BATCH_DELAY,
    ANTISPAM_DELETE_TYPES, ANTISPAM_DM_ONLY_COMMANDS,
    ADMIN_IS_GAME_MASTER, READONLY_COMMANDS,
    COMBAT_INITIATIVE_ENABLED, COMBAT_TOOL_NAME, COMBAT_END_TOOL_NAME,
    DUAL_NARRATIVE_ENABLED,
    SETTINGS_DIR, SETTINGS_DEFAULT_GENRE,
    SAVED_CHARS_DB,
)
from libs.db import (
    Database, DatabaseManager, HistoryEntry, Session, Character,
    CombatEncounter, Combatant, PlayerLanguage, SettingEntry, RoundMessageTracker,
    SavedCharsDB,
)
from libs.session_manager import SessionManager
from libs.handlers._state import (
    db_manager, dm_engine, sessions, saved_chars_db, md_logger,
    _lang_translations, _lang_round_counter,
    _active_combat_loops, _combat_recovery_sessions,
    _pending_button_rolls, _genre_selections,
    MarkdownLogger, _ChatHandle, init_state,
)

logger = logging.getLogger(__name__)

# Кросс-доменные импорты
from libs.handlers.engine import _db_busy_guard
from libs.handlers.utils import get_session
from libs.handlers.utils import send_safe


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def hp_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/hp — показать HP (все). /hp +N|-N — только Админ."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/cymeriad`")
        return

    # No args = show status (read-only, allowed for everyone)
    if not ctx.args:
        conditions = sessions.get_character_conditions(session.id, char.id)
        cond_str = ""
        if conditions:
            cond_str = "\n\n🌀 **Состояния:** " + ", ".join(c.condition for c in conditions)

        death_info = ""
        if char.hp <= 0:
            death_info = f"\n💀 Спасброски: {char.death_saves_success}✓ / {char.death_saves_failure}✗"

        await send_safe(update, 
            f"❤️ **{char.name}**: {char.hp}/{char.max_hp} HP{cond_str}{death_info}"
        )
        return

    # WRITE operation — DM ONLY
    # SECURITY: Direct modification of HP/GOLD/conditions by users is forbidden.
    # All changes must come through the DM (neural network) via the `Дн.` flow.
    await send_safe(update,
        "🚫 Изменение HP напрямую запрещено. Только просмотр.\n"
        "Все изменения характеристик выполняются Мастером (нейросетью) через реплики `Дн.`."
    )
    return


async def deathsave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """READ-ONLY — death saves managed by neural network via Дн."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "⚠️ Только просмотр. Спасброски от смерти — через `Дн.` (нейросеть).")
        return

    if not ctx.args:
        await send_safe(update, "Использование: `/arbedmarwolaeth имя` или `/arbedmarwolaeth имя success/failure`")
        return

    target_name = ctx.args[0]
    db = db_manager.get_db(session.id)
    chars = db.get_session_characters(session.id)
    target_char = None
    for c in chars:
        if c.name.lower() == target_name.lower():
            target_char = c
            break

    if not target_char:
        await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
        return

    if target_char.hp > 0:
        await send_safe(update, f"✅ {target_char.name} не при смерти (HP: {target_char.hp}).")
        return

    success = None
    if len(ctx.args) > 1:
        if ctx.args[1].lower() in ("success", "успех", "s"):
            success = True
        elif ctx.args[1].lower() in ("failure", "провал", "f"):
            success = False

    result = sessions.death_save(session.id, target_char.id, success)

    if "error" in result:
        await send_safe(update, f"❌ {result['error']}")
        return

    status_emoji = "💀" if result["is_dead"] else "✨" if result["is_stable"] else "🎲"
    status_msg = ""
    if result["is_dead"]:
        status_msg = f"\n\n💀 **{target_char.name} умирает...**"
        # Auto-kick dead player
        player = db.get_player(target_char.player_id, session.id)
        if player:
            if player.is_creator:
                # Transfer session to random other player
                other_players = [p for p in sessions.get_players(session.id) if p.user_id != player.user_id]
                if other_players:
                    new_creator = random.choice(other_players)
                    with db._connect() as conn:
                        conn.execute(
                            "UPDATE players SET is_creator = 0 WHERE user_id = ? AND session_id = ?",
                            (player.user_id, session.id)
                        )
                        conn.execute(
                            "UPDATE players SET is_creator = 1 WHERE user_id = ? AND session_id = ?",
                            (new_creator.user_id, session.id)
                        )
                    status_msg += f"\n\n👑 **Сессия передана {new_creator.display_name}!**"
                else:
                    status_msg += "\n\n⚠️ Создатель умер, в сессии нет других игроков."
            # Remove player and their character
            sessions.remove_player(session.id, player.user_id)
            status_msg += f"\n\n👢 **{player.display_name} и {target_char.name} удалены из сессии.**"
    elif result["is_stable"]:
        status_msg = f"\n\n✨ **{target_char.name} стабилизирован!** 1 HP, без сознания."
        sessions.change_hp(session.id, target_char.id, target_char.name, 1, "стабилизация")

    await send_safe(update, 
        f"{status_emoji} **{target_char.name}** — Спасброски от смерти:\n"
        f"Успехи: {result['successes']}/3  |  Провалы: {result['failures']}/3{status_msg}"
    )


VALID_CONDITIONS = ["blinded", "charmed", "deafened", "frightened", "grappled",
                   "incapacitated", "invisible", "paralyzed", "petrified",
                   "poisoned", "prone", "restrained", "stunned", "unconscious",
                   "concentrating", "exhaustion", "bleeding"]


# BUG #9: canonical D&D 5e Russian translations (and aliases for common English
# forms the AI may emit). The previous dict had non-idiomatic Russian like
# "prone": "лежащий" (means "lying" not "prone") and "incapacitated":
# "недееспособен" (means "incompetent", not "incapacitated"). Now uses the
# official D&D 5e RU PHB terminology, plus a few extra aliases for things the
# Master sometimes emits ("face-down", "stable", "dying", "bloodied") so the
# status list shown to the player is always Russian — never mixed.
CONDITIONS_RU = {
    "blinded": "ослеплён",
    "charmed": "очарован",
    "deafened": "оглох",
    "frightened": "испуган",
    "grappled": "схвачен",
    "incapacitated": "неспособен действовать",
    "invisible": "невидим",
    "paralyzed": "парализован",
    "petrified": "окаменел",
    "poisoned": "отравлен",
    "prone": "повержен",
    "restrained": "сдержан",
    "stunned": "ошеломлён",
    "unconscious": "без сознания",
    "concentrating": "концентрируется",
    "exhaustion": "истощение",
    "bleeding": "кровотечение",

    # Common extra states the AI emits — also need Russian:
    "face-down": "лежит лицом вниз",
    "facedown": "лежит лицом вниз",
    "stable": "стабилизирован",
    "dying": "при смерти",
    "dead": "мёртв",
    "bloodied": "истекает кровью",
    "asleep": "спит",
    "hidden": "скрыт",
    "surprised": "застигнут врасплох",
    "flanked": "во фланге",
    "flying": "летит",
    "swimming": "плывёт",
    "climbing": "лезет",
    "falling": "падает",
    "diseased": "болен",
    "cursed": "проклят",
    "frightened_of": "боится",
    "regenerating": "регенерирует",
    "trapped": "в ловушке",
    "stuck": "застрял",
    "kneeling": "стоит на коленях",
    "crouching": "присел",
    "sitting": "сидит",
    "lying": "лежит",
    "standing": "стоит",
}


async def condition_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """DM ONLY — players cannot add/remove conditions directly."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    # READ: anyone can list their own conditions
    if not ctx.args or ctx.args[0].lower() == "list":
        db = db_manager.get_db(session.id)
        player = db.get_player(user.id, session.id)
        if not player:
            await send_safe(update, "Сначала `/ymuno`")
            return
        char = db.get_character_by_player(user.id, session.id)
        if not char:
            await send_safe(update, "Нет персонажа.")
            return

        conditions = sessions.get_character_conditions(session.id, char.id)
        if not conditions:
            await send_safe(update, f"✅ На **{char.name}** нет активных состояний.")
            return

        lines = [f"🌀 **Состояния {char.name}:**"]
        for c in conditions:
            ru_name = CONDITIONS_RU.get(c.condition, c.condition)
            duration = f" ({c.duration})" if c.duration else ""
            source = f" — от {c.source}" if c.source else ""
            lines.append(f"• {ru_name}{duration}{source}")

        await send_safe(update, "\n".join(lines))
        return

    # SECURITY: All condition changes must go through the DM (neural network) via `Дн.` flow.
    await send_safe(update,
        "🚫 Изменение состояний напрямую запрещено. Только просмотр.\n"
        "Используйте `Дн.` для добавления/снятия состояний через Мастера."
    )
    return

    action = ctx.args[0].lower()

    if action == "add":
        if len(ctx.args) < 3:
            await send_safe(update, f"Использование: `/cyflwr add имя_персонажа название`\n\nДоступные: {', '.join(CONDITIONS_RU.values())}")
            return

        target_name = ctx.args[1]
        db = db_manager.get_db(session.id)
        chars = db.get_session_characters(session.id)
        target_char = None
        for c in chars:
            if c.name.lower() == target_name.lower():
                target_char = c
                break
        if not target_char:
            await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
            return

        condition_name = ctx.args[2].lower()
        reverse_map = {v: k for k, v in CONDITIONS_RU.items()}
        if condition_name in reverse_map:
            condition_name = reverse_map[condition_name]

        if condition_name not in VALID_CONDITIONS:
            await send_safe(update, f"❌ Неизвестное состояние. Доступные: {', '.join(CONDITIONS_RU.values())}")
            return

        duration = ctx.args[3] if len(ctx.args) > 3 else ""
        sessions.add_condition(session.id, target_char.id, target_char.name, condition_name, "DM command", duration)
        ru_name = CONDITIONS_RU.get(condition_name, condition_name)
        await send_safe(update, f"🌀 **{target_char.name}** получает состояние: **{ru_name}**")

    elif action == "remove":
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/cyflwr remove имя_персонажа название`")
            return

        target_name = ctx.args[1]
        db = db_manager.get_db(session.id)
        chars = db.get_session_characters(session.id)
        target_char = None
        for c in chars:
            if c.name.lower() == target_name.lower():
                target_char = c
                break
        if not target_char:
            await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
            return

        condition_name = ctx.args[2].lower()
        reverse_map = {v: k for k, v in CONDITIONS_RU.items()}
        if condition_name in reverse_map:
            condition_name = reverse_map[condition_name]

        sessions.remove_condition(session.id, target_char.id, condition_name)
        ru_name = CONDITIONS_RU.get(condition_name, condition_name)
        await send_safe(update, f"✨ Состояние **{ru_name}** снято с **{target_char.name}**")

    else:
        await send_safe(update, "Использование: `/cyflwr list`")


async def rest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """READ-ONLY — rest through RP (Дн. я отдыхаю)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "⚠️ Только просмотр. Отдых — через `Дн. мы отдыхаем` (нейросеть).")
        return

    if not ctx.args:
        await send_safe(update, "Использование: `/gorffwys short имя` или `/gorffwys long имя`")
        return

    rest_type = ctx.args[0].lower()
    target_name = ctx.args[1] if len(ctx.args) > 1 else ""

    db = db_manager.get_db(session.id)
    chars = db.get_session_characters(session.id)
    target_char = None
    for c in chars:
        if c.name.lower() == target_name.lower():
            target_char = c
            break
    if not target_char:
        await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
        return

    if rest_type in ("short", "короткий", "s"):
        hit_dice = 1
        if len(ctx.args) > 2:
            try:
                hit_dice = int(ctx.args[2])
            except:
                pass

        result = sessions.short_rest(session.id, target_char.id, target_char.name, hit_dice)

        if "error" in result:
            await send_safe(update, f"❌ {result['error']}")
            return

        sessions.advance_time(session.id, hours=1)

        await send_safe(update, 
            f"☕ **{target_char.name}** совершает короткий отдых!\n"
            f"❤️ Восстановлено: +{result['hp_restored']} HP\n"
            f"HP: **{result['new_hp']}**/{result['max_hp']}\n"
            f"⏰ Прошёл 1 час"
        )

    elif rest_type in ("long", "длинный", "l"):
        result = sessions.long_rest(session.id, target_char.id, target_char.name)

        if "error" in result:
            await send_safe(update, f"❌ {result['error']}")
            return

        sessions.advance_time(session.id, hours=8)

        await send_safe(update, 
            f"🌙 **{target_char.name}** совершает длинный отдых!\n"
            f"❤️ HP полностью восстановлено: **{result['new_hp']}**\n"
            f"🌀 Временные состояния сняты\n"
            f"✨ Ресурсы восстановлены\n"
            f"⏰ Прошло 8 часов"
        )
    else:
        await send_safe(update, "Использование: `/gorffwys short имя` или `/gorffwys long имя` (только Админ)")


def _get_session_currency(session) -> dict:
    """Return currency configuration for a session. Falls back to defaults."""
    if session and session.currency_name:
        return {
            "name": session.currency_name or "золото",
            "plural": session.currency_plural or "зм",
            "symbol": session.currency_symbol or "зм",
            "sub_name": session.currency_sub_name or "",
            "sub_plural": session.currency_sub_plural or "серебро",
            "sub_symbol": session.currency_sub_symbol or "см",
            "sub_value": float(session.currency_sub_value) if session.currency_sub_value else 0.1,
            "super_name": session.currency_super_name or "",
            "super_plural": session.currency_super_plural or "платина",
            "super_symbol": session.currency_super_symbol or "пм",
            "super_value": float(session.currency_super_value) if session.currency_super_value else 10.0,
        }
    return {
        "name": "золото", "plural": "ЗМ (золото)", "symbol": "зм",
        "sub_name": "", "sub_plural": "СМ (серебро)", "sub_symbol": "см", "sub_value": 0.1,
        "super_name": "", "super_plural": "ПП (платина)", "super_symbol": "пм", "super_value": 10.0,
    }


async def setcurrency_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/setcurrency — Master sets custom currency names and conversion rates.
    Usage: /setcurrency Серебро см Медь мм 0.1 Платина пм 10
    Required at game start."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Админ может устанавливать валюту.")
        return

    args = ctx.args if ctx.args else []
    if not args:
        cc = _get_session_currency(session)
        text = (
            "💰 <b>Валюта сессии</b>\n\n"
            f"📌 Основная: <b>{cc['name']}</b> ({cc['symbol']})\n"
        )
        if cc.get("sub_name"):
            text += f"📌 Мелкая: {cc['sub_name']} ({cc['sub_symbol']}) — 1 {cc['symbol']} = {int(1/cc['sub_value']) if cc['sub_value'] else '?'} {cc['sub_symbol']}\n"
        else:
            text += f"📌 Мелкая: {cc['sub_plural']} (по умолчанию)\n"
        if cc.get("super_name"):
            text += f"📌 Дорогая: {cc['super_name']} ({cc['super_symbol']}) — 1 {cc['super_symbol']} = {int(cc['super_value'])} {cc['symbol']}\n"
        else:
            text += f"📌 Дорогая: {cc['super_plural']} (по умолчанию)\n"
        text += (
            "\n<b>Формат:</b>\n"
            "<code>/setcurrency Имя символ [имя_мелкое символ_мелкое курс] [имя_дорогое символ_дорогое курс]</code>\n\n"
            "Пример (Серебро > Золото):\n"
            "<code>/setcurrency Серебро см Медь мм 0.1 Платина пм 10</code>\n\n"
            "Пример (обратно к стандарту):\n"
            "<code>/setcurrency Золото зм</code>"
        )
        await send_safe(update, text, raw_html=True)
        return

    # Parse currency arguments
    try:
        primary_name = args[0]
        primary_plural = args[1] if len(args) > 1 else args[0][:3].lower()
        primary_symbol = primary_plural.split()[0] if len(args) > 1 else args[0][:3].lower()

        sub_name = ""
        sub_plural = ""
        sub_symbol = ""
        sub_value = "0.1"
        super_name = ""
        super_plural = ""
        super_symbol = ""
        super_value = "10.0"

        idx = 2
        if len(args) > idx + 1:
            sub_name = args[idx]
            sub_plural = args[idx + 1] if len(args) > idx + 1 else args[idx][:3].lower()
            sub_symbol = sub_plural.split()[0]
            idx += 2
            if len(args) > idx:
                try:
                    sub_value = str(float(args[idx]))
                    idx += 1
                except ValueError:
                    pass
        if len(args) > idx + 1:
            super_name = args[idx]
            super_plural = args[idx + 1] if len(args) > idx + 1 else args[idx][:3].lower()
            super_symbol = super_plural.split()[0]
            idx += 2
            if len(args) > idx:
                try:
                    super_value = str(float(args[idx]))
                except ValueError:
                    pass

        session.currency_name = primary_name
        session.currency_plural = f"{primary_symbol} ({primary_name})"
        session.currency_symbol = primary_symbol
        session.currency_sub_name = sub_name
        session.currency_sub_plural = f"{sub_symbol} ({sub_name})" if sub_name else ""
        session.currency_sub_symbol = sub_symbol if sub_name else ""
        session.currency_sub_value = sub_value if sub_name else ""
        session.currency_super_name = super_name
        session.currency_super_plural = f"{super_symbol} ({super_name})" if super_name else ""
        session.currency_super_symbol = super_symbol if super_name else ""
        session.currency_super_value = super_value if super_name else ""

        db = db_manager.get_db(session.id)
        db.update_session(session)

        result_text = (
            f"✅ <b>Валюта установлена!</b>\n\n"
            f"📌 Основная: {primary_name} ({primary_symbol})\n"
        )
        if sub_name:
            conv = 1 / float(sub_value) if float(sub_value) > 0 else "?"
            result_text += f"📌 Мелкая: {sub_name} ({sub_symbol}) — 1 {primary_symbol} = {int(conv) if conv == int(conv) else conv} {sub_symbol}\n"
        if super_name:
            result_text += f"📌 Дорогая: {super_name} ({super_symbol}) — 1 {super_symbol} = {int(float(super_value))} {primary_symbol}\n"

        await send_safe(update, result_text, raw_html=True)
    except Exception as e:
        await send_safe(update, f"❌ Ошибка: {e}\n\nФормат: `/setcurrency Имя символ`")


async def gold_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/gold — read for players. /gold +N|-N — DM ONLY."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа.")
        return

    balance = sessions.get_gold(session.id, char.id)

    if not ctx.args:
        # Get custom currency from session
        currency_config = _get_session_currency(session)
        lines = [f"💰 **Кошелёк {char.name}:**"]
        has_custom = bool(currency_config.get("name") and currency_config["name"] != "золото")

        # Primary currency (gp)
        if balance["gp"]:
            if has_custom:
                lines.append(f"  {balance['gp']} {currency_config.get('symbol', 'зм')} ({currency_config.get('name', 'золото')})")
            else:
                lines.append(f"  {balance['gp']} ЗМ (золото)")

        # Sub currency (sp) — мелочь
        if balance["sp"]:
            if has_custom and currency_config.get("sub_name"):
                lines.append(f"  {balance['sp']} {currency_config.get('sub_symbol', 'см')} ({currency_config.get('sub_name', 'серебро')})")
            else:
                lines.append(f"  {balance['sp']} СМ (серебро)")

        # Copper (cp) — if custom sub exists, show it; otherwise standard
        if balance["cp"]:
            if has_custom and currency_config.get("sub_name"):
                # In custom currency, cp maps to sub-sub (smallest denomination)
                lines.append(f"  {balance['cp']} мм (медь)")
            else:
                lines.append(f"  {balance['cp']} СМм (медь)")

        # Electrum (ep) — only shown in standard
        if balance["ep"] and not has_custom:
            lines.append(f"  {balance['ep']} ЭМ (электрум)")

        # Super currency (pp) — дорогая
        if balance["pp"]:
            if has_custom and currency_config.get("super_name"):
                lines.append(f"  {balance['pp']} {currency_config.get('super_symbol', 'пм')} ({currency_config.get('super_name', 'платина')})")
            else:
                lines.append(f"  {balance['pp']} ПМ (платина)")

        # Total equivalent in primary currency
        primary_name = currency_config.get("name", "золото")
        primary_symbol = currency_config.get("symbol", "зм")
        total_gp = balance["gp"] + balance["pp"] * 10 + balance["ep"] * 0.5 + balance["sp"] * 0.1 + balance["cp"] * 0.01
        if has_custom:
            lines.append(f"\n📊 Итого: ~{total_gp:.2f} {primary_symbol} ({primary_name})")
        else:
            lines.append(f"\n📊 Эквивалент: ~{total_gp:.2f} зм")

        await send_safe(update, "\n".join(lines))
        return

    # SECURITY: Direct gold modification by users is forbidden.
    await send_safe(update,
        "🚫 Изменение золота напрямую запрещено. Только просмотр.\n"
        "Все финансовые операции выполняются Мастером (нейросетью) через реплики `Дн.`."
    )
    return

    try:
        amount = int(ctx.args[0])
        currency = "gp"
        reason = ""
        if len(ctx.args) > 1 and ctx.args[1].lower() in ("cp", "sp", "ep", "gp", "pp"):
            currency = ctx.args[1].lower()
            reason = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else ""
        else:
            reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else ""

        kwargs = {"cp": 0, "sp": 0, "ep": 0, "gp": 0, "pp": 0, "reason": reason or "ручное изменение"}
        kwargs[currency] = amount

        sessions.add_gold(session.id, char.id, char.name, **kwargs)

        action = "получает" if amount > 0 else "тратит"
        await send_safe(update, 
            f"💰 **{char.name}** {action} **{abs(amount)} {currency.upper()}**"
            f"{f' ({reason})' if reason else ''}"
        )
    except (ValueError, IndexError):
        await send_safe(update, "Использование:\n`/aur` — показать\n`/aur +10 gp награда` (только Админ)")


async def inventory_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/inventory — read for players. add/remove — DM ONLY."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа.")
        return

    if not ctx.args:
        items = sessions.get_inventory(session.id, char.id)
        if not items:
            await send_safe(update, f"🎒 **{char.name}** — инвентарь пуст.")
            return

        lines = [f"🎒 **Инвентарь {char.name}:**"]
        for item in items:
            qty_str = f" x{item['qty']}" if item['qty'] > 1 else ""
            desc_str = f" — {item['desc']}" if item['desc'] else ""
            lines.append(f"• {item['item']}{qty_str}{desc_str}")

        await send_safe(update, "\n".join(lines))
        return

    # SECURITY: Direct inventory modification by users is forbidden.
    await send_safe(update,
        "🚫 Изменение инвентаря напрямую запрещено. Только просмотр.\n"
        "Используйте `Дн.` для добавления/удаления предметов через Мастера."
    )
    return

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/eiddo add имя_персонажа предмет [кол-во]`")
            return

        target_name = ctx.args[1]
        db = db_manager.get_db(session.id)
        chars = db.get_session_characters(session.id)
        target_char = None
        for c in chars:
            if c.name.lower() == target_name.lower():
                target_char = c
                break
        if not target_char:
            await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
            return

        qty = 1
        item_name = " ".join(ctx.args[2:])
        parts = item_name.split()
        if parts[-1].isdigit():
            qty = int(parts[-1])
            item_name = " ".join(parts[:-1])

        sessions.add_item(session.id, target_char.id, target_char.name, item_name, qty)
        await send_safe(update, f"🎒 **{target_char.name}** получает: {item_name} x{qty}")

    elif action in ("remove", "удалить", "r", "rm"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/eiddo remove имя_персонажа предмет [кол-во]`")
            return

        target_name = ctx.args[1]
        db = db_manager.get_db(session.id)
        chars = db.get_session_characters(session.id)
        target_char = None
        for c in chars:
            if c.name.lower() == target_name.lower():
                target_char = c
                break
        if not target_char:
            await send_safe(update, f"❌ Персонаж '{target_name}' не найден.")
            return

        qty = 1
        item_name = " ".join(ctx.args[2:])
        parts = item_name.split()
        if parts[-1].isdigit():
            qty = int(parts[-1])
            item_name = " ".join(parts[:-1])

        sessions.remove_item(session.id, target_char.id, item_name, qty)
        await send_safe(update, f"🗑️ У **{target_char.name}** удалено: {item_name} x{qty}")

    else:
        await send_safe(update, "Использование:\n`/eiddo` — показать\n`/eiddo add имя предмет` (только Админ)")


async def quest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can list quests. DM can add/update."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    char = db.get_character_by_player(user.id, session.id)
    char_id = char.id if char else ""
    char_name = char.name if char else player.display_name

    if not ctx.args or ctx.args[0].lower() == "list":
        status_filter = None
        if len(ctx.args) > 1 and ctx.args[1].lower() in ("active", "completed", "failed", "мои"):
            if ctx.args[1].lower() == "мои":
                quests = sessions.get_quests(session.id, assignee_id=char_id)
            else:
                quests = sessions.get_quests(session.id, status=ctx.args[1].lower())
        else:
            quests = sessions.get_quests(session.id, status="active")

        if not quests:
            await send_safe(update, "📜 Нет активных квестов.")
            return

        lines = ["📜 **Журнал квестов:**"]
        for q in quests:
            status_emoji = {"active": "📋", "completed": "✅", "failed": "❌"}.get(q.status, "📋")
            assignee = f" (для {q.assignee_name})" if q.assignee_name else ""
            lines.append(f"{status_emoji} **{q.title}**{assignee}")
            if q.description:
                lines.append(f"   {q.description[:80]}")

        await send_safe(update, "\n".join(lines))
        return

    # SECURITY: Direct quest management by users is forbidden.
    await send_safe(update,
        "🚫 Управление квестами напрямую запрещено. Только просмотр.\n"
        "Квесты создаются и обновляются Мастером (нейросетью) через `Дн.`."
    )
    return

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        if len(ctx.args) < 2:
            await send_safe(update, "Использование: `/cwest add Название | Описание | @ник_игрока`")
            return

        rest = " ".join(ctx.args[1:])
        parts = rest.split(" | ")
        title = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""

        target_id = ""
        target_name = ""
        if len(parts) > 2:
            target_nick = parts[2].strip().lstrip("@")
            for p in sessions.get_players(session.id):
                if p.username == target_nick or p.display_name == target_nick:
                    target_id = p.user_id
                    tc = db.get_character_by_player(p.user_id, session.id)
                    target_name = tc.name if tc else p.display_name
                    break

        quest_id = sessions.add_quest(session.id, title, description, str(target_id), target_name)
        await send_safe(update, f"📜 **Квест добавлен:** {title}{f' (для {target_name})' if target_name else ''}")

    elif action in ("update", "обновить", "u"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/cwest update ID completed/failed`")
            return

        try:
            quest_id = int(ctx.args[1])
            new_status = ctx.args[2].lower()
            sessions.update_quest(quest_id, status=new_status)
            await send_safe(update, f"📜 Квест #{quest_id} обновлён: **{new_status}**")
        except ValueError:
            await send_safe(update, "❌ ID квеста должен быть числом.")

    else:
        await send_safe(update, "Использование: `/cwest list`")


def _setup_default_resources(session_id: str, char):
    """Setup default resources based on class"""
    db = db_manager.get_db(session_id)
    class_lower = (char.class_name or "").lower()
    resources_map = {
        "barbarian": [("Ярость (Rage)", 2, 2, False, True), ("Перезарядка ярости", 1, 1, True, True)],
        "варвар": [("Ярость (Rage)", 2, 2, False, True), ("Перезарядка ярости", 1, 1, True, True)],
        "bard": [("Вдохновение (Bardic Inspiration)", char.level // 5 + 3 if char.level else 3, char.level // 5 + 3 if char.level else 3, True, True)],
        "бард": [("Вдохновение", 3, 3, True, True)],
        "cleric": [("Изгнание нежити", 1, 1, False, True)],
        "жрец": [("Изгнание нежити", 1, 1, False, True)],
        "druid": [("Дикий облик (Wild Shape)", 2, 2, True, True)],
        "друид": [("Дикий облик", 2, 2, True, True)],
        "fighter": [("Второе дыхание", 1, 1, True, True), ("Действие surge", 1, 1, False, True)],
        "воин": [("Второе дыхание", 1, 1, True, True), ("Действие surge", 1, 1, False, True)],
        "monk": [("Ки (Ki)", char.level if char.level else 2, char.level if char.level else 2, True, True)],
        "монах": [("Ки", char.level if char.level else 2, char.level if char.level else 2, True, True)],
        "paladin": [("Излечивающая длань", 5, 5, False, True), ("Божественный channel", 1, 1, False, True)],
        "паладин": [("Излечивающая длань", 5, 5, False, True), ("Божественный channel", 1, 1, False, True)],
        "ranger": [("Избранный враг", 1, 1, False, True)],
        "следопыт": [("Избранный враг", 1, 1, False, True)],
        "rogue": [("Превосходное везение", 1, 1, False, True)],
        "плут": [("Превосходное везение", 1, 1, False, True)],
        "sorcerer": [("Очки чародейства", char.level if char.level else 2, char.level if char.level else 2, False, True)],
        "чародей": [("Очки чародейства", char.level if char.level else 2, char.level if char.level else 2, False, True)],
        "warlock": [("Чародейские ячейки", 1, 1, True, True)],
        "колдун": [("Чародейские ячейки", 1, 1, True, True)],
        "wizard": [("Восстановление магии", 1, 1, True, True)],
        "маг": [("Восстановление магии", 1, 1, True, True)],
    }

    for res in resources_map.get(class_lower, []):
        sessions.set_resource(session_id, char.id, res[0], res[1], res[2], res[3], res[4])

    if char.level and char.level > 0:
        spell_slots = _get_spell_slots(class_lower, char.level)
        for slot_level, count in spell_slots.items():
            sessions.set_resource(session_id, char.id, f"Слот {slot_level} круга", count, count, False, True)


def _get_spell_slots(class_name: str, level: int) -> dict:
    """Get spell slots for class/level"""
    full_casters = ["wizard", "маг", "sorcerer", "чародей", "bard", "бард", "cleric", "жрец", "druid", "друид"]
    half_casters = ["paladin", "паладин", "ranger", "следопыт"]

    if class_name in full_casters:
        slots_table = {
            1: {1: 2}, 2: {1: 3}, 3: {1: 4, 2: 2}, 4: {1: 4, 2: 3}, 5: {1: 4, 2: 3, 3: 2},
            6: {1: 4, 2: 3, 3: 3}, 7: {1: 4, 2: 3, 3: 3, 4: 1}, 8: {1: 4, 2: 3, 3: 3, 4: 2},
            9: {1: 4, 2: 3, 3: 3, 4: 3, 5: 1}, 10: {1: 4, 2: 3, 3: 3, 4: 3, 5: 2},
        }
        return slots_table.get(min(level, 10), {})
    elif class_name in half_casters:
        slots_table = {
            2: {1: 2}, 3: {1: 3}, 4: {1: 3}, 5: {1: 4, 2: 2}, 6: {1: 4, 2: 2},
            7: {1: 4, 2: 3}, 8: {1: 4, 2: 3}, 9: {1: 4, 2: 3, 3: 2}, 10: {1: 4, 2: 3, 3: 2},
        }
        return slots_table.get(min(level, 10), {})
    return {}
