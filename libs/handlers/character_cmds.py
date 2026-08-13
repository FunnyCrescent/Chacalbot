"""
libs.handlers.character_cmds — auto-split from libs/bot_handlers.py.

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
from libs.handlers.settings_cmds import _get_all_categories
from libs.handlers.settings_cmds import _get_setting_info
from libs.handlers.settings_cmds import _get_settings_in_category
from libs.handlers.utils import _keep_typing
from libs.handlers.settings_cmds import _load_setting_md
from libs.handlers.narrator_cmds import _process_dn_action
from libs.handlers.utils import _read_sheet_text
from libs.handlers.utils import _send_long_blockquote
from libs.handlers.utils import get_session
from libs.handlers.utils import md_to_html
from libs.handlers.utils import send_safe
from libs.handlers.utils import send_to_admin


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def char_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Upload character sheet — validated by AI model.
    
    Supports 3 modes:
    1. Attach a .txt/.md file to the command → validates, parses, saves
    2. Reply/forward to a .txt/.md file with the command → same as above
    3. No file attached → shows saved characters via inline buttons for selection/deletion
       In group: auto-applies to current session. In DM: lets pick session.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    message = update.message

    # ── Determine source document ──
    doc = None
    source_msg = None

    # Check: attached document to the command itself
    if message.document:
        doc = message.document
        source_msg = message
    # Check: reply to a forwarded/replied message with document
    elif message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
        source_msg = message.reply_to_message

    # ── No file → show saved characters picker ──
    if not doc:
        await _show_saved_chars_picker(update, user.id, chat_id)
        return

    file_name = doc.file_name or ""
    if not file_name.lower().endswith((".txt", ".md")):
        await send_safe(update, "❌ Только .txt или .md. Если файл из Word — сохрани как Обычный текст.")
        return

    await _process_character_upload(update, ctx, user, doc, file_name)


async def _process_character_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                     user, doc, file_name: str):
    """Validate, parse, and save a character sheet. Works in both group and DM."""
    chat_id = update.effective_chat.id
    is_dm = update.effective_chat.type == "private"

    try:
        file = await doc.get_file()
        # In DM use user.id only, in group use user_id + session.id
        if is_dm:
            fp = os.path.join(CHARACTERS_DIR, f"{user.id}_{doc.file_name}")
        else:
            session = get_session(chat_id)
            if not session:
                await send_safe(update, "⚠️ Нет активной сессии в этом чате.")
                return
            db = db_manager.get_db(session.id)
            if not db.get_player(user.id, session.id):
                await send_safe(update, "⚠️ Сначала `/ymuno`")
                return
            fp = os.path.join(CHARACTERS_DIR, f"{user.id}_{session.id}_{doc.file_name}")
        await file.download_to_drive(fp)

        try:
            sheet_text = _read_sheet_text(fp)
        except UnicodeDecodeError:
            await send_safe(update, "❌ Неизвестная кодировка файла. Сохрани лист как `.txt` в UTF-8 (Notepad++ → Кодировки → UTF-8).")
            return

        # === AI VALIDATION ===
        await send_safe(update, "🔍 Проверяю лист персонажа правилами D&D 5e...")
        validation = await dm_engine.validate_character_sheet(sheet_text)

        if validation["verdict"] == "REJECT":
            await send_safe(update, 
                f"❌ **Лист персонажа отклонён.**\n\n"
                f"{validation['details'][:1000]}\n\n"
                f"🔧 **Исправь ошибки и загрузи лист заново.**"
            )
            return
        elif validation["verdict"] == "NEEDS_FIX":
            await send_safe(update, 
                f"⚠️ **Лист персонажа требует исправлений:**\n\n"
                f"{validation['details'][:1200]}\n\n"
                f"🔧 **Исправь ошибки и загрузи лист заново командой `/cymeriad`**"
            )
            return
        elif validation["verdict"] == "VALID":
            await send_safe(update, "✅ Лист персонажа прошёл проверку правил!")

        # === AI PARSING (primary) ===
        parsed = await dm_engine.parse_character_sheet(sheet_text)
        if not parsed:
            parsed = CharacterParser.parse_file(fp)
        if not parsed:
            await send_safe(update, "❌ Не удалось распарсить лист персонажа.")
            return

        # === SRD ANTI-CHEAT VALIDATION (race/class/background) ===
        try:
            srd_result = dm_engine.validate_character_srd(parsed)
            if srd_result and not srd_result.is_valid:
                warnings_text = []
                for r in [srd_result.race_result, srd_result.class_result,
                          srd_result.background_result, srd_result.backstory_result]:
                    if r and not r.is_valid:
                        warnings_text.append(f"⚠️ {r.message}")
                if warnings_text:
                    await send_safe(update,
                        f"⚠️ **SRD проверки:**\n\n" +
                        "\n".join(warnings_text[:5]) +
                        "\n\n📝 *Персонаж добавлен, но DM может проверить.*"
                    )
            elif srd_result and srd_result.is_valid:
                await send_safe(update, "✅ SRD проверка пройдена: раса, класс, предыстория в норме.")
        except Exception as e:
            logger.warning(f"SRD validation failed (non-blocking): {e}")

        if is_dm:
            # ── DM mode: save to global saved_chars_db ──
            # Serialize parsed data for instant restore later (no AI re-parse)
            parsed_json = json.dumps({
                "name": parsed.name, "race": parsed.race, "class_name": parsed.class_name,
                "level": parsed.level, "hp": parsed.hp, "max_hp": parsed.max_hp, "ac": parsed.ac,
                "stats": {
                    "strength": parsed.strength, "dexterity": parsed.dexterity,
                    "constitution": parsed.constitution, "intelligence": parsed.intelligence,
                    "wisdom": parsed.wisdom, "charisma": parsed.charisma,
                },
                "proficiencies": parsed.proficiencies,
                "skills": parsed.skills, "languages": parsed.languages,
                "inventory": parsed.inventory, "spells": parsed.spells, "features": parsed.features,
                "backstory": parsed.backstory,
                "gold": parsed.gold, "gold_cp": parsed.gold_cp,
                "gold_sp": parsed.gold_sp, "gold_ep": parsed.gold_ep, "gold_pp": parsed.gold_pp,
            }, ensure_ascii=False)
            saved_id = saved_chars_db.save_character(
                player_id=user.id,
                char_name=parsed.name,
                char_class=parsed.class_name or "",
                sheet_text=sheet_text,
                file_name=file_name,
                parsed_data=parsed_json,
            )
            await send_safe(update,
                f"✅ **Персонаж сохранён в ЛС!** (#{saved_id})\n\n"
                f"📌 Используй `/cymeriad` без файла, чтобы выбрать персонажа для игры.\n\n"
                f"{CharacterParser.format_character_sheet(parsed)}"
            )
        else:
            # ── Group mode: save to session ──
            session = get_session(chat_id)
            db = db_manager.get_db(session.id)

            db.save_character_sheet(session.id, user.id, sheet_text, file_name)

            char_id = str(uuid.uuid4())[:8]
            existing_char = db.get_character_by_player(user.id, session.id)
            if existing_char:
                char_id = existing_char.id
            else:
                char_id = str(uuid.uuid4())[:8]

            db.save_character(Character(
                id=char_id, session_id=session.id, player_id=user.id,
                name=parsed.name, race=parsed.race, class_name=parsed.class_name,
                level=parsed.level, hp=parsed.hp, max_hp=parsed.max_hp, ac=parsed.ac,
                stats=parsed.get_stats_json(),
                proficiencies=json.dumps(parsed.proficiencies),
                inventory=json.dumps(parsed.inventory),
                spells=json.dumps(parsed.spells),
                features=json.dumps(parsed.features),
                backstory=parsed.backstory,
                languages=json.dumps(parsed.languages),
            ))
            # Starting gold
            currency_kwargs = {
                "gp": getattr(parsed, "gold", 0) or 0,
                "sp": getattr(parsed, "gold_sp", 0) or 0,
                "cp": getattr(parsed, "gold_cp", 0) or 0,
                "ep": getattr(parsed, "gold_ep", 0) or 0,
                "pp": getattr(parsed, "gold_pp", 0) or 0,
            }
            if any(currency_kwargs.values()):
                sessions.add_gold(session.id, char_id, parsed.name, reason="Стартовый капитал", **currency_kwargs)
            for item in parsed.inventory:
                if item and item.strip():
                    sessions.add_item(session.id, char_id, parsed.name, item.strip(), 1)
            await send_safe(update,
                f"✅ **Персонаж сохранён!** Лист обработан нейросетью.\n\n"
                f"{CharacterParser.format_character_sheet(parsed)}",
            )
    except Exception as e:
        logger.error(f"Char error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


async def _show_saved_chars_picker(update: Update, player_id: int, chat_id: int = 0):
    """Show saved characters as inline buttons."""
    is_dm = update.effective_chat.type == "private"
    chars = saved_chars_db.get_characters(player_id)
    if not chars:
        hint = "📦 У тебя пока нет сохранённых персонажей.\n\n"
        if is_dm:
            hint += "📄 Прикрепи .txt/.md файл к `/cymeriad`, чтобы загрузить и сохранить персонажа."
        else:
            hint += "📄 Прикрепи .txt/.md файл к `/cymeriad` или загрузи в **ЛС бота**."
        await send_safe(update, hint)
        return

    buttons = []
    for ch in chars:
        label = f"{ch['char_name']}"
        if ch.get("char_class"):
            label += f" ({ch['char_class']})"
        label += f" #{ch['id']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"charpick:sel:{ch['id']}:{chat_id}")])

    buttons.append([InlineKeyboardButton("🗑 Удалить персонажа", callback_data="charpick:delmenu")])

    if is_dm:
        text = f"📋 **Твои сохранённые персонажи** ({len(chars)}):\n\nВыбери для применения в сессии."
    else:
        text = f"📋 **Сохранённые персонажи** ({len(chars)}):\n\nВыбери для применения в **этой сессии**."
    await send_safe(update, text, reply_markup=InlineKeyboardMarkup(buttons))


async def _charpick_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for saved character picker."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    if data == "charpick:delmenu":
        # Show deletion submenu
        chars = saved_chars_db.get_characters(user.id)
        if not chars:
            await query.edit_message_text("📦 Нет сохранённых персонажей.")
            return
        buttons = []
        for ch in chars[:10]:  # Max 10 buttons
            label = f"🗑 {ch['char_name']}"
            if ch.get("char_class"):
                label += f" ({ch['char_class']})"
            buttons.append([InlineKeyboardButton(label, callback_data=f"charpick:del:{ch['id']}")])
        buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="charpick:back")])
        await query.edit_message_text(
            "🗑 <b>Выбери персонажа для удаления:</b>",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
        )
        return

    if data == "charpick:back":
        origin_chat_id = query.message.chat.id if query.message and query.message.chat else 0
        chars = saved_chars_db.get_characters(user.id)
        if not chars:
            await query.edit_message_text("📦 Нет сохранённых персонажей.")
            return
        buttons = []
        for ch in chars:
            label = f"{ch['char_name']}"
            if ch.get("char_class"):
                label += f" ({ch['char_class']})"
            label += f" #{ch['id']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"charpick:sel:{ch['id']}:{origin_chat_id}")])
        buttons.append([InlineKeyboardButton("🗑 Удалить персонажа", callback_data="charpick:delmenu")])
        await query.edit_message_text(
            f"📋 <b>Твои сохранённые персонажи</b> ({len(chars)}):",
            reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
        )
        return

    if data.startswith("charpick:del:"):
        char_id = int(data.split(":")[-1])
        deleted = saved_chars_db.delete_character(char_id, user.id)
        if deleted:
            origin_chat_id = query.message.chat.id if query.message and query.message.chat else 0
            chars = saved_chars_db.get_characters(user.id)
            if not chars:
                await query.edit_message_text("🗑 Персонаж удалён.\n\n📦 Список пуст.")
                return
            buttons = []
            for ch in chars:
                label = f"{ch['char_name']}"
                if ch.get("char_class"):
                    label += f" ({ch['char_class']})"
                label += f" #{ch['id']}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"charpick:sel:{ch['id']}:{origin_chat_id}")])
            buttons.append([InlineKeyboardButton("🗑 Удалить персонажа", callback_data="charpick:delmenu")])
            await query.edit_message_text(
                f"🗑 Персонаж удалён.\n\n📋 <b>Осталось</b> ({len(chars)}):",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
            )
        else:
            await query.edit_message_text("❌ Не удалось удалить.")
        return

    if data.startswith("charpick:sel:"):
        # Format: charpick:sel:CHAR_ID:CHAT_ID
        parts = data.split(":")
        char_id = int(parts[2])
        origin_chat_id = int(parts[3]) if len(parts) > 3 else 0

        saved = saved_chars_db.get_character(char_id, user.id)
        if not saved:
            await query.edit_message_text("❌ Персонаж не найден (возможно, был удалён).", parse_mode="HTML")
            return

        sheet_text = saved["sheet_text"]
        char_name = saved["char_name"]
        char_class = saved.get("char_class", "")
        parsed_data = saved.get("parsed_data", "")

        if origin_chat_id and origin_chat_id > 0:
            # Called from a group chat — apply to this chat's session directly
            session = get_session(origin_chat_id)
            if not session:
                await query.edit_message_text("⚠️ В этом чате нет активной сессии.", parse_mode="HTML")
                return
            await _apply_saved_char_to_session(update, query, user, session,
                                               sheet_text, char_name, char_class, parsed_data,
                                               saved_id=char_id)
            return

        # Called from DM — check user's active sessions
        user_sessions = db_manager.get_user_sessions(user.id)
        if not user_sessions:
            await query.edit_message_text(
                "⚠️ У тебя нет активных сессий.\n\n"
                "Сначала присоединись к игре в групповом чате (<code>/ymuno</code>), "
                "затем можно будет применить персонажа.",
                parse_mode="HTML",
            )
            return

        if len(user_sessions) == 1:
            session = user_sessions[0]
            await _apply_saved_char_to_session(update, query, user, session,
                                               sheet_text, char_name, char_class, parsed_data,
                                               saved_id=char_id)
        else:
            # Show session picker with chat NAMES, not IDs
            buttons = []
            for s in user_sessions:
                try:
                    chat = await update.get_bot().get_chat(s.chat_id)
                    chat_label = chat.title or str(s.chat_id)
                except Exception:
                    chat_label = str(s.chat_id)
                buttons.append([InlineKeyboardButton(
                    chat_label,
                    callback_data=f"charpick:apply:{char_id}:{s.id}",
                )])
            buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="charpick:back")])
            await query.edit_message_text(
                f"🎭 <b>{char_name}</b> ({char_class})\n\nВ какой сессии применить?",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
            )
        return

    if data.startswith("charpick:apply:"):
        parts = data.split(":")
        char_id = int(parts[2])
        session_id = parts[3]
        saved = saved_chars_db.get_character(char_id, user.id)
        if not saved:
            await query.edit_message_text("❌ Персонаж не найден.", parse_mode="HTML")
            return
        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            await query.edit_message_text("❌ Сессия не найдена.", parse_mode="HTML")
            return
        await _apply_saved_char_to_session(update, query, user, session,
                                           saved["sheet_text"], saved["char_name"],
                                           saved.get("char_class", ""),
                                           saved.get("parsed_data", ""),
                                           saved_id=saved["id"])
        return


async def _apply_saved_char_to_session(update: Update, query, user, session,
                                        sheet_text: str, char_name: str, char_class: str,
                                        parsed_data: str = "", saved_id: int = 0):
    """Apply a saved character sheet to a session. Uses cached parsed_data if available."""
    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await query.edit_message_text(
            "⚠️ Ты не в этой сессии. Сначала <code>/ymuno</code> в чате сессии.",
            parse_mode="HTML",
        )
        return

    # Try to restore from cached parsed_data (no AI call needed)
    parsed = None
    if parsed_data:
        try:
            data = json.loads(parsed_data)
            _stats = data.get("stats", {})
            parsed = ParsedCharacter(
                name=data.get("name", char_name),
                race=data.get("race", ""),
                class_name=data.get("class_name", char_class),
                level=data.get("level", 1),
                background=data.get("background", ""),
                alignment=data.get("alignment", ""),
                strength=_stats.get("strength", data.get("strength", 10)),
                dexterity=_stats.get("dexterity", data.get("dexterity", 10)),
                constitution=_stats.get("constitution", data.get("constitution", 10)),
                intelligence=_stats.get("intelligence", data.get("intelligence", 10)),
                wisdom=_stats.get("wisdom", data.get("wisdom", 10)),
                charisma=_stats.get("charisma", data.get("charisma", 10)),
                hp=data.get("hp", 0),
                max_hp=data.get("max_hp", 0),
                ac=data.get("ac", 10),
                speed=data.get("speed", 30),
                hit_dice=data.get("hit_dice", 8),
                proficiencies=data.get("proficiencies", []),
                skills=data.get("skills", []),
                languages=data.get("languages", []),
                inventory=data.get("inventory", []),
                spells=data.get("spells", []),
                features=data.get("features", []),
                backstory=data.get("backstory", ""),
            )
            # Restore optional gold fields
            for k in ("gold", "gold_cp", "gold_sp", "gold_ep", "gold_pp"):
                if k in data:
                    setattr(parsed, k, data[k])
            # Update cached parsed_data in saved_chars_db (flat format, same as parser output)
            try:
                import json as _json2
                fresh_json = _json2.dumps({
                    "name": parsed.name, "race": parsed.race, "class_name": parsed.class_name,
                    "level": parsed.level, "background": parsed.background, "alignment": parsed.alignment,
                    "strength": parsed.strength, "dexterity": parsed.dexterity,
                    "constitution": parsed.constitution, "intelligence": parsed.intelligence,
                    "wisdom": parsed.wisdom, "charisma": parsed.charisma,
                    "hp": parsed.hp, "max_hp": parsed.max_hp, "ac": parsed.ac,
                    "speed": parsed.speed, "hit_dice": parsed.hit_dice,
                    "proficiencies": parsed.proficiencies,
                    "skills": parsed.skills, "languages": parsed.languages,
                    "inventory": parsed.inventory, "spells": parsed.spells, "features": parsed.features,
                    "backstory": parsed.backstory,
                    "gold": getattr(parsed, "gold", 0), "gold_cp": getattr(parsed, "gold_cp", 0),
                    "gold_sp": getattr(parsed, "gold_sp", 0), "gold_ep": getattr(parsed, "gold_ep", 0),
                    "gold_pp": getattr(parsed, "gold_pp", 0),
                }, ensure_ascii=False)
                # Write back cached parsed_data so next load is instant
                if saved_id:
                    with saved_chars_db._connect() as _conn:
                        _conn.execute("UPDATE saved_characters SET parsed_data = ? WHERE id = ?",
                                      (fresh_json, saved_id))
            except Exception:
                pass  # non-fatal
        except Exception:
            parsed = None

    # Fallback: parse with AI only if no cached data
    if not parsed:
        parsed = await dm_engine.parse_character_sheet(sheet_text)
        if not parsed:
            await query.edit_message_text(
                "❌ Не удалось распарсить лист персонажа. Загрузи заново.",
                parse_mode="HTML",
            )
            return

    # === SRD ANTI-CHEAT VALIDATION (race/class/background) ===
    try:
        srd_result = dm_engine.validate_character_srd(parsed)
        if srd_result and not srd_result.is_valid:
            warnings_text = []
            for r in [srd_result.race_result, srd_result.class_result,
                      srd_result.background_result, srd_result.backstory_result]:
                if r and not r.is_valid:
                    warnings_text.append(f"⚠️ {r.message}")
            if warnings_text:
                await query.edit_message_text(
                    "⚠️ SRD проверки:\n\n" +
                    "\n".join(warnings_text[:5]) +
                    "\n\n📝 Персонаж добавлен, но DM может проверить.",
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.warning(f"SRD validation on saved-char apply failed (non-blocking): {e}")

    # Save to session
    db.save_character_sheet(session.id, user.id, sheet_text, "saved_upload")

    char_id = str(uuid.uuid4())[:8]
    existing_char = db.get_character_by_player(user.id, session.id)
    if existing_char:
        char_id = existing_char.id
    else:
        char_id = str(uuid.uuid4())[:8]

    db.save_character(Character(
        id=char_id, session_id=session.id, player_id=user.id,
        name=parsed.name, race=parsed.race, class_name=parsed.class_name,
        level=parsed.level, hp=parsed.hp, max_hp=parsed.max_hp, ac=parsed.ac,
        stats=parsed.get_stats_json(),
        proficiencies=json.dumps(parsed.proficiencies),
        inventory=json.dumps(parsed.inventory),
        spells=json.dumps(parsed.spells),
        features=json.dumps(parsed.features),
        backstory=parsed.backstory,
        languages=json.dumps(parsed.languages),
    ))

    currency_kwargs = {
        "gp": getattr(parsed, "gold", 0) or 0,
        "sp": getattr(parsed, "gold_sp", 0) or 0,
        "cp": getattr(parsed, "gold_cp", 0) or 0,
        "ep": getattr(parsed, "gold_ep", 0) or 0,
        "pp": getattr(parsed, "gold_pp", 0) or 0,
    }
    if any(currency_kwargs.values()):
        sessions.add_gold(session.id, char_id, parsed.name, reason="Стартовый капитал", **currency_kwargs)
    for item in parsed.inventory:
        if item and item.strip():
            sessions.add_item(session.id, char_id, parsed.name, item.strip(), 1)

    sheet_html = md_to_html(CharacterParser.format_character_sheet(parsed))
    await query.edit_message_text(
        f"✅ <b>{parsed.name}</b> применён в сессии!\n\n"
        f"{sheet_html}\n\n"
        f"🎮 Теперь можно играть: <code>/dn твоё действие</code>",
        parse_mode="HTML",
    )


async def sheet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if not session:
        return
    db = db_manager.get_db(session.id)
    char = db.get_character_by_player(update.effective_user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/cymeriad`")
        return
    stats = json.loads(char.stats) if char.stats else {}
    parsed = ParsedCharacter(
        name=char.name, race=char.race, class_name=char.class_name, level=char.level,
        hp=char.hp, max_hp=char.max_hp, ac=char.ac,
        strength=stats.get("strength", 10), dexterity=stats.get("dexterity", 10),
        constitution=stats.get("constitution", 10), intelligence=stats.get("intelligence", 10),
        wisdom=stats.get("wisdom", 10), charisma=stats.get("charisma", 10),
        proficiencies=json.loads(char.proficiencies) if char.proficiencies else [],
        inventory=json.loads(char.inventory) if char.inventory else [],
        spells=json.loads(char.spells) if char.spells else [],
        features=json.loads(char.features) if char.features else [],
    )
    status = "💀 **МЁРТВ**" if not char.is_alive else ""
    await send_safe(update, 
        f"{CharacterParser.format_character_sheet(parsed)}\n\n{status}"
    )


async def ability_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """H2 fix: used to ALWAYS dump every character in the party (abbreviated, no
    inventory/saves/spells). Now: no args -> full card for the CALLER's own character
    only. `/gallu all` -> the old abbreviated party overview."""
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    # Admin/readonly enforcement
    user = update.effective_user
    if ADMIN_IS_GAME_MASTER and not sessions.is_creator(user.id, session.id):
        await update.message.reply_text("🔒 Только админ может использовать эту команду.")
        return
    if not ADMIN_IS_GAME_MASTER:
        cmd_args = update.message.text.split()
        if len(cmd_args) > 1:
            await update.message.reply_text("⚠️ Эта команда только для просмотра. Нейросеть решает все изменения через нарратив (Дн.).")
            return

    db = db_manager.get_db(session.id)

    show_all = bool(ctx.args) and ctx.args[0].lower() in ("all", "все", "party", "партия")

    if not show_all:
        user = update.effective_user
        player = db.get_player(user.id, session.id)
        if not player:
            await send_safe(update, "Сначала `/ymuno`")
            return
        char = db.get_character_by_player(user.id, session.id)
        if not char:
            await send_safe(update, "Нет персонажа. `/cymeriad`\n\n(Обзор всей партии: `/gallu all`)")
            return

        stats = json.loads(char.stats) if char.stats else {}
        conds = sessions.get_character_conditions(session.id, char.id)
        cond_str = ", ".join(cc.condition for cc in conds) if conds else "нет"
        profs = json.loads(char.proficiencies) if char.proficiencies else []
        feats = json.loads(char.features) if char.features else []
        spells = json.loads(char.spells) if char.spells else []
        inventory = sessions.get_inventory(session.id, char.id)

        mods = {}
        for stat in ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]:
            mods[stat] = (stats.get(stat, 10) - 10) // 2

        prof_bonus = 2 + (max(char.level, 1) - 1) // 4
        # Best-effort: DB stores proficiencies as free text, not structured save/skill
        # flags, so we can only detect a proficient save/skill by keyword match.
        profs_lower = " ".join(profs).lower()

        def save_mod(stat_key: str, ru_names: list) -> int:
            base = mods[stat_key]
            if any(n.lower() in profs_lower for n in ru_names):
                return base + prof_bonus
            return base

        saves = {
            "СИЛ": save_mod("strength", ["сил", "strength"]),
            "ЛОВ": save_mod("dexterity", ["лов", "dexterity"]),
            "ТЕЛ": save_mod("constitution", ["тел", "constitution"]),
            "ИНТ": save_mod("intelligence", ["инт", "intelligence"]),
            "МУД": save_mod("wisdom", ["муд", "wisdom"]),
            "ХАР": save_mod("charisma", ["хар", "charisma"]),
        }
        passive_perception = 10 + mods["wisdom"] + (prof_bonus if any(w in profs_lower for w in ("восприятие", "perception")) else 0)

        status = "💀 **МЁРТВ**" if not char.is_alive else ""
        lines = [
            f"📊 **{char.name}** — {char.race} {char.class_name}, {char.level} ур. {status}",
            f"❤️ {char.hp}/{char.max_hp} HP | 🛡️ AC {char.ac} | 🎯 Бонус мастерства +{prof_bonus} | 👁️ Пассивное Восприятие {passive_perception}",
            f"🌀 Состояния: {cond_str}",
            "",
            f"💪 СИЛ {stats.get('strength',10)} ({mods['strength']:+d}, спасбросок {saves['СИЛ']:+d})",
            f"🏃 ЛОВ {stats.get('dexterity',10)} ({mods['dexterity']:+d}, спасбросок {saves['ЛОВ']:+d})",
            f"🫀 ТЕЛ {stats.get('constitution',10)} ({mods['constitution']:+d}, спасбросок {saves['ТЕЛ']:+d})",
            f"🧠 ИНТ {stats.get('intelligence',10)} ({mods['intelligence']:+d}, спасбросок {saves['ИНТ']:+d})",
            f"👁️ МУД {stats.get('wisdom',10)} ({mods['wisdom']:+d}, спасбросок {saves['МУД']:+d})",
            f"🎭 ХАР {stats.get('charisma',10)} ({mods['charisma']:+d}, спасбросок {saves['ХАР']:+d})",
        ]
        if profs:
            lines += ["", f"🔧 Владения: {', '.join(profs)}"]
        if feats:
            lines += ["", f"✨ Черты/умения: {', '.join(feats)}"]
        if spells:
            lines += ["", f"📖 Заклинания: {', '.join(spells)}"]
        if inventory:
            inv_str = ", ".join(f"{i['item']}" + (f" x{i['qty']}" if i['qty'] > 1 else "") for i in inventory)
            lines += ["", f"🎒 Инвентарь: {inv_str}"]
        lines += ["", "_Обзор всей партии: `/gallu all`_"]

        await send_safe(update, "\n".join(lines))
        return

    # /ability all — abbreviated party overview (original behavior)
    chars = db.get_session_characters(session.id)
    if not chars:
        await send_safe(update, "Нет персонажей.")
        return

    lines = ["📊 **Состояние партии:**", ""]
    for c in chars:
        stats = json.loads(c.stats) if c.stats else {}
        conds = sessions.get_character_conditions(session.id, c.id)
        cond_str = ", ".join(cc.condition for cc in conds) if conds else "-"
        profs = json.loads(c.proficiencies) if c.proficiencies else []
        feats = json.loads(c.features) if c.features else []

        # Calculate modifiers
        mods = {}
        for stat in ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]:
            val = stats.get(stat, 10)
            mods[stat[:3].upper()] = (val - 10) // 2

        lines.append(f"**{c.name}** - {c.race} {c.class_name} {c.level} lvl")
        lines.append(f"  ❤️ {c.hp}/{c.max_hp} HP | 🛡️ AC {c.ac} | 🌀 {cond_str}")
        lines.append(f"  💪 СИЛ {stats.get('strength',10)} ({mods['STR']:+d}) | 🏃 ЛОВ {stats.get('dexterity',10)} ({mods['DEX']:+d}) | 🫀 ТЕЛ {stats.get('constitution',10)} ({mods['CON']:+d})")
        lines.append(f"  🧠 ИНТ {stats.get('intelligence',10)} ({mods['INT']:+d}) | 👁️ МУД {stats.get('wisdom',10)} ({mods['WIS']:+d}) | 🎭 ХАР {stats.get('charisma',10)} ({mods['CHA']:+d})")
        if profs:
            lines.append(f"  🔧 Навыки: {', '.join(profs[:8])}")
        if feats:
            lines.append(f"  ✨ Умения: {', '.join(feats[:5])}")
        lines.append("")

    await send_safe(update, "\n".join(lines))


async def smith_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кузнец — crafting and equipment services. 
    Uses Master AI to narrate the smith interaction and DB-Bot to track items.
    TEMPORARY: Welsh name pending from user. Currently /sp."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "⚠️ Нет активной сессии.")
        return

    db = db_manager.get_db(session.id)
    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "⚠️ Нет персонажа. Сначала `/cymeriad`.")
        return

    action_text = " ".join(ctx.args) if ctx.args else ""
    if not action_text:
        # Show smith menu
        await send_safe(update,
            "⚒️ <b>Кузнец</b>\n\n"
            "Что хочешь сделать?\n"
            "• <code>/sp починить</code> — починить снаряжение\n"
            "• <code>/sp купить [предмет]</code> — купить предмет\n"
            "• <code>/sp продать [предмет]</code> — продать предмет\n"
            "• <code>/sp улучшить [предмет]</code> — заточить/улучшить\n"
            "• <code>/sp создать [предмет]</code> — выковать (крафт)\n\n"
            "Или опиши, что хочешь: <code>/sp Мне нужна кольчуга</code>",
            raw_html=True,
        )
        return

    # Use Дн. action through the game system so Master narrates the interaction
    smith_action = f"Дн. Идёт к кузнецу: {action_text}"
    await _process_dn_action(update, ctx, smith_action)


async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Delete all session data (GDPR-style right to be forgotten)"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель может удалить сессию.")
        return

    export_path = os.path.join(BASE_DIR, "logs", f"session_{session.id}_{session.name.replace(' ', '_')}.txt")
    if os.path.exists(export_path):
        os.remove(export_path)

    db_manager.clear_history(session.id)
    sessions.end_session(session.id)

    await send_safe(update, "🗑️ **Все данные сессии удалены.** Логи, история, персонажи — стёрты.")
    if ADMIN_CHAT_ID and ctx:
        await send_to_admin(ctx, f"🗑️ Session deleted by user: {session.name} ({session.id})")


async def dndstart_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Start the game with world generation (DM only)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Админ может начать игру.")
        return

    db = db_manager.get_db(session.id)
    chars = db.get_session_characters(session.id)
    if not chars:
        await send_safe(update, "❌ Нет загруженных персонажей. Игроки должны загрузить листы через `/cymeriad`.")
        return

    # ── Currency: AI creates it during world generation, /setcurrency is optional override ──

    # ── Genre / setting selection ──
    args_text = " ".join(ctx.args) if ctx.args else ""
    selected_genre = _genre_selections.pop(chat_id, None)

    # If no args and no selection → show genre picker
    if not args_text and not selected_genre:
        categories = _get_all_categories()
        if not categories:
            # Fallback: no settings at all, use default theme
            args_text = SETTINGS_DEFAULT_GENRE
        else:
            text = "📖 <b>Выбери сеттинг для генерации мира</b>\n\n"
            buttons = []
            for cat_key, cat_label in categories:
                settings = _get_settings_in_category(cat_key)
                for s in settings:
                    buttons.append([InlineKeyboardButton(
                        f"📖 {s.get('name', s['id'])} ({cat_label})",
                        callback_data=f"start:{s['id']}",
                    )])
            buttons.append([InlineKeyboardButton("✍️ Своё описание", callback_data="start:freeform")])
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update, stop_typing)) if update.effective_chat else None

    try:
        # Determine theme: setting_id, selected_genre, or freeform text
        setting_content = None
        if not args_text and selected_genre:
            # Genre was pre-selected via /categori
            setting_info = _get_setting_info(selected_genre)
            if setting_info:
                setting_content = _load_setting_md(selected_genre)
                args_text = setting_info.get("name", selected_genre)
                # Store genre in session
                session.genre = selected_genre
                db.update_session(session)
            else:
                args_text = selected_genre
        elif args_text:
            # Check if args match a setting_id
            setting_info = _get_setting_info(args_text.lower())
            if setting_info:
                setting_content = _load_setting_md(args_text.lower())
                args_text = setting_info.get("name", args_text)
                session.genre = args_text.lower()
                db.update_session(session)

        theme = args_text or SETTINGS_DEFAULT_GENRE

        # Build optional currency hint for the world generation prompt
        if session.currency_name:
            currency_info = (
                f"\n\n[ВАЛЮТА: {session.currency_name} ({session.currency_symbol})"
            )
            if session.currency_sub_name:
                currency_info += (
                    f", {session.currency_sub_name} ({session.currency_sub_symbol})"
                    f" = {session.currency_sub_value}{session.currency_symbol})"
                )
            if session.currency_super_name:
                currency_info += (
                    f", {session.currency_super_name} ({session.currency_super_symbol})"
                    f" = {session.currency_super_value}{session.currency_symbol})"
                )
            currency_info += "]"
        else:
            currency_info = ""

        # Inject setting content into world generation prompt if available
        if setting_content:
            # Truncate to avoid token overflow — keep structure and lore
            setting_brief = setting_content[:3000]
            theme = f"{theme}\n\n[СЕТТИНГ: {setting_brief}]{currency_info}"
        else:
            theme = f"{theme}{currency_info}"

        # Generate world (F2/F3: sessions.generate_world already weaves in character
        # backstories and auto-extracts personal quest hooks into /quest)
        result = await sessions.generate_world(session.id, theme)
        world_html = result.get("html", result.get("text", ""))

        # Save world to history
        db.add_history(HistoryEntry(
            session_id=session.id,
            author="SYSTEM",
            content=f"[DNDSTART] World generated. {result.get('text', '')[:500]}",
            entry_type="system",
        ))

        # Build opening narrative via Master AI
        char_names = [c.name for c in chars]

        opening_prompt = f"""Ты - ИИ-Мастер (Dungeon Master) для D&D 5e. Игра только начинается.

Персонажи: {', '.join(char_names)}
Мир: {result.get('text', '')[:1500]}

Напиши ВСТУПИТЕЛЬНЫЙ нарратив (300-500 слов): где персонажи находятся, что они видят, слышат, чувствуют. Опиши атмосферу. НЕ задавай вопросов игрокам. НЕ предлагай варианты действий. Просто погрузи их в мир.

Отвечай на русском. Используй формат бросков если нужны скрытые проверки (visible=false)."""

        opening_msgs = [{"role": "user", "content": opening_prompt}]
        opening_resp = await dm_engine.master.chat(opening_msgs, system_prompt=MASTER_PROMPT, tools=MASTER_TOOLS)

        # Handle tool calls for opening narrative dice rolls
        iteration = 0
        MAX_ITER = 10
        visible_rolls = []
        hidden_rolls = []
        while iteration < MAX_ITER:
            iteration += 1
            choice = opening_resp["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")
            if tool_calls:
                tool_results = []
                for tc in tool_calls:
                    if tc["function"]["name"] == "roll_dice":
                        args = json.loads(tc["function"]["arguments"])
                        roll_result = dm_engine._execute_roll(args)
                        tool_results.append({"call_id": tc["id"], "result": roll_result["result"]})
                        if roll_result.get("visible"):
                            visible_rolls.append(roll_result["display"])
                        else:
                            hidden_rolls.append(roll_result["display"])
                    else:
                        tool_results.append({"call_id": tc["id"], "result": "Error: Master should not call this tool"})

                opening_msgs.append({"role": "assistant", "content": choice.get("content") or "", "tool_calls": tool_calls})
                for tr in tool_results:
                    opening_msgs.append({"role": "tool", "tool_call_id": tr["call_id"], "content": tr["result"]})

                opening_resp = await dm_engine.master.chat(opening_msgs, system_prompt=MASTER_PROMPT, tools=MASTER_TOOLS)
                continue
            break

        opening_text = opening_resp["choices"][0]["message"].get("content", "")
        if visible_rolls:
            dice_block = "🎲 **Броски:**\n" + "\n".join(visible_rolls) + "\n\n"
            opening_text = dice_block + opening_text

        # Render to HTML ONCE (previously this got converted a second time right below,
        # which re-escaped the <b> tags this call had just produced into literal visible
        # "<b>...</b>" text in the sent message).
        opening_html = md_to_html(opening_text)

        # Send world as separate expandable message — use _send_long_blockquote so
        # long world text (>4096 chars) gets split into multiple <blockquote> chunks
        # instead of crashing with "Message is too long" (genworld callback already
        # uses this same helper; this brings /dndcychwyn to parity).
        if world_html.strip():
            world_converted = md_to_html(world_html)
            if world_converted.strip():
                bot_inst = ctx.bot if ctx else None
                if bot_inst is not None and update.effective_chat:
                    await _send_long_blockquote(bot_inst, update.effective_chat.id, world_converted)

        # Send opening narrative as separate expandable message — same splitter.
        if opening_html.strip():
            bot_inst = ctx.bot if ctx else None
            if bot_inst is not None and update.effective_chat:
                await _send_long_blockquote(bot_inst, update.effective_chat.id, opening_html)

        # Start first round collection
        sessions.start_action_collection(session.id)
        pending = sessions.get_pending_players(session.id)
        # Tag players — sent as its OWN raw-HTML message below, never embedded in the
        # plain-markdown message above (that message goes through md_to_html, which
        # would escape the <a> tag into visible literal text — it was previously in
        # BOTH places at once, showing up once as garbage text and once as a working link).
        players = sessions.get_players(session.id)
        tags_html = " ".join([
            f'<a href="tg://user?id={p.user_id}">{p.display_name}</a>'
            for p in players if p.user_id
        ])
        await send_safe(update, 
            f"⚔️ **Игра началась!**\n\n"
            f"📝 Пишите `Дн. ваше действие` — нейросеть разрешит, когда все сходят.\n"
            f"⏳ Ждём: {', '.join(pending)}",
        )
        if tags_html:
            await send_safe(update, tags_html, parse_html=True, raw_html=True, source="system")

    except Exception as e:
        logger.error(f"dndstart error: {e}")
        await send_safe(update, f"❌ Ошибка старта: {e}")
    finally:
        if typing_task:
            stop_typing.set()
            typing_task.cancel()
