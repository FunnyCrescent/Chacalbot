"""
libs.handlers.settings_cmds — auto-split from libs/bot_handlers.py.

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
    SETTINGS_DIR, SETTINGS_DEFAULT_GENRE, SETTINGS_MIN_COMPLEXITY,
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
from libs.handlers.utils import _send_long_blockquote
from libs.handlers.utils import get_session
from libs.handlers.utils import md_to_html
from libs.handlers.utils import send_safe


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    """Load settings registry from JSON file. Returns empty dict if missing."""
    registry_path = os.path.join(SETTINGS_DIR, "registry.json")
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"categories": {}}


def _save_registry(registry: dict):
    """Save settings registry to JSON file."""
    registry_path = os.path.join(SETTINGS_DIR, "registry.json")
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _load_setting_md(setting_id: str) -> Optional[str]:
    """Load the full .md content of a setting file. Returns None if not found."""
    registry = _load_registry()
    for cat_key, cat_data in registry.get("categories", {}).items():
        for s_id, s_data in cat_data.get("settings", {}).items():
            if s_id == setting_id:
                file_name = s_data.get("file", "")
                if file_name:
                    file_path = os.path.join(SETTINGS_DIR, file_name)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            return f.read()
                    except FileNotFoundError:
                        logger.warning(f"Setting file not found: {file_path}")
                        return None
    # Also check custom/ directory
    custom_path = os.path.join(SETTINGS_DIR, "custom", f"{setting_id}.md")
    try:
        with open(custom_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _get_setting_info(setting_id: str) -> Optional[dict]:
    """Get setting metadata from registry. Returns dict or None."""
    registry = _load_registry()
    for cat_key, cat_data in registry.get("categories", {}).items():
        for s_id, s_data in cat_data.get("settings", {}).items():
            if s_id == setting_id:
                return {
                    "id": s_id,
                    "category": cat_key,
                    "category_label": cat_data.get("label", cat_key),
                    **s_data,
                }
    return None


def _get_all_categories() -> list:
    """Get list of (category_key, category_label) tuples."""
    registry = _load_registry()
    return [(k, v.get("label", k)) for k, v in registry.get("categories", {}).items()]


def _get_settings_in_category(category_key: str) -> list:
    """Get list of setting dicts in a category."""
    registry = _load_registry()
    cat = registry.get("categories", {}).get(category_key, {})
    result = []
    for s_id, s_data in cat.get("settings", {}).items():
        result.append({"id": s_id, **s_data})
    return result


def _search_settings(query: str) -> list:
    """Search settings by name, category, or tags."""
    registry = _load_registry()
    query_lower = query.lower()
    results = []
    for cat_key, cat_data in registry.get("categories", {}).items():
        for s_id, s_data in cat_data.get("settings", {}).items():
            name = s_data.get("name", "").lower()
            tags = " ".join(s_data.get("tags", [])).lower() if isinstance(s_data.get("tags"), list) else str(s_data.get("tags", "")).lower()
            if query_lower in name or query_lower in cat_key or query_lower in tags:
                results.append({
                    "id": s_id,
                    "category": cat_key,
                    "category_label": cat_data.get("label", cat_key),
                    **s_data,
                })
    return results


def _parse_custom_setting_md(content: str) -> Optional[dict]:
    """Parse a custom .md setting file and extract metadata. Returns dict or None if invalid."""
    lines = content.strip().split("\n")
    if not lines:
        return None

    metadata = {
        "name": "",
        "id": "",
        "category": "custom",
        "tags": [],
        "complexity": 0.0,
        "description": "",
        "lore": "",
    }

    # Parse front matter (lines starting with **key:**)
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("#"):
            # Skip the title line
            if not metadata["name"]:
                metadata["name"] = line_stripped.lstrip("#").strip()
            continue
        if "**ID:**" in line_stripped:
            metadata["id"] = line_stripped.split("**ID:**")[-1].strip()
        elif "**Категория:**" in line_stripped:
            metadata["category"] = line_stripped.split("**Категория:**")[-1].strip()
        elif "**Теги:**" in line_stripped:
            tags_str = line_stripped.split("**Теги:**")[-1].strip().strip("[]")
            metadata["tags"] = [t.strip() for t in tags_str.split(",") if t.strip()]
        elif "**Сложность:**" in line_stripped:
            try:
                metadata["complexity"] = float(line_stripped.split("**Сложность:**")[-1].strip())
            except ValueError:
                pass

    # Auto-generate ID from name if not provided
    if not metadata["id"] and metadata["name"]:
        metadata["id"] = metadata["name"].lower().replace(" ", "_").replace("-", "_")[:30]

    if not metadata["name"]:
        return None

    # Calculate complexity if not set
    if metadata["complexity"] <= 0:
        word_count = len(content.split())
        section_count = content.count("## ")
        metadata["complexity"] = min(1.0, round((word_count / 2000 * 0.5 + section_count / 10 * 0.5), 2))

    # Extract description section
    in_section = None
    section_lines = []
    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("## "):
            if in_section and section_lines:
                metadata[in_section] = "\n".join(section_lines).strip()
            in_section = line_stripped.lstrip("#").strip().lower()
            if in_section == "описание":
                in_section = "description"
            elif in_section == "лор":
                in_section = "lore"
            section_lines = []
        elif in_section:
            section_lines.append(line)

    if in_section and section_lines:
        metadata[in_section] = "\n".join(section_lines).strip()

    return metadata


def _validate_custom_setting(content: str) -> tuple:
    """Validate a custom setting file. Returns (is_valid, score, message)."""
    parsed = _parse_custom_setting_md(content)
    if not parsed:
        return False, 0, "Не удалось распознать формат. Файл должен начинаться с # Название и содержать метаданные (**ID:**, **Категория:** и т.д.)"

    word_count = len(content.split())
    if word_count < 50:
        return False, 0, f"Слишком мало текста ({word_count} слов). Минимум 50 слов."

    if not parsed.get("description"):
        return False, 0, "Отсутствует секция ## Описание. Добавьте описание сеттинга."

    if not parsed.get("lore"):
        return False, 0, "Отсутствует секция ## Лор. Добавьте лор/историю мира."

    complexity = parsed["complexity"]
    if complexity < SETTINGS_MIN_COMPLEXITY:
        return False, complexity, f"Сложность сеттинга {complexity} ниже порога ({SETTINGS_MIN_COMPLEXITY}). Добавьте больше деталей, лор, описания атмосферы."

    return True, complexity, "OK"


async def categori_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Browse game settings/genres. /categori [category|setting_id|search]"""
    chat_id = update.effective_chat.id

    if not ctx.args:
        # Show categories with inline buttons
        categories = _get_all_categories()
        if not categories:
            await send_safe(update, "📖 Нет доступных сеттингов.")
            return

        text = "📖 <b>Сеттинги и жанры</b>\n\nВыбери категорию:"
        buttons = []
        for cat_key, cat_label in categories:
            settings_count = len(_get_settings_in_category(cat_key))
            buttons.append([InlineKeyboardButton(
                f"📂 {cat_label} ({settings_count})",
                callback_data=f"cat:{cat_key}",
            )])
        # Add upload button (DM only check is handled by DM-only commands config)
        buttons.append([InlineKeyboardButton("📤 Загрузить свой сеттинг", callback_data="cat:upload_info")])

        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    arg = " ".join(ctx.args).lower().strip()

    # Check if arg is a setting_id
    info = _get_setting_info(arg)
    if info:
        await _show_setting_detail(update, arg, info)
        return

    # Check if arg is a category
    settings = _get_settings_in_category(arg)
    if settings:
        cat_label = None
        for ck, cl in _get_all_categories():
            if ck == arg:
                cat_label = cl
                break
        text = f"📂 <b>{cat_label or arg}</b>\n\n"
        buttons = []
        for s in settings:
            text += f"• <b>{s.get('name', s['id'])}</b> — {_truncate(s.get('summary', ''), 60)}\n"
            buttons.append([InlineKeyboardButton(
                f"📖 {s.get('name', s['id'])}",
                callback_data=f"set:{s['id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    # Search
    results = _search_settings(arg)
    if results:
        text = f"🔍 <b>Результаты: \"{arg}\"</b>\n\n"
        buttons = []
        for s in results[:8]:
            text += f"• <b>{s.get('name', s['id'])}</b> ({s.get('category_label', '')}) — {_truncate(s.get('summary', ''), 50)}\n"
            buttons.append([InlineKeyboardButton(
                f"📖 {s.get('name', s['id'])}",
                callback_data=f"set:{s['id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    else:
        await send_safe(update, f"🔍 Ничего не найдено по запросу \"{arg}\".\n\n/categori — все категории")


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


async def _show_setting_detail(update: Update, setting_id: str, info: dict):
    """Show full setting details with 'Choose' button."""
    name = info.get("name", setting_id)
    summary = info.get("summary", "Нет описания.")
    complexity = info.get("complexity", 0)
    builtin = "Встроенный" if info.get("builtin", True) else "Пользовательский"

    # Stars for complexity
    stars = int(round(complexity * 5))
    star_str = "⭐" * stars + "☆" * (5 - stars)

    text = (
        f"📖 <b>{name}</b>\n"
        f"📂 Категория: {info.get('category_label', info.get('category', ''))}\n"
        f"📊 Сложность: {star_str} ({complexity})\n"
        f"🏷️ Тип: {builtin}\n\n"
        f"{summary}\n\n"
        f"_Для использования: /dndcychwyn {setting_id}_"
    )

    buttons = [
        [InlineKeyboardButton(f"✅ Выбрать для старта", callback_data=f"sel:{setting_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"cat:{info.get('category', 'root')}")],
    ]

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")


async def _categori_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks from /categori."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    chat_id = query.message.chat_id

    if data == "cat:root":
        # Back to categories
        categories = _get_all_categories()
        text = "📖 <b>Сеттинги и жанры</b>\n\nВыбери категорию:"
        buttons = []
        for cat_key, cat_label in categories:
            settings_count = len(_get_settings_in_category(cat_key))
            buttons.append([InlineKeyboardButton(
                f"📂 {cat_label} ({settings_count})",
                callback_data=f"cat:{cat_key}",
            )])
        buttons.append([InlineKeyboardButton("📤 Загрузить свой сеттинг", callback_data="cat:upload_info")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data == "cat:upload_info":
        text = (
            "📤 <b>Загрузка своего сеттинга</b>\n\n"
            "1. Создай .md файл в формате:\n"
            "<code># Название сеттинга</code>\n"
            "<code>**ID:** my_setting</code>\n"
            "<code>**Категория:** custom</code>\n"
            "<code>**Теги:** tag1, tag2</code>\n"
            "<code>**Сложность:** 0.7</code>\n\n"
            "<code>## Описание</code>\nКраткое описание...\n\n"
            "<code>## Лор</code>\nПодробная история мира...\n\n"
            "<code>## Атмосфера</code>\nОщущение и тон...\n\n"
            "2. Отправь файл боту (в ЛС или группу)\n"
            "3. Ответь на файл: <code>/categori add</code>\n\n"
            "Минимум 50 слов, обязательны секции Описание и Лор.\n"
            f"Порог сложности: {SETTINGS_MIN_COMPLEXITY}"
        )
        buttons = [[InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data.startswith("cat:"):
        # Browse category
        cat_key = data[4:]
        settings = _get_settings_in_category(cat_key)
        cat_label = None
        for ck, cl in _get_all_categories():
            if ck == cat_key:
                cat_label = cl
                break

        if not settings:
            await query.edit_message_text(f"📂 Категория \"{cat_label or cat_key}\" пуста.",
                                          reply_markup=InlineKeyboardMarkup([
                                              [InlineKeyboardButton("◀️ Назад", callback_data="cat:root")]
                                          ]), parse_mode="HTML")
            return

        text = f"📂 <b>{cat_label or cat_key}</b>\n\n"
        buttons = []
        for s in settings:
            text += f"• <b>{s.get('name', s['id'])}</b> — {_truncate(s.get('summary', ''), 60)}\n"
            buttons.append([InlineKeyboardButton(
                f"📖 {s.get('name', s['id'])}",
                callback_data=f"set:{s['id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    elif data.startswith("set:"):
        # Show setting detail
        setting_id = data[4:]
        info = _get_setting_info(setting_id)
        if info:
            name = info.get("name", setting_id)
            summary = info.get("summary", "Нет описания.")
            complexity = info.get("complexity", 0)
            builtin = "Встроенный" if info.get("builtin", True) else "Пользовательский"
            stars = int(round(complexity * 5))
            star_str = "⭐" * stars + "☆" * (5 - stars)

            text = (
                f"📖 <b>{name}</b>\n"
                f"📂 Категория: {info.get('category_label', '')}\n"
                f"📊 Сложность: {star_str} ({complexity})\n"
                f"🏷️ Тип: {builtin}\n\n"
                f"{summary}\n\n"
                f"_Для использования: /dndcychwyn {setting_id}_"
            )
            buttons = [
                [InlineKeyboardButton(f"✅ Выбрать для старта", callback_data=f"sel:{setting_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"cat:{info.get('category', 'root')}")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        else:
            await query.edit_message_text(f"❌ Сеттинг \"{setting_id}\" не найден.",
                                          parse_mode="HTML")

    elif data.startswith("sel:"):
        # Select genre for /dndcychwyn
        setting_id = data[4:]
        info = _get_setting_info(setting_id)
        if info:
            _genre_selections[chat_id] = setting_id
            name = info.get("name", setting_id)
            text = (
                f"✅ <b>Выбран: {name}</b>\n\n"
                f"Сгенерировать мир по этому сеттингу?"
            )
            buttons = [
                [InlineKeyboardButton("🌍 Сгенерировать мир", callback_data=f"genworld:{setting_id}")],
                [InlineKeyboardButton("📖 Смотреть детали", callback_data=f"set:{setting_id}")],
                [InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        else:
            await query.edit_message_text("❌ Сеттинг не найден.", parse_mode="HTML")

    elif data.startswith("start:"):
        # Genre selected from /dndcychwyn picker → trigger start with that setting
        setting_id = data[6:]
        if setting_id == "freeform":
            await query.edit_message_text(
                "✍️ Напиши: <code>/dndcychwyn твоё описание мира</code>\n\n"
                "Пример: /dndcychwyn мрачное морское приключение с пиратами и морскими чудовищами",
                parse_mode="HTML",
            )
        else:
            _genre_selections[chat_id] = setting_id
            info = _get_setting_info(setting_id)
            name = info.get("name", setting_id) if info else setting_id
            buttons = [
                [InlineKeyboardButton("🌍 Сгенерировать мир", callback_data=f"genworld:{setting_id}")],
                [InlineKeyboardButton("◀️ Назад к категориям", callback_data="cat:root")],
            ]
            await query.edit_message_text(
                f"✅ <b>{name}</b> выбран!\n\nСгенерировать мир?",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
            )

    elif data.startswith("genworld:"):
        # User confirmed world generation from /categori selection
        # Directly trigger world generation — no more redirecting to /dndcychwyn
        setting_id = data[9:]
        info = _get_setting_info(setting_id)
        name = info.get("name", setting_id) if info else setting_id
        _genre_selections[chat_id] = setting_id

        try:
            session = get_session(chat_id)
            if not session:
                await query.edit_message_text(
                    "❌ В этом чате нет активной сессии.\nСначала создай: <code>/newydd Название</code>",
                    parse_mode="HTML",
                )
                return

            if not sessions.is_creator(query.from_user.id, session.id):
                await query.edit_message_text(
                    "❌ Только Админ может начать игру.",
                    parse_mode="HTML",
                )
                return

            db = db_manager.get_db(session.id)
            chars = db.get_session_characters(session.id)
            if not chars:
                await query.edit_message_text(
                    "❌ Нет загруженных персонажей. Игроки должны загрузить листы через <code>/cymeriad</code>.",
                    parse_mode="HTML",
                )
                return

            # Currency: AI creates it during world generation

            # ── All checks passed → generate world directly ──
            await query.edit_message_text(
                f"🌍 <b>{name}</b> — генерация мира...\n\n"
                f"⏳ Генерация мира, подожди...",
                parse_mode="HTML",
            )

            # Build the theme with setting content
            setting_content = _load_setting_md(setting_id)
            theme = name
            if setting_content:
                setting_brief = setting_content[:3000]
                theme = f"{theme}\n\n[СЕТТИНГ: {setting_brief}]"
            else:
                theme = f"{theme}"

            # Store genre in session
            session.genre = setting_id
            db.update_session(session)

            # Generate world
            from telegram.ext import CallbackContext
            stop_typing_ev = asyncio.Event()
            result = await sessions.generate_world(session.id, theme)
            world_html = result.get("html", result.get("text", ""))

            db.add_history(HistoryEntry(
                session_id=session.id,
                author="SYSTEM",
                content=f"[DNDSTART] World generated. {result.get('text', '')[:500]}",
                entry_type="system",
            ))

            # Build opening narrative
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

            opening_html = md_to_html(opening_text)

            # Send world as expandable (split long messages by paragraph)
            bot_instance = ctx.bot
            if world_html.strip():
                world_converted = md_to_html(world_html)
                if world_converted.strip():
                    await _send_long_blockquote(bot_instance, chat_id, world_converted)

            # Send opening narrative as expandable (split long messages by paragraph)
            if opening_html.strip():
                await _send_long_blockquote(bot_instance, chat_id, opening_html)

            # Start first round collection
            sessions.start_action_collection(session.id)
            pending = sessions.get_pending_players(session.id)
            players = sessions.get_players(session.id)

            # Send round start message
            bot_instance = ctx.bot
            await bot_instance.send_message(
                chat_id=chat_id,
                text=(
                    f"⚔️ <b>Игра началась!</b>\n\n"
                    f"📝 Пишите <code>Дн. ваше действие</code> — нейросеть разрешит, когда все сходят.\n"
                    f"⏳ Ждём: {', '.join(pending)}"
                ),
                parse_mode="HTML",
            )

            # Tag players
            tags_html = " ".join([
                f'<a href="tg://user?id={p.user_id}">{p.display_name}</a>'
                for p in players if p.user_id
            ])
            if tags_html:
                await bot_instance.send_message(
                    chat_id=chat_id, text=tags_html, parse_mode="HTML",
                )

        except Exception as e:
            logger.error(f"genworld callback error: {e}")
            try:
                await query.edit_message_text(
                    f"❌ Ошибка генерации: {e}\n\nПопробуй вручную: <code>/dndcychwyn {setting_id}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def categori_add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Upload a custom setting: reply to a .md file with /categori add"""
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Check reply to document
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await send_safe(update, "📤 Ответь на .md файл командой `/categori add`")
        return

    doc = update.message.reply_to_message.document
    if not doc.file_name.endswith((".md", ".txt")):
        await send_safe(update, "❌ Файл должен быть .md или .txt")
        return

    # Check admin/DM
    session = get_session(chat_id)
    if session and not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Админ может добавлять сеттинги.")
        return

    try:
        file = await doc.get_file()
        content = file.decode() if hasattr(file, 'decode') else None
        if content is None:
            # Download file content
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
                await file.download_to_drive(tmp.name)
                with open(tmp.name, "r", encoding="utf-8") as f:
                    content = f.read()
                os.unlink(tmp.name)
    except Exception as e:
        await send_safe(update, f"❌ Ошибка чтения файла: {e}")
        return

    if not content or not content.strip():
        await send_safe(update, "❌ Файл пуст.")
        return

    # Validate
    is_valid, score, msg = _validate_custom_setting(content)
    if not is_valid:
        await send_safe(update, f"❌ Сеттинг не прошёл проверку:\n\n{msg}")
        return

    # Parse and save
    parsed = _parse_custom_setting_md(content)
    if not parsed:
        await send_safe(update, "❌ Ошибка парсинга.")
        return

    setting_id = parsed["id"]
    name = parsed["name"]

    # Save .md file to custom/
    os.makedirs(os.path.join(SETTINGS_DIR, "custom"), exist_ok=True)
    custom_path = os.path.join(SETTINGS_DIR, "custom", f"{setting_id}.md")
    with open(custom_path, "w", encoding="utf-8") as f:
        f.write(content)

    # Update registry
    registry = _load_registry()
    if "custom" not in registry.get("categories", {}):
        registry.setdefault("categories", {})["custom"] = {"label": "Пользовательские", "settings": {}}

    registry["categories"]["custom"]["settings"][setting_id] = {
        "name": name,
        "file": f"custom/{setting_id}.md",
        "builtin": False,
        "complexity": parsed["complexity"],
        "summary": parsed.get("description", "")[:200],
        "tags": parsed.get("tags", []),
    }
    _save_registry(registry)

    await send_safe(update,
        f"✅ **Сеттинг \"{name}\" добавлен!**\n\n"
        f"📊 Сложность: {score}\n"
        f"🏷️ Категория: {parsed['category']}\n\n"
        f"Используй: /dndcychwyn {setting_id}\n"
        f"Или: /categori custom — посмотреть"
    )


async def dyfroddi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Scan settings/ directory for new .md files and add them to registry.
    /dyfroddi — scan built-in dir, /dyfroddi custom — scan custom/ dir"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    is_dm_only = update.effective_chat.type == "private"

    # DM-only restriction
    session = get_session(chat_id)
    if session and not sessions.is_creator(user.id, session.id) and not is_dm_only:
        await send_safe(update, "❌ Только Админ может сканировать сеттинги.")
        return

    scan_dir = os.path.join(SETTINGS_DIR, "custom")
    if ctx.args and ctx.args[0] == "builtin":
        scan_dir = SETTINGS_DIR
    elif ctx.args and ctx.args[0]:
        scan_dir = ctx.args[0]

    if not os.path.isdir(scan_dir):
        await send_safe(update, f"❌ Директория не найдена: {scan_dir}")
        return

    await send_safe(update, f"🔍 Сканирую {scan_dir}...")

    registry = _load_registry()
    # Collect all known setting IDs
    known_ids = set()
    for cat_data in registry.get("categories", {}).values():
        for sid in cat_data.get("settings", {}).keys():
            known_ids.add(sid)

    # Find .md files
    new_found = []
    for fname in sorted(os.listdir(scan_dir)):
        if not fname.endswith(".md"):
            continue
        setting_id = fname[:-3]  # remove .md
        if setting_id in known_ids:
            continue

        fpath = os.path.join(scan_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        if not content.strip():
            continue

        # Validate
        is_valid, score, msg = _validate_custom_setting(content)
        parsed = _parse_custom_setting_md(content)

        if not parsed:
            continue

        if not is_valid:
            new_found.append(f"⚠️ {setting_id}: {msg[:80]}")
            continue

        # Add to registry
        is_custom = scan_dir != SETTINGS_DIR
        cat_key = parsed.get("category", "custom")
        name = parsed["name"]

        if cat_key not in registry.get("categories", {}):
            registry.setdefault("categories", {})[cat_key] = {"label": cat_key.title(), "settings": {}}

        registry["categories"][cat_key]["settings"][setting_id] = {
            "name": name,
            "file": f"custom/{setting_id}.md" if is_custom else f"{setting_id}.md",
            "builtin": not is_custom,
            "complexity": parsed["complexity"],
            "summary": parsed.get("description", "")[:200],
            "tags": parsed.get("tags", []),
        }

        new_found.append(f"✅ <b>{name}</b> (score: {score:.2f})")

    if not new_found:
        await send_safe(update, "📭 Новых .md файлов не найдено. Все сеттинги уже в реестре.")
        return

    _save_registry(registry)

    result_text = "📋 <b>Результаты сканирования:</b>\n\n" + "\n".join(new_found)
    await send_safe(update, result_text, raw_html=True)
