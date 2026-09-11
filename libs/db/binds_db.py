"""
BindsDB — персональные псевдонимы команд (/rhwymo), Фича 7.

Глобальная БД data/binds.db. Каждый бинд принадлежит ОДНОМУ пользователю
(user_id) и работает только для него: /rhwymo cymeriad character →
пользователь пишет /character, и для НЕГО это /cymeriad. Для остальных
игроков /character так и останется неизвестной командой.

Ограничения (по ТЗ):
  - создавать бинды можно ТОЛЬКО в ЛС бота (проверка в хендлере);
  - нельзя биндить уже зарегистрированные команды бота (ни в качестве цели,
    ни в качестве псевдонима);
  - псевдоним: 2-32 символа, [a-z0-9_] (без пробелов, эмодзи, слэшей);
  - нечувствительность к регистру — alias всегда хранится в нижнем регистре.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

ALIAS_RE = re.compile(r"^[a-z0-9_]{2,32}$")


class BindsDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS command_binds (
                    user_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    target TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, alias)
                )
            """)
            conn.commit()
        logger.info(f"[binds] init: {self.db_path}")

    # ── CRUD ──────────────────────────────────────────────────────

    def add_bind(self, user_id: int, alias: str, target: str) -> Tuple[bool, str]:
        """Add (or silently replace) a personal bind. Returns (ok, message_code)."""
        alias = (alias or "").strip().lstrip("/").lower()
        target = (target or "").strip().lstrip("/").lower()

        if not ALIAS_RE.match(alias):
            return False, "bad_alias"
        if not ALIAS_RE.match(target):
            return False, "bad_target"
        if alias == target:
            return False, "same"
        if alias in RESERVED_BIND_WORDS:
            return False, "reserved"

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO command_binds (user_id, alias, target) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, alias) DO UPDATE SET target = excluded.target",
                (user_id, alias, target),
            )
            conn.commit()
        logger.info(f"[binds] user {user_id}: /{alias} -> /{target}")
        return True, "ok"

    def remove_bind(self, user_id: int, alias: str) -> bool:
        alias = (alias or "").strip().lstrip("/").lower()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM command_binds WHERE user_id = ? AND alias = ?",
                (user_id, alias),
            )
            conn.commit()
            return cur.rowcount > 0

    def remove_all_binds(self, user_id: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM command_binds WHERE user_id = ?", (user_id,))
            conn.commit()
            return cur.rowcount

    def get_bind(self, user_id: int, alias: str) -> Optional[str]:
        """Resolve a personal bind: alias → target command (or None)."""
        alias = (alias or "").strip().lstrip("/").lower()
        if not alias:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT target FROM command_binds WHERE user_id = ? AND alias = ?",
                (user_id, alias),
            ).fetchone()
        return row["target"] if row else None

    def list_binds(self, user_id: int) -> List[Tuple[str, str, str]]:
        """All binds of a user: [(alias, target, created_at), ...]"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias, target, created_at FROM command_binds "
                "WHERE user_id = ? ORDER BY alias",
                (user_id,),
            ).fetchall()
        return [(r["alias"], r["target"], r["created_at"]) for r in rows]


# Слова, которые нельзя занимать под псевдоним (подкоманды /rhwymo и /datgysylltu)
RESERVED_BIND_WORDS = {"rhestr", "list", "help", "cymorth", "popeth", "all"}
