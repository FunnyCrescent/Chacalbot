"""SavedCharsDB — глобальная библиотека сохранённых персонажей (cross-session)."""
import json
import logging
import os
import sqlite3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class SavedCharsDB:
    """Lightweight SQLite wrapper for the global saved_chars.db file."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_tables()

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS saved_characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id INTEGER NOT NULL,
                    char_name TEXT NOT NULL,
                    char_class TEXT DEFAULT '',
                    sheet_text TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    parsed_data TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Migration: add parsed_data column if missing
            try:
                existing = {r["name"] for r in conn.execute("PRAGMA table_info(saved_characters)").fetchall()}
                if "parsed_data" not in existing:
                    conn.execute("ALTER TABLE saved_characters ADD COLUMN parsed_data TEXT DEFAULT ''")
            except Exception:
                pass

    def save_character(self, player_id: int, char_name: str, char_class: str,
                      sheet_text: str = "", file_name: str = "",
                      parsed_data: str = "") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO saved_characters (player_id, char_name, char_class, sheet_text, file_name, parsed_data)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (player_id, char_name, char_class, sheet_text, file_name, parsed_data)
            )
            return cursor.lastrowid

    def get_characters(self, player_id: int) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, char_name, char_class, file_name, created_at FROM saved_characters "
                "WHERE player_id = ? ORDER BY created_at DESC",
                (player_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_character(self, char_id: int, player_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM saved_characters WHERE id = ? AND player_id = ?",
                (char_id, player_id)
            ).fetchone()
            return dict(row) if row else None

    def delete_character(self, char_id: int, player_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM saved_characters WHERE id = ? AND player_id = ?",
                (char_id, player_id)
            )
            return cursor.rowcount > 0
