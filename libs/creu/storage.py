import sqlite3
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# BUG 4 FIX: раньше путь указывал на libs/data/creu_sandbox.db, а каталога
# libs/data/ не существовало → sqlite3.connect кидал "unable to open database file",
# исключение молча глоталось в plugins/character/plugin.py и /creu вообще
# НЕ регистрировался. Теперь БД живёт в <корень проекта>/data (как все остальные)
# и каталог гарантированно создаётся перед подключением.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _PROJECT_ROOT / "data" / "creu_sandbox.db"


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS creu_sandbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                character_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                delivered INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    logger.info(f"[creu] Песочница инициализирована: {DB_PATH}")


def save_to_sandbox(user_id: int, name: str, character_json: str) -> int:
    with _get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO creu_sandbox (user_id, name, character_json, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, character_json, time.time()),
        )
        conn.commit()
        row_id = cursor.lastrowid
    logger.info(f"[creu] {name} сохранён в песочницу (id={row_id})")
    return row_id


def delete_from_sandbox(row_id: int) -> bool:
    with _get_connection() as conn:
        cursor = conn.execute("DELETE FROM creu_sandbox WHERE id = ?", (row_id,))
        conn.commit()
        ok = cursor.rowcount > 0
    if ok:
        logger.info(f"[creu] id={row_id} удалён из песочницы")
    return ok


def cleanup_stale() -> int:
    """Удалить недоставленные записи старше 10 минут (зависшие сессии)."""
    cutoff = time.time() - 600
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM creu_sandbox WHERE delivered = 0 AND created_at < ?",
            (cutoff,),
        )
        conn.commit()
        count = cursor.rowcount
    if count:
        logger.info(f"[creu] Очистка: удалено {count} зависших записей")
    return count
