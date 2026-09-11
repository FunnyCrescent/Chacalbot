"""
libs.handlers.combat_cmds — auto-split from libs/bot_handlers.py.

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
    ADMIN_CHAT_ID, PLAYER_ROLL_TIMEOUT_SECONDS, COMBAT_PC_TURN_TIMEOUT_SECONDS, MAX_MANUAL_ROLLS_PER_ROUND,
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
from libs.handlers.engine import _resolve_and_send
from libs.handlers.engine import _start_combat_turn_loop
from libs.handlers.engine import _track_pending_combat_message
from libs.handlers.engine import _track_round_messages_with_fallback
from libs.handlers.utils import get_session
from libs.handlers.utils import send_safe
from libs.handlers.utils import world_gen_guard


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def combat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """БОЙ-FIX v3 (Причина #1 из ТЗ): /combat идёт по ЕДИНОМУ V10b-пути.

    Раньше здесь вызывался легаси sessions.start_combat(), который ставил
    combat_active=True прямо в строку сессии, НЕ создавая CombatEncounter/
    Combatant, и запускал устаревшую all-players очередь
    (start_action_collection, «все ходят одновременно»). Весь per-turn боевой
    движок (get_current_initiative_turn / get_combat_action_group / боевой
    цикл) читает состояние из CombatEncounter/Combatant — без этих строк он
    отдавал заглушку «никто не заблокирован»: очередь рассинхронизировалась,
    раунд никогда не резолвился, бой зависал навсегда.

    Теперь: бой создаётся через start_combat_for_participants (те же строки
    БД, что и при старте из нарратива), после чего запускается per-turn боевой
    цикл _start_combat_turn_loop — как это делает нарративный путь (engine.py).

    Участники: /combat [имя1 имя2 ...] — без аргументов все живые персонажи
    сессии; с аргументами — перечисленные имена (неизвестные станут NPC).
    Инициатива бросается серверно (d20 + DEX-мод)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return
    if session.combat_active:
        await send_safe(update, "⚔️ Бой уже идёт!")
        return

    db = db_manager.get_db(session.id)

    # ── Определяем участников ──
    if ctx.args:
        participants = [a.strip() for a in ctx.args if a.strip()]
    else:
        participants = [
            c.name for c in db.get_session_characters(session.id)
            if getattr(c, "is_alive", True) and c.name
        ]
    if not participants:
        await send_safe(update,
            "❌ Некому драться: в сессии нет живых персонажей.\n"
            "Можно указать участников явно: `/combat Имя1 Имя2`")
        return

    # ── Единый V10b-путь: CombatEncounter + Combatant в БД ──
    result = sessions.start_combat_for_participants(
        session.id, participants, reason="по команде /combat")

    if result.startswith("Error"):
        await send_safe(update, f"❌ {result}")
        return

    db.add_history(HistoryEntry(session_id=session.id, author="DM",
                                content="Бой начался!", entry_type="combat"))

    # Порядок ходов — свежий из БД (отсортированный по инициативе).
    encounter = db.get_active_combat_encounter(session.id)
    order = db.get_initiative_order(encounter.id) if encounter else []
    lines = ["⚔️ **БОЙ НАЧИНАЕТСЯ!** ⚔️", "", "*Порядок ходов:*"]
    for i, e in enumerate(order, 1):
        lines.append(f"{i}. {e['name']} (Инициатива: {e['initiative']})")
    if order:
        first = order[0]
        lines.append(f"\n🎲 Раунд 1 — ход **{first['name']}**!")
    await send_safe(update, "\n".join(lines))

    # ── Запускаем per-turn боевой цикл (как в нарративном пути) ──
    # Цикл сам: установит single-player очередь для текущего PC, попросит
    # «Дн.», авто-разрешит NPC-ходы и сообщит «Ход X — ожидайте» остальным.
    bot_obj = ctx.bot if ctx else None
    if bot_obj:
        asyncio.create_task(_start_combat_turn_loop(session.id, chat_id, bot_obj))
    else:
        # Практически недостижимо (ctx.bot всегда есть), но не оставляем
        # боевой loop без оркестратора: логируем, чтобы заметить в проде.
        logger.error(
            f"[combat-cmd] no bot object for {session.id} — combat loop NOT started")


async def endcombat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return
    sessions.end_combat(session.id)
    db = db_manager.get_db(session.id)
    db.add_history(HistoryEntry(session_id=session.id, author="DM", content="Бой завершён.", entry_type="combat"))
    await send_safe(update, "🏳️ **Бой завершён.**")


async def skip_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Фича 1: два режима /sgipio.

    /sgipio            — пропустить СВОЙ ход (доступно КАЖДОМУ игроку).
    /sgipio @ник1 ...  — пропустить ход ДРУГИХ игроков (только создатель сессии).

    Бой учитывается: в бою пропустить можно только ход ТЕКУЩЕГО игрока —
    тогда инициатива просто переходит к следующему (как авто-скип по таймауту)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    db = db_manager.get_db(session.id)
    bot_obj = ctx.bot if ctx else None

    # ─────────── Режим 1: /sgipio без аргументов — скип СВОЕГО хода ───────────
    if not ctx.args:
        player = db.get_player(user.id, session.id)
        if not player:
            await send_safe(update, "Сначала `/ymuno`")
            return
        # BUG 3 FIX: до/во время генерации мира скипать нечего.
        if await world_gen_guard(update, session):
            return

        char = db.get_character_by_player(user.id, session.id)
        char_name = char.name if char else player.display_name

        # ── Бой: пропускаем СВОЙ боевой ход (инициатива переходит дальше) ──
        if session.combat_active and COMBAT_INITIATIVE_ENABLED:
            combat_group = sessions.get_combat_action_group(session.id)
            current = combat_group.get("current", {})
            if combat_group.get("npc_turn"):
                await send_safe(update, "⚔️ Сейчас ход NPC — пропускать нечего.")
                return
            if current.get("player_id") != user.id:
                await send_safe(update,
                    f"⚔️ Сейчас не твой ход (ходит: {current.get('name', '?')}). "
                    f"Пропустить можно только СВОЙ ход.")
                return
            _sent = await send_safe(update, f"⏭️ {char_name} пропустил свой ход.")
            _track_pending_combat_message(session.id, _sent or [], chat_id)
            sessions.advance_initiative_turn(session.id)
            if bot_obj:
                asyncio.create_task(_start_combat_turn_loop(session.id, chat_id, bot_obj))
            return

        # ── Вне боя: убираем игрока из очереди раунда ──
        async with sessions.get_round_lock(session.id):
            should_resolve = sessions.skip_player(
                session.id, user.id, reason="*[Ход пропущен игроком]*")
            defer = should_resolve and sessions.is_db_busy(session.id)
            if defer:
                sessions.mark_pending_resolve(session.id, chat_id, ctx)

        if not should_resolve:
            await send_safe(update,
                "ℹ️ Тебе нечего пропускать: ты не в очереди текущего раунда или уже сходил.")
            return

        pending = sessions.get_pending_players(session.id)
        if should_resolve:
            if defer:
                _sent = await send_safe(update,
                    "⏳ Все на месте, но мир ещё обновляется после прошлого раунда — разрешение начнётся автоматически.")
                await _track_round_messages_with_fallback(
                    session.id, _sent or [], chat_id, user.id,
                    bot=bot_obj, message_type="bot_confirm")
            else:
                _sent = await send_safe(update, "🎲 Все на месте — нейросеть разрешает...")
                await _track_round_messages_with_fallback(
                    session.id, _sent or [], chat_id, user.id,
                    bot=bot_obj, message_type="bot_confirm")
                await _resolve_and_send(session.id, update, ctx)
        else:
            _sent = await send_safe(update, f"⏭️ Ты пропустил свой ход.\n⏳ Ждём: {', '.join(pending)}")
            await _track_round_messages_with_fallback(
                session.id, _sent or [], chat_id, user.id,
                bot=bot_obj, message_type="bot_confirm")
        return

    # ─────────── Режим 2: /sgipio @ник — скип ДРУГИХ (только создатель) ───────────
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update,
            "❌ Пропускать ходы ДРУГИХ игроков может только создатель сессии.\n"
            "ℹ️ Пропустить СВОЙ ход: `/sgipio` (без аргументов).")
        return

    targets = [a.strip("@") for a in ctx.args]
    players = sessions.get_players(session.id)
    skipped_names = []
    all_resolved = False
    skipped_current_combat_player = None

    # ── Бой: пропускаем только ТЕКУЩЕГО игрока (инициатива идёт дальше) ──
    if session.combat_active and COMBAT_INITIATIVE_ENABLED:
        combat_group = sessions.get_combat_action_group(session.id)
        if combat_group.get("npc_turn"):
            await send_safe(update, "⚔️ Сейчас ход NPC — пропускать нечего.")
            return
        current = combat_group.get("current", {})
        for target in targets:
            target_p = None
            for p in players:
                if p.username == target or p.display_name == target:
                    target_p = p
                    break
                char = db.get_character_by_player(p.user_id, session.id)
                if char and char.name == target:
                    target_p = p
                    break
            if not target_p:
                continue
            if current.get("player_id") == target_p.user_id:
                skipped_current_combat_player = target_p
                skipped_names.append(target_p.display_name)
            else:
                await send_safe(update,
                    f"⚔️ {target_p.display_name} сейчас не в очереди боевого хода — "
                    f"в бою скипается только текущий игрок ({current.get('name', '?')}).")
        if skipped_current_combat_player:
            _sent = await send_safe(update,
                f"⏭️ Ход пропущен: {', '.join(skipped_names)}")
            _track_pending_combat_message(session.id, _sent or [], chat_id)
            sessions.advance_initiative_turn(session.id)
            if bot_obj:
                asyncio.create_task(_start_combat_turn_loop(session.id, chat_id, bot_obj))
        else:
            await send_safe(update, f"❌ Игроки не найдены: {', '.join(targets)}")
        return

    # ── Вне боя: прежняя логика скипа из очереди раунда ──
    async with sessions.get_round_lock(session.id):
        for target in targets:
            target_p = None
            for p in players:
                if p.username == target or p.display_name == target:
                    target_p = p
                    break
                char = db.get_character_by_player(p.user_id, session.id)
                if char and char.name == target:
                    target_p = p
                    break

            if target_p:
                should_resolve = sessions.skip_player(session.id, target_p.user_id)
                skipped_names.append(target_p.display_name)
                if should_resolve:
                    all_resolved = True

        defer = all_resolved and sessions.is_db_busy(session.id)
        if defer:
            sessions.mark_pending_resolve(session.id, chat_id, ctx)

    if skipped_names:
        await send_safe(update, f"⏭️ Ход пропущен: {', '.join(skipped_names)}")
    else:
        await send_safe(update, f"❌ Игроки не найдены: {', '.join(targets)}")

    if all_resolved:
        if defer:
            await send_safe(update, "⏳ Все на месте, но мир ещё обновляется после прошлого раунда — разрешение начнётся автоматически.")
        else:
            _sent = await send_safe(update, "🎲 Все на месте — нейросеть разрешает...")
            await _track_round_messages_with_fallback(
                session.id, _sent or [], chat_id, user.id,
                bot=bot_obj, message_type="bot_confirm")
            await _resolve_and_send(session.id, update, ctx)


async def kick_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Kick player from session (DM only)"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель может выгонять игроков.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/cicio @ник`")
        return

    target = " ".join(ctx.args).strip("@")
    players = sessions.get_players(session.id)
    target_p = None
    for p in players:
        if p.username == target or p.display_name == target:
            target_p = p
            break
        db = db_manager.get_db(session.id)
        char = db.get_character_by_player(p.user_id, session.id)
        if char and char.name == target:
            target_p = p
            break

    if not target_p:
        await send_safe(update, f"❌ Игрок '{target}' не найден.")
        return

    if target_p.is_creator:
        await send_safe(update, "❌ Нельзя выгнать создателя сессии.")
        return

    async with sessions.get_round_lock(session.id):
        _existed, round_now_complete = sessions.kick_player(session.id, target_p.user_id)
        defer = round_now_complete and sessions.is_db_busy(session.id)
        if defer:
            sessions.mark_pending_resolve(session.id, chat_id, ctx)

    await send_safe(update, f"👢 **{target_p.display_name}** выгнан из сессии.")

    # Before this fix: kicking a player removed them from the players table, but they
    # stayed in the round's waiting_for/collected_actions forever — waiting_for could
    # never empty, /skip couldn't even find them anymore to skip, and the round (and
    # every /Дн. after it) just hung. See db.remove_player / _remove_from_pending_round.
    if round_now_complete:
        if defer:
            await send_safe(update, "⏳ Все на месте, но мир ещё обновляется после прошлого раунда — разрешение начнётся автоматически.")
        else:
            # BUG 2 FIX: без auto_delete — сообщение удаляется ПОСЛЕ нарратива.
            _sent = await send_safe(update, "🎲 Все на месте — нейросеть разрешает...")
            await _track_round_messages_with_fallback(
                session.id, _sent or [], chat_id, user.id,
                bot=(ctx.bot if ctx else None), message_type="bot_confirm")
            await _resolve_and_send(session.id, update, ctx)


async def transfer_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Transfer session creator rights to another player."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель может передать права.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/trosglwyddo @ник` или `/trosglwyddo имя_персонажа`")
        return

    target = ctx.args[0].strip("@")
    players = sessions.get_players(session.id)
    target_p = None
    for p in players:
        if p.username == target or p.display_name == target:
            target_p = p
            break
        db = db_manager.get_db(session.id)
        char = db.get_character_by_player(p.user_id, session.id)
        if char and char.name == target:
            target_p = p
            break

    if not target_p:
        await send_safe(update, f"❌ Игрок или персонаж '{target}' не найден.")
        return
    if target_p.user_id == user.id:
        await send_safe(update, "❌ Нельзя передать права себе.")
        return

    # Transfer
    db = db_manager.get_db(session.id)
    with db._connect() as conn:
        conn.execute(
            "UPDATE players SET is_creator = 0 WHERE user_id = ? AND session_id = ?",
            (user.id, session.id)
        )
        conn.execute(
            "UPDATE players SET is_creator = 1 WHERE user_id = ? AND session_id = ?",
            (target_p.user_id, session.id)
        )

    await send_safe(update, f"👑 **Права Админа переданы {target_p.display_name}!**")


async def roll_check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A4: /roll <характеристика/навык/спасбросок>. The DB-Bot reads the raw sheet PLUS
    current DB progression (so a level-up after upload is still respected) and performs
    a REAL, system-verified d20 roll — never something the player typed. The result is
    attached to this player's NEXT Дн. this round as a [ROLL] tag, kept structurally
    separate from their [PLAYER] text so the Master can never confuse "what the player
    claims" with "a number the server actually rolled". The Master still has the right
    to reject the CHECK as narratively illegitimate — see MASTER_PROMPT — but cannot
    silently overwrite or re-roll the verified number itself."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/rholio атлетика` или `/rholio спасбросок телосложения`")
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

    # BUG 3 FIX: броски до/во время генерации мира запрещены.
    if await world_gen_guard(update, session):
        return

    check_name = " ".join(ctx.args)

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")

    try:
        result = await sessions.resolve_manual_roll(session.id, user.id, check_name)
    except Exception as e:
        logger.error(f"roll_cmd error: {e}")
        await send_safe(update, f"❌ Ошибка броска: {e}")
        return

    if result.get("ok"):
        remaining = result.get('remaining_rolls', '?')
        roll_num = result.get('roll_number', '?')
        await send_safe(update,
            f"🎲 **ПРОВЕРЕННЫЙ БРОСОК СЕРВЕРА** — {char.name}: {check_name}\n"
            f"{result['display']}\n\n"
            f"_Это реальный, системно подтверждённый бросок — не то, что игрок написал текстом._\n"
            f"Бросок {roll_num}/{MAX_MANUAL_ROLLS_PER_ROUND} за раунд. Осталось: {remaining}.\n"
            f"Теперь напиши `Дн. твоё действие` — результат прикрепится автоматически."
        )
    else:
        await send_safe(update, f"❌ {result.get('error', 'Не удалось выполнить бросок.')}")


async def roll_dispatch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/roll is shared between two historically separate features:
    - `/rholio encounter [местность]` (DM only) — random encounter generator, original behavior.
    - `/rholio <характеристика/навык>` (any player) — A4 verified skill/save check.
    Dispatches on the first argument to keep both working under one command name."""
    if ctx.args and ctx.args[0].lower() in ("encounter", "энкаунтер", "столкновение"):
        await roll_encounter_cmd(update, ctx)
    else:
        await roll_check_cmd(update, ctx)


async def roll_encounter_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Админ.")
        return

    terrain = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else ""

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")
    try:
        result = await sessions.generate_encounter(session.id, terrain)
        sessions.add_world_event(session.id, "encounter", result.get("text", "")[:200])
        html_text = result.get("html", result.get("text", ""))
        await send_safe(update, html_text, parse_html=False, source="master")
    except Exception as e:
        logger.error(f"Encounter error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


async def pvp_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return
    sessions.toggle_pvp(session.id)
    status = "включён" if sessions.is_pvp(session.id) else "выключён"
    await send_safe(update, f"⚔️ PvP режим {status}!")


async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    # SECURITY: Direct roll-mode change by users is forbidden — must go through DM.
    await send_safe(update,
        "🚫 Изменение режима бросков напрямую запрещено.\n"
        "Эта настройка управляется Мастером (нейросетью) через `Дн.`."
    )
    return

    current = sessions.get_roll_mode(session.id)

    if not ctx.args:
        await send_safe(update, 
            f"🎲 **Текущий режим:** {current}\n\n"
            f"`gmroll` — все броски скрыты от игроков\n"
            f"`playerroll` — все броски видны игрокам\n"
            f"`mixed` — стандарт: игроки видят свои, NPC скрыты"
        )
        return

    mode = ctx.args[0].lower()
    if mode not in ("gmroll", "playerroll", "mixed"):
        await send_safe(update, "❌ Режимы: gmroll, playerroll, mixed")
        return

    sessions.set_roll_mode(session.id, mode)

    desc = {
        "gmroll": "🎭 Все броски скрыты от игроков",
        "playerroll": "👁️ Все броски видны игрокам",
        "mixed": "⚖️ Стандарт: свои видят, NPC скрыты",
    }
    await send_safe(update, f"🎲 **Режим изменён:** {desc[mode]}")


async def concentration_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    # SECURITY: Concentration changes must go through the DM (neural network) via `Дн.` flow.
    await send_safe(update,
        "🚫 Изменение концентрации напрямую запрещено. Только просмотр.\n"
        "Используйте `Дн.` для управления концентрацией через Мастера."
    )
    return

    if not ctx.args:
        await send_safe(update, "Использование: `/canolbwyntio start имя заклинание` | `/canolbwyntio end имя` | `/canolbwyntio check имя 15`")
        return

    action = ctx.args[0].lower()

    if action in ("start", "начать", "s"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/canolbwyntio start имя_персонажа заклинание`")
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

        spell = " ".join(ctx.args[2:])
        sessions.add_condition(session.id, target_char.id, target_char.name, "concentrating", spell)
        await send_safe(update, f"🧠 **{target_char.name}** начинает концентрацию: *{spell}*")

    elif action in ("end", "закончить", "e"):
        if len(ctx.args) < 2:
            await send_safe(update, "Использование: `/canolbwyntio end имя_персонажа`")
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

        sessions.remove_condition(session.id, target_char.id, "concentrating")
        await send_safe(update, f"❌ **{target_char.name}** теряет концентрацию.")

    elif action in ("check", "проверка", "c"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/canolbwyntio check имя_персонажа 15` (урон для КС)")
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

        damage = int(ctx.args[2]) if ctx.args[2].isdigit() else 0
        dc = max(10, damage // 2) if damage > 0 else 10

        import random
        roll = random.randint(1, 20)
        try:
            stats = json.loads(target_char.stats) if target_char.stats else {}
            con_mod = (stats.get("constitution", 10) - 10) // 2
        except:
            con_mod = 0

        total = roll + con_mod
        success = total >= dc

        result = "УСПЕХ" if success else "ПРОВАЛ"
        emoji = "✅" if success else "❌"

        if not success:
            sessions.remove_condition(session.id, target_char.id, "concentrating")

        await send_safe(update, 
            f"🧠 **Проверка концентрации** {target_char.name}:\n"
            f"КС: {dc} | d20+{con_mod}: {roll}+{con_mod} = **{total}**\n"
            f"{emoji} **{result}**"
            f"{f'\n❌ Концентрация потеряна!' if not success else ''}"
        )

    else:
        await send_safe(update, "⚠️ Только просмотр.")
