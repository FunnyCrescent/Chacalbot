"""Unit tests for libs/session/combat_coordinator.py (CombatCoordinatorMixin).

Fakes instead of Telegram/SQLite. Covers the initiative machinery that
/sgipio (Фича 1) and BUG #11 (strict one-player-per-turn) rely on:
- turn advancement with round wrap + increment_combat_round
- get_combat_action_group: PC turn → ONE waiting player, others blocked;
  NPC turn → npc_streak, all PCs blocked
- start_combat_turn_collection (queue only for PC turns)
- end_initiative_combat (flags reset, non-combat queue cleared, normal mode)
- V10b session-based get_current_turn / advance_turn
"""
import json
from types import SimpleNamespace

import pytest

from libs.db import CombatEncounter, Combatant, HistoryEntry, QueueState
from libs.session.combat_coordinator import CombatCoordinatorMixin
from libs.session.round_coordinator import RoundCoordinatorMixin
from tests.conftest import FakeDB, FakeDBManager, FakePlayer, FakeSession


class Bot(CombatCoordinatorMixin, RoundCoordinatorMixin):
    def __init__(self, db: FakeDB):
        self.db_manager = FakeDBManager(db)
        self._non_combat_queue = {}


# ── FakeDB extensions for combat surface ─────────────────────────

class FakeEncounter:
    def __init__(self, id="e1", session_id="s1", reason="засада"):
        self.id = id
        self.session_id = session_id
        self.reason = reason
        self.active = True


def make_entry(name, entity_type="pc", player_id=0, initiative=10):
    return {"name": name, "entity_type": entity_type,
            "player_id": player_id, "initiative": initiative}


class CombatDB(FakeDB):
    """FakeDB + combat tables (encounters, initiative order, history)."""

    def __init__(self):
        super().__init__()
        self.encounter: FakeEncounter = FakeEncounter()
        self.initiative_order: list = []
        self.round_increments = 0
        self.ended_encounters: list = []
        self.history: list = []
        self.updated_sessions: list = []

    def get_active_combat_encounter(self, session_id):
        return self.encounter

    def get_initiative_order(self, encounter_id):
        return list(self.initiative_order)

    def create_combat_encounter(self, enc):
        self.encounter = enc

    def add_combatant(self, c: Combatant):
        self.initiative_order.append({
            "name": c.name, "entity_type": c.entity_type,
            "player_id": c.player_id, "initiative": c.initiative,
        })

    def update_session(self, session):
        self.updated_sessions.append(session.current_turn_index)

    def increment_combat_round(self, encounter_id):
        self.round_increments += 1

    def end_combat_encounter(self, encounter_id):
        self.ended_encounters.append(encounter_id)
        self.encounter = None

    def add_history(self, entry: HistoryEntry):
        self.history.append(entry)

    def get_session_characters(self, session_id):
        return []


def no_encounter_db() -> CombatDB:
    db = CombatDB()
    db.encounter = None
    return db


# ═══════════════════════════════════════════════════════════════
# Initiative turn machinery
# ═══════════════════════════════════════════════════════════════

def three_fighter_db() -> CombatDB:
    db = CombatDB()
    db.initiative_order = [
        make_entry("Лёха", "pc", player_id=1, initiative=18),
        make_entry("СЛИЗЬ", "npc", initiative=14),
        make_entry("Гоша", "pc", player_id=2, initiative=7),
    ]
    db._session = FakeSession(combat_active=True)
    return db


class TestAdvanceInitiativeTurn:

    def test_simple_advance(self):
        db = three_fighter_db()
        bot = Bot(db)
        nxt = bot.advance_initiative_turn("s1")
        assert nxt["name"] == "СЛИЗЬ"
        assert db._session.current_turn_index == 1
        assert db.round_increments == 0

    def test_wrap_increments_round(self):
        db = three_fighter_db()
        db._session.current_turn_index = 2     # last combatant (Гоша)
        bot = Bot(db)
        nxt = bot.advance_initiative_turn("s1")
        assert nxt["name"] == "Лёха"           # wraps to first
        assert db._session.current_turn_index == 0
        assert db._session.round_number == 2   # 1 → 2
        assert db.round_increments == 1

    def test_no_combat_returns_empty(self):
        bot = Bot(no_encounter_db())
        assert bot.advance_initiative_turn("s1") == {}


class TestTurnQueries:

    def test_current_turn(self):
        db = three_fighter_db()
        bot = Bot(db)
        assert bot.get_current_initiative_turn("s1")["name"] == "Лёха"

    def test_current_turn_no_combat(self):
        assert Bot(no_encounter_db()).get_current_initiative_turn("s1") is None

    def test_next_turns_wraparound(self):
        db = three_fighter_db()
        db._session.current_turn_index = 1     # СЛИЗЬ
        bot = Bot(db)
        names = [e["name"] for e in bot.get_next_initiative_turns("s1", 3)]
        assert names == ["Гоша", "Лёха", "СЛИЗЬ"]


# ═══════════════════════════════════════════════════════════════
# get_combat_action_group — BUG #11: STRICT one-player-per-turn
# ═══════════════════════════════════════════════════════════════

class TestCombatActionGroup:

    def test_no_encounter_is_normal_mode(self):
        group = Bot(no_encounter_db()).get_combat_action_group("s1")
        assert group["mode"] == "normal"
        assert group["waiting_for"] == []
        assert group["blocked_players"] == []

    def test_pc_turn_one_waiting_others_blocked(self):
        db = three_fighter_db()
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["npc_turn"] is False
        assert group["waiting_for"] == [1]            # ONLY Лёха
        assert group["blocked_players"] == [2]        # Гоша waits his turn
        assert group["current"]["name"] == "Лёха"

    def test_pc_turn_second_pc_not_unblocked(self):
        """Regression for BUG #11: the old pc_streak logic let Eira AND СЛИЗЬ
        act together. Now the NEXT pc is never in waiting_for."""
        db = three_fighter_db()
        db._session.current_turn_index = 2            # Гоша's turn
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["waiting_for"] == [2]
        assert group["blocked_players"] == [1]        # Лёха blocked

    def test_npc_turn_blocks_all_pcs(self):
        db = three_fighter_db()
        db._session.current_turn_index = 1            # СЛИЗЬ's turn
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["npc_turn"] is True
        assert group["waiting_for"] == []
        assert sorted(group["blocked_players"]) == [1, 2]
        # npc_streak: СЛИЗЬ followed by... Гоша (pc) → streak = 1 npc
        assert [e["name"] for e in group["npc_streak"]] == ["СЛИЗЬ"]

    def test_npc_streak_extends_over_consecutive_npcs(self):
        db = three_fighter_db()
        db.initiative_order.insert(2, make_entry("Гоблин", "npc", initiative=12))
        db._session.current_turn_index = 1            # СЛИЗЬ
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        # order: Лёха(pc) → СЛИЗЬ(npc) → Гоблин(npc) → Гоша(pc)
        assert [e["name"] for e in group["npc_streak"]] == ["СЛИЗЬ", "Гоблин"]


# ═══════════════════════════════════════════════════════════════
# start_combat_turn_collection
# ═══════════════════════════════════════════════════════════════

class TestStartCombatTurnCollection:

    def test_pc_turn_opens_queue(self):
        db = three_fighter_db()
        bot = Bot(db)
        group = bot.start_combat_turn_collection("s1")
        state = db.get_queue_state("s1")
        assert state is not None
        assert json.loads(state.waiting_for) == [1]
        assert state.is_resolving is False

    def test_npc_turn_no_queue(self):
        db = three_fighter_db()
        db._session.current_turn_index = 1            # СЛИЗЬ
        bot = Bot(db)
        group = bot.start_combat_turn_collection("s1")
        assert group["npc_turn"] is True
        assert db.get_queue_state("s1") is None       # nothing opened


# ═══════════════════════════════════════════════════════════════
# end_initiative_combat
# ═══════════════════════════════════════════════════════════════

class TestEndInitiativeCombat:

    def test_no_active_combat(self):
        bot = Bot(no_encounter_db())
        assert bot.end_initiative_combat("s1", "победа") == "No active combat"

    def test_end_resets_session_and_restores_normal_mode(self):
        db = three_fighter_db()
        db._session.round_number = 3
        bot = Bot(db)
        bot.submit_non_combat_action("s1", 2, "читаю книгу в укрытии")

        out = bot.end_initiative_combat("s1", "победа")

        assert "победа" in out
        assert db.ended_encounters == ["e1"]
        assert db._session.combat_active is False
        assert json.loads(db._session.initiative_order) == []
        assert db._session.current_turn_index == 0
        assert db._session.round_number == 0
        # БОЙ-FIX: non-combat очередь больше НЕ чистится при конце боя —
        # остатки подмешиваются к первому обычному раунду
        # (см. test_combat_race.py::TestLeftoverNonCombatQueue).
        assert bot.get_non_combat_queue("s1") == {2: "читаю книгу в укрытии"}
        # normal action collection restarted
        fresh = db.get_queue_state("s1")
        assert fresh is not None and fresh.is_resolving is False
        # history entry written
        assert any("Combat ended: победа" in h.content for h in db.history)


# ═══════════════════════════════════════════════════════════════
# start_initiative_combat
# ═══════════════════════════════════════════════════════════════

class TestStartInitiativeCombat:
    """Called by Master's dechrauymladd tool. Initiative = d20 + dex_mod."""

    def _db_with_pc(self) -> CombatDB:
        db = CombatDB()
        db.players = [FakePlayer(1, username="leha", display_name="Лёха")]

        class SessionChar:
            name = "Лёха"
            stats = '{"dexterity": 16}'     # DB stores stats as JSON string
            hp = 12
            max_hp = 12
            ac = 14

        db.get_session_characters = lambda sid: [SessionChar()]
        return db

    def test_start_sets_up_combat_deterministically(self, monkeypatch):
        import libs.session.combat_coordinator as cc
        # d20 always rolls 11: PC Лёха 11+3=14, NPC Огр 11+0=11
        monkeypatch.setattr(cc, "random",
                            SimpleNamespace(randint=lambda a, b: 11))
        db = self._db_with_pc()
        bot = Bot(db)

        out = bot.start_initiative_combat(
            "s1", ["Лёха", "Огр"], reason="засада гоблинов")

        assert "COMBAT" in out and "Initiative order" in out
        assert "Лёха" in out and "Огр" in out
        assert db._session.combat_active is True
        assert db._session.round_number == 1
        assert db._session.current_turn_index == 0
        order = json.loads(db._session.initiative_order)
        assert [e["name"] for e in order] == ["Лёха", "Огр"]
        # initiative: PC = roll + dex_mod (+3)
        assert order[0]["player_id"] == 1
        assert any("COMBAT STARTED" in h.content for h in db.history)

    def test_start_unknown_names_are_npcs(self, monkeypatch):
        import libs.session.combat_coordinator as cc
        monkeypatch.setattr(cc, "random",
                            SimpleNamespace(randint=lambda a, b: 11))
        db = self._db_with_pc()
        Bot(db).start_initiative_combat("s1", ["Кто-то"], reason="тень")
        order = json.loads(db._session.initiative_order)
        assert order[0]["name"] == "Кто-то"
        assert order[0]["player_id"] == 0           # not a known player

    def test_start_no_session(self):
        db = CombatDB()
        db.get_session = lambda sid: None
        out = Bot(db).start_initiative_combat("s1", ["x"], reason="r")
        assert out == "Error: session not found"


# ═══════════════════════════════════════════════════════════════
# V10b session-based turn helpers
# ═══════════════════════════════════════════════════════════════

class TestSessionTurnHelpers:

    def _session(self) -> FakeSession:
        s = FakeSession(combat_active=True)
        s.initiative_order = json.dumps([
            {"name": "Лёха", "player_id": 1},
            {"name": "Гоша", "player_id": 2},
        ])
        s.current_turn_index = 0
        s.round_number = 1
        return s

    def test_get_current_turn(self):
        db = CombatDB()
        db._session = self._session()
        assert Bot(db).get_current_turn("s1")["name"] == "Лёха"

    def test_get_current_turn_no_combat(self):
        db = CombatDB()
        db._session = FakeSession(combat_active=False)
        assert Bot(db).get_current_turn("s1") is None

    def test_advance_turn_wraps_round(self):
        db = CombatDB()
        s = self._session()
        s.current_turn_index = 1
        db._session = s
        bot = Bot(db)
        nxt = bot.advance_turn("s1")
        assert nxt["name"] == "Лёха"
        assert s.current_turn_index == 0
        assert s.round_number == 2
