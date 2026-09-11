"""
libs.handlers.translations — auto-split from libs/bot_handlers.py.

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


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

def _store_lang_translations(chat_id: int, entries: list):
    """Store language translations for this chat. Overwrites previous round."""
    import time
    _lang_round_counter[chat_id] = _lang_round_counter.get(chat_id, 0) + 1
    _lang_translations[chat_id] = [(lang, text, _lang_round_counter[chat_id]) for lang, text in entries]


def _get_lang_translations(chat_id: int) -> list:
    """Get stored translations for chat. Returns [(lang_name, text, round_ts), ...]"""
    return _lang_translations.get(chat_id, [])


def _character_knows_language(char_languages_str: str, target_lang: str) -> bool:
    """Check if a character knows a language. Supports fuzzy matching."""
    try:
        languages = json.loads(char_languages_str)
    except (json.JSONDecodeError, TypeError):
        languages = []
    if not languages:
        return False
    target_lower = target_lang.lower()
    for lang in languages:
        lang_lower = lang.lower()
        # Exact match
        if lang_lower == target_lower:
            return True
        # Partial match (e.g. "Дварфийский" matches "Дварфийский (Dwarvish)")
        if target_lower in lang_lower or lang_lower in target_lower:
            return True
        # Language family matching
        lang_families = {
            "эльфийский": ["elvish", "sylvan", "эльфийский"],
            "дварфийский": ["dwarvish", "дварфийский"],
            "гоблинский": ["goblin", "гоблинский"],
            "орочий": ["orcish", "орочий"],
            "драконий": ["draconic", "драконий"],
            "инфернальный": ["infernal", "инфернальный"],
            "подземный": ["undercommon", "underdark", "дроу", "подземный"],
            "гнолл": ["gnoll", "гнолл"],
            "гигантский": ["giant", "giantish", "гигантский"],
            "примитивный": ["primordial", "примитивный"],
            "небесный": ["celestial", "небесный"],
            "общий": ["common", "общий"],
        }
        for family in lang_families.get(target_lower, []):
            if family in lang_lower:
                return True
    return False


def _strip_summary_and_commands(text: str) -> str:
    """Strip the summary block (─── СВОДКА ─── ... ─── КОНЕЦ СВОДКИ ───) and any
    stray bot commands (/aur, /nodau, /tywydd, /lleoliad, /dinas, etc.) from
    the Master's narrative before sending to players. The DB-Bot still gets
    the full text with the summary block intact.

    BUG #9: also auto-translate any English condition words that slipped through
    the prompt's anti-English rule (e.g. "unconscious", "face-down", "prone")
    into their canonical Russian form so the player never sees mixed-language
    status text."""
    if not text:
        return text

    # 1. Strip summary block (including variations: СВОДКА/RESUMEN/SUMMARY/ИТОГИ
    #    and their END markers, in any language the master might use)
    text = _re.sub(
        r'\n?[─━═]{3,}\s*(?:СВОДКА|RESUMEN|SUMMARY|ИТОГИ|СВОД|СТАТУС)\s*[─━═]{3,}'
        r'.*?'
        r'[─━═]{3,}\s*(?:КОНЕЦ СВОДКИ|FIN DEL RESUMEN|END OF SUMMARY|КОНЕЦ ИТОГОВ|КОНЕЦ СВОДКИ|КОНЕЦ)\s*[─━═]{3,}\s*',
        '\n', text, flags=_re.DOTALL
    )

    # 2. Strip stray bot command references in parentheses or at line starts
    bot_commands = [
        '/aur', '/nodau', '/tywydd', '/lleoliad', '/dinas', '/cwest',
        '/iechyd', '/eiddo', '/cyflwr', '/amser', '/ffactiynau', '/byd',
        '/cymeriadnc', '/perthynasau', '/gallu', '/rholio', '/adnoddau',
        '/canolbwyntio', '/digwyddiad', '/modd', '/gorffwys',
    ]
    for cmd in bot_commands:
        # Remove "Также смотри /command" style hints
        text = _re.sub(r'[Сс]мотри\s+' + _re.escape(cmd) + r'[^\n]*', '', text)
        # Remove bare commands at line start (not part of a sentence)
        text = _re.sub(r'^\s*' + _re.escape(cmd) + r'\s*$', '', text, flags=_re.MULTILINE)

    # BUG #9: auto-translate any English condition word that the model slipped
    # into the player-facing narrative. Match on word boundaries so we don't
    # accidentally translate "face-down" inside a larger token. Order matters —
    # longer phrases first so "face-down" matches before "down".
    en_to_ru = [
        # Multi-word phrases first (longer match first)
        (r'\bface-down\b', 'лежит лицом вниз'),
        (r'\bfacedown\b', 'лежит лицом вниз'),
        (r'\bunconscious\b', 'без сознания'),
        (r'\bincapacitated\b', 'неспособен действовать'),
        (r'\bblinded\b', 'ослеплён'),
        (r'\bdeafened\b', 'оглох'),
        (r'\bfrightened\b', 'испуган'),
        (r'\bgrappled\b', 'схвачен'),
        (r'\brestrained\b', 'сдержан'),
        (r'\bparalyzed\b', 'парализован'),
        (r'\bpetrified\b', 'окаменел'),
        (r'\bpoisoned\b', 'отравлен'),
        (r'\bstunned\b', 'ошеломлён'),
        (r'\bcharmed\b', 'очарован'),
        (r'\binvisible\b', 'невидим'),
        (r'\bprone\b', 'повержен'),
        (r'\bbleeding\b', 'кровотечение'),
        (r'\bbloodied\b', 'истекает кровью'),
        (r'\bdying\b', 'при смерти'),
        (r'\bstable\b', 'стабилизирован'),
        (r'\bdead\b', 'мёртв'),
        (r'\basleep\b', 'спит'),
        (r'\bsurprised\b', 'застигнут врасплох'),
        (r'\bdiseased\b', 'болен'),
        (r'\bcursed\b', 'проклят'),
        (r'\bhidden\b', 'скрыт'),
        (r'\bregenerating\b', 'регенерирует'),
        (r'\btrapped\b', 'в ловушке'),
        (r'\bstuck\b', 'застрял'),
        (r'\bflying\b', 'летит'),
        (r'\bswimming\b', 'плывёт'),
        (r'\bclimbing\b', 'лезет'),
        (r'\bfalling\b', 'падает'),
        (r'\bkneeling\b', 'стоит на коленях'),
        (r'\bcrouching\b', 'присел'),
        (r'\bsitting\b', 'сидит'),
        (r'\blying\b', 'лежит'),
        (r'\bstanding\b', 'стоит'),
        (r'\bconcentrating\b', 'концентрируется'),
        (r'\bexhaustion\b', 'истощение'),
    ]
    for pattern, ru in en_to_ru:
        text = _re.sub(pattern, ru, text, flags=_re.IGNORECASE)

    return text.strip()


_LANG_TAG_RE = _re.compile(r'\[LANG:(\w+)\](.*?)\[/LANG:\1\]', _re.DOTALL)


def _parse_language_blocks(text: str):
    """Extract [LANG:xx]text[/LANG] blocks from narrative. Returns:
    - cleaned text (with gibberish, LANG tags removed)
    - list of (language_name, original_text) tuples for DM routing
    """
    lang_entries = []
    
    def _replace_tag(match):
        lang = match.group(1)
        original = match.group(2)
        lang_entries.append((lang, original))
        # Replace with gibberish placeholder — the master already provides it after the tag
        return ""
    
    # Remove the LANG tags but keep what's after them (gibberish)
    result = _LANG_TAG_RE.sub(_replace_tag, text)
    return result, lang_entries
