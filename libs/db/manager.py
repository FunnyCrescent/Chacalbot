"""DatabaseManager — multi-session manager (один Database на session_id)."""
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from .database import Database
from .models import Session, Player

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Factory + registry: one Database per session."""

    def __init__(self, base_dir: str = "data/sessions"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Database] = {}
        self._chat_to_session: Dict[int, str] = {}
        self._load_active_sessions()

    def _db_path(self, session_id: str) -> str:
        return str(self.base_dir / f"{session_id}.db")

    def get_db(self, session_id: str) -> Database:
        if session_id not in self._cache:
            self._cache[session_id] = Database(self._db_path(session_id))
        return self._cache[session_id]

    def _load_active_sessions(self):
        for db_file in self.base_dir.glob("*.db"):
            sid = db_file.stem
            try:
                db = Database(str(db_file))
                for session in db.get_all_active_sessions():
                    self._chat_to_session[session.chat_id] = session.id
            except Exception:
                continue

    def get_session_by_chat(self, chat_id: int) -> Optional[Session]:
        sid = self._chat_to_session.get(chat_id)
        if sid:
            return self.get_db(sid).get_session(sid)
        return None

    def create_session(self, session: Session) -> Session:
        db = self.get_db(session.id)
        db.create_session(session)
        self._chat_to_session[session.chat_id] = session.id
        return session

    def get_all_active_sessions(self) -> List[Session]:
        result = []
        for sid in list(self._chat_to_session.values()):
            s = self.get_db(sid).get_session(sid)
            if s and s.status == "active":
                result.append(s)
        return result

    def end_session(self, session_id: str):
        db = self.get_db(session_id)
        session = db.get_session(session_id)
        if session:
            db.end_session(session_id)
            db.clear_queue_state(session_id)
            self._chat_to_session.pop(session.chat_id, None)

    def drop_session(self, session_id: str) -> bool:
        """BUG #7 + #10: physically delete the session's per-session .db file AND evict
        every trace of it from the in-memory cache. Returns True on success, False
        if the session file didn't exist. Called from /dileu AFTER
        clear_session_runtime_state + end_session — the goal is that after /dileu the
        session is GONE: no /ailddechrau, no recovery, no orphaned .db file.

        Note: closes the cached Database handle (releasing its SQLite connection)
        before unlinking the file — otherwise Windows / sqlite3 holding the file
        open would fail the unlink."""
        session = None
        db = self._cache.get(session_id)
        if db:
            session = db.get_session(session_id)
        # Close cached handle so the OS will let us unlink the .db file.
        if session_id in self._cache:
            try:
                # sqlite3 connections close themselves on garbage collection, but we
                # force-close any active connection by removing our reference.
                self._cache.pop(session_id, None)
            except Exception as e:
                logger.warning(f"drop_session: failed to evict cached handle for {session_id}: {e}")
        # Remove chat_id mapping.
        if session:
            self._chat_to_session.pop(session.chat_id, None)
        # Unlink the .db file (and any -wal / -shm sidecar files).
        db_path = self._db_path(session_id)
        removed = False
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = f"{db_path}{suffix}"
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
                    removed = True
            except OSError as e:
                logger.warning(f"drop_session: failed to remove {candidate}: {e}")
        # MD-консолидация: удалить вместе с БД и per-session MD-копию
        # (data/sessions/<sid>/MD/) — /dileu убирает сессию целиком.
        try:
            from libs.md_store import cleanup_session_md
            cleanup_session_md(session_id)
        except Exception as e:
            logger.warning(f"drop_session: md cleanup failed for {session_id}: {e}")
        return removed

    def get_last_ended_session_by_chat(self, chat_id: int) -> Optional[Session]:
        """Scans every known session db for the most recently ended session in this
        chat. Not indexed globally (sessions are one-per-file), but session counts per
        chat are small enough that this is fine."""
        candidates = []
        for db_file in self.base_dir.glob("*.db"):
            try:
                db = self.get_db(db_file.stem)
                s = db.get_last_ended_session_by_chat(chat_id)
                if s:
                    candidates.append(s)
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.updated_at, reverse=True)
        return candidates[0]

    def resume_session(self, session_id: str) -> Optional[Session]:
        """Reactivate an ended session and restore its chat_id -> session_id routing so
        get_session_by_chat finds it again."""
        db = self.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return None
        db.reactivate_session(session_id)
        self._chat_to_session[session.chat_id] = session_id
        # ИТЕРАЦИЯ 15: сессия снова активна — снимаем запрет генераций
        # (id сессии при resume не меняется, а флаг отмены глобальный).
        try:
            from libs.session.generation_guard import clear_cancel
            clear_cancel(session_id)
        except Exception:
            pass
        # MD-консолидация: end_session удалил per-session MD-копию — переснимаем
        # из MD/_applied/ (состояние на момент возобновления). Это единственный
        # случай, когда копия существующей сессии создаётся заново.
        try:
            from libs.md_store import snapshot_for_session
            snapshot_for_session(session_id)
        except Exception as e:
            logger.warning(f"[md_store] re-snapshot on resume failed for {session_id}: {e}")
        return db.get_session(session_id)

    def clear_history(self, session_id: str):
        self.get_db(session_id).clear_history(session_id)

    def get_user_sessions(self, user_id: int) -> List[Session]:
        """Return all active sessions where the user is a player."""
        result = []
        for sid, db in self._cache.items():
            try:
                session = db.get_session(sid)
                if session and session.status == "active":
                    player = db.get_player(user_id, sid)
                    if player:
                        result.append(session)
            except Exception:
                continue
        return result

