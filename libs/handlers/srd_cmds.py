"""
libs.handlers.srd_cmds — auto-split from libs/bot_handlers.py.

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
from libs.handlers.utils import get_session
from libs.handlers.utils import md_to_html
from libs.handlers.utils import send_safe


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def srd_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not ctx.args:
        await send_safe(update, "📖 Использование: `/cyfeirlyfr огненный шар` или `/cyfeirlyfr условия отравления`")
        return

    query = " ".join(ctx.args)

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")
    try:
        result = await sessions.srd_lookup(query)
        await send_safe(update, f"📖 **SRD: {query}**\n\n{result}")
    except Exception as e:
        logger.error(f"SRD error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


async def private_action_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Private action in DM — with inline roll button.
    On success: consequences sent ONLY to player (DM).
    On failure: action + result sent to group chat (everyone sees).
    """
    user = update.effective_user

    if update.effective_chat.type != "private":
        await send_safe(update, "🕵️ Эта команда только в ЛС бота. Напиши мне в личку.")
        return

    if not ctx.args:
        await send_safe(update, "🕵️ Использование (в ЛС): `/gwneud краду карман у торговца`")
        return

    action_text = " ".join(ctx.args)

    active_sessions = db_manager.get_all_active_sessions()
    player_session = None
    player = None
    for s in active_sessions:
        db = db_manager.get_db(s.id)
        p = db.get_player(user.id, s.id)
        if p:
            player_session = s
            player = p
            break

    if not player_session:
        await send_safe(update, "❌ Ты не в активной сессии. Сначала `/ymuno` в группе.")
        return

    char = db_manager.get_db(player_session.id).get_character_by_player(user.id, player_session.id)
    char_name = char.name if char else player.display_name

    db = db_manager.get_db(player_session.id)
    db.add_history(HistoryEntry(
        session_id=player_session.id,
        author=f"{char_name} [ПРИВАТНО]",
        content=f"Дн. {action_text}",
        entry_type="action",
    ))

    # Send confirmation with inline roll button
    roll_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Бросить (проверка навыка)", callback_data=f"gwroll:{player_session.id}:{user.id}")]
    ])

    await send_safe(update,
        f"🕵️ **Приватное действие:**\n_{action_text}_\n\n"
        f"Нажми кнопку чтобы бросить кубик.\n"
        f"✅ Успех — результат только тебе в ЛС.\n"
        f"❌ Провал — все увидят твою попытку.",
        reply_markup=roll_btn
    )

    # Notify admin (creator) in DM about private action
    players = sessions.get_players(player_session.id)
    for pl in players:
        if pl.is_creator:
            try:
                await ctx.bot.send_message(
                    chat_id=pl.user_id,
                    text=md_to_html(f"🕵️ **Приватное действие** от {char_name}:\n_{action_text}_"),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            break


async def _private_roll_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the private action roll button press."""
    query = update.callback_query
    await query.answer()  # acknowledge button press

    data = query.data  # "gwroll:session_id:user_id"
    parts = data.split(":")
    if len(parts) < 3:
        return

    session_id = parts[1]
    target_user_id = int(parts[2])
    user = query.from_user

    # Security: only the player who initiated can press
    if user.id != target_user_id:
        await query.answer("❌ Это не твоя кнопка!", show_alert=True)
        return

    try:
        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            await query.answer("❌ Сессия не найдена.", show_alert=True)
            return

        player = db.get_player(user.id, session_id)
        char = db.get_character_by_player(user.id, session_id)
        if not player or not char:
            await query.answer("❌ Персонаж не найден.", show_alert=True)
            return

        char_name = char.name

        # Get the private action from history
        history = db.get_history(session_id, limit=5)
        private_action = ""
        for h in reversed(history):
            if f"[ПРИВАТНО]" in h.author and h.entry_type == "action":
                private_action = h.content.replace("Дн. ", "", 1)
                break

        if not private_action:
            await query.answer("❌ Приватное действие не найдено.", show_alert=True)
            return

        # Roll d20 + appropriate modifier (ask AI to determine which skill)
        # Use the master model to resolve the private action
        import random
        raw_roll = random.randint(1, 20)

        # Build a prompt for the neural network to resolve the private action
        # Character stats are stored in char.stats as JSON, not as direct attributes
        _char_stats = json.loads(char.stats) if char.stats else {}
        private_prompt = (
            f"Игрок {char.name} делает ПРИВАТНОЕ действие: {private_action}\n\n"
            f"Бросок d20: {raw_roll}\n"
            f"Характеристики: STR {_char_stats.get('strength', 10)} DEX {_char_stats.get('dexterity', 10)} CON {_char_stats.get('constitution', 10)} INT {_char_stats.get('intelligence', 10)} WIS {_char_stats.get('wisdom', 10)} CHA {_char_stats.get('charisma', 10)}\n"
            f"Раса: {char.race}, Класс: {char.class_name}, Уровень: {char.level}\n"
            f"Языки: {char.languages}\n\n"
            f"Определи:\n"
            f"1. Какой навык/характеристику использовать (Скрытность, Ловкость рук, Убеждение и т.д.)\n"
            f"2. Модификатор (+профессиональность если есть)\n"
            f"3. СЛ (DC) для проверки\n"
            f"4. Результат: УСПЕХ или ПРОВАЛ (применяй градацию)\n\n"
            f"Ответь в формате JSON:\n"
            f"{{\"skill\": \"название\", \"modifier\": N, \"dc\": N, \"total\": N, "
            f"\"success\": true/false, \"narrative\": \"нарратив результата\"}}\n"
            f"НЕ добавляй текст за пределами JSON."
        )

        try:
            dm_response = await dm_engine.master.chat(
                [{"role": "user", "content": private_prompt}],
                system_prompt="Ты — судья бросков для D&D 5e. Отвечай ТОЛЬКО JSON без markdown.",
                tools=None,
            )
            resp_text = dm_response.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Parse JSON from response — robust parsing that handles:
            # 1. Markdown code fences (```json ... ```)
            # 2. Nested braces in narrative text
            # 3. Extra text before/after the JSON object
            result = None
            # Strip markdown code fences
            cleaned = resp_text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            cleaned = cleaned.strip()

            # Try parsing the entire cleaned text first
            try:
                result = json.loads(cleaned)
            except json.JSONDecodeError:
                pass

            # If that fails, find the outermost { ... } using brace matching
            if result is None:
                first_brace = cleaned.find("{")
                if first_brace != -1:
                    depth = 0
                    last_brace = -1
                    for ci in range(first_brace, len(cleaned)):
                        if cleaned[ci] == '{':
                            depth += 1
                        elif cleaned[ci] == '}':
                            depth -= 1
                            if depth == 0:
                                last_brace = ci + 1
                                break
                    if last_brace > first_brace:
                        try:
                            result = json.loads(cleaned[first_brace:last_brace])
                        except json.JSONDecodeError:
                            pass

            if result is None:
                raise ValueError(f"No valid JSON in response: {resp_text[:200]}")

            skill = result.get("skill", "Проверка")
            modifier = result.get("modifier", 0)
            dc = result.get("dc", 15)
            total = result.get("total", raw_roll + modifier)
            success = result.get("success", False)
            narrative = result.get("narrative", "")

            roll_display = f"🎲 d20({raw_roll}) + {modifier} = **{total}** vs СЛ **{dc}**"
            if success:
                emoji = "✅"
                status = "УСПЕХ"
            else:
                emoji = "❌"
                status = "ПРОВАЛ"

            # Send roll result to player in DM
            await ctx.bot.send_message(
                chat_id=user.id,
                text=md_to_html(
                    f"🕵️ **Приватный бросок** {char_name}:\n"
                    f"{roll_display}\n"
                    f"{emoji} **{status}** — {skill}\n\n"
                    f"_{narrative}_"
                ),
                parse_mode="HTML",
            )

            if success:
                # Success: consequences are PRIVATE — send narrative only to player
                await ctx.bot.send_message(
                    chat_id=user.id,
                    text=md_to_html(
                        f"🔒 **Приватный результат:**\n\n"
                        f"_{narrative}_\n\n"
                        f"_Только ты видишь этот результат. Другие игроки НЕ знают о твоём действии._"
                    ),
                    parse_mode="HTML",
                )
            else:
                # Failure: EVERYONE sees the action and result
                # Send to group chat
                group_msg = (
                    f"🕵️ **{char_name}** пытался что-то сделать скрытно...\n"
                    f"_{private_action}_\n\n"
                    f"Но не справился:\n{roll_display} — {emoji} **{status}**\n\n"
                    f"_{narrative}_"
                )
                if session.chat_id:
                    try:
                        await ctx.bot.send_message(
                            chat_id=session.chat_id,
                            text=md_to_html(group_msg),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.debug(f"[gwneud] Failed to send to group: {e}")

            # Log to history
            db.add_history(HistoryEntry(
                session_id=session_id,
                author=f"[ПРИВАТНО:{status}]",
                content=f"{char_name}: {private_action} | {roll_display} {status}",
                entry_type="system",
            ))

        except Exception as e:
            logger.error(f"[gwneud] Error resolving private action: {e}")
            # Fallback: simple roll display
            await ctx.bot.send_message(
                chat_id=user.id,
                text=md_to_html(f"🎲 Приватный бросок: **{raw_roll}** (d20)\n\nОшибка нейросети: {e}"),
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(f"[gwneud] Callback error: {e}")
        await query.answer(f"❌ Ошибка: {e}", show_alert=True)
