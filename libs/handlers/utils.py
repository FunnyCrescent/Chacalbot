"""
libs.handlers.utils — auto-split from libs/bot_handlers.py.

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

_ALLOWED_TAG_NAMES = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "span"}


_TAG_SCAN_RE = _re.compile(r'<(/?)([a-zA-Z]+)( [^>]*)?>')


def _tags_are_balanced(html_text: str) -> bool:
    """Stack-check that every opening tag we generated has a matching close, in the
    right order, using only Telegram's allowed tag set. If this ever returns False we
    must NOT send the text as HTML \u2014 Telegram rejects the WHOLE message on one bad tag."""
    stack = []
    for is_close, name, _attrs in _TAG_SCAN_RE.findall(html_text):
        name = name.lower()
        if name not in _ALLOWED_TAG_NAMES:
            return False
        if not is_close:
            stack.append(name)
        else:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
    return not stack


def md_to_html(text: str) -> str:
    """Convert Markdown to Telegram HTML. Only uses Bot API supported tags.

    C1 fix: the old version ran markdown->HTML substitutions FIRST and only escaped
    bare &/</> afterwards by guessing which substrings "looked like" real tags, so any
    stray '<', a bullet-list '*' colliding with an in-line '*emphasis*' on the same
    line, or a malformed markdown fragment produced UNBALANCED HTML with no validation.
    Telegram then silently rejected the entire message (400 error).

    The fix: escape user content FIRST (so nothing the model typed can ever be mistaken
    for one of our own tags), convert list markers to a safe bullet BEFORE the italic
    regex runs (removes the '* item' vs '*emphasis*' ambiguity), then validate tag
    balance at the end -- if anything is still unbalanced, fail SOFT to plain escaped
    text instead of ever handing Telegram malformed HTML.
    """
    if not text:
        return ""

    # STEP 1: escape raw content FIRST. Everything after this point that introduces a
    # '<' or '>' is OUR OWN deliberately-inserted tag, never something the model typed.
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # Horizontal rules: --- or *** or ___ -> short Unicode separator (shortened from 18
    # to 10 chars — long dash runs are exactly what wraps/splits badly on a phone screen)
    text = _re.sub(r'(?m)^[-*_]{3,}\s*$', '─' * 10, text)
    # Also catch Unicode box-drawing lines the AI models love: ──── ━━━━ ════ etc.
    text = _re.sub(r'(?m)^[─━═]{4,}\s*$', '─' * 10, text)

    # Markdown tables: pipe-delimited rows with | separator row (|---|---|).
    # Convert to bold header row + "key: value" per data row — Telegram has no tables.
    # Process from bottom-up so line removals don't invalidate earlier line nums.
    lines = text.split('\n')
    table_indices = []  # [start_line, header_line, sep_line, end_line]
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('|') and stripped.endswith('|') and i + 1 < len(lines):
            # Check if next line is a separator row
            sep = lines[i + 1].strip()
            if _re.match(r'^\|[-:\s|]+\|$', sep):
                # Found a table header + separator; now scan data rows
                start = i
                header = i
                i += 2  # skip header + separator
                while i < len(lines) and lines[i].strip().startswith('|') and lines[i].strip().endswith('|'):
                    i += 1
                table_indices.append((start, header, i))
                continue
        i += 1
    # Replace tables in reverse order
    for start, header, end in reversed(table_indices):
        hdr_cells = [c.strip() for c in lines[header].strip().strip('|').split('|')]
        rows = []
        for j in range(start + 2, end):
            cells = [c.strip() for c in lines[j].strip().strip('|').split('|')]
            row_parts = []
            for k, cell in enumerate(cells):
                col_name = hdr_cells[k].strip() if k < len(hdr_cells) else f"Col {k}"
                if cell and col_name:
                    row_parts.append(f'<b>{col_name}</b>: {cell}')
            if row_parts:
                rows.append('  '.join(row_parts))
        replacement = '\n'.join(rows) if rows else ''
        lines[start:end] = [replacement]
    text = '\n'.join(lines)

    # Headers -> emoji + bold (before bold/italic, safe now that '<'/'>' are gone).
    # No more ───── dash borders: on a narrow phone screen they wrap onto their own
    # line (or split mid-word) and just look broken instead of decorative.
    text = _re.sub(r'(?m)^#{5,6}\s+(.+)$', lambda m: f'<b>📌 {m.group(1).strip()}</b>', text)
    text = _re.sub(r'(?m)^####\s+(.+)$', lambda m: f'<b>📌 {m.group(1).strip()}</b>', text)
    text = _re.sub(r'(?m)^###\s+(.+)$', lambda m: f'<b>✦ {m.group(1).strip()}</b>', text)
    text = _re.sub(r'(?m)^##\s+(.+)$', lambda m: f'<b>🏛️ {m.group(1).strip()}</b>', text)
    text = _re.sub(r'(?m)^#\s+(.+)$', lambda m: f'<b>📍 {m.group(1).strip()}</b>', text)

    # Numbered lists: '1. item' / '2. item' -> plain '1. item' with bold number
    # (Telegram doesn't support <ol>, so we just keep the numbers readable)
    text = _re.sub(r'(?m)^(\s*)(\d+)\.\s+(.+)$', lambda m: f"{m.group(1)}<b>{m.group(2)}.</b> {m.group(3)}", text)

    # List markers ('- item' / '* item') -> a plain bullet, BEFORE the italic regex runs.
    # Key fix: a leading '*' on a bullet line used to be indistinguishable from an
    # opening '*emphasis*' marker and could pair up with an unrelated '*' elsewhere,
    # producing garbage <i> spans.
    text = _re.sub(r'(?m)^(\s*)[-*]\s+', r'\1• ', text)

    # Bold: **text** or __text__
    text = _re.sub(r'\*\*(.+?)\*\*', lambda m: f'<b>{m.group(1)}</b>', text)
    text = _re.sub(r'__(.+?)__', lambda m: f'<b>{m.group(1)}</b>', text)
    # Italic: *text* or _text_ (single markers only, never spanning a newline)
    text = _re.sub(r'(?<!\*)\*(?!\*)([^\n*]+?)(?<!\*)\*(?!\*)', lambda m: f'<i>{m.group(1)}</i>', text)
    text = _re.sub(r'(?<!_)_(?!_)([^\n_]+?)(?<!_)_(?!_)', lambda m: f'<i>{m.group(1)}</i>', text)
    # Code: `text`
    text = _re.sub(r'`([^\n`]+?)`', lambda m: f'<code>{m.group(1)}</code>', text)
    # Strikethrough: ~~text~~
    text = _re.sub(r'~~(.+?)~~', lambda m: f'<s>{m.group(1)}</s>', text)
    # Spoiler: ||text||
    text = _re.sub(r'\|\|(.+?)\|\|', lambda m: f'<span class="tg-spoiler">{m.group(1)}</span>', text)
    # Blockquote: > text at start of line (note: '>' was already escaped to &gt; in STEP 1)
    text = _re.sub(r'(?m)^&gt;(.+)$', lambda m: f'<blockquote>{m.group(1)}</blockquote>', text)
    # Links: [text](url) -- url was already HTML-escaped in STEP 1, which is correct
    # for an HTML attribute value.
    text = _re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)

    # STEP FINAL: never hand Telegram malformed HTML. If our own substitutions somehow
    # produced an unbalanced tag (overlapping emphasis, an odd number of markers, etc.),
    # strip every tag we generated and fall back to safe, readable plain text instead of
    # letting the whole message get silently rejected.
    if not _tags_are_balanced(text):
        logger.warning("[md_to_html] Unbalanced tags detected, falling back to plain text")
        text = _TAG_SCAN_RE.sub('', text)

    return text


def _chunk_html(html_text: str, max_len: int = 3900) -> list:
    """C3 fix: the old chunker cut purely by character count, which could slice a
    message in half in the MIDDLE of an HTML tag (or leave a tag open with its closer
    in the next chunk) -- producing a second, independently-invalid chunk that Telegram
    rejects. This version treats tags as atomic units, tracks which tags are currently
    open, and when it has to cut, closes every open tag at the end of the current chunk
    and re-opens the SAME tags (with their original attributes, e.g. 'blockquote
    expandable') at the start of the next one -- so every chunk sent is independently
    well-formed HTML."""
    if not html_text:
        return []
    if len(html_text) <= max_len:
        return [html_text]

    segments = []  # ('tag', full_tag_text) | ('text', content)
    last_end = 0
    for m in _TAG_SCAN_RE.finditer(html_text):
        if m.start() > last_end:
            segments.append(('text', html_text[last_end:m.start()]))
        segments.append(('tag', m.group(0)))
        last_end = m.end()
    if last_end < len(html_text):
        segments.append(('text', html_text[last_end:]))

    chunks = []
    stack = []  # list of (tag_name, original_open_tag_text)
    current = ""

    def closing_overhead():
        return sum(len(name) + 3 for name, _ in stack)  # len('</' + name + '>')

    def flush():
        nonlocal current
        if not current:
            return
        closer = ''.join(f'</{name}>' for name, _ in reversed(stack))
        chunks.append(current + closer)
        current = ''.join(open_text for _, open_text in stack)

    for kind, content in segments:
        if kind == 'tag':
            is_close = content.startswith('</')
            name_match = _re.match(r'</?([a-zA-Z]+)', content)
            name = name_match.group(1).lower() if name_match else ''
            if len(current) + len(content) + closing_overhead() > max_len:
                flush()
            current += content
            if not is_close:
                stack.append((name, content))
            elif stack and stack[-1][0] == name:
                stack.pop()
        else:
            remaining = content
            while remaining:
                room = max_len - len(current) - closing_overhead()
                if room <= 0:
                    flush()
                    room = max_len - len(current) - closing_overhead()
                if len(remaining) <= room:
                    current += remaining
                    remaining = ""
                else:
                    # Prefer a paragraph break, then a line break, then a word boundary —
                    # never split a word in half (old version rfind'd only '\n' and, if
                    # none was nearby, hard-cut at an arbitrary character offset, which is
                    # how "ГЛУБИНЫ" ended up split into "ГЛУБИ" / "НЫ" across two messages).
                    window = remaining[:max(room, 1)]
                    cut = window.rfind('\n\n')
                    if cut <= 0:
                        cut = window.rfind('\n')
                    if cut <= 0:
                        cut = window.rfind(' ')
                    if cut <= 0:
                        cut = max(room, 1)  # genuinely no break found — last-resort hard cut
                    current += remaining[:cut]
                    remaining = remaining[cut:].lstrip('\n ')
                    flush()

    if current:
        closer = ''.join(f'</{name}>' for name, _ in reversed(stack))
        chunks.append(current + closer)

    return chunks


async def _send_long_blockquote(bot_instance, chat_id: int, html_content: str, message_thread_id: int = None):
    """Send HTML content as expandable blockquote(s), splitting on paragraph
    boundaries when too long for a single Telegram message (~4096 chars).
    
    - ALL chunks use <blockquote expandable>...</blockquote> (every part
      of the narrative/world description is collapsible)
    - Splits on blank lines (\\n\\n) — never mid-paragraph
    - If a single paragraph exceeds 4000 chars, hard-splits it on \\n as fallback
    - On timeout: retries with exponential backoff, then tries smaller chunks
    - Falls back to plain text if blockquote format keeps failing

    BUG #2 FIX: `message_thread_id` routes the message to the correct forum topic
    when the bot is running in a forum-style chat. None means use the chat default
    (i.e. the main group chat)."""
    if not html_content or not html_content.strip():
        return
    
    MAX_LEN = 3900  # leave room for <blockquote expandable>\n...\n</blockquote> tags
    
    # Split into paragraphs (preserve blank-line separators)
    paragraphs = html_content.split("\n\n")
    
    # Group paragraphs into chunks under MAX_LEN
    chunks = []
    current = ""
    for para in paragraphs:
        # If a single paragraph is itself too long, hard-split on \n
        if len(para) > MAX_LEN:
            if current:
                chunks.append(current)
                current = ""
            sub_lines = para.split("\n")
            sub_chunk = ""
            for line in sub_lines:
                if len(sub_chunk) + len(line) + 1 > MAX_LEN:
                    if sub_chunk:
                        chunks.append(sub_chunk)
                    sub_chunk = line
                else:
                    sub_chunk = sub_chunk + "\n" + line if sub_chunk else line
            if sub_chunk:
                current = sub_chunk
            continue
        
        candidate = current + "\n\n" + para if current else para
        if len(candidate) > MAX_LEN:
            if current:
                chunks.append(current)
            current = para
        else:
            current = candidate
    
    if current:
        chunks.append(current)
    
    total_chunks = len(chunks)
    
    # Send each chunk wrapped in expandable blockquote — ALL parts of the
    # narrative/world description must be collapsible, not just the first one.
    for i, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        
        # ── Continuation markers for multi-chunk narratives ──
        if total_chunks > 1:
            if i == 0:
                chunk = chunk + f"\n\n<i>\u25b8 часть 1 из {total_chunks}</i>"
            else:
                cont_marker = f"<i>\u25b8 продолжение ({i+1}/{total_chunks})</i>"
                chunk = cont_marker + "\n\n" + chunk
                if i == total_chunks - 1:
                    chunk = chunk + f"\n\n<i>\u25b8 конец</i>"

        async def _try_send(text: str, timeout: float = 60.0, use_blockquote: str = "expandable") -> bool:
            """Try sending a single chunk. Returns True on success."""
            if use_blockquote == "expandable":
                wrapped = f"<blockquote expandable>\n{text}\n</blockquote>"
            elif use_blockquote == "simple":
                wrapped = f"<blockquote>\n{text}\n</blockquote>"
            else:
                wrapped = text  # plain text fallback

            # BUG #2: build send_kwargs once, include message_thread_id only when set
            send_kwargs = {"chat_id": chat_id, "text": wrapped}
            if use_blockquote != "plain":
                send_kwargs["parse_mode"] = "HTML"
            if message_thread_id:
                send_kwargs["message_thread_id"] = message_thread_id

            for attempt in range(3):
                try:
                    await asyncio.wait_for(
                        bot_instance.send_message(**send_kwargs),
                        timeout=timeout,
                    )
                    return True
                except asyncio.TimeoutError:
                    logger.warning(f"_send_long_blockquote: chunk {i} attempt {attempt+1} timed out (timeout={timeout}s)")
                    if attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))  # 2s, 4s
                except Exception as e:
                    if "Timed out" in str(e) or "timed out" in str(e).lower():
                        logger.warning(f"_send_long_blockquote: chunk {i} attempt {attempt+1} API timeout")
                        if attempt < 2:
                            await asyncio.sleep(2 ** (attempt + 1))
                    else:
                        logger.warning(f"_send_long_blockquote: chunk {i} failed: {e}")
                        return False  # non-timeout error — don't retry this format
            return False

        # Strategy 1: expandable blockquote with full chunk
        if await _try_send(chunk, timeout=60.0, use_blockquote="expandable"):
            continue

        # Strategy 2: if chunk is large, split it in half and try each part
        if len(chunk) > 2000:
            mid = chunk.rfind("\n", len(chunk) // 4, 3 * len(chunk) // 4)
            if mid == -1:
                mid = len(chunk) // 2
            first_half = chunk[:mid].strip()
            second_half = chunk[mid:].strip()
            half_ok = False
            if first_half:
                if await _try_send(first_half, timeout=60.0, use_blockquote="expandable"):
                    half_ok = True
                else:
                    # Try plain text for this half
                    try:
                        plain = _TAG_SCAN_RE.sub('', first_half)
                        await bot_instance.send_message(chat_id=chat_id, text=plain)
                        half_ok = True
                    except Exception:
                        pass
            if second_half:
                if not await _try_send(second_half, timeout=60.0, use_blockquote="expandable"):
                    # Try plain text for second half
                    try:
                        plain = _TAG_SCAN_RE.sub('', second_half)
                        await bot_instance.send_message(chat_id=chat_id, text=plain)
                    except Exception:
                        pass
            if half_ok or first_half:
                continue

        # Strategy 3: simple (non-expandable) blockquote
        if await _try_send(chunk, timeout=60.0, use_blockquote="simple"):
            continue

        # Strategy 4: plain text (no blockquote) — last resort
        try:
            plain = _TAG_SCAN_RE.sub('', chunk)
            send_kwargs = {"chat_id": chat_id, "text": plain}
            if message_thread_id:
                send_kwargs["message_thread_id"] = message_thread_id
            await bot_instance.send_message(**send_kwargs)
        except Exception:
            pass


async def send_safe(update_obj, text: str, parse_html: bool = True, raw_html: bool = False,
                   source: str = "system", reply_markup=None, message_thread_id: int = None,
                   auto_delete: float = 0) -> list:
    """Send text to Telegram -- converts Markdown to HTML, logs to Markdown journal.
    Chunks long messages automatically (tag-aware, see _chunk_html). Falls back to
    plain text on HTML errors. Respects message_thread_id for forum/topic chats.

    BUG #2 FIX: If `message_thread_id` is not explicitly provided, the function tries
    (in this order) to discover one from:
      1. `update_obj.message.message_thread_id` — set by Telegram for forum/topic chats.
      2. The persisted session row (sessions.message_thread_id) — captured on the
         first command we saw in this chat, so async/background sends can still find
         the right topic even when they don't have an Update to read from.

    auto_delete: if > 0, auto-delete the sent message(s) after this many seconds.
    Returns list of sent message_ids (may be empty on failure)."""
    if not update_obj or not update_obj.effective_chat:
        logger.warning("send_safe: no effective_chat, skipping")
        return []

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

    if not html_text or not html_text.strip():
        return []

    # Auto-detect thread_id from update (forum/topic chats)
    if message_thread_id is None and update_obj.message and hasattr(update_obj.message, 'message_thread_id'):
        message_thread_id = update_obj.message.message_thread_id

    # BUG #2: fall back to the persisted thread_id on the session — this is what
    # async/background sends (via _ChatHandle) rely on, since they don't have a
    # real Update with .message.
    if not message_thread_id and session and getattr(session, 'message_thread_id', 0):
        message_thread_id = session.message_thread_id or None

    # If we just discovered a thread_id from the update but the session row has a
    # different one (or none), persist the new one so future background sends can
    # find it. Skip the write if it's already correct (avoids needless DB churn).
    if session_id and message_thread_id and session and \
            (session.message_thread_id or 0) != int(message_thread_id):
        try:
            db = db_manager.get_db(session_id)
            db.set_session_thread_id(session_id, int(message_thread_id))
        except Exception as _e:
            logger.debug(f"send_safe: failed to persist message_thread_id: {_e}")

    chunks = _chunk_html(html_text, max_len=3900)

    send_kwargs = {}
    if reply_markup:
        send_kwargs["reply_markup"] = reply_markup
    if message_thread_id:
        send_kwargs["message_thread_id"] = message_thread_id

    sent_ids = []
    for chunk in chunks:
        try:
            msg = await update_obj.effective_chat.send_message(chunk, parse_mode="HTML", **send_kwargs)
            sent_ids.append(msg.message_id)
        except Exception as e:
            logger.error(f"HTML send failed: {e}. Chunk preview: {chunk[:200]}")
            try:
                plain = _TAG_SCAN_RE.sub('', chunk)
                msg = await update_obj.effective_chat.send_message(plain, **send_kwargs)
                sent_ids.append(msg.message_id)
            except Exception as e2:
                logger.error(f"Plain send failed: {e2}")
                if session_id:
                    md_logger.log(session_id, "error", f"HTML: {e} | Plain: {e2}")

    # Auto-delete after delay
    if auto_delete > 0 and sent_ids:
        # Capture bot reference for deletion — _ChatHandle stores it as _bot,
        # real Update objects expose it via .effective_chat.get_bot() or via
        # the bot attribute on the underlying chat object.
        _del_bot = None
        if hasattr(update_obj, '_bot') and update_obj._bot:
            _del_bot = update_obj._bot
        elif hasattr(update_obj.effective_chat, '_bot') and update_obj.effective_chat._bot:
            _del_bot = update_obj.effective_chat._bot
        elif hasattr(update_obj.effective_chat, 'get_bot'):
            try:
                _del_bot = update_obj.effective_chat.get_bot()
            except Exception:
                pass
        if _del_bot:
            async def _auto_del(bot_ref=_del_bot, chat_id_ref=chat_id, ids=sent_ids, delay=auto_delete):
                await asyncio.sleep(delay)
                for mid in ids:
                    try:
                        await bot_ref.delete_message(chat_id=chat_id_ref, message_id=mid)
                    except Exception:
                        pass
            asyncio.create_task(_auto_del())

    return sent_ids


def _read_sheet_text(fp: str) -> str:
    """Пробуем UTF-8 → cp1251 → latin-1 → cp1252"""
    p = Path(fp)
    for enc in ("utf-8", "cp1251", "latin-1", "cp1252"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(f"Cannot decode {fp}: tried utf-8, cp1251, latin-1, cp1252")


async def _dm_only_check(update: Update, command_name: str) -> bool:
    """If command should only work in DM, send hint and return True."""
    if command_name in ANTISPAM_DM_ONLY_COMMANDS and update.effective_chat.type != "private":
        await update.message.reply_text("ℹ️ Ответ будет отправлен в личные сообщения.")
        return True
    return False


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


def get_session(chat_id: int) -> Session | None:
    return db_manager.get_session_by_chat(chat_id)


async def world_gen_guard(update, session) -> bool:
    """BUG 3 FIX: block player turns before/during world generation.

    Returns True (and sends an explanatory message) when the player may NOT act:
      - мир прямо сейчас генерируется (/dndcychwyn или genworld в процессе), или
      - мир ещё вообще не сгенерирован (session.current_scene пуст — он заполняется
        только в generate_world, т.е. это надёжный персистентный маркер "мир готов").

    Call it in every player-turn entry point (Дн., /rholio, /gofyn, /dbgofyn, /gwneud)
    right after the player-membership check."""
    from libs.handlers._state import is_world_generating
    if not session:
        return False
    if is_world_generating(session.id):
        await send_safe(update,
            "🌍 **Мир сейчас генерируется** — подожди пару минут, ходы принимаются только после начала игры."
        )
        return True
    if not (getattr(session, "current_scene", "") or "").strip():
        await send_safe(update,
            "🌍 **Мир ещё не создан.** Дождись, пока админ запустит генерацию (`/dndcychwyn`) — "
            "ходить можно только после начала игры."
        )
        return True
    return False


def get_billing_plugin(ctx) -> Optional[object]:
    """ИТЕРАЦИЯ 10 (Раздел 7): ЕДИНСТВЕННЫЙ безопасный способ достать
    опциональный плагин из хендлера.

    Возвращает None, если: ctx нет / bot_data пуст / менеджер не положен
    bootstrap'ом / плагин billing удалён из plugins/ / выключен в
    plugins.toml / не загрузился при старте. Во всех случаях вызывающий
    код просто пропускает проверки — бот работает как бесплатный.
    НИКОГДА не бросает исключений и НИКОГДА не импортирует plugins.*
    напрямую (тот падал бы ModuleNotFoundError при удалении папки).

    Usage:
        billing = get_billing_plugin(ctx)
        if billing is not None:
            allowed, msg = await billing.check_game_allowed(update, ctx, session)
            if not allowed:
                await send_safe(update, msg)
                return
    """
    try:
        bot_data = getattr(ctx, "bot_data", None) if ctx else None
        manager = bot_data.get("plugin_manager") if isinstance(bot_data, dict) else None
        return manager.get_plugin("billing") if manager else None
    except Exception as e:
        logger.debug(f"get_billing_plugin unavailable: {e}")
        return None


def is_world_created(session: Session) -> bool:
    """ИТЕРАЦИЯ 10 (Раздел 2): персистентный маркер «мир уже сгенерирован».

    world_gen_guard выше использует тот же признак: session.current_scene
    заполняется ТОЛЬКО в generate_world (см. libs/session/generators.py), т.е.
    это надёжный персистентный флаг «мир готов» (in-memory
    is_world_generating — только на время генерации, для этого не годится)."""
    return bool(session and (getattr(session, "current_scene", "") or "").strip())


def has_any_round_history(session_id: str) -> bool:
    """ИТЕРАЦИЯ 10 (Раздел 3): был ли в сессии хоть один разрешённый ход.

    «Ход» = нарратив Мастера в истории (entry_type="narrative"). Записи
    "system" ([DNDSTART]/[AUTO_START]) сюда не считаются: между «мир создан»
    и «первый ход игрока» есть окно, когда нарративов ещё нет вообще —
    именно его отсекаем. При ошибке чтения истории НЕ блокируем (True)."""
    try:
        db = db_manager.get_db(session_id)
        return bool(db.get_history_by_type(session_id, "narrative", limit=1))
    except Exception:
        return True


def get_thread_id_for_session(session_id: Optional[str], fallback: int = None) -> Optional[int]:
    """BUG #2: Helper for async/background send paths that don't have a real Update
    to read `message_thread_id` from. Reads the persisted thread_id from the
    session row, falling back to `fallback` (typically `update.message.message_thread_id`
    captured at the entry point). Returns None when neither is set so callers can
    pass it as `message_thread_id=None` to `bot.send_message` without surprises."""
    if not session_id:
        return fallback if fallback else None
    try:
        db = db_manager.get_db(session_id)
        sess = db.get_session(session_id)
        if sess and getattr(sess, 'message_thread_id', 0):
            return int(sess.message_thread_id)
    except Exception as _e:
        logger.debug(f"get_thread_id_for_session: lookup failed: {_e}")
    return fallback if fallback else None


def capture_thread_id(update: Update, session_id: Optional[str]) -> Optional[int]:
    """BUG #2: Read `message_thread_id` from the incoming update and persist it to
    the session so every async background send can find it later. Safe to call on
    every command / message — writes only if the value changed."""
    if not update or not update.message:
        return None
    tid = getattr(update.message, 'message_thread_id', None)
    if not tid:
        return None
    if session_id:
        try:
            db = db_manager.get_db(session_id)
            sess = db.get_session(session_id)
            if not sess or (sess.message_thread_id or 0) != int(tid):
                db.set_session_thread_id(session_id, int(tid))
        except Exception as _e:
            logger.debug(f"capture_thread_id: persist failed: {_e}")
    return int(tid)


def fmt_players(players) -> str:
    return "\n".join(
        f"{i}. {'👑' if p.is_creator else '🎮'} {p.display_name} (@{p.username})"
        for i, p in enumerate(players, 1)
    )


async def _keep_typing(update_obj, stop_event: asyncio.Event):
    """Refresh Telegram's 'typing...' indicator every ~4.5s (it only lasts ~5s on its
    own) for as long as the Master is still generating — otherwise a 10-30s tool-calling
    turn just looks like the bot went silent/crashed."""
    while not stop_event.is_set():
        try:
            await update_obj.effective_chat.send_action(action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass
