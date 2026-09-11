"""BillingRepo — глобальная БД биллинга (ИТЕРАЦИЯ 10, Разделы 6-7).

Баланс токенов ИГРОКА ГЛОБАЛЕН по user_id: не по сессии и не по чату.
Грант 30 000 000 выдаётся РОВНО ОДИН РАЗ за жизнь аккаунта (при первом
входе в любой платный чат) и НЕ пересоздаётся при выходе/повторном входе,
смене сессии или чата — это осознанно закрывает эксплойт «вышел-зашёл в
новую сессию — получил свежие токены»: при привязке к user_id он невозможен.

Журнал billing_log фиксирует КАЖДУЮ операцию: гранты, списания за раунды и
ручные пополнения тестерами (кто, кому, сколько, когда) — «печать денег»
должна быть видна постфактум.
"""
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BillingRepo:
    """Lightweight SQLite wrapper for the global billing.db file."""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_tables()

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS token_balances (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    granted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS billing_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT DEFAULT CURRENT_TIMESTAMP,
                    kind TEXT NOT NULL,             -- grant | round_charge | manual_topup
                    actor_id INTEGER DEFAULT 0,     -- кто инициировал (тестер / игрок / 0)
                    user_id INTEGER NOT NULL,       -- чей баланс затронут
                    session_id TEXT DEFAULT '',
                    amount INTEGER NOT NULL,        -- фактическое изменение баланса (>= 0)
                    cost INTEGER DEFAULT 0,         -- полная стоимость (может превышать amount)
                    note TEXT DEFAULT ''
                )
            """)

    # ─────────────────────────────────────────────────────────
    # Балансы
    # ─────────────────────────────────────────────────────────

    def get_balance(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance FROM token_balances WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            return int(row["balance"]) if row else 0

    def has_received_grant(self, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT granted FROM token_balances WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
            return bool(row and row["granted"])

    def ensure_grant(self, user_id: int, amount: int) -> int:
        """Выдать стартовый грант ОДИН РАЗ. Возвращает текущий баланс.
        Идемпотентно: повторный вызов ничего не начисляет."""
        uid = int(user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance, granted FROM token_balances WHERE user_id = ?",
                (uid,),
            ).fetchone()
            if row and row["granted"]:
                return int(row["balance"])
            if row:
                conn.execute(
                    "UPDATE token_balances SET granted = 1, balance = balance + ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (int(amount), uid),
                )
                new_balance = int(row["balance"]) + int(amount)
            else:
                conn.execute(
                    "INSERT INTO token_balances (user_id, balance, granted, updated_at) "
                    "VALUES (?, ?, 1, CURRENT_TIMESTAMP)",
                    (uid, int(amount)),
                )
                new_balance = int(amount)
            conn.execute(
                "INSERT INTO billing_log (kind, actor_id, user_id, amount, cost, note) "
                "VALUES ('grant', 0, ?, ?, ?, 'стартовый грант (один раз за аккаунт)')",
                (uid, int(amount), int(amount)),
            )
            return new_balance

    def charge(self, user_id: int, cost: int, session_id: str = "",
               note: str = "") -> int:
        """Списать tokens за раунд. Баланс НЕ уходит ниже 0: если cost больше
        остатка, списывается остаток (раунд уже произошёл — токены потрачены),
        полная стоимость фиксируется в журнале (cost), блокировка следующего
        хода произойдёт на гейте Дн. Возвращает НОВЫЙ баланс."""
        uid = int(user_id)
        cost = max(0, int(cost or 0))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance FROM token_balances WHERE user_id = ?", (uid,)
            ).fetchone()
            old = int(row["balance"]) if row else 0
            charged = min(old, cost)
            new_balance = old - charged
            if row:
                conn.execute(
                    "UPDATE token_balances SET balance = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ?",
                    (new_balance, uid),
                )
            elif charged or cost:
                # Баланса нет и списывать нечего — фиксируем факт в журнале.
                conn.execute(
                    "INSERT INTO token_balances (user_id, balance, granted, updated_at) "
                    "VALUES (?, 0, 0, CURRENT_TIMESTAMP)",
                    (uid,),
                )
            if charged or cost:
                conn.execute(
                    "INSERT INTO billing_log (kind, actor_id, user_id, session_id, "
                    "amount, cost, note) VALUES ('round_charge', ?, ?, ?, ?, ?, ?)",
                    (uid, uid, session_id or "", charged, cost, note or ""),
                )
            return new_balance

    def topup(self, actor_id: int, user_id: int, amount: int,
              note: str = "") -> int:
        """Ручное пополнение тестером (грант извне бота: оплата обрабатывается
        тестером вне бота). Никакого списания ниоткуда — просто начисление."""
        uid = int(user_id)
        amount = int(amount)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT balance FROM token_balances WHERE user_id = ?", (uid,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE token_balances SET balance = balance + ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (amount, uid),
                )
                new_balance = int(row["balance"]) + amount
            else:
                conn.execute(
                    "INSERT INTO token_balances (user_id, balance, granted, updated_at) "
                    "VALUES (?, ?, 0, CURRENT_TIMESTAMP)",
                    (uid, amount),
                )
                new_balance = amount
            conn.execute(
                "INSERT INTO billing_log (kind, actor_id, user_id, amount, cost, note) "
                "VALUES ('manual_topup', ?, ?, ?, ?, ?)",
                (int(actor_id or 0), uid, amount, amount, note or ""),
            )
            return new_balance

    # ─────────────────────────────────────────────────────────
    # Журнал
    # ─────────────────────────────────────────────────────────

    def get_log(self, limit: int = 50) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM billing_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
