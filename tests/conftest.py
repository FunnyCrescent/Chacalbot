"""Shared fixtures and fakes for Chacalbot unit tests.

Design notes
────────────
The project is built as "enterprise monolith in mixins", so the engine classes
can't be instantiated directly (they need .env, Telegram token, LLM keys).
These tests therefore mount ONLY the mixin under test onto a tiny local class
and inject fake collaborators (db, db_manager, db_bot). No network, no files,
no SQLite — everything is in-memory.

Run:  python -m pytest tests/ -v
"""
import json
import os
import sys
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

# Make `libs.*` importable regardless of where pytest is invoked from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from libs.db import QueueState  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Fakes: players / characters
# ═══════════════════════════════════════════════════════════════

class FakePlayer:
    def __init__(self, user_id: int, username: str = "",
                 display_name: str = ""):
        self.user_id = user_id
        self.username = username
        self.display_name = display_name or username or str(user_id)


class FakeChar:
    def __init__(self, id: int, player_id: int, name: str,
                 race: str = "Человек", class_name: str = "Воин",
                 level: int = 1):
        self.id = id
        self.player_id = player_id
        self.name = name
        self.race = race
        self.class_name = class_name
        self.level = level
        # combat surface (start_combat_for_participants / combat turns):
        self.stats = '{"dexterity": 14}'
        self.hp = 10
        self.max_hp = 12
        self.ac = 14
        self.is_alive = True
        self.languages = ""


class FakeSession:
    def __init__(self, session_id: str = "s1", combat_active: bool = False):
        self.id = session_id
        self.combat_active = combat_active
        # ИТЕРАЦИЯ 15: session_is_active() читает status (гвард завершённых сессий)
        self.status = "active"
        # initiative machinery (combat_coordinator / V10b helpers)
        self.initiative_order = "[]"
        self.current_turn_index = 0
        self.round_number = 1
        # resolution surface (resolve_round_master_only / summarize watermark)
        self.summary = ""
        self.summary_at_count = 0
        self.pvp_active = False
        self.currency_name = "золото"
        self.currency_symbol = "gp"
        self.currency_sub_name = ""
        self.currency_sub_symbol = ""
        self.currency_sub_value = 0
        self.currency_super_name = ""
        self.currency_super_symbol = ""
        self.currency_super_value = 0
        self.chat_id = -100


# ═══════════════════════════════════════════════════════════════
# Fake DB — covers the surface used by ResolutionMixin /
# RoundCoordinatorMixin. Methods raise NotImplementedError when a
# test hits an unexpected call — better loud than silent.
# ═══════════════════════════════════════════════════════════════

class FakeDB:
    def __init__(self):
        self.players: List[FakePlayer] = []
        self.chars: List[FakeChar] = []
        self.summaries: Dict[int, str] = {}
        self._meta: Dict[str, str] = {}
        self._queue: Optional[QueueState] = None
        self._session = FakeSession()

        # fail_load_players / fail_summaries — to exercise graceful fallbacks
        self.fail_load_players = False
        self.fail_summaries = False

    # -- players / characters --
    def get_players(self, session_id):
        if self.fail_load_players:
            raise RuntimeError("boom: players")
        return list(self.players)

    def get_player(self, user_id, session_id):
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def get_character_progression_summary(self, char_id):
        if self.fail_summaries:
            raise RuntimeError("boom: summary")
        return self.summaries.get(char_id, f"Summary #{char_id}")

    # -- session --
    def get_session(self, session_id):
        return self._session

    def update_session(self, session):
        """No-op: тесты читают состояние через self._session напрямую.
        Нужен surface'у end_combat/start_combat (БОЙ-FIX v3)."""
        self.updated_sessions = getattr(self, "updated_sessions", [])
        self.updated_sessions.append(session.current_turn_index)

    # -- session meta (global_round counter) --
    def get_meta(self, key, default=""):
        return self._meta.get(key, default)

    def set_meta(self, key, value):
        self._meta[key] = value

    def increment_meta(self, key, default="0"):
        """Атомарный инкремент — семантика идентична Database.increment_meta
        (SQL UPSERT ... CAST(value AS INTEGER) + 1). В однопоточном фейке
        атомарность обеспечивается отсутствием await внутри."""
        try:
            base = int(default)
        except (TypeError, ValueError):
            base = 0
        current = self._meta.get(key)
        if current is None:
            nxt = base + 1
        else:
            try:
                nxt = int(current) + 1
            except (TypeError, ValueError):
                nxt = 1  # «banana» → CAST → 0 → +1
        self._meta[key] = str(nxt)
        return nxt

    # -- action queue --
    def get_queue_state(self, session_id):
        return self._queue

    def set_queue_state(self, state: QueueState):
        self._queue = state

    def clear_queue_state(self, session_id):
        self._queue = None

    # -- explicitly not expected in these tests --
    def get_character_by_player(self, uid, sid):
        for c in self.chars:
            if c.player_id == uid:
                return c
        return None

    # ── resolution surface (test_combat_race.py) ──
    # Дефолтные, безопасные реализации: у сессии нет персонажей/квестов/
    # фракций — резолвер доходит до вызова Мастера и пишет историю.

    def get_session_characters(self, session_id):
        return list(self.chars)

    def get_all_character_sheets(self, session_id):
        return []

    def get_history(self, session_id, limit=30):
        return list(getattr(self, "_history", []))[-limit:]

    def add_history(self, entry):
        if not hasattr(self, "_history"):
            self._history = []
        self._history.append(entry)

    def count_history(self, session_id):
        return len(getattr(self, "_history", []))

    def get_conditions(self, session_id, char_id):
        return []

    def get_location(self, session_id, char_id):
        return None

    def get_quests(self, session_id, status="active"):
        return []

    def get_character_goals(self, session_id, status="active"):
        return []

    def get_factions(self, session_id):
        return []

    def get_game_time(self, session_id):
        return SimpleNamespace(day=1, hour=8, minute=0,
                               weather="ясно", temperature="+20°C")

    def get_journal_summary(self, session_id):
        return ""

    def advance_time(self, session_id, minutes=10):
        self.advanced_minutes = getattr(self, "advanced_minutes", 0) + minutes


class FakeDBManager:
    def __init__(self, db: FakeDB):
        self._db = db

    def get_db(self, session_id) -> FakeDB:
        return self._db


class RaisingDBManager:
    """db_manager whose get_db blows up — round counter must survive it."""

    def get_db(self, session_id):
        raise RuntimeError("boom: db")


def make_queue(players: List[int]) -> QueueState:
    """A fresh queue waiting for all `players`, nothing collected yet."""
    return QueueState(
        session_id="s1",
        waiting_for=json.dumps(list(players)),
        collected_actions="{}",
        is_resolving=False,
    )


class FakeDBBot:
    """LLM client stub with a canned reply (and a call counter).

    `strict=True` makes any call raise — used to prove guards reject
    garbage sheets WITHOUT burning a single LLM token."""

    def __init__(self, content: str = "", strict: bool = False):
        self.content = content
        self.strict = strict
        self.calls: List[list] = []

    async def chat(self, messages, *args, **kwargs):
        if self.strict:
            raise AssertionError("LLM must NOT be called for this input")
        self.calls.append(messages)
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def dbm(db) -> FakeDBManager:
    return FakeDBManager(db)
