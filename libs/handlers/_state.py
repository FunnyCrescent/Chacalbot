"""
libs.handlers._state — общий стейт для всех подмодулей обработчиков.

Раньше эти объекты жили как module-level globals в libs/bot_handlers.py
(6969 строк). После распила на 12+ подмодулей каждый подмодуль должен
иметь доступ к одному и тому же экземпляру DatabaseManager, DMEngine,
SessionManager и т.д. Без этого центрального файла подмодули пришлось
бы связывать циклическими импортами.

Инициализация происходит один раз при первом импорте (автоматически).
После этого любой подмодуль делает:
    from libs.handlers._state import sessions, db_manager, dm_engine, ...
и получает те же экземпляры.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from libs.config_legacy import (
    BASE_DIR,
    CHARACTERS_DIR,
    LOG_PATH,
    SAVED_CHARS_DB,
)
from libs.ai_client import DMEngine
from libs.db import DatabaseManager, SavedCharsDB
from libs.session_manager import SessionManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# MarkdownLogger — вынесен сюда из bot_handlers.py (был module-level)
# ─────────────────────────────────────────────────────────────────

class MarkdownLogger:
    """Logs all bot activity to a Markdown file per session"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _get_path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"session_{session_id}.md")

    def log(self, session_id: str, source: str, text: str):
        if not session_id:
            return
        path = self._get_path(session_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if source == "player":
            prefix = f"**[{timestamp}] Игрок:**"
        elif source == "master":
            prefix = f"**[{timestamp}] Нейросеть:**"
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
        if not session_id:
            return
        path = self._get_path(session_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n**[{timestamp}] {label}:**\n\n```\n{content}\n```\n\n---\n")


# ─────────────────────────────────────────────────────────────────
# _ChatHandle — Minimal Update-like shim (вынесен из bot_handlers.py)
# ─────────────────────────────────────────────────────────────────

class _ChatHandle:
    """Minimal Update-like shim exposing just .effective_chat.id / .send_message /
    .send_action, backed directly by a bot+chat_id. send_safe and _resolve_and_send
    only ever touch update_obj.effective_chat, so this is a drop-in stand-in for a real
    Update — needed because the async Master/DB-Bot split means the DB-Bot's completion
    notice (and any round it unblocks) fires well after the Telegram Update that
    triggered the round is gone; ctx.bot itself is a long-lived object, so capturing
    just (bot, chat_id) is enough to keep talking to the same chat later.

    BUG #2 FIX: This shim now preserves `message_thread_id` so async/background sends
    (post-`_resolve_and_send`, `_run_db_bot_background` completion notice,
    `_resolve_pc_combat_turn` results) still land in the same forum/topic chat
    as the original command instead of falling back to the main group chat.
    """

    def __init__(self, bot, chat_id: int, message_thread_id: Optional[int] = None):
        self._bot = bot
        self.effective_chat = self
        self.id = chat_id
        # Telegram forum/topic thread id — captured from the original Update so
        # async sends (DB-Bot phase, combat turn resolution, etc.) go back to
        # the same topic where the player issued their command.
        self.message_thread_id = message_thread_id
        # send_safe touches update_obj.message for message_thread_id detection on
        # forum/topic chats. Real Update objects have .message; expose a tiny
        # shim with the captured thread_id so send_safe can pick it up.
        self.message = _MsgThreadShim(message_thread_id) if message_thread_id else None

    async def send_message(self, text, parse_mode=None, reply_markup=None, message_thread_id=None):
        tid = message_thread_id if message_thread_id is not None else self.message_thread_id
        kwargs = {"chat_id": self.id, "text": text}
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if tid:
            kwargs["message_thread_id"] = tid
        return await self._bot.send_message(**kwargs)

    async def send_action(self, action):
        return await self._bot.send_chat_action(chat_id=self.id, action=action)


class _MsgThreadShim:
    """Tiny stand-in for `update.message` so send_safe can auto-detect the
    forum topic thread id from a `_ChatHandle` (which doesn't have a real
    Update.message object)."""

    __slots__ = ("message_thread_id",)

    def __init__(self, message_thread_id: Optional[int]):
        self.message_thread_id = message_thread_id


# ─────────────────────────────────────────────────────────────────
# Синглтон-стейт
# ─────────────────────────────────────────────────────────────────

db_manager: Optional[DatabaseManager] = None
dm_engine: Optional[DMEngine] = None
sessions: Optional[SessionManager] = None
saved_chars_db: Optional[SavedCharsDB] = None
md_logger: Optional[MarkdownLogger] = None

# Mutable state (раньше жили как module-level globals в bot_handlers.py)
_lang_translations: Dict[int, list] = {}
_lang_round_counter: Dict[int, int] = {}
_active_combat_loops: Dict[str, bool] = {}
_combat_recovery_sessions: List[str] = []
_pending_button_rolls: Dict[str, dict] = {}
_genre_selections: Dict[int, str] = {}

# БОЙ-FIX: single-flight guard для авто-резолва подряд идущих NPC-ходов
# (_auto_resolve_npcs_then_pc). Раньше два игрока, отправившие «Дн.» во время
# хода стражника, порождали ДВА параллельных авто-резолвера — каждый своим
# вызовом Мастера резолвил один и тот же ход NPC, в ленту летели два
# противоречащих GM_SECRET+нарратива (двойные броски, расходящиеся HP).
_auto_npc_resolving: Dict[str, bool] = {}

# BUG 3 FIX: глобальный флаг "мир генерируется" (per-session). Пока он поднят —
# игроки не могут ходить (Дн./rholio/gofyn/gwneud), и повторный запуск генерации
# (/dndcychwyn, genworld: callback) отклоняется. In-memory, как и остальные флаги
# рантайма: генерация — синхронный процесс внутри этого же процесса, так что
# переживать перезапуск нечему.
_world_generating: Dict[str, dict] = {}  # session_id -> {"chat_id": int, "started": float}


def mark_world_generating(session_id: str, chat_id: int = 0) -> None:
    """Поднять флаг генерации мира для сессии (в начале /dndcychwyn или genworld)."""
    import time as _time
    _world_generating[session_id] = {"chat_id": chat_id, "started": _time.time()}
    logger.info(f"[worldgen] generation STARTED for session {session_id} (chat {chat_id})")


def clear_world_generating(session_id: str) -> None:
    """Снять флаг генерации мира (в finally — даже при ошибке генерации)."""
    if _world_generating.pop(session_id, None) is not None:
        logger.info(f"[worldgen] generation FINISHED for session {session_id}")


def is_world_generating(session_id: str) -> bool:
    """True, если мир этой сессии прямо сейчас генерируется."""
    return session_id in _world_generating


def init_state() -> None:
    """Инициализация синглтонов. Идемпотентна."""
    global db_manager, dm_engine, sessions, saved_chars_db, md_logger
    if db_manager is not None:
        return

    for d in [os.path.join(BASE_DIR, "logs"),
              os.path.join(BASE_DIR, "data"),
              CHARACTERS_DIR,  # MD/characters — БЫВШИЙ хардкод «characters» в корне удалён (ТЗ MD-консолидация)
              os.path.join(BASE_DIR, "logs", "markdown")]:
        os.makedirs(d, exist_ok=True)

    # MD-хранилище: структура + первичная инициализация MD/_applied/ (первый деплой).
    try:
        from libs.md_store import ensure_md_layout
        ensure_md_layout()
    except Exception as e:
        logger.warning(f"[md_store] ensure_md_layout failed: {e}")

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(LOG_PATH),
                logging.StreamHandler(),
            ],
        )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    db_manager = DatabaseManager(os.path.join(BASE_DIR, "data", "sessions"))
    dm_engine = DMEngine(db_manager=db_manager)
    sessions = SessionManager(db_manager, dm_engine)
    saved_chars_db = SavedCharsDB(SAVED_CHARS_DB)
    md_logger = MarkdownLogger(os.path.join(BASE_DIR, "logs", "markdown"))

    logger.info("[handlers._state] initialized: db_manager, dm_engine, sessions, saved_chars_db, md_logger")


# Авто-инициализация при импорте модуля
init_state()
