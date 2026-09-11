"""
libs.handlers.world_cmds — auto-split from libs/bot_handlers.py.

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
from libs.handlers.state_cmds import _setup_default_resources
from libs.handlers.utils import get_session
from libs.handlers.utils import send_safe


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def city_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """H8: what LOCATIONS know about the caller's character — fame, reputation, wanted
    status (personal view). `/dinas all` — every location that exists in the world,
    regardless of contact; not metagaming since players already saw this lore text
    when the world was generated."""
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)

    if ctx.args and ctx.args[0].lower() in ("all", "все"):
        locations = sessions.get_all_locations_brief(session.id)
        if not locations:
            await send_safe(update, "🌍 В реестре пока нет локаций.")
            return
        lines = ["🌍 **Все локации мира:**", ""]
        for l in locations:
            danger = " ⚠️" * min(l.danger_level, 3) if l.danger_level > 1 else ""
            lines.append(f"📍 **{l.name}** _{l.type}_{danger}")
            if l.description:
                lines.append(f"   {l.description[:120]}")
        lines.append("")
        lines.append("_Что конкретное место знает о тебе: `/dinas`_")
        await send_safe(update, "\n".join(lines))
        return

    user = update.effective_user
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return
    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/cymeriad`")
        return

    relations = sessions.get_location_relations_for_character(session.id, char.id)
    if not relations:
        await send_safe(update, f"🏰 Ни один город/локация пока ничего не знает о **{char.name}**.\n\n_Все локации мира: `/dinas all`_")
        return

    lines = [f"🏰 **Что мир знает о {char.name}:**", ""]
    for r in relations:
        fame_desc = "известен" if r.fame >= 0 else "печально известен"
        wanted_str = " 🚨 **В РОЗЫСКЕ**" if r.is_wanted else ""
        lines.append(f"📍 **{r.location_name}**{wanted_str}")
        lines.append(f"   Слава: {r.fame:+d} ({fame_desc}) | Репутация: {r.reputation:+d}")
        if r.notoriety:
            lines.append(f"   {r.notoriety}")
        lines.append("")

    await send_safe(update, "\n".join(lines))


async def relations_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Aggregated relationship map — NPCs known + what locations know about the caller
    + party-wide faction standings, all in one place (previously scattered across
    /npc, /city, /factions with no single view, and grudges/debts were never surfaced
    anywhere at all despite the DB having columns for them). `/perthynasau all` — brief
    per-character overview for the whole party."""
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    show_all = bool(ctx.args) and ctx.args[0].lower() in ("all", "все", "party", "партия")

    if show_all:
        chars = db.get_session_characters(session.id)
        if not chars:
            await send_safe(update, "Нет персонажей.")
            return
        lines = ["🕸️ **Карта отношений — вся партия:**", ""]
        for c in chars:
            known = sessions.get_known_npcs_for_character(session.id, c.id)
            loc_rels = sessions.get_location_relations_for_character(session.id, c.id)
            wanted_locs = [r.location_name for r in loc_rels if r.is_wanted]
            bits = [f"👥 NPC: {len(known)}"]
            if loc_rels:
                bits.append(f"🏰 Локаций знает о нём: {len(loc_rels)}")
            if wanted_locs:
                bits.append(f"🚨 В розыске: {', '.join(wanted_locs)}")
            lines.append(f"**{c.name}** — " + " | ".join(bits))
        lines.append("")
        lines.append("_Подробная карта: `/perthynasau` (свой персонаж)_")
        await send_safe(update, "\n".join(lines))
        return

    user = update.effective_user
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return
    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/cymeriad`\n\n(Обзор партии: `/perthynasau all`)")
        return

    known_npcs = sessions.get_known_npcs_for_character(session.id, char.id)
    loc_rels = sessions.get_location_relations_for_character(session.id, char.id)
    factions = sessions.get_factions(session.id)

    if not known_npcs and not loc_rels and not factions:
        await send_safe(update, f"🕸️ **{char.name}** пока ни с кем и ничем не связан(а).")
        return

    attitude_emoji = {"friendly": "🤝", "helpful": "😊", "neutral": "😐", "unfriendly": "😠", "hostile": "⚔️"}
    lines = [f"🕸️ **Карта отношений — {char.name}**", ""]

    if known_npcs:
        lines.append("👥 **NPC:**")
        for n in known_npcs:
            emoji = attitude_emoji.get(n["attitude"], "😐")
            race_occ = " ".join(filter(None, [n.get("npc_race", ""), n.get("npc_occupation", "")]))
            lines.append(f"{emoji} **{n['npc_name']}**{f' ({race_occ})' if race_occ else ''} — {n['attitude']} (реп: {n['reputation']:+d})")
            if n.get("known_facts"):
                lines.append(f"   💭 Знает: {n['known_facts']}")
            if n.get("grudges"):
                lines.append(f"   😠 Обида: {n['grudges']}")
            if n.get("debts"):
                lines.append(f"   💰 Долг: {n['debts']}")
        lines.append("")

    if loc_rels:
        lines.append("🏰 **Локации:**")
        for r in loc_rels:
            fame_desc = "известна" if r.fame >= 0 else "печально известна"
            wanted_str = " 🚨 **В РОЗЫСКЕ**" if r.is_wanted else ""
            lines.append(f"📍 **{r.location_name}**{wanted_str} — слава {r.fame:+d} ({fame_desc}), реп {r.reputation:+d}")
            if r.notoriety:
                lines.append(f"   {r.notoriety}")
        lines.append("")

    if factions:
        lines.append("🏛️ **Фракции** _(общие для партии)_:")
        for f in factions:
            emoji = attitude_emoji.get(f.attitude, "😐")
            lines.append(f"{emoji} **{f.name}** — {f.attitude} (реп: {f.reputation:+d})")

    await send_safe(update, "\n".join(lines))


async def goals_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Personal goals — distinct from /quest. A quest needs a narratively confirmed
    NPC-given task; a goal is what the character ALREADY wants (from backstory, or a
    stated intention/thought during play), no confirmation required. `/nodau all` —
    party overview (DM or anyone, mirrors /ability all's openness)."""
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)
    show_all = bool(ctx.args) and ctx.args[0].lower() in ("all", "все", "party", "партия")
    status_emoji = {"active": "🎯", "achieved": "✅", "abandoned": "🗑️"}

    if show_all:
        chars = db.get_session_characters(session.id)
        if not chars:
            await send_safe(update, "Нет персонажей.")
            return
        lines = ["🎯 **Личные цели партии:**", ""]
        any_goals = False
        for c in chars:
            goals = sessions.get_character_goals(session.id, character_id=c.id)
            if not goals:
                continue
            any_goals = True
            lines.append(f"**{c.name}:**")
            for g in goals:
                lines.append(f"  {status_emoji.get(g['status'], '🎯')} {g['title']}")
            lines.append("")
        if not any_goals:
            await send_safe(update, "🎯 Ни у кого пока нет отслеживаемых личных целей.")
            return
        await send_safe(update, "\n".join(lines))
        return

    user = update.effective_user
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return
    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/cymeriad`")
        return

    goals = sessions.get_character_goals(session.id, character_id=char.id)
    if not goals:
        await send_safe(update, f"🎯 У **{char.name}** пока нет отслеживаемых личных целей.\n\n_Это не квесты — квесты через `/cwest`._")
        return

    lines = [f"🎯 **Личные цели {char.name}:**", ""]
    for g in goals:
        source_note = " _(из предыстории)_" if g["source"] == "backstory" else ""
        lines.append(f"{status_emoji.get(g['status'], '🎯')} {g['title']}{source_note}")
    await send_safe(update, "\n".join(lines))


async def time_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can view time. DM can advance."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    time_info = sessions.get_time(session.id)

    if not ctx.args:
        await send_safe(update, 
            f"⏰ **Игровое время:**\n"
            f"{time_info['time_str']}\n"
            f"🌤 Погода: {time_info['weather']}\n"
            f"🌡 Температура: {time_info['temperature']}\n"
            f"🍂 Сезон: {time_info['season']}"
        )
        return

    await send_safe(update, "⚠️ Только просмотр. Время — через `Дн.` (нейросеть).")

    arg = ctx.args[0]
    if arg.startswith("+"):
        try:
            if arg.endswith("h"):
                hours = int(arg[1:-1])
                result = sessions.advance_time(session.id, hours=hours)
                await send_safe(update, f"⏰ Время продвинуто на **{hours}ч**. Сейчас: День {result['day']}, {result['hour']:02d}:{result['minute']:02d}")
            elif arg.endswith("m"):
                minutes = int(arg[1:-1])
                result = sessions.advance_time(session.id, minutes=minutes)
                await send_safe(update, f"⏰ Время продвинуто на **{minutes}мин**. Сейчас: День {result['day']}, {result['hour']:02d}:{result['minute']:02d}")
            else:
                hours = int(arg[1:])
                result = sessions.advance_time(session.id, hours=hours)
                await send_safe(update, f"⏰ Время продвинуто на **{hours}ч**. Сейчас: День {result['day']}, {result['hour']:02d}:{result['minute']:02d}")
        except ValueError:
            await send_safe(update, "Использование: `/amser +2h` или `/amser +30m`")
    else:
        await send_safe(update, "Использование: `/amser` — показать")


async def weather_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can view weather. DM can generate."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if ctx.args and ctx.args[0].lower() in ("generate", "gen", "сгенерировать"):
        # Admin/readonly enforcement
        await send_safe(update, "⚠️ Только просмотр. Погода — через `Дн.` (нейросеть).")

        if update.effective_chat:
            await update.effective_chat.send_action(action="typing")
        try:
            result = await sessions.generate_weather(session.id)
            html_text = result.get("html", result.get("text", ""))
            await send_safe(update, html_text, parse_html=False, source="master")
        except Exception as e:
            logger.error(f"Weather error: {e}")
            await send_safe(update, f"❌ Ошибка: {e}")
        return

    time_info = sessions.get_time(session.id)
    await send_safe(update, 
        f"🌤 **Текущая погода:**\n"
        f"Состояние: {time_info['weather']}\n"
        f"Температура: {time_info['temperature']}\n"
        f"Сезон: {time_info['season']}\n\n"
        f"Админ: `/tywydd generate` — сгенерировать новую"
    )


async def factions_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can list factions. DM can add/change rep."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not ctx.args or ctx.args[0].lower() == "list":
        factions = sessions.get_factions(session.id)
        if not factions:
            await send_safe(update, "🏛️ В этом регионе нет известных фракций.")
            return

        lines = ["🏛️ **Фракции:**"]
        for f in factions:
            emoji = {"friendly": "🤝", "helpful": "😊", "neutral": "😐", "unfriendly": "😠", "hostile": "⚔️"}.get(f.attitude, "😐")
            lines.append(f"{emoji} **{f.name}** (репутация: {f.reputation:+d})")
            if f.description:
                lines.append(f"   {f.description[:80]}")

        await send_safe(update, "\n".join(lines))
        return

    await send_safe(update, "⚠️ Только просмотр. Фракции — через `Дн.` (нейросеть).")

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        rest = " ".join(ctx.args[1:])
        parts = rest.split(" | ")
        name = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""

        faction_id = sessions.add_faction(session.id, name, description)
        await send_safe(update, f"🏛️ **Фракция добавлена:** {name}")

    elif action in ("rep", "репутация", "reputation"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/ffactiynau rep ID +5` или `/ffactiynau rep ID -3`")
            return

        try:
            faction_id = int(ctx.args[1])
            delta = int(ctx.args[2])
            sessions.change_reputation(session.id, faction_id, delta)
            await send_safe(update, f"🏛️ Репутация с фракцией #{faction_id} изменена на **{delta:+d}**")
        except ValueError:
            await send_safe(update, "❌ Использование: `/ffactiynau rep ID +N`")

    else:
        await send_safe(update, "Использование: `/ffactiynau list`")


async def location_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        loc = sessions.get_location(session.id, char.id)
        if loc and loc.location_name:
            await send_safe(update, 
                f"📍 **{char.name}** находится в:\n**{loc.location_name}**\n{loc.location_description}"
            )
        else:
            await send_safe(update, f"📍 **{char.name}** — локация неизвестна.")
        return

    await send_safe(update, "⚠️ Только просмотр. Локации — через `Дн.` (нейросеть).")

    parts = " ".join(ctx.args).split(" | ")
    loc_name = parts[0].strip()
    loc_desc = parts[1].strip() if len(parts) > 1 else ""

    sessions.set_location(session.id, char.id, loc_name, loc_desc)
    await send_safe(update, f"📍 **{char.name}** → **{loc_name}**")


async def resources_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can view resources. DM can set/use/recover."""
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
        resources = sessions.get_resources(session.id, char.id)
        if not resources:
            _setup_default_resources(session.id, char)
            resources = sessions.get_resources(session.id, char.id)

        if not resources:
            await send_safe(update, f"⚡ **{char.name}** — нет отслеживаемых ресурсов.")
            return

        lines = [f"⚡ **Ресурсы {char.name}:**"]
        for r in resources:
            pct = (r.current / r.maximum * 100) if r.maximum > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"{r.resource_name}: [{bar}] {r.current}/{r.maximum}")

        await send_safe(update, "\n".join(lines))
        return

    await send_safe(update, "⚠️ Только просмотр. Ресурсы — через `Дн.` (нейросеть).")

    action = ctx.args[0].lower()

    if action in ("set", "установить", "s"):
        if len(ctx.args) < 5:
            await send_safe(update, "Использование: `/adnoddau set имя_персонажа Название текущий максимум`")
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

        try:
            name = ctx.args[2]
            current = int(ctx.args[3])
            maximum = int(ctx.args[4])
            short = len(ctx.args) > 5 and ctx.args[5].lower() in ("short", "s", "true")
            long_r = len(ctx.args) <= 5 or ctx.args[5].lower() not in ("none", "false", "no")

            sessions.set_resource(session.id, target_char.id, name, current, maximum, short, long_r)
            await send_safe(update, f"⚡ **{target_char.name}**: {name} = {current}/{maximum}")
        except ValueError:
            await send_safe(update, "❌ Текущий и максимум должны быть числами.")

    elif action in ("use", "потратить", "u"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/adnoddau use имя_персонажа Ярость`")
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

        name = ctx.args[2]
        amount = 1
        if len(ctx.args) > 3:
            try:
                amount = int(ctx.args[3])
            except:
                pass

        sessions.use_resource(session.id, target_char.id, name, amount)
        await send_safe(update, f"⚡ **{target_char.name}** тратит **{name}** x{amount}")

    elif action in ("recover", "восстановить", "r"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/adnoddau recover имя_персонажа Ярость`")
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

        name = ctx.args[2]
        amount = 1
        if len(ctx.args) > 3:
            try:
                amount = int(ctx.args[3])
            except:
                pass

        sessions.recover_resource(session.id, target_char.id, name, amount)
        await send_safe(update, f"✨ **{target_char.name}** восстанавливает **{name}** x{amount}")

    else:
        await send_safe(update, "Использование: `/adnoddau` — показать")


async def npc_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    db = db_manager.get_db(session.id)

    # Everyone can see WHO EXISTS in the world — not metagaming, since the same lore
    # text was already posted to the group chat when the world was generated. Only
    # what a SPECIFIC NPC personally knows/feels about a character (below) is gated to
    # actual in-character contact. This also fixes the old DM-only /npc list, which read
    # from a disconnected legacy table (npc_memory) that world-gen never wrote to.
    if ctx.args and ctx.args[0].lower() in ("all", "все"):
        npcs = sessions.get_all_npcs_brief(session.id)
        if not npcs:
            await send_safe(update, "🎭 В реестре пока нет NPC.")
            return
        lines = ["🎭 **Все NPC мира:**", ""]
        for npc in npcs:
            dead = " 💀" if not npc.is_alive else ""
            race_occ = " ".join(filter(None, [npc.race, npc.occupation]))
            lines.append(f"• **{npc.name}**{dead}{f' — {race_occ}' if race_occ else ''}")
        lines.append("")
        lines.append("_Кто что о тебе знает: `/cymeriadnc`_")
        await send_safe(update, "\n".join(lines))
        return

    # H1: players get a DIFFERENT view — only NPCs their character has actually met,
    # with attitude/reputation/known facts/grudges/debts (what the NPC feels about
    # THEM), not the DM's full world registry.
    if not sessions.is_creator(user.id, session.id):
        player = db.get_player(user.id, session.id)
        if not player:
            await send_safe(update, "Сначала `/ymuno`")
            return
        char = db.get_character_by_player(user.id, session.id)
        if not char:
            await send_safe(update, "Нет персонажа. `/cymeriad`")
            return

        known = sessions.get_known_npcs_for_character(session.id, char.id)
        if not known:
            await send_safe(update, f"🎭 **{char.name}** пока никого не встретил.\n\n_Кто вообще есть в мире: `/cymeriadnc all`_")
            return

        attitude_emoji = {"friendly": "🤝", "helpful": "😊", "neutral": "😐", "unfriendly": "😠", "hostile": "⚔️"}
        lines = [f"🎭 **Кого встретил {char.name}:**", ""]
        for n in known:
            emoji = attitude_emoji.get(n["attitude"], "😐")
            race_occ = " ".join(filter(None, [n["npc_race"], n["npc_occupation"]]))
            lines.append(f"{emoji} **{n['npc_name']}**{f' ({race_occ})' if race_occ else ''} — {n['attitude']} (реп: {n['reputation']:+d})")
            if n["known_facts"]:
                lines.append(f"   💭 Знает: {n['known_facts']}")
            if n.get("grudges"):
                lines.append(f"   😠 Обида: {n['grudges']}")
            if n.get("debts"):
                lines.append(f"   💰 Долг: {n['debts']}")
        await send_safe(update, "\n".join(lines))
        return

    if not ctx.args or ctx.args[0].lower() == "list":
        npcs = db.get_all_npcs(session.id)
        if npcs:
            lines = ["🎭 **Заметки NPC (легаси):**"]
            for npc in npcs:
                lines.append(f"• **{npc.npc_name}**{f' — {npc.personality_pattern[:50]}' if npc.personality_pattern else ''}")
            await send_safe(update, "\n".join(lines))
            return

        # Legacy npc_memory table is essentially dead now — world-gen and
        # set_npc_relation write to the real `npcs`/`npc_relations` tables instead.
        # Telling the DM "реестр пуст" when real NPCs actually exist there (visible via
        # /npc all or /relations) was confusing and looked like a bug. Fall back to the
        # real registry so the DM's default /npc is never a false "empty" dead end.
        real_npcs = sessions.get_all_npcs_brief(session.id)
        if not real_npcs:
            await send_safe(update, "🎭 В реестре пока нет NPC.")
            return
        lines = ["🎭 **Все NPC мира:**", ""]
        for npc in real_npcs:
            dead = " 💀" if not npc.is_alive else ""
            race_occ = " ".join(filter(None, [npc.race, npc.occupation]))
            lines.append(f"• **{npc.name}**{dead}{f' — {race_occ}' if race_occ else ''}")
        lines.append("")
        lines.append("_Заметка (легаси): `/cymeriadnc note Имя Текст`_")
        await send_safe(update, "\n".join(lines))
        return

    await send_safe(update, "⚠️ Только просмотр. NPC — через `Дн.` (нейросеть).")

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        rest = " ".join(ctx.args[1:])
        parts = rest.split(" | ")
        name = parts[0].strip()
        personality = parts[1].strip() if len(parts) > 1 else ""
        facts = parts[2].strip() if len(parts) > 2 else ""

        sessions.add_npc(session.id, name, personality, facts)
        await send_safe(update, f"🎭 **NPC добавлен:** {name}")

    elif action in ("note", "заметка", "n"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/cymeriadnc note Имя Текст заметки`")
            return

        name = ctx.args[1]
        note = " ".join(ctx.args[2:])
        sessions.update_npc_facts(session.id, name, note)
        await send_safe(update, f"📝 Заметка о **{name}** сохранена.")

    else:
        await send_safe(update, "Использование: `/cymeriadnc list` или `/cymeriadnc all`")


async def event_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    # Admin/readonly enforcement
    if ADMIN_IS_GAME_MASTER and not sessions.is_creator(user.id, session.id):
        await update.message.reply_text("🔒 Только админ может использовать эту команду.")
        return
    if not ADMIN_IS_GAME_MASTER:
        await update.message.reply_text("⚠️ Эта команда только для просмотра. Нейросеть решает все изменения через нарратив (Дн.).")
        return
    await send_safe(update, "⚠️ Эта настройка недоступна.")

    db = db_manager.get_db(session.id)
    if not ctx.args or ctx.args[0].lower() == "list":
        events = sessions.get_world_events(session.id, unresolved_only=True)
        if not events:
            await send_safe(update, "🌍 Нет незавершённых событий.")
            return

        lines = ["🌍 **События в мире:**"]
        for e in events:
            lines.append(f"#{e.id} [{e.event_type}]: {e.description[:100]}")

        await send_safe(update, "\n".join(lines))
        return

    action = ctx.args[0].lower()

    if action in ("generate", "gen", "сгенерировать"):
        if update.effective_chat:
            await update.effective_chat.send_action(action="typing")
        try:
            result = await sessions.generate_living_world_event(session.id)
            if result:
                html_text = result.get("html", result.get("text", ""))
                await send_safe(update, html_text, parse_html=False, source="master")
            else:
                await send_safe(update, "❌ Не удалось сгенерировать событие.")
        except Exception as e:
            logger.error(f"Event error: {e}")
            await send_safe(update, f"❌ Ошибка: {e}")

    elif action in ("resolve", "решить", "r"):
        if len(ctx.args) < 2:
            await send_safe(update, "Использование: `/digwyddiad resolve ID`")
            return
        try:
            event_id = int(ctx.args[1])
            sessions.resolve_world_event(event_id)
            await send_safe(update, f"✅ Событие #{event_id} решено.")
        except ValueError:
            await send_safe(update, "❌ ID должен быть числом.")

    else:
        await send_safe(update, "Использование: `/digwyddiad list`")


async def world_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    await send_safe(update, "⚠️ Эта настройка недоступна.")

    db = db_manager.get_db(session.id)
    if not ctx.args:
        session_obj = db.get_session(session.id)
        if session_obj and session_obj.current_scene:
            await send_safe(update, f"🌍 **Текущий мир:**\n\n{session_obj.current_scene[:500]}")
        else:
            await send_safe(update, "🌍 Мир ещё не описан. `/byd generate [тема]`")
        return

    action = ctx.args[0].lower()

    if action in ("generate", "gen", "сгенерировать"):
        theme = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "dark fantasy"

        if update.effective_chat:
            await update.effective_chat.send_action(action="typing")
        try:
            result = await sessions.generate_world(session.id, theme)

            # Show generation status
            status = sessions.get_world_status(session.id)
            status_text = (
                f"🌍 **Мир создан!**\n\n"
                f"📍 Локаций: {status['locations_count']}\n"
                f"  {', '.join(status['locations'][:5]) if status['locations'] else 'Нет'}\n\n"
                f"👥 NPC: {status['npcs_count']}\n"
                f"  {', '.join(status['npcs'][:5]) if status['npcs'] else 'Нет'}\n\n"
                f"📜 Лор: {status['lore_count']} | События: {status['events_count']} | Фракции: {status['factions_count']}\n\n"
                f"{result.get('text', '')[:800]}"
            )
            await send_safe(update, status_text, source="system")
        except Exception as e:
            logger.error(f"World gen error: {e}")
            await send_safe(update, f"❌ Ошибка: {e}")
        return

    elif action in ("status", "статус", "s"):
        status = sessions.get_world_status(session.id)
        status_text = (
            f"🌍 **Статус мира:**\n\n"
            f"📍 Локаций: {status['locations_count']}\n"
            f"  {', '.join(status['locations']) if status['locations'] else 'Нет'}\n\n"
            f"👥 NPC: {status['npcs_count']}\n"
            f"  {', '.join(status['npcs']) if status['npcs'] else 'Нет'}\n\n"
            f"📜 Лор: {status['lore_count']}\n"
            f"⚔️ События: {status['events_count']}\n"
            f"🏛️ Фракции: {status['factions_count']}"
        )
        await send_safe(update, status_text)
        return

    elif action in ("sync", "синх", "sync"):
        await send_safe(update, "🔄 Синхронизация мира с БД...")
        session_obj = db.get_session(session.id)
        if session_obj and session_obj.current_scene:
            game_actions = await dm_engine.process_with_db_bot(session_obj.current_scene, session_id=session.id)
            if game_actions:
                applied, errors = sessions._apply_game_actions(session.id, game_actions)
                await send_safe(update, f"✅ Синхронизировано: {applied} действий. Ошибок: {len(errors)}")
            else:
                await send_safe(update, "📭 Нет новых действий для синхронизации.")
        else:
            await send_safe(update, "🌍 Нет текущего описания мира.")
        return

    else:
        await send_safe(update, 
            "🌍 **Команды мира:**\n"
            "`/byd generate [тема]` — создать мир\n"
            "`/byd status` — статус мира\n"
            "`/byd sync` — синхронизировать текст мира с БД"
        )
