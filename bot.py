#!/usr/bin/env python3
"""
D&D Dark Fantasy DM Bot — Telegram Bot (RUSSIAN)
Queue-based: one Дн. per player per round, wait for ALL, then batch to AI.
Dice rolls ONLY via tool calling (no auto-parse).
/ask is OOC only — does not advance game.

SECURITY: Only the DM (session creator) can modify the database directly.
Players change the world ONLY through RP actions (Дн. ...).
Kimi (master AI) applies DB changes via tool calls.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_client import DMEngine, MASTER_PROMPT, MASTER_TOOLS
from character_parser import CharacterParser, ParsedCharacter
from config import TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, BASE_DIR, DB_PATH, LOG_PATH, CHARACTERS_DIR, ADMIN_CHAT_ID
from db import Database, DatabaseManager, HistoryEntry, Session, Character
from session_manager import SessionManager


class MarkdownLogger:
    """Logs all bot activity to a Markdown file per session"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _get_path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"session_{session_id}.md")

    def log(self, session_id: str, source: str, text: str):
        """Log an entry. source: 'player', 'master', 'db_bot', 'renderer', 'system', 'error'"""
        if not session_id:
            return
        path = self._get_path(session_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Format based on source
        if source == "player":
            prefix = f"**[{timestamp}] Игрок:**"
        elif source == "master":
            prefix = f"**[{timestamp}] Мастер (Kimi):**"
        elif source == "db_bot":
            prefix = f"**[{timestamp}] DB-Bot (Llama 4):**"
        elif source == "renderer":
            prefix = f"**[{timestamp}] Рендерер (Llama 3.3):**"
        elif source == "system":
            prefix = f"**[{timestamp}] Система:**"
        elif source == "error":
            prefix = f"**[{timestamp}] ❌ Ошибка:**"
        else:
            prefix = f"**[{timestamp}] {source}:**"

        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{prefix}\n\n{text}\n\n---\n")

    def log_raw(self, session_id: str, label: str, content: str):
        """Log raw data (prompts, responses, tool calls)"""
        if not session_id:
            return
        path = self._get_path(session_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n**[{timestamp}] {label}:**\n\n```\n{content}\n```\n\n---\n")


md_logger = MarkdownLogger(os.path.join(BASE_DIR, "logs", "markdown"))

# ═══════════════════════════════════════════════════════════════
# Auto-create dirs + Logging
# ═══════════════════════════════════════════════════════════════

for d in [os.path.join(BASE_DIR, "logs"),
          os.path.join(BASE_DIR, "data"),
          os.path.join(BASE_DIR, "characters")]:
    os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Safe Markdown sender
# ═══════════════════════════════════════════════════════════════

import re as _re
import html as _html

def md_to_html(text: str) -> str:
    """Convert Markdown to Telegram HTML. Only uses Bot API supported tags."""
    if not text:
        return ""

    # Replace horizontal rules: --- or *** or ___ -> Unicode separator
    text = _re.sub(r'(?m)^[-*_]{3,}\s*$', '──────────────────', text)

    # Headers → styled dividers (process BEFORE bold/italic)
    text = _re.sub(r'(?m)^###\s+(.+)$', lambda m: f'<b>───── ✦ {m.group(1).strip()} ─────</b>', text)
    text = _re.sub(r'(?m)^##\s+(.+)$', lambda m: f'<b>───── 🏛️ {m.group(1).strip()} ─────</b>', text)
    text = _re.sub(r'(?m)^#\s+(.+)$', lambda m: f'<b>───── 📍 {m.group(1).strip()} ─────</b>', text)

    # Convert markdown to HTML
    # Bold: **text** or __text__
    text = _re.sub(r'\*\*(.+?)\*\*', lambda m: f'<b>{m.group(1)}</b>', text)
    text = _re.sub(r'__(.+?)__', lambda m: f'<b>{m.group(1)}</b>', text)
    # Italic: *text* or _text_
    text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: f'<i>{m.group(1)}</i>', text)
    text = _re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', lambda m: f'<i>{m.group(1)}</i>', text)
    # Code: `text`
    text = _re.sub(r'`(.+?)`', lambda m: f'<code>{m.group(1)}</code>', text)
    # Strikethrough: ~~text~~
    text = _re.sub(r'~~(.+?)~~', lambda m: f'<s>{m.group(1)}</s>', text)
    # Spoiler: ||text||
    text = _re.sub(r'\|\|(.+?)\|\|', lambda m: f'<span class="tg-spoiler">{m.group(1)}</span>', text)
    # Blockquote: > text at start of line
    text = _re.sub(r'(?m)^>(.+)$', lambda m: f'<blockquote>{m.group(1)}</blockquote>', text)
    # Links: [text](url)
    text = _re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)

    # Escape remaining bare <, >, & that are not part of HTML tags
    # Split by HTML tags, escape non-tag parts
    parts = _re.split(r'(<[^>]+>)', text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # HTML tag (odd indices after split)
            result.append(part)
        else:
            # Protect existing entities, then escape bare chars
            part = part.replace('&amp;', 'AMP')
            part = part.replace('&lt;', 'LT')
            part = part.replace('&gt;', 'GT')
            part = part.replace('&quot;', 'QUOT')
            part = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            part = part.replace('AMP', '&amp;')
            part = part.replace('LT', '&lt;')
            part = part.replace('GT', '&gt;')
            part = part.replace('QUOT', '&quot;')
            result.append(part)

    return ''.join(result)


async def send_safe(update_obj, text: str, parse_html: bool = True, raw_html: bool = False, source: str = "system"):
    """Send text to Telegram — converts Markdown to HTML, logs to Markdown journal.
    Chunks long messages automatically. Falls back to plain text on HTML errors."""
    if not update_obj or not update_obj.effective_chat:
        logger.warning("send_safe: no effective_chat, skipping")
        return

    chat_id = update_obj.effective_chat.id
    session = get_session(chat_id)
    session_id = session.id if session else None

    if session_id:
        md_logger.log(session_id, source, text)

    if raw_html:
        html_text = text
    elif parse_html:
        html_text = md_to_html(text)
    else:
        html_text = text

    # Telegram limit: 4096 UTF-16 code units. Use 4000 for safety.
    MAX_LEN = 4000
    is_blockquote = html_text.strip().startswith("<blockquote expandable>")
    chunks = []
    current = html_text
    while current:
        if len(current) <= MAX_LEN:
            chunks.append(current)
            break
        split_at = current.rfind('\n', 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(current[:split_at])
        current = current[split_at:].lstrip()

    for i, chunk in enumerate(chunks):
        if is_blockquote and len(chunks) > 1:
            # Re-wrap each chunk in its own expandable blockquote
            chunk = chunk.strip()
            if chunk.startswith("<blockquote expandable>"):
                chunk = chunk[len("<blockquote expandable>"):].strip()
            if chunk.endswith("</blockquote>"):
                chunk = chunk[:-len("</blockquote>")].strip()
            chunk = "<blockquote expandable>\n" + chunk + "\n</blockquote>"
        try:
            if update_obj.message:
                await update_obj.effective_chat.send_message(chunk, parse_mode="HTML")
            else:
                await update_obj.effective_chat.send_message(chunk, parse_mode="HTML")
        except Exception as e:
            logger.error(f"HTML send failed: {e}. Chunk preview: {chunk[:200]}")
            try:
                plain = chunk.replace('<b>', '**').replace('</b>', '**').replace('<i>', '*').replace('</i>', '*')
                if update_obj.message:
                    await update_obj.effective_chat.send_message(plain)
                else:
                    await update_obj.effective_chat.send_message(plain)
            except Exception as e2:
                logger.error(f"Plain send failed: {e2}")
                if session_id:
                    md_logger.log(session_id, "error", f"HTML: {e} | Plain: {e2}")



# ═══════════════════════════════════════════════════════════════
# Admin telemetry sender
# ═══════════════════════════════════════════════════════════════

async def send_to_admin(ctx: ContextTypes.DEFAULT_TYPE, text: str = "", document_path: str = ""):
    """Send logs/alerts to admin channel. Silent if ADMIN_CHAT_ID not set."""
    if not ADMIN_CHAT_ID:
        return
    try:
        html_text = md_to_html(text) if text else ""
        if document_path and os.path.exists(document_path):
            with open(document_path, 'rb') as f:
                await ctx.bot.send_document(
                    chat_id=int(ADMIN_CHAT_ID),
                    document=f,
                    caption=html_text[:1000] if html_text else None,
                    parse_mode="HTML"
                )
        elif html_text:
            await ctx.bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=html_text[:4000],
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Admin send failed: {e}")

# ═══════════════════════════════════════════════════════════════
# Global Instances
# ═══════════════════════════════════════════════════════════════

db_manager = DatabaseManager(os.path.join(BASE_DIR, "data", "sessions"))
dm_engine = DMEngine(db_manager=db_manager)
sessions = SessionManager(db_manager, dm_engine)


def get_session(chat_id: int) -> Session | None:
    return db_manager.get_session_by_chat(chat_id)


def fmt_players(players) -> str:
    return "\n".join(
        f"{i}. {'👑' if p.is_creator else '🎮'} {p.display_name} (@{p.username})"
        for i, p in enumerate(players, 1)
    )


# ═══════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_safe(update, 
        "⚔️ **D&D Dark Fantasy Bot** ⚔️\n\n"
        "Я — твой Дунгеон Мастер. Мир тёмный, опасный и беспощадный.\n\n"
        "**Быстрый старт:**\n"
        "1. `/new Название` — создать сессию\n"
        "2. `/join` — присоединиться\n"
        "3. `/character` (ответ на .txt/.md) — загрузить персонажа\n"
        "4. `Дн. я атакую гоблина мечом!` — сходить\n\n"
        "**Правила ходов:**\n"
        "• Каждый раунд — ОДНО действие с префиксом `Дн.`\n"
        "• Мастер ждёт, пока ВСЕ игроки сходят\n"
        "• Потом разрешает все действия разом\n"
        "• `/ask ВОПРОС` — вопрос мастеру вне очереди\n"
        "• `/clear` — очистить память мастера (создатель)\n\n"
        "**Бой:** /combat /endcombat /skip\n"
        "**HP & Жизнь:** /hp /deathsave /condition /rest\n"
        "**Экономика:** /gold /inventory\n"
        "**Квесты:** /quest\n"
        "**Мир:** /time /weather /factions /location /world\n"
        "**Мастеру:** /dndstart /roll encounter /srd /mode /npc /event /concentration /resources /summary /dbask /transfer\n"
        "**Партия:** /ability - все статы и навыки\n"
        "**Приватно (в ЛС):** /do действие\n\n"
        "🔒 *Только Мастер может менять базу данных напрямую.*\n"
        "*Игроки влияют на мир только через RP-действия (Дн.).*\n\n"
        "📋 *Используя бота, вы соглашаетесь с /terms*\n"
        "🗑️ *Удалить все данные: /delete*\n\n"
        "Тени сгущаются...",
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, ctx)


async def new_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if get_session(chat_id):
        await send_safe(update, "❌ В чате уже есть сессия. `/end` чтобы завершить.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/new Название кампании`")
        return

    name = " ".join(ctx.args)
    session = sessions.create_session(chat_id, name, user.id, user.username or user.first_name)
    await send_safe(update, 
        f"⚔️ **Сессия создана!**\n*{session.name}*\nID: `{session.id}`\n\nДругие игроки: `/join`",
    )


async def join_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии. Создай `/new НАЗВАНИЕ`")
        return
    db = db_manager.get_db(session.id)
    if db.get_player(user.id, session.id):
        await send_safe(update, "Ты уже в сессии!")
        return

    sessions.add_player(session.id, user.id, user.username or "", user.first_name or user.username or "Неизвестный")
    sessions.add_player_to_queue(session.id, user.id)
    await send_safe(update, 
        f"✅ **{user.first_name or user.username}** присоединился!\n\n"
        "Загрузи персонажа: отправь .txt/.md и ответь `/character`",
    )


async def leave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        return
    if sessions.is_creator(user.id, session.id):
        await send_safe(update, "⚠️ Ты создатель. Используй `/end` чтобы завершить.")
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


async def char_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Upload character sheet — validated by Granite"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    message = update.message
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    db = db_manager.get_db(session.id)
    if not db.get_player(user.id, session.id):
        await send_safe(update, "Сначала `/join`")
        return
    if not message.reply_to_message or not message.reply_to_message.document:
        await send_safe(update, "📄 Ответь на .txt/.md файл командой `/character`")
        return

    doc = message.reply_to_message.document
    if doc.mime_type not in ["text/plain", "text/markdown"]:
        await send_safe(update, "❌ Только .txt или .md")
        return

    try:
        file = await doc.get_file()
        fp = os.path.join(CHARACTERS_DIR, f"{user.id}_{session.id}_{doc.file_name}")
        await file.download_to_drive(fp)

        sheet_text = Path(fp).read_text(encoding="utf-8")

        # === AI VALIDATION ===
        await send_safe(update, "🔍 Проверяю лист персонажа...")
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
                f"🔧 **Исправь ошибки и загрузи лист заново командой `/character` (ответом на файл).**"
            )
            return
        elif validation["verdict"] == "VALID":
            await send_safe(update, "✅ Лист персонажа прошёл проверку правил!")
        # ==========================

        db.save_character_sheet(session.id, user.id, sheet_text, doc.file_name)

        # === AI PARSING (primary) ===
        parsed = await dm_engine.parse_character_sheet(sheet_text)
        if not parsed:
            # Fallback to Python parser
            parsed = CharacterParser.parse_file(fp)
        if not parsed:
            await send_safe(update, "❌ Не удалось распарсить лист персонажа.")
            return

        char_id = str(uuid.uuid4())[:8]
        # Check if player already has a character - replace it
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
        ))
        # Add starting gold if parsed from sheet
        if parsed.gold and parsed.gold > 0:
            sessions.add_gold(session.id, char_id, parsed.name, gp=parsed.gold, reason="Стартовый капитал")
        # Add starting inventory items
        for item in parsed.inventory:
            if item and item.strip():
                sessions.add_item(session.id, char_id, parsed.name, item.strip(), 1)
        await send_safe(update, 
            f"✅ **Персонаж сохранён!** Лист отправлен мастеру.\n\n"
            f"{CharacterParser.format_character_sheet(parsed)}",
        )
    except Exception as e:
        logger.error(f"Char error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


async def sheet_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if not session:
        return
    db = db_manager.get_db(session.id)
    char = db.get_character_by_player(update.effective_user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/character`")
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
    """Show all characters stats, skills, conditions - party overview."""
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
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

async def combat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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

    result = sessions.start_combat(session.id)
    db = db_manager.get_db(session.id)
    db.add_history(HistoryEntry(session_id=session.id, author="DM", content="Бой начался!", entry_type="combat"))
    await send_safe(update, result)

    sessions.start_action_collection(session.id)
    pending = sessions.get_pending_players(session.id)
    await send_safe(update, 
        f"📝 Ходи: `Дн. твое действие`\n\n⏳ Ждём: {', '.join(pending)}",
    )


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
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/skip @ник1 @ник2 ...`")
        return

    targets = [a.strip("@") for a in ctx.args]
    players = sessions.get_players(session.id)
    skipped_names = []
    all_resolved = False

    for target in targets:
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

        if target_p:
            should_resolve = sessions.skip_player(session.id, target_p.user_id)
            skipped_names.append(target_p.display_name)
            if should_resolve:
                all_resolved = True

    if skipped_names:
        await send_safe(update, f"⏭️ Ход пропущен: {', '.join(skipped_names)}")
    else:
        await send_safe(update, f"❌ Игроки не найдены: {', '.join(targets)}")

    if all_resolved:
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
        await send_safe(update, "Использование: `/kick @ник`")
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

    sessions.kick_player(session.id, target_p.user_id)
    await send_safe(update, f"👢 **{target_p.display_name}** выгнан из сессии.")




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
        await send_safe(update, "Использование: `/transfer @ник` или `/transfer имя_персонажа`")
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

    await send_safe(update, f"👑 **Права Мастера переданы {target_p.display_name}!**")

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Out-of-turn question — does NOT go through queue, does NOT advance game"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/ask Как работает огненный шар?`")
        return

    question = " ".join(ctx.args)
    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
        return

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")

    try:
        history = db.get_history(session.id, limit=30)
        history_msgs = [{"role": "user" if h.author != "DM" else "assistant", "content": h.content} for h in history]

        answer = await dm_engine.answer_question(history_msgs, question, player.display_name)

        db.add_history(HistoryEntry(session_id=session.id, author=player.display_name, content=f"[ASK] {question}", entry_type="ooc"))
        db.add_history(HistoryEntry(session_id=session.id, author="DM", content=f"[ASK] {answer}", entry_type="ooc"))

        await send_safe(update, f"❓ **{player.display_name}:** {question}\n\n🎭 **Мастер:** {answer}")
    except Exception as e:
        logger.error(f"Ask error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")




async def dbask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Appeal to DB-Bot to fix database state. Players can correct AI mistakes."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/dbask у меня 15 зм, а не 10` или `/dbask убери состояние отравления`")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
        return

    appeal = " ".join(ctx.args)
    char = db.get_character_by_player(user.id, session.id)
    char_name = char.name if char else player.display_name

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")

    try:
        # Build current DB state for context
        chars = db.get_session_characters(session.id)
        state_lines = ["Текущее состояние БД:"]
        for c in chars:
            gold = sessions.get_gold(session.id, c.id)
            inv = sessions.get_inventory(session.id, c.id)
            loc = sessions.get_location(session.id, c.id)
            conds = sessions.get_character_conditions(session.id, c.id)
            cond_str = ", ".join(cc.condition for cc in conds) if conds else "нет"
            inv_str = ", ".join(i["item"] for i in inv) if inv else "пусто"
            state_lines.append(
                f"{c.name}: HP={c.hp}/{c.max_hp}, AC={c.ac}, GP={gold.get('gp',0)}, Loc={loc.location_name if loc else '?'}, Conds=[{cond_str}], Inv=[{inv_str}]"
            )
        state_text = "\n".join(state_lines)

        # Call DB-Bot with appeal
        db_prompt = f"""Игрок {char_name} апеллирует: "{appeal}"

{state_text}

Проанализируй апелляцию. Если игрок прав - вызови инструменты для исправления БД.
Если неправ - ответь почему. НЕ выдумывай изменения, которых нет в апелляции."""

        from ai_client import DB_BOT_PROMPT, DB_TOOLS
        messages = [{"role": "user", "content": db_prompt}]
        response = await dm_engine.db_bot.chat(messages, system_prompt=DB_BOT_PROMPT, tools=DB_TOOLS)

        # Process any tool calls from DB-Bot
        choice = response["choices"][0]["message"]
        tool_calls = choice.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                args = json.loads(tc["function"]["arguments"])
                # Apply the fix directly
                if tool_name == "change_hp":
                    target = args.get("character_name", "")
                    delta = args.get("delta", 0)
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.change_hp(session.id, c.id, c.name, delta, f"dbask by {char_name}")
                            break
                elif tool_name == "change_gold":
                    target = args.get("character_name", "")
                    amount = args.get("amount", 0)
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.add_gold(session.id, c.id, c.name, gp=amount, reason=f"dbask by {char_name}")
                            break
                elif tool_name == "add_condition":
                    target = args.get("character_name", "")
                    cond = args.get("condition", "")
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.add_condition(session.id, c.id, c.name, cond, f"dbask by {char_name}")
                            break
                elif tool_name == "remove_condition":
                    target = args.get("character_name", "")
                    cond = args.get("condition", "")
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.remove_condition(session.id, c.id, cond)
                            break
                elif tool_name == "add_item":
                    target = args.get("character_name", "")
                    item = args.get("item_name", "")
                    qty = args.get("quantity", 1)
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.add_item(session.id, c.id, c.name, item, qty)
                            break
                elif tool_name == "remove_item":
                    target = args.get("character_name", "")
                    item = args.get("item_name", "")
                    qty = args.get("quantity", 1)
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.remove_item(session.id, c.id, item, qty)
                            break
                elif tool_name == "set_location":
                    target = args.get("character_name", "")
                    loc_name = args.get("location_name", "")
                    for c in chars:
                        if target.lower() in c.name.lower():
                            sessions.set_location(session.id, c.id, loc_name)
                            break
                elif tool_name == "advance_time":
                    mins = args.get("minutes", 0)
                    hrs = args.get("hours", 0)
                    sessions.advance_time(session.id, minutes=mins, hours=hrs)

        answer = choice.get("content", "DB-Bot не ответил.")

        db.add_history(HistoryEntry(
            session_id=session.id,
            author=player.display_name,
            content=f"[DB-ASK] {appeal}",
            entry_type="ooc",
        ))
        db.add_history(HistoryEntry(
            session_id=session.id,
            author="DB-Bot",
            content=f"[DB-ASK] {answer}",
            entry_type="ooc",
        ))

        await send_safe(update, f"🗃️ **Апелляция {char_name}:** {appeal}\n\n🤖 **DB-Bot:** {answer}")
    except Exception as e:
        logger.error(f"DB-ask error: {e}")
        await send_safe(update, f"❌ Ошибка DB-Bot: {e}")

async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    players = sessions.get_players(session.id)
    chars = db.get_session_characters(session.id)
    lines = [
        f"📜 **{session.name}** | ID: `{session.id}`",
        f"Игроков: {len(players)} | Персонажей: {len(chars)}",
    ]
    if session.combat_active:
        lines.append(f"⚔️ Бой: Раунд {session.round_number}")
        cur = sessions.get_current_turn(session.id)
        if cur:
            lines.append(f"Ход: **{cur['name']}**")

    queue = db.get_queue_state(session.id)
    if queue:
        waiting = json.loads(queue.waiting_for)
        if waiting:
            lines.append(f"\n🎲 Ждём: {', '.join(sessions.get_pending_players(session.id))}")
        elif queue.is_resolving:
            lines.append("\n✅ Разрешаю...")

    await send_safe(update, "\n".join(lines))


async def end_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return

    db = db_manager.get_db(session.id)
    history = db.get_history(session.id, limit=1000)
    export_path = None
    if history:
        lines = [f"Session: {session.name}", f"ID: {session.id}", "", "=" * 50, ""]
        for h in history:
            lines.append(f"[{h.author}]: {h.content}")
            lines.append("")

        export_path = os.path.join(BASE_DIR, "logs", f"session_{session.id}_{session.name.replace(' ', '_')}.txt")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"[EXPORT] Session saved to {export_path}")

    sessions.end_session(session.id)
    await send_safe(update, f"🏁 **{session.name}** завершена. История экспортирована в logs/.", source="system")

    if ADMIN_CHAT_ID and ctx and export_path:
        await send_to_admin(ctx, f"📋 Session ended: {session.name} ({session.id})", export_path)


async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Clear AI context (history) — keeps characters and session"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель может очистить контекст.")
        return

    db_manager.clear_history(session.id)
    await send_safe(update, 
        "🧹 Контекст мастера очищен!\n\n"
        "История игры удалена. Мастер начинает с чистого листа.\n"
        "Листы персонажей и игроки сохранены.",
    )


async def summary_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate session summary using Memory model"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return

    await send_safe(update, "📋 Генерирую сводку кампании...")

    try:
        summary = await sessions.summarize_and_save(session.id)
        if summary:
            await send_safe(update, f"📋 Сводка кампании:\n\n{summary}")
        else:
            await send_safe(update, "📋 История слишком короткая для сводки.")
    except Exception as e:
        logger.error(f"Summary error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


# ═══════════════════════════════════════════════════════════════
# HP TRACKER — READ-ONLY for players, WRITE-ONLY for DM
# ═══════════════════════════════════════════════════════════════

async def hp_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/hp — показать HP (все). /hp +N|-N — только Мастер."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
        return

    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа. `/character`")
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
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять HP. Используй `Дн. я использую зелье/лечу себя`.")
        return

    arg = ctx.args[0]
    if arg.startswith("+") or arg.startswith("-"):
        try:
            delta = int(arg)
        except ValueError:
            await send_safe(update, "Использование: `/hp +5` или `/hp -10`")
            return

        result = sessions.change_hp(session.id, char.id, char.name, delta, f"команда от {player.display_name}")

        if "error" in result:
            await send_safe(update, f"❌ {result['error']}")
            return

        emoji = "❤️" if result["change"] > 0 else "💔"
        status_msg = ""
        if result["status"] == "dying":
            status_msg = f"\n\n💀 **{char.name} падает!** Спасброски от смерти..."
        elif result["status"] == "stabilized":
            status_msg = f"\n\n✨ **{char.name} стабилизирован!**"

        await send_safe(update, 
            f"{emoji} **{char.name}**: {result['old_hp']} → **{result['new_hp']}** HP "
            f"({result['change']:+d}){status_msg}"
        )

        if result["status"] == "dying":
            await send_safe(update, 
                f"🎲 **Спасбросок от смерти** для {char.name}:\n"
                f"`/deathsave {char.name}` — бросить\n"
                f"`/deathsave {char.name} success` — отметить успех\n"
                f"`/deathsave {char.name} failure` — отметить провал"
            )
    else:
        await send_safe(update, "Использование:\n`/hp` — показать\n`/hp +5` — лечить (только Мастер)\n`/hp -10` — урон (только Мастер)")


async def deathsave_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """DM ONLY — players cannot manipulate death saves directly."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер управляет спасбросками от смерти. Игроки — через RP (Дн.).")
        return

    if not ctx.args:
        await send_safe(update, "Использование: `/deathsave имя` или `/deathsave имя success/failure`")
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


# ═══════════════════════════════════════════════════════════════
# CONDITIONS — DM ONLY
# ═══════════════════════════════════════════════════════════════

VALID_CONDITIONS = ["blinded", "charmed", "deafened", "frightened", "grappled",
                   "incapacitated", "invisible", "paralyzed", "petrified",
                   "poisoned", "prone", "restrained", "stunned", "unconscious",
                   "concentrating", "exhaustion", "bleeding"]

CONDITIONS_RU = {
    "blinded": "ослеплён", "charmed": "очарован", "deafened": "оглохший",
    "frightened": "испуган", "grappled": "схвачен", "incapacitated": "недееспособен",
    "invisible": "невидим", "paralyzed": "парализован", "petrified": "окаменел",
    "poisoned": "отравлен", "prone": "лежащий", "restrained": "сдерживаемый",
    "stunned": "ошеломлён", "unconscious": "без сознания", "concentrating": "концентрация",
    "exhaustion": "истощение", "bleeding": "кровотечение",
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
            await send_safe(update, "Сначала `/join`")
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

    # WRITE: DM ONLY
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять состояния. Игроки — через RP (Дн.).")
        return

    action = ctx.args[0].lower()

    if action == "add":
        if len(ctx.args) < 3:
            await send_safe(update, f"Использование: `/condition add имя_персонажа название`\n\nДоступные: {', '.join(CONDITIONS_RU.values())}")
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
            await send_safe(update, "Использование: `/condition remove имя_персонажа название`")
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
        await send_safe(update, "Использование: `/condition list` | `/condition add имя состояние` | `/condition remove имя состояние` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# REST — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def rest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """DM ONLY — players rest through RP (Дн. я отдыхаю)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может запускать отдых. Игроки — через `Дн. мы отдыхаем`.")
        return

    if not ctx.args:
        await send_safe(update, "Использование: `/rest short имя` или `/rest long имя`")
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
        await send_safe(update, "Использование: `/rest short имя` или `/rest long имя` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# GOLD & INVENTORY — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def gold_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/gold — read for players. /gold +N|-N — DM ONLY."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
        return

    char = db.get_character_by_player(user.id, session.id)
    if not char:
        await send_safe(update, "Нет персонажа.")
        return

    balance = sessions.get_gold(session.id, char.id)

    if not ctx.args:
        lines = [f"💰 **Кошелёк {char.name}:**"]
        if balance["pp"]:
            lines.append(f"  {balance['pp']} ПП (платина)")
        if balance["gp"]:
            lines.append(f"  {balance['gp']} ЗМ (золото)")
        if balance["ep"]:
            lines.append(f"  {balance['ep']} ЭМ (электрум)")
        if balance["sp"]:
            lines.append(f"  {balance['sp']} СМ (серебро)")
        if balance["cp"]:
            lines.append(f"  {balance['cp']} СМм (медь)")

        total_gp = balance["gp"] + balance["pp"] * 10 + balance["ep"] * 0.5 + balance["sp"] * 0.1 + balance["cp"] * 0.01
        lines.append(f"\n📊 Эквивалент: ~{total_gp:.2f} зм")

        await send_safe(update, "\n".join(lines))
        return

    # WRITE: DM ONLY
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять золото. Игроки — через `Дн. я беру/трачу золото`.")
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
        await send_safe(update, "Использование:\n`/gold` — показать\n`/gold +10 gp награда` (только Мастер)")


async def inventory_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/inventory — read for players. add/remove — DM ONLY."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
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

    # WRITE: DM ONLY
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять инвентарь. Игроки — через `Дн. я беру/бросаю предмет`.")
        return

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/inventory add имя_персонажа предмет [кол-во]`")
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
            await send_safe(update, "Использование: `/inventory remove имя_персонажа предмет [кол-во]`")
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
        await send_safe(update, "Использование:\n`/inventory` — показать\n`/inventory add имя предмет` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# QUESTS
# ═══════════════════════════════════════════════════════════════

async def quest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can list quests. DM can add/update."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
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

    # WRITE: DM ONLY
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может управлять квестами.")
        return

    action = ctx.args[0].lower()

    if action in ("add", "добавить", "a"):
        if len(ctx.args) < 2:
            await send_safe(update, "Использование: `/quest add Название | Описание | @ник_игрока`")
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
            await send_safe(update, "Использование: `/quest update ID completed/failed`")
            return

        try:
            quest_id = int(ctx.args[1])
            new_status = ctx.args[2].lower()
            sessions.update_quest(quest_id, status=new_status)
            await send_safe(update, f"📜 Квест #{quest_id} обновлён: **{new_status}**")
        except ValueError:
            await send_safe(update, "❌ ID квеста должен быть числом.")

    else:
        await send_safe(update, "Использование:\n`/quest list` — активные\n`/quest add Название | Описание` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# TIME & WEATHER
# ═══════════════════════════════════════════════════════════════

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

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять время.")
        return

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
            await send_safe(update, "Использование: `/time +2h` или `/time +30m`")
    else:
        await send_safe(update, "Использование:\n`/time` — показать\n`/time +2h` — +2 часа (только Мастер)")


async def weather_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can view weather. DM can generate."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if ctx.args and ctx.args[0].lower() in ("generate", "gen", "сгенерировать"):
        if not sessions.is_creator(user.id, session.id):
            await send_safe(update, "🔒 Только Мастер может менять погоду.")
            return

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
        f"Мастер: `/weather generate` — сгенерировать новую"
    )


# ═══════════════════════════════════════════════════════════════
# FACTIONS
# ═══════════════════════════════════════════════════════════════

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

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может управлять фракциями.")
        return

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
            await send_safe(update, "Использование: `/factions rep ID +5` или `/factions rep ID -3`")
            return

        try:
            faction_id = int(ctx.args[1])
            delta = int(ctx.args[2])
            sessions.change_reputation(session.id, faction_id, delta)
            await send_safe(update, f"🏛️ Репутация с фракцией #{faction_id} изменена на **{delta:+d}**")
        except ValueError:
            await send_safe(update, "❌ Использование: `/factions rep ID +N`")

    else:
        await send_safe(update, "Использование:\n`/factions list`\n`/factions add Название | Описание` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# RANDOM ENCOUNTERS — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def roll_encounter_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер.")
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


# ═══════════════════════════════════════════════════════════════
# SRD LOOKUP — anyone can read
# ═══════════════════════════════════════════════════════════════

async def srd_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not ctx.args:
        await send_safe(update, "📖 Использование: `/srd огненный шар` или `/srd условия отравления`")
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


# ═══════════════════════════════════════════════════════════════
# LOCATION — read for all, write DM only
# ═══════════════════════════════════════════════════════════════

async def location_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
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

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Локацию устанавливает Мастер.")
        return

    parts = " ".join(ctx.args).split(" | ")
    loc_name = parts[0].strip()
    loc_desc = parts[1].strip() if len(parts) > 1 else ""

    sessions.set_location(session.id, char.id, loc_name, loc_desc)
    await send_safe(update, f"📍 **{char.name}** → **{loc_name}**")


# ═══════════════════════════════════════════════════════════════
# RESOURCES — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def resources_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Players can view resources. DM can set/use/recover."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/join`")
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

    # WRITE: DM ONLY
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер может менять ресурсы. Игроки — через `Дн. я использую...`.")
        return

    action = ctx.args[0].lower()

    if action in ("set", "установить", "s"):
        if len(ctx.args) < 5:
            await send_safe(update, "Использование: `/resources set имя_персонажа Название текущий максимум`")
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
            await send_safe(update, "Использование: `/resources use имя_персонажа Ярость`")
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
            await send_safe(update, "Использование: `/resources recover имя_персонажа Ярость`")
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
        await send_safe(update, "Использование:\n`/resources` — показать\n`/resources set имя Название текущий максимум` (только Мастер)")


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


# ═══════════════════════════════════════════════════════════════
# ROLL MODE — DM ONLY
# ═══════════════════════════════════════════════════════════════

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

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Мастер.")
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


# ═══════════════════════════════════════════════════════════════
# CONCENTRATION — DM ONLY (players manage via RP)
# ═══════════════════════════════════════════════════════════════

async def concentration_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "🔒 Только Мастер управляет концентрацией. Игроки — через `Дн. я концентрируюсь на...`.")
        return

    if not ctx.args:
        await send_safe(update, "Использование: `/concentration start имя заклинание` | `/concentration end имя` | `/concentration check имя 15`")
        return

    action = ctx.args[0].lower()

    if action in ("start", "начать", "s"):
        if len(ctx.args) < 3:
            await send_safe(update, "Использование: `/concentration start имя_персонажа заклинание`")
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
            await send_safe(update, "Использование: `/concentration end имя_персонажа`")
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
            await send_safe(update, "Использование: `/concentration check имя_персонажа 15` (урон для КС)")
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
        await send_safe(update, "Использование (только Мастер):\n`/concentration start имя заклинание`\n`/concentration end имя`\n`/concentration check имя 15`")


# ═══════════════════════════════════════════════════════════════
# NPC MEMORY — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def npc_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Мастер.")
        return

    db = db_manager.get_db(session.id)
    if not ctx.args or ctx.args[0].lower() == "list":
        npcs = db.get_all_npcs(session.id)
        if not npcs:
            await send_safe(update, "🎭 В реестре пока нет NPC.")
            return

        lines = ["🎭 **NPC в реестре:**"]
        for npc in npcs:
            personality = ""
            if npc.personality:
                try:
                    p = json.loads(npc.personality)
                    personality = p.get("traits", "")[:50]
                except:
                    personality = str(npc.personality)[:50]
            lines.append(f"• **{npc.name}**{f' — {personality}' if personality else ''}")

        await send_safe(update, "\n".join(lines))
        return

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
            await send_safe(update, "Использование: `/npc note Имя Текст заметки`")
            return

        name = ctx.args[1]
        note = " ".join(ctx.args[2:])
        sessions.update_npc_facts(session.id, name, note)
        await send_safe(update, f"📝 Заметка о **{name}** сохранена.")

    else:
        await send_safe(update, "Использование:\n`/npc list`\n`/npc add Имя | Персонажность | Факты` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# WORLD EVENTS — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def event_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Мастер.")
        return

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
            await send_safe(update, "Использование: `/event resolve ID`")
            return
        try:
            event_id = int(ctx.args[1])
            sessions.resolve_world_event(event_id)
            await send_safe(update, f"✅ Событие #{event_id} решено.")
        except ValueError:
            await send_safe(update, "❌ ID должен быть числом.")

    else:
        await send_safe(update, "Использование:\n`/event list`\n`/event generate` (только Мастер)")


# ═══════════════════════════════════════════════════════════════
# WORLD PREGENERATION — DM ONLY
# ═══════════════════════════════════════════════════════════════

async def world_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Мастер.")
        return

    db = db_manager.get_db(session.id)
    if not ctx.args:
        session_obj = db.get_session(session.id)
        if session_obj and session_obj.current_scene:
            await send_safe(update, f"🌍 **Текущий мир:**\n\n{session_obj.current_scene[:500]}")
        else:
            await send_safe(update, "🌍 Мир ещё не описан. `/world generate [тема]`")
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
            game_actions = await dm_engine.process_with_db_bot(session_obj.current_scene)
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
            "`/world generate [тема]` — создать мир\n"
            "`/world status` — статус мира\n"
            "`/world sync` — синхронизировать текст мира с БД"
        )


# ═══════════════════════════════════════════════════════════════
# PRIVATE ACTIONS — via DM to bot
# ═══════════════════════════════════════════════════════════════

async def private_action_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if update.effective_chat.type != "private":
        await send_safe(update, "🕵️ Эта команда только в ЛС бота. Напиши мне в личку.")
        return

    if not ctx.args:
        await send_safe(update, "🕵️ Использование (в ЛС): `/do краду карман у торговца`")
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
        await send_safe(update, "❌ Ты не в активной сессии. Сначала `/join` в группе.")
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

    await send_safe(update, 
        f"🕵️ **Приватное действие записано:**\n_{action_text}_\n\n"
        f"Мастер увидит это при разрешении раунда. Другие игроки НЕ увидят."
    )

    players = sessions.get_players(player_session.id)
    for pl in players:
        if pl.is_creator:
            try:
                await ctx.bot.send_message(
                    chat_id=pl.user_id,
                    text=f"🕵️ **Приватное действие** от {char_name}:\n_{action_text}_"
                )
            except Exception:
                pass
            break


# ═══════════════════════════════════════════════════════════════
# MAIN GAME LOOP — Queue-based action collection
# ═══════════════════════════════════════════════════════════════

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_chat or not update.message or not update.message.text:
        return
    text = (update.message.text or "").strip()

    if not text:
        return

    session = get_session(update.effective_chat.id)
    if not session:
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(update.effective_user.id, session.id)
    if not player:
        return

    if not (text.startswith("Дн.") or text.startswith("Dn.")):
        return

    action_text = text[3:].strip()
    if not action_text:
        await send_safe(update, "После `Дн.` или `Dn.` напиши действие. Пример: `Дн. Я атакую гоблина мечом!`")
        return

    char = db.get_character_by_player(update.effective_user.id, session.id)
    char_name = char.name if char else player.display_name
    telegram_nick = player.username or player.display_name

    queue = db.get_queue_state(session.id)

    if not queue:
        sessions.start_action_collection(session.id)
        queue = db.get_queue_state(session.id)

    if queue and queue.is_resolving:
        await send_safe(update, "⏳ Разрешаю предыдущий раунд... подожди.")
        return

    if queue:
        waiting_for = json.loads(queue.waiting_for)
        collected = json.loads(queue.collected_actions)

        if str(update.effective_user.id) in collected:
            await send_safe(update, "⏳ Ты уже сходил в этом раунде. Жди следующего.")
            return

        if update.effective_user.id not in waiting_for:
            sessions.start_action_collection(session.id)
            queue = db.get_queue_state(session.id)
            waiting_for = json.loads(queue.waiting_for)

        is_complete, msg = sessions.submit_action(session.id, update.effective_user.id, action_text)

        if is_complete:
            await send_safe(update, 
                f"✅ {char_name} (@{telegram_nick}) сходил!\n"
                f"🎲 Все на месте — мастер разрешает...",
                source="player",
            )
            await _resolve_and_send(session.id, update, ctx)
        else:
            pending = sessions.get_pending_players(session.id)
            await send_safe(update, 
                f"✅ {char_name} (@{telegram_nick}): {action_text}\n"
                f"⏳ Ждём: {', '.join(pending)}",
                source="player",
            )


async def _resolve_and_send(session_id: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE = None):
    try:
        roll_mode = sessions.get_roll_mode(session_id)

        result = await sessions.resolve_round(session_id, roll_mode=roll_mode)
        raw_text = result.get("player_text", "")
        if not raw_text.strip():
            await send_safe(update, "🤖 Мастер задумался, но ничего не ответил. Попробуйте ещё раз или используйте `/ask`.")
            return
        html_text = md_to_html(raw_text)
        # Wrap entire Master response in expandable blockquote
        if html_text.strip():
            html_text = "<blockquote expandable>\n" + html_text + "\n</blockquote>"
        await send_safe(update, html_text, parse_html=False, source="master")
        # Log raw narrative and DB actions
        md_logger.log(session_id, "master", result.get("player_text", ""))
        md_logger.log(session_id, "db_bot", f"Applied {result.get('game_actions_applied', 0)} DB actions")
        if result.get("gm_log"):
            md_logger.log(session_id, "system", f"GM Log: {result['gm_log']}")
            logger.info(f"GM: {result['gm_log']}")

        # Real-time admin stream
        if ctx and ADMIN_CHAT_ID:
            raw_text = result.get('player_text', '')
            admin_text = f"📜 {session_id[:6]} | {raw_text[:3000]}"
            await send_to_admin(ctx, admin_text)

        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if session and session.combat_active:
            pending = sessions.get_pending_players(session_id)
            if pending:
                cur = sessions.get_current_turn(session_id)
                turn_info = f"Ход: <b>{cur['name']}</b>\n" if cur else ""
                await send_safe(update,
                    f"⚔️ Новый раунд!\n\n{turn_info}Ждём: {', '.join(pending)}\n\nПиши Дн. твое действие"
                )
                # HTML mentions via user_id
                players = sessions.get_players(session_id)
                tags_html = " ".join([
                    f'<a href="tg://user?id={p.user_id}">{p.display_name}</a>'
                    for p in players if p.user_id
                ])
                if tags_html:
                    await send_safe(update, tags_html, parse_html=True, raw_html=True, source="system")

    except Exception as e:
        logger.error(f"Resolve error: {e}")
        await send_safe(update, f"❌ Ошибка разрешения: {e}")


async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update error: {ctx.error}")
    try:
        if ADMIN_CHAT_ID and ctx and update and update.effective_chat:
            await send_to_admin(ctx, f"⚠️ ERROR in chat {update.effective_chat.id}: {str(ctx.error)[:500]}")
    except Exception:
        pass



async def terms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show terms of service"""
    await send_safe(update,
        "📋 **Пользовательское соглашение**\n\n"
        "1. Бот собирает полные логи сессий для исправления ошибок и улучшения ИИ-мастера.\n"
        "2. Данные хранятся на сервере разработчика и не передаются третьим лицам.\n"
        "3. Администратор имеет технический доступ к логам сессий.\n"
        "4. По команде `/delete` вы можете запросить удаление всех данных сессии.\n"
        "5. Используя бота, вы даёте согласие на сбор и обработку данных.\n\n"
        "Если не согласны — не используйте бота или разверните свою копию."
    )

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
        await send_safe(update, "❌ Только Мастер может начать игру.")
        return

    db = db_manager.get_db(session.id)
    chars = db.get_session_characters(session.id)
    if not chars:
        await send_safe(update, "❌ Нет загруженных персонажей. Игроки должны загрузить листы через `/character`.")
        return

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")

    try:
        # Generate world
        result = await sessions.generate_world(session.id, "dark fantasy")
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

        opening_prompt = f"""Ты - Дунгеон Мастер. Игра только начинается.

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

        # Render to HTML via fallback (AI renderer cuts text)
        opening_html = md_to_html(opening_text)

        # Send world as separate expandable message
        if world_html.strip():
            world_chunked = md_to_html(world_html)
            if world_chunked.strip():
                world_chunked = "<blockquote expandable>\n" + world_chunked + "\n</blockquote>"
            await send_safe(update, world_chunked, parse_html=False, source="master")

        # Send opening narrative as separate expandable message
        if opening_html.strip():
            opening_chunked = md_to_html(opening_html)
            if opening_chunked.strip():
                opening_chunked = "<blockquote expandable>\n" + opening_chunked + "\n</blockquote>"
            await send_safe(update, opening_chunked, parse_html=False, source="master")

        # Start first round collection
        sessions.start_action_collection(session.id)
        pending = sessions.get_pending_players(session.id)
        # Tag players outside the quote
        players = sessions.get_players(session.id)
        tags_html = " ".join([
            f'<a href="tg://user?id={p.user_id}">{p.display_name}</a>'
            for p in players if p.user_id
        ])
        await send_safe(update, 
            f"⚔️ **Игра началась!**\n\n"
            f"📝 Пишите `Дн. ваше действие` — мастер разрешит, когда все сходят.\n"
            f"⏳ Ждём: {', '.join(pending)}\n\n{tags_html}",
        )
        if tags_html:
            await send_safe(update, tags_html, parse_html=True, raw_html=True, source="system")

    except Exception as e:
        logger.error(f"dndstart error: {e}")
        await send_safe(update, f"❌ Ошибка старта: {e}")



# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("=" * 50)
        print("  ERROR: TELEGRAM_BOT_TOKEN not set!")
        print("=" * 50)
        print()
        print("  1. Copy .env.example to .env:")
        print("     cp .env.example .env")
        print()
        print("  2. Open .env and set your keys:")
        print("     TELEGRAM_BOT_TOKEN=your_token")
        print("     OPENAI_API_KEY=your_key")
        print()
        print("  Get token: @BotFather on Telegram")
        print("  Get API key: your-provider.com/dashboard/api-keys")
        print()
        sys.exit(1)

    if not OPENAI_API_KEY:
        print("=" * 50)
        print("  ERROR: OPENAI_API_KEY not set!")
        print("=" * 50)
        print()
        print("  Open .env and set:")
        print("  OPENAI_API_KEY=your_key")
        print()
        print("  Get API key: your-provider.com/dashboard/api-keys")
        print()
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CommandHandler("leave", leave_cmd))
    app.add_handler(CommandHandler("players", players_cmd))
    app.add_handler(CommandHandler("character", char_cmd))
    app.add_handler(CommandHandler("sheet", sheet_cmd))
    app.add_handler(CommandHandler("combat", combat_cmd))
    app.add_handler(CommandHandler("endcombat", endcombat_cmd))
    app.add_handler(CommandHandler("skip", skip_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))

    app.add_handler(CommandHandler("hp", hp_cmd))
    app.add_handler(CommandHandler("deathsave", deathsave_cmd))
    app.add_handler(CommandHandler("condition", condition_cmd))
    app.add_handler(CommandHandler("rest", rest_cmd))
    app.add_handler(CommandHandler("gold", gold_cmd))
    app.add_handler(CommandHandler("inventory", inventory_cmd))
    app.add_handler(CommandHandler("quest", quest_cmd))
    app.add_handler(CommandHandler("time", time_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("factions", factions_cmd))
    app.add_handler(CommandHandler("roll", roll_encounter_cmd))
    app.add_handler(CommandHandler("srd", srd_cmd))
    app.add_handler(CommandHandler("location", location_cmd))
    app.add_handler(CommandHandler("resources", resources_cmd))
    app.add_handler(CommandHandler("pvp", pvp_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("concentration", concentration_cmd))
    app.add_handler(CommandHandler("npc", npc_cmd))
    app.add_handler(CommandHandler("event", event_cmd))
    app.add_handler(CommandHandler("world", world_cmd))

    app.add_handler(CommandHandler("dndstart", dndstart_cmd))
    app.add_handler(CommandHandler("dbask", dbask_cmd))
    app.add_handler(CommandHandler("ability", ability_cmd))
    app.add_handler(CommandHandler("transfer", transfer_cmd))
    app.add_handler(CommandHandler("do", private_action_cmd))
    app.add_handler(CommandHandler("terms", terms_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    active_sessions = db_manager.get_all_active_sessions()
    if active_sessions:
        logger.info(f"[RECOVERY] Found {len(active_sessions)} active session(s)")
        for s in active_sessions:
            history_count = len(db_manager.get_db(s.id).get_history(s.id, limit=30))
            logger.info(f"[RECOVERY] Session '{s.name}' (ID: {s.id}): {history_count} history entries")

    else:
        logger.info("[RECOVERY] No active sessions found")

    logger.info("🎲 Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
