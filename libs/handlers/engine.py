"""
libs.handlers.engine — auto-split from libs/bot_handlers.py.

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
from libs.handlers.translations import _character_knows_language
from libs.handlers.utils import _keep_typing
from libs.handlers.translations import _parse_language_blocks
from libs.handlers.utils import _send_long_blockquote
from libs.handlers.translations import _store_lang_translations
from libs.handlers.translations import _strip_summary_and_commands
from libs.handlers.utils import md_to_html
from libs.handlers.utils import send_safe
from libs.handlers.utils import send_to_admin


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def _track_round_message(session_id: str, message_id: int, chat_id: int,
                                 player_id: int, message_type: str):
    """Track a message for potential anti-spam deletion after narrative."""
    if not ANTISPAM_ENABLED:
        return
    try:
        db = db_manager.get_db(session_id)
        db.track_round_message(session_id, message_id, chat_id, player_id, message_type)
    except Exception as e:
        logger.warning(f"[antispam] Failed to track message {message_id}: {e}")


async def _delete_round_messages(session_id: str, bot_instance, chat_id: int):
    """Delete all tracked round messages after narrative is sent."""
    if not ANTISPAM_ENABLED:
        return
    try:
        db = db_manager.get_db(session_id)
        messages = db.get_round_messages(session_id)
        if not messages:
            return
        deletable = [m for m in messages if m.message_type in ANTISPAM_DELETE_TYPES and m.chat_id == chat_id]
        db.clear_round_messages(session_id)
        
        for i in range(0, len(deletable), ANTISPAM_DELETE_BATCH_SIZE):
            batch = deletable[i:i + ANTISPAM_DELETE_BATCH_SIZE]
            for msg in batch:
                try:
                    await bot_instance.delete_message(chat_id=chat_id, message_id=msg.message_id)
                    logger.debug(f"[antispam] Deleted message {msg.message_id} ({msg.message_type})")
                except Exception as e:
                    logger.debug(f"[antispam] Could not delete message {msg.message_id}: {e}")
            if i + ANTISPAM_DELETE_BATCH_SIZE < len(deletable):
                await asyncio.sleep(ANTISPAM_DELETE_BATCH_DELAY)
    except Exception as e:
        logger.warning(f"[antispam] Failed to clean round messages: {e}")


async def _send_translations(session_id: str, narrative_text: str, bot_instance):
    """Send async translations to players who have it enabled. Group by language."""
    if not TRANSLATOR_ENABLED:
        return
    try:
        db = db_manager.get_db(session_id)
        translations = db.get_enabled_translations(session_id)
        if not translations:
            return
        
        # Group by language
        lang_groups: Dict[str, List[PlayerLanguage]] = {}
        for t in translations:
            if t.language and t.enabled:
                lang_groups.setdefault(t.language, []).append(t)
        
        for language, players in lang_groups.items():
            try:
                # Translate using the translator model
                translator = OpenAIClient(
                    TRANSLATOR_MODEL, TRANSLATOR_TEMP, TRANSLATOR_MAX_TOKENS,
                    OPENAI_BASE_URL, OPENAI_API_KEY
                )
                prompt = f"Переведи следующий текст на {language}. Сохрани форматирование, эмодзи и смысл. Это нарратив для настольной ролевой игры D&D:\n\n{narrative_text}"
                response = await translator.chat(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=TRANSLATOR_MAX_TOKENS,
                )
                translated = response["choices"][0]["message"]["content"].strip()
                
                for player in players:
                    try:
                        await bot_instance.send_message(
                            chat_id=player.player_id,
                            text=md_to_html(f"🌐 [{language}]\n\n{translated}"),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"[translation] Failed to send to player {player.player_id}: {e}")
                
                await asyncio.sleep(TRANSLATOR_BATCH_DELAY)
            except Exception as e:
                logger.warning(f"[translation] Failed for language {language}: {e}")
    except Exception as e:
        logger.warning(f"[translation] Failed: {e}")


async def _roll_button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handles the inline button press. Rolls server-side (never trusts the client),
    resolves the waiting Future in process_master_turn, edits the message to show the
    result, and rejects presses from anyone other than the intended player."""
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("roll:"):
        return
    short_id = data[len("roll:"):]
    entry = _pending_button_rolls.get(short_id)
    if not entry:
        await query.answer("Этот бросок уже неактуален.", show_alert=True)
        return

    if query.from_user.id != entry["player_id"]:
        await query.answer("Мастер не просит у тебя броски", show_alert=True)
        return

    if entry["future"].done():
        await query.answer("Бросок уже засчитан.", show_alert=True)
        return

    result = dm_engine._execute_roll(entry["args"])
    entry["future"].set_result(result)
    _pending_button_rolls.pop(short_id, None)

    # Show result in popup (only the pressing player sees this)
    await query.answer(f"🎲 {result['display']}", show_alert=True)
    try:
        # result['display'] (built in _execute_roll) already contains the full label —
        # which itself already contains the character name, per MASTER_PROMPT's own
        # convention ("Эйра — атака рапирой"). Prepending character_name/label again
        # here used to triple up the name ("Хейд — Хейд — Скрытность..."). It was also
        # sent with parse_mode="HTML" while still containing raw **markdown** asterisks
        # (HTML doesn't understand those, so they showed up literally) — md_to_html
        # converts them properly.
        await query.edit_message_text(md_to_html(result['display']), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to edit roll button message: {e}")


def make_player_roll_requester(session_id: str, ctx: ContextTypes.DEFAULT_TYPE, group_chat_id: int):
    """Builds the async callback passed into DMEngine.process_master_turn as
    player_roll_requester. Captures the session/ctx/chat so the generic engine code
    doesn't need to know anything about Telegram."""

    async def requester(character_name: str, args: dict, tool_call_id: str) -> dict:
        db = db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)
        target_char = None
        character_name_lower = character_name.lower()
        for c in chars:
            name_lower = c.name.lower()
            # Also match against player display_name (master sometimes uses
            # the player's telegram name instead of the character name)
            display_lower = (getattr(c, 'display_name', '') or '').lower()
            if character_name_lower in name_lower or name_lower in character_name_lower \
                    or character_name_lower in display_lower or display_lower in character_name_lower:
                target_char = c
                break
        if not target_char:
            logger.warning(f"[roll button] No character matched '{character_name}' (available: {[c.name for c in chars]}), executing immediately as fallback")
            return dm_engine._execute_roll(args)

        player = db.get_player(target_char.player_id, session_id)
        player_display = player.display_name if player else character_name

        short_id = uuid.uuid4().hex[:12]
        future = asyncio.get_event_loop().create_future()
        _pending_button_rolls[short_id] = {
            "future": future, "player_id": target_char.player_id,
            "character_name": character_name, "args": args,
        }

        label = args.get("label", "Бросок")
        button = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎲 Кинуть кубик", callback_data=f"roll:{short_id}")]])
        # Bug fix: this was sent with parse_mode="HTML" while the text still contained
        # raw **markdown** asterisks (HTML doesn't understand those — they showed up as
        # literal "**...**" to the player until the button was pressed). Convert through
        # md_to_html like every other outgoing message in the bot.
        prompt_text = md_to_html(f"🎲 **{player_display}**, нейросеть просит бросок:\n**{label}**")

        # Always send the button in the GROUP chat — the callback checks who pressed it
        try:
            await ctx.bot.send_message(chat_id=group_chat_id, text=prompt_text, reply_markup=button, parse_mode="HTML")
        except Exception as e:
            logger.error(f"[roll button] Group message failed: {e}")
            _pending_button_rolls.pop(short_id, None)
            return dm_engine._execute_roll(args)

        try:
            result = await asyncio.wait_for(future, timeout=PLAYER_ROLL_TIMEOUT_SECONDS)
            return result
        except asyncio.TimeoutError:
            _pending_button_rolls.pop(short_id, None)
            logger.info(f"[roll button] Timeout waiting for {character_name}'s roll")
            return {"timeout": True}

    return requester


async def _db_busy_guard(update: Update, session: Session) -> bool:
    """Returns True (and sends a warning) if this session's DB-Bot background pass is
    still applying the previous round's changes — callers should `return` immediately
    when this is True, since HP/inventory/quests/NPCs etc. may be mid-update."""
    if sessions.is_db_busy(session.id):
        await send_safe(update, "⏳ Мир ещё обновляется после прошлого раунда — секунду, и данные будут точными.")
        return True
    return False


async def _start_combat_turn_loop(session_id: str, chat_id: int, bot_obj):
    """Per-turn combat orchestrator.

    Iterates through the initiative order one character at a time.
    - NPC turns: auto-resolve via Master + npc_ai_action, send separate message, auto-advance.
    - PC turns: send 'your turn' prompt, set queue for that player ONLY, then RETURN
      (the loop resumes when _resolve_pc_combat_turn calls back after the player acts).

    Dead combatants are automatically skipped (get_initiative_order filters is_alive=1).
    Round boundary is announced when current_turn_index wraps around."""
    # Guard: prevent multiple combat loops for the same session
    if _active_combat_loops.get(session_id):
        logger.warning(f"[combat-loop] Loop already active for {session_id}, skipping duplicate")
        return
    _active_combat_loops[session_id] = True
    logger.info(f"[combat-loop] Starting combat turn loop for {session_id}")

    try:
        # Wait for DB-Bot to finish before starting — otherwise we may read stale
        # character state (HP, conditions, etc.) from the round that just resolved.
        db_wait_start = asyncio.get_event_loop().time()
        for _ in range(60):  # up to 60 seconds
            if not sessions.is_db_busy(session_id):
                break
            await asyncio.sleep(1)
        db_wait_elapsed = asyncio.get_event_loop().time() - db_wait_start
        if db_wait_elapsed > 5:
            logger.warning(f"[combat-loop] Waited {db_wait_elapsed:.1f}s for DB-Bot to finish")

        db = db_manager.get_db(session_id)
        handle = _ChatHandle(bot_obj, chat_id)

        # Track the round number we started with so we can detect round transitions
        session = db.get_session(session_id)
        prev_round = session.round_number if session else 1
        prev_index = session.current_turn_index if session else 0

        while True:
            session = db.get_session(session_id)
            if not session or not session.combat_active:
                return

            # ── Detect round boundary ──
            if session.round_number > prev_round or session.current_turn_index < prev_index:
                await send_safe(handle,
                    f"\n🎲 ─── Раунд {session.round_number} ───"
                )
                await asyncio.sleep(0.5)
                prev_round = session.round_number
                prev_index = session.current_turn_index

            current = sessions.get_current_initiative_turn(session_id)
            if not current:
                logger.error(f"[combat-loop] No current turn for {session_id}")
                return

            if current["entity_type"] != "pc":
                # ── NPC turn — auto-resolve, separate message ──
                logger.info(f"[combat-loop] NPC turn: {current['name']} (round {session.round_number}, index {session.current_turn_index})")
                await _resolve_npc_combat_turn(session_id, current, chat_id, bot_obj)

                # Check if combat ended (diweddymladd called by Master)
                session = db.get_session(session_id)
                if not session or not session.combat_active:
                    return

                sessions.advance_initiative_turn(session_id)
                prev_index = db.get_session(session_id).current_turn_index
                # Delay between NPC turns for readability
                await asyncio.sleep(2)
            else:
                # ── PC turn — prompt player and EXIT loop ──
                logger.info(f"[combat-loop] PC turn: {current['name']} (round {session.round_number}, index {session.current_turn_index})")
                await _setup_pc_combat_turn(session_id, current, chat_id, bot_obj)
                _active_combat_loops.pop(session_id, None)  # allow re-entry when PC acts
                return

    except Exception as e:
        logger.error(f"[combat-loop] Error: {e}", exc_info=True)
    finally:
        _active_combat_loops.pop(session_id, None)  # always clear the guard


async def _resolve_npc_combat_turn(session_id: str, current: dict, chat_id: int, bot_obj):
    """Resolve an NPC's combat turn automatically — sends ONE separate message.
    On error, logs and sends a fallback message so the combat loop doesn't stall."""
    from types import SimpleNamespace
    db = db_manager.get_db(session_id)
    npc_name = current["name"]

    try:
        # Build combat context
        combat_context = sessions.get_combat_context(session_id)

        # History
        history = db.get_history(session_id, limit=15)
        history_msgs = []
        for h in history:
            if h.entry_type == "ooc":
                continue
            role = "user" if h.author != "DM" else "assistant"
            content = h.content if h.author in ("DM", "SYSTEM", "GM_SECRET") else f"{h.author}: {h.content}"
            history_msgs.append({"role": role, "content": content})

        chars = db.get_session_characters(session_id)
        progression_summaries = [db.get_character_progression_summary(c.id) for c in chars] if chars else []

        session = db.get_session(session_id)
        summary = session.summary if session else ""
        db_journal = db.get_journal_summary(session_id)
        memory_digest = await sessions.memory_store.build_context_digest(session_id, f"combat turn {npc_name}")

        # No player_roll_requester for NPC turns (NPCs don't press buttons)
        result = await sessions.dm.process_master_turn(
            session_history=history_msgs,
            player_actions={"NPC_SYSTEM": f"Дн. [Ход NPC: {npc_name}]"},
            character_sheets=progression_summaries if progression_summaries else None,
            context=combat_context,
            roll_mode=sessions.get_roll_mode(session_id),
            summary=summary,
            db_journal=db_journal,
            memory_digest=memory_digest,
            combat_ender=lambda reason: sessions._combat_ender_async(session_id, reason),
            npc_ai_enabled=True,
            combat_turn_mode=True,
            pc_names=[c.name for c in chars] if chars else [],
        )

        # Send as separate message
        handle = _ChatHandle(bot_obj, chat_id)
        await _send_combat_turn_result(session_id, result, handle)

        # Run DB-Bot in background for state updates
        raw_narrative = result.get("raw_narrative", "")
        player_text = result.get("player_text", "")
        if raw_narrative:
            sessions.set_db_busy(session_id, True)
            asyncio.create_task(_run_db_bot_background(
                session_id, raw_narrative, player_text, None, bot_obj, chat_id,
            ))

        # Log
        md_logger.log(session_id, "master", player_text)
        if result.get("gm_log"):
            md_logger.log(session_id, "system", f"GM Log: {result['gm_log']}")

    except Exception as e:
        logger.error(f"[combat-npc] Error resolving NPC turn for {npc_name}: {e}", exc_info=True)
        # Send fallback message so combat doesn't stall
        handle = _ChatHandle(bot_obj, chat_id)
        try:
            await send_safe(handle, f"⚠️ Ошибка хода NPC {npc_name}. Пропуск хода.")
        except Exception:
            pass


async def _auto_resolve_npcs_then_pc(session_id: str, player_id: int, chat_id: int, bot_obj):
    """Auto-resolve all consecutive NPC turns, then set up for the requesting player's turn.
    This is called when a player writes Дн. during an NPC turn — instead of blocking them,
    we auto-resolve the NPCs and then give the player their turn."""
    try:
        # Resolve consecutive NPC turns
        max_npc_turns = 10  # safety limit
        for _ in range(max_npc_turns):
            db = db_manager.get_db(session_id)
            session = db.get_session(session_id)
            if not session or not session.combat_active:
                return

            current = sessions.get_current_initiative_turn(session_id)
            if not current:
                return

            if current["entity_type"] == "pc":
                # Reached a PC turn — stop resolving NPCs
                break

            # Resolve this NPC turn
            await _resolve_npc_combat_turn(session_id, current, chat_id, bot_obj)

            # Check if combat ended
            session = db.get_session(session_id)
            if not session or not session.combat_active:
                return

            # Advance to next turn
            sessions.advance_initiative_turn(session_id)
            await asyncio.sleep(2)  # delay between NPC turns for readability

        # Now set up the player's turn
        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return

        current = sessions.get_current_initiative_turn(session_id)
        if not current or current.get("player_id") != player_id:
            # It's not this player's turn yet — start the combat loop normally
            asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot_obj))
            return

        # Set up this player's turn
        await _setup_pc_combat_turn(session_id, current, chat_id, bot_obj)

    except Exception as e:
        logger.error(f"[auto-npc-resolve] Error: {e}", exc_info=True)
        # Fallback: start the combat loop normally
        try:
            asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot_obj))
        except Exception:
            pass


async def _setup_pc_combat_turn(session_id: str, current: dict, chat_id: int, bot_obj):
    """Set up waiting state for a PC's combat turn — ONE player only.
    Includes an auto-skip timeout: if the player doesn't act within
    COMBAT_PC_TURN_TIMEOUT_SECONDS, the turn is auto-advanced so combat
    doesn't deadlock."""
    char_name = current["name"]
    player_id = current["player_id"]

    sessions.start_single_player_collection(session_id, player_id)

    db = db_manager.get_db(session_id)
    player = db.get_player(player_id, session_id)
    telegram_nick = player.username or player.display_name if player else char_name

    handle = _ChatHandle(bot_obj, chat_id)
    await send_safe(handle,
        f"⚔️ Ход: {char_name}\n\n{char_name}, пиши `Дн.` твое действие"
    )
    # Tag the player via mention
    if player_id:
        try:
            await bot_obj.send_message(
                chat_id=chat_id,
                text=f'<a href="tg://user?id={player_id}">{telegram_nick}</a>',
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Auto-skip timeout: if the player doesn't act, advance the turn ──
    async def _auto_skip_turn():
        """Wait for the player to act, then auto-skip if they don't."""
        await asyncio.sleep(COMBAT_PC_TURN_TIMEOUT_SECONDS)
        # Check if this player's turn is still active
        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return  # combat ended — nothing to do
        current_now = sessions.get_current_initiative_turn(session_id)
        if not current_now or current_now.get("player_id") != player_id:
            return  # turn already advanced — player acted
        # Player didn't act — auto-skip
        logger.warning(f"[combat] Auto-skipping turn for {char_name} (timeout {COMBAT_PC_TURN_TIMEOUT_SECONDS}s)")
        try:
            await send_safe(handle, f"⏭️ {char_name} не ответил вовремя — ход пропущен.")
        except Exception:
            pass
        # Advance the turn and restart the loop
        sessions.advance_initiative_turn(session_id)
        asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot_obj))

    asyncio.create_task(_auto_skip_turn())


async def _resolve_pc_combat_turn(session_id: str, action_text: str, chat_id: int, bot_obj):
    """Resolve a PC's combat turn after they submit Дн. — sends ONE separate message.
    On error, logs and auto-advances the turn so combat doesn't stall."""
    from types import SimpleNamespace

    db = db_manager.get_db(session_id)
    current = sessions.get_current_initiative_turn(session_id)
    if not current:
        return

    char_name = current["name"]
    player_id = current["player_id"]

    try:
        # Build combat context
        combat_context = sessions.get_combat_context(session_id)

        # Player roll requester
        simple_ctx = SimpleNamespace(bot=bot_obj)
        player_roll_requester = make_player_roll_requester(session_id, simple_ctx, chat_id)

        # History
        history = db.get_history(session_id, limit=15)
        history_msgs = []
        for h in history:
            if h.entry_type == "ooc":
                continue
            role = "user" if h.author != "DM" else "assistant"
            content = h.content if h.author in ("DM", "SYSTEM", "GM_SECRET") else f"{h.author}: {h.content}"
            history_msgs.append({"role": role, "content": content})

        chars = db.get_session_characters(session_id)
        progression_summaries = [db.get_character_progression_summary(c.id) for c in chars] if chars else []

        session = db.get_session(session_id)
        summary = session.summary if session else ""
        db_journal = db.get_journal_summary(session_id)

        player = db.get_player(player_id, session_id)
        nick = player.username or player.display_name if player else char_name

        # Pull any /roll results submitted this turn
        pending_rolls = sessions._pop_pending_manual_rolls(session_id)
        verified_rolls = {}
        for pid, rolls in pending_rolls.items():
            if pid == player_id:
                verified_rolls[nick] = rolls

        memory_digest = await sessions.memory_store.build_context_digest(session_id, action_text)

        # Typing indicator
        handle = _ChatHandle(bot_obj, chat_id)
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(handle, stop_typing))

        try:
            result = await sessions.dm.process_master_turn(
                session_history=history_msgs,
                player_actions={nick: f"Дн. {action_text}"},
                character_sheets=progression_summaries if progression_summaries else None,
                context=combat_context,
                roll_mode=sessions.get_roll_mode(session_id),
                summary=summary,
                db_journal=db_journal,
                verified_rolls=verified_rolls if verified_rolls else None,
                player_roll_requester=player_roll_requester,
                memory_digest=memory_digest,
                combat_ender=lambda reason: sessions._combat_ender_async(session_id, reason),
                npc_ai_enabled=False,
                combat_turn_mode=True,
                pc_names=[c.name for c in chars] if chars else [],
            )
        finally:
            stop_typing.set()
            typing_task.cancel()

        # Send as separate message
        await _send_combat_turn_result(session_id, result, handle)

        # Log
        raw_text = result.get("player_text", "")
        md_logger.log(session_id, "master", raw_text)
        if result.get("gm_log"):
            md_logger.log(session_id, "system", f"GM Log: {result['gm_log']}")

        # DB-Bot background
        raw_narrative = result.get("raw_narrative", "")
        if raw_narrative:
            sessions.set_db_busy(session_id, True)
            asyncio.create_task(_run_db_bot_background(
                session_id, raw_narrative, raw_text, None, bot_obj, chat_id,
            ))

    except Exception as e:
        logger.error(f"[combat-pc] Error resolving PC turn for {char_name}: {e}", exc_info=True)
        # Send fallback message so the player knows something went wrong
        handle = _ChatHandle(bot_obj, chat_id)
        try:
            await send_safe(handle, f"⚠️ Ошибка обработки хода {char_name}. Пропуск хода.")
        except Exception:
            pass

    # Check if combat ended (must be outside try/except so we always advance)
    session = db.get_session(session_id)
    if not session or not session.combat_active:
        return

    # Advance to next turn and continue the loop
    sessions.advance_initiative_turn(session_id)
    asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot_obj))


async def _send_combat_turn_result(session_id: str, result: dict, handle):
    """Send a single combat turn's narrative as a separate message."""
    raw_text = result.get("player_text", "")
    if not raw_text.strip():
        # GM returned empty — send a fallback message so the player isn't left hanging
        logger.warning(f"[combat] Empty narrative for session {session_id}, sending fallback")
        try:
            await send_safe(handle, "📝 Мастер задумался... но мир не изменился.")
        except Exception:
            pass
        return

    player_text = _strip_summary_and_commands(raw_text)
    player_text, lang_entries = _parse_language_blocks(player_text)

    # Store lang translations
    if lang_entries:
        db = db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)
        for lang_name, original_text in lang_entries:
            for char in chars:
                if _character_knows_language(char.languages, lang_name):
                    try:
                        bot_obj = handle._bot
                        await bot_obj.send_message(
                            chat_id=char.player_id,
                            text=md_to_html(
                                f"🗣️ **[{lang_name}]**\n\n{original_text}\n\n"
                                f"_Перевод для {char.name}._"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

    html_text = md_to_html(player_text)

    lang_buttons = []
    for lang_name, _ in lang_entries:
        lang_buttons.append([InlineKeyboardButton(
            f"🗣️ {lang_name}",
            callback_data=f"lang:{lang_name}:char",
        )])

    # Use _send_long_blockquote for proper chunking of long combat narratives
    # instead of send_safe which would split the <blockquote> tag across chunks.
    if html_text.strip():
        bot_inst = handle._bot
        chat_id = handle.id
        if bot_inst and chat_id:
            await _send_long_blockquote(bot_inst, chat_id, html_text)
        # If there are language buttons, send them as a separate message
        if lang_buttons:
            try:
                await bot_inst.send_message(
                    chat_id=chat_id,
                    text="🌐 Переводы:",
                    reply_markup=InlineKeyboardMarkup(lang_buttons),
                )
            except Exception:
                pass


async def _resolve_non_combat_round(session_id: str, chat_id: int, bot_obj, update: Update = None, ctx=None):
    """DUAL NARRATIVE — resolve non-combat players' actions with a SEPARATE Master call.
    This runs in parallel with the combat turn loop, so non-combat players don't wait.
    Sends the narrative as a separate message with a '📖' prefix."""
    try:
        roll_mode = sessions.get_roll_mode(session_id)
        
        player_roll_requester = None
        if ctx is not None and update and update.effective_chat:
            player_roll_requester = make_player_roll_requester(session_id, ctx, update.effective_chat.id)

        stop_typing = asyncio.Event()
        handle = _ChatHandle(bot_obj, chat_id)
        typing_task = asyncio.create_task(_keep_typing(handle, stop_typing))
        try:
            result = await sessions.resolve_non_combat_round(session_id, roll_mode=roll_mode,
                                                              player_roll_requester=player_roll_requester)
        finally:
            stop_typing.set()
            typing_task.cancel()

        raw_text = result.get("player_text", "")
        if not raw_text.strip():
            return  # nothing to send

        # Strip summary block and bot commands from player-facing text
        player_text = _strip_summary_and_commands(raw_text)
        player_text, lang_entries = _parse_language_blocks(player_text)

        html_text = md_to_html(player_text)
        if html_text.strip():
            # Send non-combat narrative with a visual marker
            await _send_long_blockquote(bot_obj, chat_id, html_text)

        # Language translations
        if lang_entries:
            try:
                db = db_manager.get_db(session_id)
                chars = db.get_session_characters(session_id)
                for lang_name, original_text in lang_entries:
                    for char in chars:
                        if _character_knows_language(char.languages, lang_name):
                            try:
                                await bot_obj.send_message(
                                    chat_id=char.player_id,
                                    text=md_to_html(
                                        f"🗣️ **[{lang_name}]**\n\n{original_text}\n\n"
                                        f"_Перевод для {char.name}._"
                                    ),
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
            except Exception:
                pass

        # DB-Bot background
        raw_narrative = result.get("raw_narrative", "")
        if raw_narrative:
            sessions.set_db_busy(session_id, True)
            asyncio.create_task(_run_db_bot_background(
                session_id, raw_narrative, raw_text, None, bot_obj, chat_id,
            ))

        # Log
        md_logger.log(session_id, "master", f"[NON-COMBAT] {raw_text}")
        if result.get("gm_log"):
            md_logger.log(session_id, "system", f"GM Log: {result['gm_log']}")

        logger.info(f"[dual-narrative] Non-combat narrative sent for session {session_id}")

    except Exception as e:
        logger.error(f"[dual-narrative] Error resolving non-combat round: {e}", exc_info=True)


async def _resolve_and_send(session_id: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE = None):
    """
    Phase 1 (the Master) is awaited here and its narrative sent immediately. Phase 2
    (the DB-Bot) is launched as a background task right after — see
    _run_db_bot_background — so players see the story without also waiting for every
    HP/inventory/quest/NPC update to finish applying first.
    """
    try:
        roll_mode = sessions.get_roll_mode(session_id)

        player_roll_requester = None
        if ctx is not None and update and update.effective_chat:
            player_roll_requester = make_player_roll_requester(session_id, ctx, update.effective_chat.id)

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(update, stop_typing))
        try:
            result = await sessions.resolve_round_master_only(session_id, roll_mode=roll_mode, player_roll_requester=player_roll_requester)
        finally:
            stop_typing.set()
            typing_task.cancel()

        raw_text = result.get("player_text", "")
        raw_narrative_for_db = result.get("raw_narrative", "")
        if not raw_text.strip():
            await send_safe(update, "🤖 ДМ задумался, но ничего не ответил. Попробуйте ещё раз или используйте `/gofyn`.")
            return

        # QoL 2+3: Strip summary block and bot commands from player-facing text
        player_text = _strip_summary_and_commands(raw_text)

        # Language system: parse [LANG:xx] tags and send translations to DM
        player_text, lang_entries = _parse_language_blocks(player_text)
        if lang_entries:
            try:
                db = db_manager.get_db(session_id)
                chars = db.get_session_characters(session_id)
                bot_obj = ctx.bot if ctx else None
                # Store translations for inline button callback
                chat_id = update.effective_chat.id if update and update.effective_chat else 0
                _store_lang_translations(chat_id, lang_entries)
                # Send translations via DM only to characters who know the language
                for lang_name, original_text in lang_entries:
                    for char in chars:
                        if _character_knows_language(char.languages, lang_name):
                            try:
                                await bot_obj.send_message(
                                    chat_id=char.player_id,
                                    text=md_to_html(
                                        f"🗣️ **[{lang_name}]**\n\n{original_text}\n\n"
                                        f"_Перевод для {char.name} — другие не понимают._"
                                    ),
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
            except Exception as e:
                logger.debug(f"[lang] Failed to send language translations: {e}")

            # Add "Translate?" button to the narrative if there were language blocks
            if lang_entries:
                lang_buttons = []
                for lang_name, _ in lang_entries:
                    lang_buttons.append([InlineKeyboardButton(
                        f"🗣️ {lang_name}",
                        callback_data=f"lang:{lang_name}:char",
                    )])
                # We'll add the buttons to the narrative message below
            else:
                lang_buttons = []
        else:
            lang_buttons = []

        # Keep full narrative (with summary) for DB-Bot processing
        html_text = md_to_html(player_text)
        # Use _send_long_blockquote for proper chunking of long narratives
        # instead of manually wrapping in <blockquote expandable> and then
        # passing to send_safe which would split the tag across chunks.
        if html_text.strip():
            bot_inst = ctx.bot if ctx else None
            chat_id = update.effective_chat.id if update and update.effective_chat else 0
            if bot_inst and chat_id:
                await _send_long_blockquote(bot_inst, chat_id, html_text)
        # If there are language buttons, send them as a separate message
        if lang_buttons:
            try:
                bot_inst = ctx.bot if ctx else None
                chat_id = update.effective_chat.id if update and update.effective_chat else 0
                if bot_inst and chat_id:
                    await bot_inst.send_message(
                        chat_id=chat_id,
                        text="🌐 Переводы:",
                        reply_markup=InlineKeyboardMarkup(lang_buttons),
                    )
            except Exception:
                pass
        md_logger.log(session_id, "master", raw_text)
        if result.get("gm_log"):
            md_logger.log(session_id, "system", f"GM Log: {result['gm_log']}")
            logger.info(f"GM: {result['gm_log']}")

        # Anti-spam: delete tracked round messages
        if ANTISPAM_ENABLED and update and update.effective_chat:
            asyncio.create_task(_delete_round_messages(session_id, ctx.bot, update.effective_chat.id))

        # Async translations
        if TRANSLATOR_ENABLED and ctx and ctx.bot:
            asyncio.create_task(_send_translations(session_id, result.get("raw_narrative", ""), ctx.bot))

        # Real-time admin stream
        if ctx and ADMIN_CHAT_ID:
            admin_text = f"📜 {session_id[:6]} | {raw_text[:3000]}"
            await send_to_admin(ctx, admin_text)

        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if session and session.combat_active and result.get("combat_active_after"):
            # V10b: Combat just started — trigger per-turn combat loop
            chat_id = update.effective_chat.id if update and update.effective_chat else session.chat_id
            bot_obj = ctx.bot if ctx else None
            if bot_obj and chat_id:
                asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot_obj))

        if result.get("error"):
            return  # phase 1 itself failed — nothing valid to hand off to the DB-Bot

        # Phase 2: DB-Bot runs in the background — players already have their story.
        chat_id = update.effective_chat.id if update and update.effective_chat else (session.chat_id if session else None)
        bot_obj = ctx.bot if ctx else None
        sessions.set_db_busy(session_id, True)
        asyncio.create_task(_run_db_bot_background(
            session_id, result.get("raw_narrative", ""), raw_text, result.get("raw_sheets"), bot_obj, chat_id, ctx,
        ))

    except Exception as e:
        logger.error(f"Resolve error: {e}")
        await send_safe(update, f"❌ Ошибка разрешения: {e}")


async def _run_db_bot_background(session_id: str, raw_narrative: str, player_text: str,
                                  raw_sheets, bot_obj, chat_id, ctx=None):
    """Runs the DB-Bot phase, then: flips is_db_busy off, sends a short completion
    notice to the chat EVEN IF NOBODY ASKED (explicitly requested — players otherwise
    had no way to tell 'still updating' from 'done, nothing changed'), and resolves any
    round that finished collecting while this was running (see mark_pending_resolve).

    Idea 2 (reconciliation): if ctx is provided, builds a player_roll_requester so
    DB-Bot's `dispatch_roll` tool calls can send inline-button rolls to players.
    Also builds session_history from DB so DB-Bot's `ask_master` tool calls give
    Master enough context to answer."""
    applied = 0
    # Build player_roll_requester for DB-Bot's dispatch_roll tool (Idea 2).
    # If ctx/chat_id aren't available, dispatch_roll will fall back to server-side rolls.
    db_player_roll_requester = None
    if ctx is not None and bot_obj and chat_id:
        try:
            db_player_roll_requester = make_player_roll_requester(session_id, ctx, chat_id)
        except Exception as e:
            logger.warning(f"[db-bot-background] failed to build player_roll_requester: {e}")

    # Build session_history for DB-Bot's ask_master tool (Idea 2).
    session_history_msgs = []
    try:
        db = db_manager.get_db(session_id)
        history = db.get_history(session_id, limit=30)
        for h in history:
            role = "user" if h.author != "DM" else "assistant"
            content = h.content if h.author in ("DM", "SYSTEM", "GM_SECRET") else f"{h.author}: {h.content}"
            session_history_msgs.append({"role": role, "content": content})
    except Exception as e:
        logger.warning(f"[db-bot-background] failed to build session_history: {e}")

    try:
        db_result = await sessions.run_db_bot_phase(
            session_id, raw_narrative, player_text, raw_sheets,
            player_roll_requester=db_player_roll_requester,
            session_history=session_history_msgs,
        )
        applied = db_result.get("game_actions_applied", 0)
        md_logger.log(session_id, "db_bot", f"Applied {applied} DB actions")
        if db_result.get("errors"):
            logger.warning(f"[db-bot-background] {session_id}: {db_result['errors']}")
    except Exception as e:
        logger.error(f"[db-bot-background] {session_id}: {e}")
    finally:
        sessions.set_db_busy(session_id, False)

    if not bot_obj or not chat_id:
        return
    handle = _ChatHandle(bot_obj, chat_id)
    try:
        await send_safe(handle, f"✅ Мир обновлён. Применено изменений: {applied}.", auto_delete=15)
    except Exception as e:
        logger.warning(f"[db-bot-background] completion notice failed: {e}")

    pending = sessions.pop_pending_resolve(session_id)
    if pending:
        try:
            await send_safe(handle, "🎲 Все на месте — нейросеть разрешает...", auto_delete=15)
            await _resolve_and_send(session_id, handle, pending.get("ctx"))
        except Exception as e:
            logger.error(f"[db-bot-background] deferred resolve failed: {e}")
