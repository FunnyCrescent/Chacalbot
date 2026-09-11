"""ИТЕРАЦИЯ 8 — Баг 1 (ложное «не ответил вовремя»), Баг 2 (/rholio),
Баг 3 (Мастер не умеет заканчивать бой), Дополнение 1 (скрытый канал
Мастер → DB о состояниях НПС).

Покрывается:
  A. _route_deferred_resolve — отложенный резолв уважает боевой режим
     (раньше боевой ход резолвился как обычный раунд → инициатива не
     сдвигалась → через 5 минут «игрок не ответил вовремя», хотя он успел).
  B. sync_npc_state — скрытое хранилище состояний НПС (Combatant в бою /
     session_meta вне боя), раскрытие через revealed.
  C. get_combat_context — HP/состояния НПС приходят Мастеру с пометкой
     СКРЫТО + правило раскрытия по навыкам.
  D. _apply_game_actions — change_hp/add_condition/remove_condition по имени
     НПС скрыто обновляют Combatant (раньше — «char not found», HP НПС
     не сохранялись нигде → Мастер не мог понять, что бой окончен).
  E. Детерминированный /rholio — стандартные навыки/спасброски считаются
     на сервере из БД (работает с локальной Gemma без tool-calls), LLM —
     только fallback для экзотики.
"""
import json
from types import SimpleNamespace

import pytest

from libs.db import Combatant, HistoryEntry, QueueState
from libs.session.combat_coordinator import CombatCoordinatorMixin
from libs.session.resolution import ResolutionMixin
from libs.session import manual_rolls as mr
from tests.conftest import FakeDB, FakeDBManager, FakeSession


# ═══════════════════════════════════════════════════════════════
# A. Маршрут отложенного резолва (Баг 1, часть А)
# ═══════════════════════════════════════════════════════════════

from libs.handlers.engine import _route_deferred_resolve


def _queue(collected: dict = None, broken: bool = False):
    return QueueState(
        session_id="s1",
        waiting_for="[]",
        collected_actions="{bad json" if broken else json.dumps(collected or {}),
        is_resolving=True,
    )


class TestDeferredResolveRoute:
    def test_combat_action_routes_to_combat_path(self):
        sess = FakeSession(combat_active=True)
        route, action = _route_deferred_resolve(sess, _queue({101: "Атакую разбойника"}))
        assert route == "combat"
        assert action == "Атакую разбойника"

    def test_non_combat_routes_to_normal_path(self):
        sess = FakeSession(combat_active=False)
        route, action = _route_deferred_resolve(sess, _queue({101: "Ищу следы"}))
        assert route == "normal"
        assert action == "Ищу следы"

    def test_empty_queue_after_combat_end_skips(self):
        # бой закончился, очередь пересобрана (end_initiative_combat) →
        # собранное действие потеряно вместе с боем — начинаем новый сбор
        sess = FakeSession(combat_active=True)
        route, action = _route_deferred_resolve(sess, _queue({}))
        assert route == "skip"
        assert action == ""

    def test_none_session_and_none_queue_skips(self):
        route, action = _route_deferred_resolve(None, None)
        assert route == "skip"

    def test_broken_json_is_not_crash(self):
        sess = FakeSession(combat_active=True)
        route, _ = _route_deferred_resolve(sess, _queue(broken=True))
        assert route == "skip"

    def test_action_text_preserved_verbatim(self):
        sess = FakeSession(combat_active=True)
        text = "Дн. Атакую разбойника уроборосом, двигаюсь к воротам"
        _, action = _route_deferred_resolve(sess, _queue({55: text}))
        assert action == text


# ═══════════════════════════════════════════════════════════════
# Fakes с combat-поверхностью (Combatant-объекты)
# ═══════════════════════════════════════════════════════════════

class FakeEncounter:
    def __init__(self, id="e1"):
        self.id = id
        self.session_id = "s1"
        self.active = True


class CombatDB(FakeDB):
    """FakeDB + encounters/combatants — для скрытого канала НПС."""

    def __init__(self):
        super().__init__()
        self.encounter = None
        self.combatants: dict = {}   # id -> Combatant
        self.updated_combatants: list = []  # (id, kwargs)

    def get_active_combat_encounter(self, session_id):
        return self.encounter

    def get_combatants(self, encounter_id):
        return [c for c in self.combatants.values() if c.encounter_id == encounter_id]

    def update_combatant(self, combatant_id, **kwargs):
        self.updated_combatants.append((combatant_id, kwargs))
        c = self.combatants[combatant_id]
        for k, v in kwargs.items():
            setattr(c, k, v)

    def add_combatant_obj(self, c: Combatant):
        self.combatants[c.id] = c


def make_npc(cid="n1", name="Разбойник", hp=15, max_hp=22, conds=None, traits=""):
    c = Combatant(id=cid, encounter_id="e1", session_id="s1", name=name)
    c.entity_type = "npc"
    c.player_id = 0
    c.initiative = 10
    c.hp = hp
    c.max_hp = max_hp
    c.current_conditions = json.dumps(conds or [], ensure_ascii=False)
    c.is_alive = True
    c.traits = traits or ""
    return c


class CombatBot(CombatCoordinatorMixin):
    def __init__(self, db: CombatDB):
        self.db_manager = FakeDBManager(db)


# ═══════════════════════════════════════════════════════════════
# B. Скрытый канал: sync_npc_state (Дополнение 1)
# ═══════════════════════════════════════════════════════════════

class TestSyncNpcState:
    def _bot(self, db=None):
        db = db or CombatDB()
        return CombatBot(db), db

    async def test_in_combat_updates_combatant_hp_and_conditions(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc())
        text = await bot._npc_state_syncer_async("s1", {
            "npc_name": "Разбойник", "hp_current": 7, "hp_max": 22,
            "conditions": ["сбит с ног"],
        })
        comb = db.combatants["n1"]
        assert comb.hp == 7
        assert json.loads(comb.current_conditions) == ["сбит с ног"]
        assert comb.is_alive is True
        assert "СКРЫТО" in text or "сохранено" in text.lower()

    async def test_zero_hp_marks_dead(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc())
        await bot._npc_state_syncer_async("s1", {"npc_name": "Разбойник", "hp_current": 0})
        assert db.combatants["n1"].hp == 0
        assert db.combatants["n1"].is_alive is False

    async def test_hp_clamped_to_max(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc(hp=15, max_hp=22))
        await bot._npc_state_syncer_async("s1", {"npc_name": "Разбойник", "hp_current": 999})
        assert db.combatants["n1"].hp == 22

    async def test_partial_name_match(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc(cid="n2", name="Гоблин-вожак", hp=9))
        await bot._npc_state_syncer_async("s1", {"npc_name": "Гоблин", "hp_current": 4})
        assert db.combatants["n2"].hp == 4

    async def test_note_and_revealed_stored_in_traits(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc())
        await bot._npc_state_syncer_async("s1", {
            "npc_name": "Разбойник", "note": "боится огня", "revealed": True,
        })
        traits = json.loads(db.combatants["n1"].traits)
        assert traits["gm_note"] == "боится огня"
        assert traits["revealed"] is True

    async def test_out_of_combat_goes_to_meta_notebook(self):
        bot, db = self._bot()  # encounter = None
        await bot._npc_state_syncer_async("s1", {
            "npc_name": "Торговец Бран", "hp_current": 11, "hp_max": 11,
            "conditions": ["пьян"], "note": "знает проход в канализацию",
        })
        states = json.loads(db.get_meta("npc_states"))
        entry = states["торговец бран"]
        assert entry["hp"] == 11
        assert entry["conditions"] == ["пьян"]
        assert "канализацию" in entry["note"]

    async def test_combatant_not_found_falls_back_to_notebook(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()  # бой есть, но НПС не участник
        db.add_combatant_obj(make_npc(cid="n1", name="Разбойник"))
        await bot._npc_state_syncer_async("s1", {"npc_name": "Крестьянин", "hp_current": 3})
        states = json.loads(db.get_meta("npc_states"))
        assert states["крестьянин"]["hp"] == 3
        # участник боя не задет
        assert db.combatants["n1"].hp == 15

    async def test_empty_name_rejected(self):
        bot, db = self._bot()
        text = await bot._npc_state_syncer_async("s1", {"npc_name": "  "})
        assert "not saved" in text

    async def test_explicit_is_alive_false(self):
        bot, db = self._bot()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc(hp=5))
        await bot._npc_state_syncer_async("s1", {"npc_name": "Разбойник", "is_alive": False})
        assert db.combatants["n1"].is_alive is False


# ═══════════════════════════════════════════════════════════════
# C. Боевой контекст: HP НПС приходят СКРЫТО (Баг 3 + Дополнение 1)
# ═══════════════════════════════════════════════════════════════

class TestCombatContextNpcHp:
    def _bot_with_combat(self, db=None):
        db = db or CombatDB()
        db.encounter = FakeEncounter()
        # pc-участник
        pc = FakeCharWithStats()
        db.chars = [pc]
        db.initiative = [
            {"name": pc.name, "entity_type": "pc", "player_id": 7,
             "initiative": 15, "natural_roll": 15, "sort_order": 0},
            {"name": "Разбойник", "entity_type": "npc", "player_id": 0,
             "initiative": 8, "natural_roll": 8, "sort_order": 1},
        ]
        db.get_initiative_order = lambda encounter_id: list(db.initiative)
        db.add_combatant_obj(make_npc())
        db.add_combatant_obj(make_npc(cid="pc1", name=pc.name, hp=10, max_hp=12))
        db.combatants["pc1"].entity_type = "pc"
        return CombatBot(db), db

    def test_npc_hp_present_and_marked_hidden(self):
        bot, db = self._bot_with_combat()
        ctx = bot.get_combat_context("s1")
        assert "HP (СКРЫТО): 15/22" in ctx
        assert "Разбойник" in ctx
        assert "СЕКРЕТНО" in ctx  # правило раскрытия
        assert "diweddaru_npc" in ctx

    def test_conditions_and_note_rendered(self):
        bot, db = self._bot_with_combat()
        db.combatants["n1"].current_conditions = json.dumps(["сбит с ног"], ensure_ascii=False)
        db.combatants["n1"].traits = json.dumps({"gm_note": "боится огня"}, ensure_ascii=False)
        ctx = bot.get_combat_context("s1")
        assert "сбит с ног" in ctx
        assert "боится огня" in ctx

    def test_revealed_flag_shown_to_master(self):
        bot, db = self._bot_with_combat()
        db.combatants["n1"].traits = json.dumps({"revealed": True}, ensure_ascii=False)
        ctx = bot.get_combat_context("s1")
        assert "РАСКРЫТО" in ctx

    def test_pc_line_unchanged(self):
        bot, db = self._bot_with_combat()
        ctx = bot.get_combat_context("s1")
        # у ПК HP остаётся видимым в том же формате, что раньше
        assert "HP 10/12 | KB" in ctx

    def test_hidden_notebook_rendered(self):
        bot, db = self._bot_with_combat()
        db.set_meta("npc_states", json.dumps(
            {"торговец": {"hp": 4, "hp_max": 5, "conditions": ["испуган"], "note": "врёт о товаре"}},
            ensure_ascii=False))
        ctx = bot.get_combat_context("s1")
        assert "торговец" in ctx
        assert "врёт о товаре" in ctx


class FakeCharWithStats:
    def __init__(self):
        self.id = "c1"
        self.player_id = 7
        self.name = "Калючка"
        self.hp = 10
        self.max_hp = 12
        self.ac = 14
        self.languages = "[]"


# ═══════════════════════════════════════════════════════════════
# D. _apply_game_actions: НПС-бойцы обновляются скрыто (Баг 3)
# ═══════════════════════════════════════════════════════════════

class ResolutionBot(ResolutionMixin):
    def __init__(self, db: CombatDB):
        self.db_manager = FakeDBManager(db)

    def change_hp(self, session_id, char_id, char_name, delta, source="AI"):
        self.hp_calls = getattr(self, "hp_calls", []) + [(char_id, delta)]

    def add_condition(self, session_id, char_id, char_name, cond, source="AI", duration=""):
        self.cond_calls = getattr(self, "cond_calls", []) + [(char_id, cond)]

    def remove_condition(self, session_id, char_id, cond):
        self.rm_cond_calls = getattr(self, "rm_cond_calls", []) + [(char_id, cond)]


class TestApplyGameActionsNpcFallback:
    def _bot(self):
        db = CombatDB()
        db.encounter = FakeEncounter()
        db.add_combatant_obj(make_npc(hp=15, max_hp=22))
        return ResolutionBot(db), db

    def test_change_hp_npc_updates_combatant(self):
        bot, db = self._bot()
        applied, errors = bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Разбойник", "delta": -8}},
        ])
        assert applied == 1 and errors == []
        assert db.combatants["n1"].hp == 7
        assert db.combatants["n1"].is_alive is True

    def test_change_hp_npc_to_zero_marks_dead(self):
        bot, db = self._bot()
        bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Разбойник", "delta": -50}},
        ])
        assert db.combatants["n1"].hp == 0
        assert db.combatants["n1"].is_alive is False

    def test_change_hp_heal_clamped_to_max(self):
        bot, db = self._bot()
        db.combatants["n1"].hp = 18
        bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Разбойник", "delta": 100}},
        ])
        assert db.combatants["n1"].hp == 22

    def test_add_and_remove_condition_npc(self):
        bot, db = self._bot()
        bot._apply_game_actions("s1", [
            {"tool_name": "add_condition", "arguments": {"character_name": "Разбойник", "condition": "Сбит с ног"}},
        ])
        assert json.loads(db.combatants["n1"].current_conditions) == ["сбит с ног"]
        bot._apply_game_actions("s1", [
            {"tool_name": "remove_condition", "arguments": {"character_name": "Разбойник", "condition": "сбит с ног"}},
        ])
        assert json.loads(db.combatants["n1"].current_conditions) == []

    def test_add_condition_no_duplicates(self):
        bot, db = self._bot()
        db.combatants["n1"].current_conditions = json.dumps(["сбит с ног"], ensure_ascii=False)
        bot._apply_game_actions("s1", [
            {"tool_name": "add_condition", "arguments": {"character_name": "Разбойник", "condition": "сбит с ног"}},
        ])
        assert json.loads(db.combatants["n1"].current_conditions) == ["сбит с ног"]

    def test_unknown_name_is_error(self):
        bot, db = self._bot()
        applied, errors = bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Неведомый", "delta": -3}},
        ])
        assert applied == 0
        assert errors and "Неведомый" in errors[0]

    def test_pc_char_still_routed_to_character_path(self):
        bot, db = self._bot()
        db.chars = [FakeCharWithStats()]
        applied, errors = bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Калючка", "delta": -4}},
        ])
        assert applied == 1 and errors == []
        assert bot.hp_calls == [("c1", -4)]
        # НПС не задет
        assert db.combatants["n1"].hp == 15

    def test_no_encounter_npc_name_is_error(self):
        db = CombatDB()  # encounter не создан
        bot = ResolutionBot(db)
        applied, errors = bot._apply_game_actions("s1", [
            {"tool_name": "change_hp", "arguments": {"character_name": "Гоблин", "delta": -3}},
        ])
        assert applied == 0 and errors


# ═══════════════════════════════════════════════════════════════
# E. Детерминированный /rholio (Баг 2)
# ═══════════════════════════════════════════════════════════════

def make_char():
    return SimpleNamespace(
        name="Калючка", level=3,
        stats=json.dumps({"strength": 16, "dexterity": 14, "constitution": 12,
                          "intelligence": 10, "wisdom": 13, "charisma": 8}),
        proficiencies=json.dumps(["Атлетика (Сила)", "Внимательность", "Спасброски: Мудрость"]),
        strength=16, dexterity=14, constitution=12,
        intelligence=10, wisdom=13, charisma=8,
    )


class RollsBot(mr.ManualRollsMixin):
    def __init__(self, db: FakeDB, dm=None):
        self.db_manager = FakeDBManager(db)
        self.dm = dm
        self._pending_manual_rolls = {}
        self._pending_manual_roll_skills = {}


class RollsDB(FakeDB):
    def get_character_sheet(self, session_id, player_id):
        return "лист"


def _db_with_char():
    db = RollsDB()
    ch = make_char()
    ch.id = "c1"
    ch.player_id = 7
    db.chars = [ch]
    return db


class TestDeterministicRoll:
    def test_skill_with_proficiency(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "Атлетика")
        assert res and res["ok"]
        # STR16 → +3, уровень 3 → PB +2, всего +5
        assert res["total"] == res["natural"] + 5
        assert "владение" in res["display"]
        assert "Сила" in res["display"]

    def test_save_without_proficiency(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "спасбросок телосложения")
        assert res and res["ok"]
        # TEL12 → +1, нет владения
        assert res["total"] == res["natural"] + 1
        assert "владение" not in res["display"]
        assert "Телосложение" in res["display"]

    def test_save_proficiency_from_sheet(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "спасбросок мудрости")
        assert res and res["ok"]
        # WIS13 → +1, владение +2 → +3
        assert res["total"] == res["natural"] + 3

    def test_perception_stem(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "внимательность")
        assert res and res["ok"]
        # WIS13 → +1 + PB2 = +3
        assert res["total"] == res["natural"] + 3

    def test_advantage_keyword(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "атлетика с преимуществом")
        assert res and res["ok"]
        assert "преимущество" in res["display"]

    def test_unknown_check_returns_none(self):
        bot = RollsBot(_db_with_char())
        assert bot._deterministic_manual_roll(make_char(), "абракадабра") is None

    def test_prefix_check_word_stripped(self):
        bot = RollsBot(_db_with_char())
        res = bot._deterministic_manual_roll(make_char(), "проверка атлетики")
        assert res and res["ok"]


class TestResolveManualRollFlow:
    async def test_standard_check_never_calls_llm(self):
        db = _db_with_char()
        strict = SimpleNamespace(
            db_bot=SimpleNamespace(chat=None),
            _execute_roll=None,
        )
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("LLM must not be called")

        bot = RollsBot(db)
        bot.dm = SimpleNamespace(
            db_bot=SimpleNamespace(chat=_boom),
            _execute_roll=_boom,
        )
        res = await bot.resolve_manual_roll("s1", 7, "атлетика")
        assert res["ok"] is True
        assert called["n"] == 0
        assert res["roll_number"] == 1
        assert res["remaining_rolls"] == mr.MAX_MANUAL_ROLLS_PER_ROUND - 1

    async def test_duplicate_skill_rejected(self):
        bot = RollsBot(_db_with_char())
        await bot.resolve_manual_roll("s1", 7, "атлетика")
        res2 = await bot.resolve_manual_roll("s1", 7, "атлетика")
        assert res2["ok"] is False
        assert "уже кидал" in res2["error"]

    async def test_per_round_limit(self, monkeypatch):
        monkeypatch.setattr(mr, "MAX_MANUAL_ROLLS_PER_ROUND", 1)
        bot = RollsBot(_db_with_char())
        await bot.resolve_manual_roll("s1", 7, "атлетика")
        res = await bot.resolve_manual_roll("s1", 7, "убеждение")
        assert res["ok"] is False
        assert "Лимит" in res["error"]

    async def test_exotic_falls_back_to_llm_text_error(self):
        class FakeLLM:
            async def chat(self, messages, **kwargs):
                return {"choices": [{"message": {"content": "Не понимаю такой бросок"}}]}

        bot = RollsBot(_db_with_char())
        bot.dm = SimpleNamespace(db_bot=FakeLLM())
        res = await bot.resolve_manual_roll("s1", 7, "заварить чай с преимуществом судьбы")
        assert res["ok"] is False
        assert "Не понимаю" in res["error"]

    async def test_exotic_falls_back_to_llm_tool_roll(self):
        class FakeLLM:
            async def chat(self, messages, **kwargs):
                return {"choices": [{"message": {"tool_calls": [{
                    "id": "t1",
                    "function": {"name": "roll_dice",
                                 "arguments": json.dumps(
                                     {"count": 1, "sides": 20, "modifier": 2,
                                      "label": "Калючка — экзотика", "visible": True})},
                }]}}]}

        bot = RollsBot(_db_with_char())
        bot.dm = SimpleNamespace(
            db_bot=FakeLLM(),
            _execute_roll=lambda args: {
                "display": "🎲 1d20+2 → [7]+2 = **9**",
                "result": "nat 7, total 9", "natural": 7, "total": 9, "visible": True,
            },
        )
        res = await bot.resolve_manual_roll("s1", 7, "экзотический бросок древних")
        assert res["ok"] is True
        assert res["total"] == 9

    async def test_llm_broken_json_args_no_crash(self):
        class FakeLLM:
            async def chat(self, messages, **kwargs):
                return {"choices": [{"message": {"tool_calls": [{
                    "id": "t1",
                    "function": {"name": "roll_dice", "arguments": "{broken"},
                }]}}]}

        bot = RollsBot(_db_with_char())
        bot.dm = SimpleNamespace(db_bot=FakeLLM())
        res = await bot.resolve_manual_roll("s1", 7, "экзотический бросок древних")
        assert res["ok"] is False
        assert "error" in res

    async def test_no_character_error(self):
        db = RollsDB()
        bot = RollsBot(db)
        res = await bot.resolve_manual_roll("s1", 7, "атлетика")
        assert res["ok"] is False and "персонаж" in res["error"]
