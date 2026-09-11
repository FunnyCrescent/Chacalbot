"""Unit tests for libs/session/resolution.py + round_coordinator.py.

Covered (pure logic, fakes instead of Telegram/LLM/SQLite):
1. ResolutionMixin._build_sheets_with_usernames — @username headers for
   ModerAI dispatch, incl. graceful fallbacks (no player, no username,
   db failures, summary failure).
2. RoundCoordinatorMixin round counter (Фича 3) — global_round persists in
   session_meta, monotonic, survives db hiccups.
3. RoundCoordinatorMixin action queue — submit/cancel/skip semantics that
   the /sgipio rework (Фича 1) and world-gen gate (Bug 3) rely on.
"""
import json

import pytest

from libs.session import round_coordinator as rc_module
from libs.session.resolution import ResolutionMixin
from libs.session.round_coordinator import RoundCoordinatorMixin
from tests.conftest import (
    FakeChar, FakePlayer, RaisingDBManager, make_queue,
)


class R(ResolutionMixin):
    def __init__(self, db):
        self._db = db


class RC(RoundCoordinatorMixin):
    def __init__(self, db_manager):
        self.db_manager = db_manager
        self._non_combat_queue = {}

    def get_combat_action_group(self, session_id):
        # used when a faked session has combat_active=True
        return {"blocked_players": [777]}


# ═══════════════════════════════════════════════════════════════
# 1. _build_sheets_with_usernames
# ═══════════════════════════════════════════════════════════════

class TestBuildSheetsWithUsernames:

    def test_empty_chars(self, db):
        assert R(db)._build_sheets_with_usernames(db, "s1", []) == []

    def test_username_preferred(self, db):
        db.players = [FakePlayer(1, username="leha", display_name="Лёха")]
        db.chars = [FakeChar(10, 1, "Анджей")]
        db.summaries = {10: "HP 12/12, AC 14"}
        sheets = R(db)._build_sheets_with_usernames(db, "s1", db.chars)
        assert sheets == ["@username: @leha\nHP 12/12, AC 14"]

    def test_display_name_fallback(self, db):
        db.players = [FakePlayer(1, username="", display_name="Лёха")]
        db.chars = [FakeChar(10, 1, "Анджей")]
        sheets = R(db)._build_sheets_with_usernames(db, "s1", db.chars)
        assert sheets[0].startswith("@username: @Лёха\n")

    def test_no_player_falls_back_to_char_name(self, db):
        db.players = []                     # character of a player who left?
        db.chars = [FakeChar(10, 1, "Анджей")]
        sheets = R(db)._build_sheets_with_usernames(db, "s1", db.chars)
        assert sheets[0].startswith("@username: @Анджей\n")

    def test_db_failure_still_builds_sheets(self, db):
        db.fail_load_players = True
        db.chars = [FakeChar(10, 1, "Анджей")]
        sheets = R(db)._build_sheets_with_usernames(db, "s1", db.chars)
        assert len(sheets) == 1
        assert sheets[0].startswith("@username: @Анджей\n")

    def test_summary_failure_uses_fallback_text(self, db):
        db.fail_summaries = True
        db.players = [FakePlayer(1, username="leha")]
        db.chars = [FakeChar(10, 1, "Анджей", race="Эльф",
                              class_name="Следопыт", level=2)]
        sheets = R(db)._build_sheets_with_usernames(db, "s1", db.chars)
        assert "Анджей (Эльф Следопыт, уровень 2)" in sheets[0]


# ═══════════════════════════════════════════════════════════════
# 2. Round counter (Фича 3: AI must understand round numbers)
# ═══════════════════════════════════════════════════════════════

class TestRoundCounter:

    def test_starts_at_zero(self, dbm):
        assert RC(dbm).get_current_round("s1") == 0

    def test_advance_is_monotonic(self, dbm):
        rc = RC(dbm)
        assert rc.advance_round("s1") == 1
        assert rc.advance_round("s1") == 2
        assert rc.advance_round("s1") == 3
        assert rc.get_current_round("s1") == 3

    def test_counter_persists_in_meta(self, db, dbm):
        """The whole point of Фича 3: stored in session_meta, survives
        restarts — two coordinators over the same db see the same number."""
        RC(dbm).advance_round("s1")
        RC(dbm).advance_round("s1")
        assert db.get_meta(RoundCoordinatorMixin.ROUND_META_KEY) == "2"

    def test_db_failure_returns_zero_not_crash(self):
        rc = RC(RaisingDBManager())
        assert rc.get_current_round("s1") == 0
        assert rc.advance_round("s1") == 0

    def test_corrupt_meta_value_is_tolerated(self, db, dbm):
        db.set_meta(RoundCoordinatorMixin.ROUND_META_KEY, "banana")
        assert RC(dbm).get_current_round("s1") == 0


# ═══════════════════════════════════════════════════════════════
# 3. Action queue: submit / cancel / skip
# ═══════════════════════════════════════════════════════════════

class TestSubmitAction:

    def test_no_queue_is_noop(self, dbm):
        ok, msg = RC(dbm).submit_action("s1", 1, "атакую")
        assert ok is False and msg is None

    def test_round_completes_on_last_action(self, db, dbm):
        db.set_queue_state(make_queue([1, 2]))
        rc = RC(dbm)
        ok, _ = rc.submit_action("s1", 1, "ищу ловушку")
        assert ok is False
        ok, _ = rc.submit_action("s1", 2, "иду вперёд")
        assert ok is True
        state = db.get_queue_state("s1")
        assert state.is_resolving is True          # locked same-write
        assert json.loads(state.collected_actions)["2"] == "иду вперёд"

    def test_duplicate_submit_blocked(self, db, dbm):
        db.set_queue_state(make_queue([1, 2]))
        rc = RC(dbm)
        rc.submit_action("s1", 1, "раз")
        ok, msg = rc.submit_action("s1", 1, "два")
        assert ok is False
        assert "already" in msg.lower()

    def test_non_waiting_player_blocked(self, db, dbm):
        db.set_queue_state(make_queue([1]))
        ok, msg = RC(dbm).submit_action("s1", 42, "я вообще не в раунде")
        assert ok is False and "not your turn" in msg.lower()

    def test_submit_during_resolve_blocked(self, db, dbm):
        state = make_queue([1])
        state.is_resolving = True
        db.set_queue_state(state)
        ok, msg = RC(dbm).submit_action("s1", 1, "опоздал")
        assert ok is False and "resolving" in msg.lower()

    def test_blocked_initiative_player_cannot_act(self, db, dbm, monkeypatch):
        """Combat: player 777 is blocked by initiative (see RC.get_combat_action_group).
        The flag is imported into round_coordinator's namespace, so patch it there."""
        monkeypatch.setattr(rc_module, "COMBAT_INITIATIVE_ENABLED", True)
        db._session.combat_active = True
        db.set_queue_state(make_queue([777]))
        ok, msg = RC(dbm).submit_action("s1", 777, "не мой ход")
        assert ok is False and "initiative" in msg.lower()

    def test_unblocked_player_acts_during_combat(self, db, dbm, monkeypatch):
        """Player NOT in the blocked list acts normally during combat."""
        monkeypatch.setattr(rc_module, "COMBAT_INITIATIVE_ENABLED", True)
        db._session.combat_active = True
        db.set_queue_state(make_queue([1]))
        ok, msg = RC(dbm).submit_action("s1", 1, "мой ход")
        assert ok is True and msg is None


class TestCancelAction:

    def test_no_queue(self, dbm):
        ok, msg = RC(dbm).cancel_action("s1", 1)
        assert ok is False and "Нет активного раунда" in msg

    def test_during_resolve_denied(self, db, dbm):
        state = make_queue([1])
        state.is_resolving = True
        db.set_queue_state(state)
        ok, msg = RC(dbm).cancel_action("s1", 1)
        assert ok is False and "уже разрешается" in msg

    def test_nothing_to_cancel(self, db, dbm):
        db.set_queue_state(make_queue([1]))
        ok, msg = RC(dbm).cancel_action("s1", 1)
        assert ok is False and "нет поданного действия" in msg.lower()

    def test_cancel_returns_player_to_waiting(self, db, dbm):
        db.set_queue_state(make_queue([1, 2]))
        rc = RC(dbm)
        rc.submit_action("s1", 2, "меняю решение")
        ok, msg = rc.cancel_action("s1", 2)
        assert ok is True
        state = db.get_queue_state("s1")
        assert 2 in json.loads(state.waiting_for)
        assert "2" not in json.loads(state.collected_actions)


class TestSkipPlayer:
    """Фича 1: /sgipio without args (self-skip) and admin-skip both land
    in skip_player with a reason the Master can see."""

    def test_skip_self_when_waiting(self, db, dbm):
        db.set_queue_state(make_queue([1, 2]))
        ok = RC(dbm).skip_player("s1", 1, reason="*[self-skip]*")
        assert ok is False                    # 2 still waiting
        state = db.get_queue_state("s1")
        assert json.loads(state.collected_actions)["1"] == "*[self-skip]*"
        assert 1 not in json.loads(state.waiting_for)

    def test_skip_last_player_completes_round(self, db, dbm):
        db.set_queue_state(make_queue([1]))
        ok = RC(dbm).skip_player("s1", 1)
        assert ok is True
        assert db.get_queue_state("s1").is_resolving is True

    def test_skip_unknown_player_fails(self, db, dbm):
        db.set_queue_state(make_queue([1]))
        assert RC(dbm).skip_player("s1", 99) is False

    def test_skip_without_queue_fails(self, dbm):
        assert RC(dbm).skip_player("s1", 1) is False


class TestNonCombatQueue:
    """DUAL NARRATIVE — in-memory queue for non-combat players."""

    def test_pop_is_atomic(self, dbm):
        rc = RC(dbm)
        rc.submit_non_combat_action("s1", 5, "читаю книгу")
        rc.submit_non_combat_action("s1", 6, "тренируюсь")
        popped = rc.pop_non_combat_queue("s1")
        assert popped == {5: "читаю книгу", 6: "тренируюсь"}
        assert rc.get_non_combat_queue("s1") == {}
        assert rc.pop_non_combat_queue("s1") == {}


class TestForceResetRound:
    """DM safety valve for a round stuck mid-resolve."""

    def test_reset_discards_and_restarts(self, db, dbm):
        # Build a REALISTIC mid-resolve state through the real submit path:
        # player 1 submitted, player 2 is still waiting, round got locked.
        db.set_queue_state(make_queue([1, 2]))
        rc = RC(dbm)
        rc.submit_action("s1", 1, "шёл по лесу")
        state = db.get_queue_state("s1")
        state.is_resolving = True                 # crashed mid-resolve
        db.set_queue_state(state)
        db.players = [FakePlayer(1, display_name="Лёха"),
                      FakePlayer(2, display_name="Гоша")]

        out = rc.force_reset_round("s1")

        assert out["discarded_waiting"] == ["Гоша"]
        assert out["discarded_actions"] == ["Лёха"]
        fresh = db.get_queue_state("s1")
        assert fresh is not None and fresh.is_resolving is False
        assert json.loads(fresh.waiting_for) == [1, 2]
