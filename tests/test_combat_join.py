"""Regression tests for БОЙ-FIX v2 (итерация 7).

Реальный инцидент (сессия с СЛИЗЬЮ у ворот таверны):
  • Рими атакует слизь, но бот отвечает «📖 Ты не в бою — Мастер разрешит
    отдельно» → его атаки резолвятся ПАРАЛЛЕЛЬНЫМ вызовом Мастера, пока
    главная очередь вечно показывает «Ждём: Храфна Морвен, Рими» (дедлок);
  • устаревшая all-players очередь пережила начало боя и приняла ход
    СЛИЗЬ, хотя во время инициативного боя очередь должна быть
    single-player для текущего PC.

Фиксы, покрытые здесь:
1. join_active_combat — игрок вступает в УЖЕ ИДУЩИЙ бой: Combatant(pc,
   player_id>0), серверная инициатива, текущий ход НИКОГО не скипает.
2. join_active_combat_by_name — text-обёртка для tool-call Мастера
   (ymuno_ymladd) + запись факта вступления (анонс игрокам).
3. sync_combat_queue — очередь во время боя пересобирается под текущего
   PC, если разошлась с боевой группой; уже поданное действие НЕ стирается.
4. Non-combat остатки подмешиваются к обычному раунду ТОЛЬКО после конца
   боя — пока бой идёт, dual-narrative lane владеет своей очередью.
5. Классификация самокорректируется: после вступления is_non_combat_player
   == False.
"""
import json

import pytest

from libs.session.base import BaseSessionMixin
from libs.session.resolution import ResolutionMixin
from libs.session.round_coordinator import RoundCoordinatorMixin
from libs.session.manual_rolls import ManualRollsMixin
from libs.session.combat_coordinator import CombatCoordinatorMixin
from libs.db import Combatant, QueueState
from tests.conftest import FakeDB, FakeDBManager, FakePlayer, FakeChar


# ═══════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════

class FakeDM:
    def __init__(self, delay: float = 0.01):
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def process_master_turn(self, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            import asyncio
            await asyncio.sleep(self.delay)
            self.calls.append(dict(kwargs.get("player_actions") or {}))
            return {
                "player_text": "нарратив",
                "gm_log": "GM",
                "raw_narrative": "нарратив",
                "tool_audit": [],
            }
        finally:
            self.active -= 1


class FakeMemoryStore:
    async def build_context_digest(self, session_id, query):
        return ""

    async def remember(self, session_id, text, source_type=""):
        pass

    async def maybe_sleep(self, session_id):
        pass


class SimpleEncounter:
    def __init__(self, id="e1", session_id="s1"):
        self.id = id
        self.session_id = session_id
        self.is_active = True
        self.reason = ""
        self.round_number = 1
        self.location_name = ""
        self.created_at = ""


class CombatDB(FakeDB):
    """FakeDB + боевой слой: encounters, combatants, порядок инициативы."""

    def __init__(self):
        super().__init__()
        self.encounter = None
        self.combatants = []

    # -- encounters --
    def create_combat_encounter(self, encounter):
        self.encounter = encounter

    def get_active_combat_encounter(self, session_id):
        if self.encounter and self.encounter.is_active:
            return self.encounter
        return None

    def end_combat_encounter(self, encounter_id):
        if self.encounter and self.encounter.id == encounter_id:
            self.encounter.is_active = False

    def increment_combat_round(self, encounter_id):
        if self.encounter:
            self.encounter.round_number += 1

    # -- combatants --
    def add_combatant(self, combatant: Combatant):
        self.combatants.append(combatant)

    def get_combatants(self, encounter_id):
        return [c for c in self.combatants
                if c.encounter_id == encounter_id and c.is_alive]

    def get_initiative_order(self, encounter_id):
        rows = sorted(
            (c for c in self.combatants
             if c.encounter_id == encounter_id and c.is_alive),
            key=lambda c: (c.initiative, c.natural_roll, c.sort_order),
            reverse=True,
        )
        return [{"name": c.name, "entity_type": c.entity_type,
                 "player_id": c.player_id, "initiative": c.initiative,
                 "natural_roll": c.natural_roll, "sort_order": c.sort_order}
                for c in rows]

    def update_session(self, session):
        pass


class SeqRand:
    """Детерминированный random.randint: выдаёт значения по очереди."""

    def __init__(self, *values):
        self.values = list(values)
        self.i = 0

    def __call__(self, a, b):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


class CombatHost(BaseSessionMixin, ResolutionMixin, RoundCoordinatorMixin,
                 CombatCoordinatorMixin, ManualRollsMixin):
    def __init__(self, db: FakeDB, dm: FakeDM = None):
        BaseSessionMixin.__init__(self, FakeDBManager(db), dm or FakeDM())
        self.memory_store = FakeMemoryStore()


def make_combat_db() -> CombatDB:
    """Бой: Храфна (игрок 101, init 12) против Слизня (NPC, init 8)."""
    db = CombatDB()
    db.players = [
        FakePlayer(101, username="hrafna", display_name="Храфна"),
        FakePlayer(103, username="rimi", display_name="Рими"),
    ]
    db.chars = [
        FakeChar(id=1, player_id=101, name="Храфна"),
        FakeChar(id=2, player_id=103, name="Рими"),
    ]
    return db


def start_basic_combat(host: CombatHost, db: CombatDB, monkeypatch=None,
                       rolls=(8,)):
    """Храфна pre-rolled 10 (+2 dex = 12), Слизень — серверный бросок.
    monkeypatch обязателен ДО старта: иначе NPC-бросок реальный и порядок
    инициативы недетерминирован (ролл > 12 перевернёт очередь ходов).
    rolls — последовательность для random.randint: первый уходит NPC,
    последующие — будущим join'ам."""
    if monkeypatch:
        monkeypatch.setattr("random.randint", SeqRand(*rolls))
    host.start_combat_for_participants(
        "s1", ["Храфна", "Слизень"], "тест",
        pc_initiatives={"Храфна": {"natural": 10, "total": 12}},
    )
    assert db._session.combat_active
    assert host.get_current_initiative_turn("s1")["name"] == "Храфна"
    return db


# ═══════════════════════════════════════════════════════════════
# 1. Вступление в идущий бой
# ═══════════════════════════════════════════════════════════════

class TestJoinActiveCombat:

    def test_join_adds_pc_and_preserves_current_turn(self, monkeypatch):
        """Рими (init 19) вступает в бой Храфна(12) vs Слизень(8):
        порядок пересобран, НО ходит по-прежнему Храфна — вставка
        не скипает и не дублирует ничей ход."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        monkeypatch.setattr("random.randint", SeqRand(17))  # d20 Рими
        result = host.join_active_combat("s1", db.chars[1])

        assert result["joined"] is True
        assert result["name"] == "Рими"
        assert result["initiative"] == 19  # 17 + 2 dex

        order = db.get_initiative_order(db.encounter.id)
        names = [e["name"] for e in order]
        # Рими 19 (17+2 dex) > Храфна 12 > Слизень 8
        assert names == ["Рими", "Храфна", "Слизень"]

        # Текущий ход НЕ сбился
        current = host.get_current_initiative_turn("s1")
        assert current["name"] == "Храфна"

        # Рими в БД как pc с player_id
        combatants = db.get_combatants(db.encounter.id)
        rimi = next(c for c in combatants if c.name == "Рими")
        assert rimi.entity_type == "pc"
        assert rimi.player_id == 103

        # session.initiative_order обновился
        sess_order = json.loads(db._session.initiative_order)
        assert len(sess_order) == 3
        assert sess_order[0]["name"] == "Рими"

    def test_join_twice_returns_already(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        monkeypatch.setattr("random.randint", SeqRand(17))
        first = host.join_active_combat("s1", db.chars[1])
        assert first["joined"] is True

        second = host.join_active_combat("s1", db.chars[1])
        assert second["joined"] is False
        assert second["reason"] == "already"

        # В бою всё ещё один Рими
        rimi_rows = [c for c in db.combatants if c.name == "Рими"]
        assert len(rimi_rows) == 1

    def test_join_without_combat_rejected(self):
        db = make_combat_db()
        host = CombatHost(db)  # бой не стартовали
        result = host.join_active_combat("s1", db.chars[1])
        assert result["joined"] is False
        assert result["reason"] == "no_combat"

    def test_join_unlinked_char_rejected(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))
        ghost = FakeChar(id=9, player_id=0, name="Безымянный")
        result = host.join_active_combat("s1", ghost)
        assert result["joined"] is False
        assert result["reason"] == "unlinked"

    def test_join_by_name_returns_tool_text_and_records_join(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8, 17))

        text = host.join_active_combat_by_name("s1", "Рими")

        assert "Рими" in text
        assert "19" in text, "инициатива должна попасть в tool-result"

        joins = host.pop_combat_joins("s1")
        assert len(joins) == 1
        assert joins[0]["name"] == "Рими"
        assert joins[0]["initiative"] == 19
        # pop опустошает
        assert host.pop_combat_joins("s1") == []

    def test_join_by_name_unknown_char(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))
        text = host.join_active_combat_by_name("s1", "Гоблин-третий")
        assert "не найден" in text
        assert host.pop_combat_joins("s1") == []

    @pytest.mark.asyncio
    async def test_combat_joiner_async_wrapper(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8, 17))

        text = await host._combat_joiner_async("s1", "Рими")
        assert "добавлен" in text
        assert len(host.pop_combat_joins("s1")) == 1

    def test_join_corrects_non_combat_classification(self, monkeypatch):
        """ГЛАВНЫЙ КЕЙС: до вступления Рими «не в бою» (non-combat lane),
        после — полноценный боевой участник. Больше не застрянет в
        параллельном резолвере и не заблокирует главную очередь."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8, 17))

        assert host.is_non_combat_player("s1", 103) is True

        assert host.join_active_combat("s1", db.chars[1])["joined"]

        assert host.is_non_combat_player("s1", 103) is False
        # и он действительно в боевом порядке
        all_ids = [e["player_id"] for e in db.get_initiative_order(db.encounter.id)]
        assert 103 in all_ids

    def test_join_after_round_wrap_still_reaches_joiner(self, monkeypatch):
        """Присоединившийся с низкой инициативой будет достигнут в этом же
        раунде: после действий текущего PC порядок доходит до него."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8, 1))

        result = host.join_active_combat("s1", db.chars[1])
        assert result["joined"]

        order = db.get_initiative_order(db.encounter.id)
        # Храфна ходит сейчас (индекс указывает на неё)
        idx = db._session.current_turn_index
        assert order[idx]["name"] == "Храфна"
        # Рими в порядке и ниже Храфны
        rimi_pos = next(i for i, e in enumerate(order) if e["name"] == "Рими")
        assert rimi_pos > idx


# ═══════════════════════════════════════════════════════════════
# 2. Синхронизация очереди с боевой группой
# ═══════════════════════════════════════════════════════════════

class TestSyncCombatQueue:

    def test_rebuilds_stale_all_players_queue(self, monkeypatch):
        """Класс из лога: all-players очередь [101,102,103] пережила начало
        боя. Синк пересобирает её в single-player для текущего PC."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([101, 102, 103]),
            collected_actions="{}",
            is_resolving=False,
        )

        current = host.sync_combat_queue("s1")
        assert current == 101

        q = db.get_queue_state("s1")
        assert json.loads(q.waiting_for) == [101]
        assert json.loads(q.collected_actions) == {}

    def test_keeps_fresh_single_player_queue(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        host.start_single_player_collection("s1", 101)
        before = db.get_queue_state("s1")
        assert host.sync_combat_queue("s1") == 101
        after = db.get_queue_state("s1")
        assert before is after, "консистентная очередь не должна пересоздаваться"

    def test_does_not_wipe_submitted_action(self, monkeypatch):
        """Игрок уже сходил (waiting=[] + collected) — синк НЕ должен
        пересобрать очередь и дать ему сходить второй раз."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([]),
            collected_actions=json.dumps({"101": "атакую слизь"}),
            is_resolving=False,
        )
        assert host.sync_combat_queue("s1") == 101
        q = db.get_queue_state("s1")
        assert json.loads(q.waiting_for) == []
        assert json.loads(q.collected_actions) == {"101": "атакую слизь"}

    def test_noop_during_npc_turn(self, monkeypatch):
        """Во время хода NPC очередь не трогаем: pc-ветка хендлера всё равно
        вернёт «Ход NPC — ожидайте», а NPC-цикл сам управляет состоянием."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        db._session.current_turn_index = 1  # Слизень (npc)
        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([101, 103]),
            collected_actions="{}",
            is_resolving=False,
        )
        assert host.sync_combat_queue("s1") is None
        q = db.get_queue_state("s1")
        assert json.loads(q.waiting_for) == [101, 103], "очередь изменена во время NPC-хода"

    def test_noop_outside_combat(self):
        db = make_combat_db()
        host = CombatHost(db)  # combat_active=False
        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([101, 103]),
            collected_actions="{}",
            is_resolving=False,
        )
        assert host.sync_combat_queue("s1") is None


# ═══════════════════════════════════════════════════════════════
# 3. Non-combat остатки: merge ТОЛЬКО после конца боя
# ═══════════════════════════════════════════════════════════════

class TestLeftoversMergeGuard:

    @pytest.mark.asyncio
    async def test_leftovers_not_stolen_while_combat_active(self, monkeypatch):
        """Пока бой идёт, обычный резолв НЕ имеет права утащить действия из
        non-combat очереди и написать параллельную версию боевой сцены."""
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        host.submit_non_combat_action("s1", 103, "бежит к слизи с кружкой")
        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([]),
            collected_actions=json.dumps({"101": "атакую"}),
            is_resolving=False,
        )
        await host.resolve_round_master_only("s1")

        # Очередь non-combat НЕ тронута
        assert host.get_non_combat_queue("s1") == {103: "бежит к слизи с кружкой"}
        # И к Мастеру действие не попало
        assert host.dm.calls, "обычный раунд должен был резолвиться"
        assert "Дн. бежит к слизи с кружкой" not in host.dm.calls[0].values()

    @pytest.mark.asyncio
    async def test_leftovers_merged_after_combat_ends(self, monkeypatch):
        db = make_combat_db()
        host = CombatHost(db)
        start_basic_combat(host, db, monkeypatch, rolls=(8,))

        host.submit_non_combat_action("s1", 103, "договариваюсь с барменом")
        host.end_initiative_combat("s1", "слизь добита")
        assert not db._session.combat_active

        db._queue = QueueState(
            session_id="s1",
            waiting_for=json.dumps([]),
            collected_actions="{}",
            is_resolving=False,
        )
        await host.resolve_round_master_only("s1")

        assert "Дн. договариваюсь с барменом" in host.dm.calls[0].values()
        assert host.get_non_combat_queue("s1") == {}
