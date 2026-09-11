"""
libs.handlers.lobby_cmds — auto-split from libs/bot_handlers.py.

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
from libs.handlers.utils import fmt_players
from libs.handlers.utils import get_session
from libs.handlers.utils import send_safe


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def new_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if get_session(chat_id):
        await send_safe(update, "❌ В чате уже есть сессия. `/diwedd` чтобы завершить.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/newydd Название кампании`")
        return

    name = " ".join(ctx.args)
    session = sessions.create_session(chat_id, name, user.id, user.username or user.first_name)
    await send_safe(update, 
        f"⚔️ **Сессия создана!**\n*{session.name}*\nID: `{session.id}`\n\nДругие игроки: `/ymuno`",
    )


async def join_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии. Создай `/newydd НАЗВАНИЕ`")
        return
    db = db_manager.get_db(session.id)
    if db.get_player(user.id, session.id):
        await send_safe(update, "Ты уже в сессии!")
        return

    sessions.add_player(session.id, user.id, user.username or "", user.first_name or user.username or "Неизвестный")
    sessions.add_player_to_queue(session.id, user.id)
    await send_safe(update, 
        f"✅ **{user.first_name or user.username}** присоединился!\n\n"
        "Загрузи персонажа: отправь .txt/.md и ответь `/cymeriad`",
    )


async def leave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        return
    db = db_manager.get_db(session.id)
    if sessions.is_creator(user.id, session.id):
        # Creator is leaving — auto-transfer to the next player
        players = db.get_players(session.id)
        other_players = [p for p in players if p.user_id != user.id]
        if other_players:
            # Transfer creator rights to the first other player
            new_creator = other_players[0]
            with db._connect() as conn:
                conn.execute(
                    "UPDATE players SET is_creator = 0 WHERE user_id = ? AND session_id = ?",
                    (user.id, session.id)
                )
                conn.execute(
                    "UPDATE players SET is_creator = 1 WHERE user_id = ? AND session_id = ?",
                    (new_creator.user_id, session.id)
                )
            await send_safe(update, f"👑 **Права Админа автоматически переданы {new_creator.display_name}!**")
        else:
            # No other players — just end the session
            sessions.end_session(session.id)
            await send_safe(update, f"🏁 Сессия *{session.name}* завершена — больше нет игроков.")
            return
    sessions.remove_player(session.id, user.id)
    await send_safe(update, f"👋 Ты покинул *{session.name}*.")


async def players_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    players = sessions.get_players(session.id)
    if not players:
        await send_safe(update, "Пусто.")
        return
    await send_safe(update, f"🎮 **Игроки:**\n\n{fmt_players(players)}")
