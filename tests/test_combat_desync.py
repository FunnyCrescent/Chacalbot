"""БОЙ-FIX v3 — регрессионные тесты ТЗ «диагностика и починка боевой очереди».

Причина #1 (ТЗ): легаси /combat → sessions.start_combat() ставил
combat_active=True БЕЗ CombatEncounter/Combatant + поднимал устаревшую
all-players очередь. Per-turn движок не видел боя: get_combat_action_group
тихо отдавал заглушку «никто не заблокирован», submit_action собирал ВСЕХ,
_resolve_pc_combat_turn молча выходил (current=None) — раунд не резолвился
НИКОГДА. Симптом: «уже сходил» / «не твой ход» / «Ждём: игрок2, игрок3» /
зависший бой без нарратива.

Причина #2 (ТЗ): narrator доверял существующей QueueState, не проверяя её
против текущего хода инициативы; при рассинхроне sync_combat_queue был no-op.

Здесь проверяется ЛОКАЛЬНОЕ ВОСПРОИЗВЕДЕНИЕ и лечение:
  1. defensive-check: рассинхрон (combat_active=True, encounter отсутствует)
     обнаруживается и аварийно лечится end_combat'ом вместо тихой заглушки;
  2. легаси start_combat теперь создаёт ту же БД-состояние, что и V10b;
  3. end_combat закрывает «зомби-encounter»;
  4. после лечения обычный раунд собирается и резолвится (бой больше не
     зависает), свежий /combat поднимает полноценный per-turn бой.
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


# ── FakeDB с боевой поверхностью (как в test_combat_coordinator) ──

class FakeEncounter:
    def __init__(self, id="e1", session_id="s1", reason="рассинхрон"):
        self.id = id
        self.session_id = session_id
        self.reason = reason
        self.active = True


class DesyncDB(FakeDB):
    """FakeDB + combat-таблицы: encounters, инициатива, история."""

    def __init__(self):
        super().__init__()
        self.encounter = None                # по умолчанию боевой НЕТ
        self.initiative_order: list = []
        self.round_increments = 0
        self.ended_encounters: list = []
        self.history: list = []
        self.updated_sessions: list = []

    # -- combat surface --
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


def desync_db(players=(1, 2, 3)) -> DesyncDB:
    """Точное состояние легаси /combat до фикса: combat_active=True,
    активного CombatEncounter НЕТ, в очереди ВСЕ игроки (один уже сходил)."""
    db = DesyncDB()
    db._session = FakeSession(combat_active=True)
    db._session.initiative_order = json.dumps(
        [{"name": f"P{pid}", "player_id": pid} for pid in players])
    db.players = [FakePlayer(pid) for pid in players]
    db._queue = QueueState(
        session_id="s1",
        waiting_for=json.dumps(list(players)),
        collected_actions=json.dumps({str(players[0]): "атакую стражника"}),
        is_resolving=False,
    )
    return db


def make_entry(name, entity_type="pc", player_id=0, initiative=10):
    return {"name": name, "entity_type": entity_type,
            "player_id": player_id, "initiative": initiative}


# ═══════════════════════════════════════════════════════════════
# Причина #1: defensive-check аварийно лечит рассинхрон
# ═══════════════════════════════════════════════════════════════

class TestDesyncEmergencyEnd:

    def test_action_group_emergency_ends_desynced_combat(self):
        """Заглушка «никто не заблокирован» больше не тихая: бой завершён."""
        db = desync_db()
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["mode"] == "normal"          # безопасный ответ
        assert db._session.combat_active is False  # …но бой аварийно закрыт
        assert db.get_queue_state("s1") is None    # устаревшая очередь очищена

    def test_current_turn_emergency_ends_desynced_combat(self):
        db = desync_db()
        bot = Bot(db)
        assert bot.get_current_initiative_turn("s1") is None
        assert db._session.combat_active is False

    def test_advance_turn_emergency_ends_desynced_combat(self):
        db = desync_db()
        bot = Bot(db)
        assert bot.advance_initiative_turn("s1") == {}
        assert db._session.combat_active is False

    def test_desync_flag_set_and_popped_once(self):
        db = desync_db()
        bot = Bot(db)
        bot.get_combat_action_group("s1")
        assert bot.pop_combat_desync_flag("s1") is True   # одно уведомление
        assert bot.pop_combat_desync_flag("s1") is False  # повторных нет

    def test_healthy_combat_is_not_touched(self):
        """Бой с живым encounter работает как раньше — без аварий."""
        db = desync_db()
        db.encounter = FakeEncounter()
        db.initiative_order = [
            make_entry("Лёха", "pc", player_id=1, initiative=18),
            make_entry("Гоша", "pc", player_id=2, initiative=7),
        ]
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["npc_turn"] is False
        assert group["waiting_for"] == [1]
        assert group["blocked_players"] == [2]
        assert db._session.combat_active is True          # бой жив
        assert bot.pop_combat_desync_flag("s1") is False  # флага нет

    def test_no_combat_no_emergency(self):
        """Без боя и без encounter — обычная заглушка, ничего не лечим."""
        db = desync_db()
        db._session = FakeSession(combat_active=False)
        db._queue = None
        bot = Bot(db)
        group = bot.get_combat_action_group("s1")
        assert group["mode"] == "normal"
        assert bot.pop_combat_desync_flag("s1") is False
        assert db.get_queue_state("s1") is None  # ничего не создавалось


# ═══════════════════════════════════════════════════════════════
# Причина #1: легаси start_combat теперь создаёт V10b-состояние
# ═══════════════════════════════════════════════════════════════

class TestLegacyStartCombatDelegatesToV10b:

    def _db_with_chars(self) -> DesyncDB:
        db = DesyncDB()
        db.players = [
            FakePlayer(1, username="leha", display_name="Лёха"),
            FakePlayer(2, username="gosha", display_name="Гоша"),
        ]
        db.chars = [
            type("C", (), {"name": "Лёха", "player_id": 1,
                           "stats": '{"dexterity": 16}', "hp": 12,
                           "max_hp": 12, "ac": 14, "is_alive": True})(),
            type("C", (), {"name": "Гоша", "player_id": 2,
                           "stats": '{"dexterity": 10}', "hp": 8,
                           "max_hp": 10, "ac": 12, "is_alive": True})(),
        ]
        return db

    def test_start_combat_creates_encounter_and_combatants(self):
        db = self._db_with_chars()
        bot = Bot(db)
        out = bot.start_combat("s1")
        # БД-состояние теперь то же, что у V10b-старта из нарратива:
        assert db._session.combat_active is True
        assert db.encounter is not None and db.encounter.is_active
        assert len(db.initiative_order) == 2
        by_name = {e["name"]: e for e in db.initiative_order}
        assert by_name["Лёха"]["entity_type"] == "pc"
        assert by_name["Лёха"]["player_id"] == 1
        assert by_name["Гоша"]["player_id"] == 2
        # per-turn движок видит бой — рассинхрона больше нет:
        assert bot.get_current_initiative_turn("s1") is not None
        assert bot.pop_combat_desync_flag("s1") is False
        assert "БОЙ НАЧИНАЕТСЯ" in out and "Порядок ходов" in out

    def test_start_combat_does_not_create_all_players_queue(self):
        """Очередь ведёт боевой цикл (single-player), а не start_action_collection."""
        db = self._db_with_chars()
        Bot(db).start_combat("s1")
        assert db.get_queue_state("s1") is None

    def test_start_combat_while_active_is_rejected(self):
        db = self._db_with_chars()
        bot = Bot(db)
        bot.start_combat("s1")
        out = bot.start_combat("s1")
        assert "already active" in out

    def test_start_combat_without_alive_chars(self):
        db = DesyncDB()
        out = Bot(db).start_combat("s1")
        assert out == "Error: no alive participants to start combat."
        assert db._session.combat_active is False  # ничего не поднялось

    def test_start_combat_deterministic_initiative(self, monkeypatch):
        import libs.session.combat_coordinator as cc
        monkeypatch.setattr(cc, "random",
                            SimpleNamespace(randint=lambda a, b: 11))
        db = self._db_with_chars()
        bot = Bot(db)
        bot.start_combat("s1")
        order = {e["name"]: e["initiative"] for e in db.initiative_order}
        assert order["Лёха"] == 14     # d20=11 + DEX-мод +3
        assert order["Гоша"] == 11     # d20=11 + 0


# ═══════════════════════════════════════════════════════════════
# end_combat закрывает зомби-encounter
# ═══════════════════════════════════════════════════════════════

class TestEndCombatClosesEncounter:

    def test_end_combat_closes_active_encounter(self):
        db = desync_db()
        db.encounter = FakeEncounter()
        db.initiative_order = [make_entry("Лёха", "pc", player_id=1)]
        bot = Bot(db)
        out = bot.end_combat("s1")
        assert "Бой завершён" in out
        assert db.ended_encounters == ["e1"]
        assert db._session.combat_active is False
        assert json.loads(db._session.initiative_order) == []

    def test_end_combat_without_combat_surface_does_not_crash(self):
        """FakeDB без combat-методов (conftest) — только warning, не падение."""
        db = FakeDB()
        db._session = FakeSession(combat_active=True)
        bot = Bot(db)
        out = bot.end_combat("s1")
        assert "Бой завершён" in out
        assert db._session.combat_active is False


# ═══════════════════════════════════════════════════════════════
# Воспроизведение баг-сценария ТЗ целиком
# ═══════════════════════════════════════════════════════════════

class TestBugScenarioRecovery:

    def test_stuck_round_now_resolves_as_normal_round(self):
        """Симптом ТЗ: игрок1 сходил (застрял в collected), остальные
        получали разнобой «не твой ход»/«ждём», раунд не резолвился НИКОГДА.
        Теперь рассинхрон лечится на первом же Дн., и обычный раунд собирается
        и резолвится (is_complete достижим)."""
        db = desync_db(players=(1, 2, 3))
        bot = Bot(db)

        # Игрок1 пишет Дн. → движок обнаруживает рассинхрон и лечит его:
        group = bot.get_combat_action_group("s1")
        assert group["mode"] == "normal"
        assert db._session.combat_active is False
        assert db.get_queue_state("s1") is None  # ловушка «уже сходил» снята

        # Обычный раунд стартует заново и реально завершается:
        bot.start_action_collection("s1")
        complete1, _ = bot.submit_action("s1", 1, "ищу выход")
        assert complete1 is False
        complete2, _ = bot.submit_action("s1", 2, "говорю со стражей")
        assert complete2 is False
        complete3, _ = bot.submit_action("s1", 3, "прячусь в тени")
        assert complete3 is True      # раньше — недостижимо навсегда

    def test_sync_combat_queue_is_noop_in_desync_and_heals(self):
        """sync_combat_queue при рассинхроне не трогает очередь вручную —
        её чистит emergency end_combat внутри get_combat_action_group."""
        db = desync_db()
        bot = Bot(db)
        assert bot.sync_combat_queue("s1") is None
        assert db._session.combat_active is False
        assert db.get_queue_state("s1") is None

    def test_after_recovery_fresh_combat_starts_correctly(self):
        """После лечения новый бой (по V10b) поднимается без последствий."""
        db = desync_db()
        db.players = [FakePlayer(1, username="leha", display_name="Лёха")]
        db.chars = [type("C", (), {"name": "Лёха", "player_id": 1,
                                   "stats": '{"dexterity": 14}', "hp": 10,
                                   "max_hp": 12, "ac": 14,
                                   "is_alive": True})()]
        bot = Bot(db)

        bot.get_combat_action_group("s1")          # лечение рассинхрона
        assert db._session.combat_active is False

        out = bot.start_combat("s1")               # новый бой
        assert "БОЙ НАЧИНАЕТСЯ" in out
        cur = bot.get_current_initiative_turn("s1")
        assert cur is not None and cur["name"] == "Лёха"
        group = bot.get_combat_action_group("s1")
        assert group["waiting_for"] == [1]
        assert group["blocked_players"] == []
