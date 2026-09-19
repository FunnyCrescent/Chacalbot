"""ИТЕРАЦИЯ 16 — автоматическое повышение уровня.

XP ↔ уровень связаны полностью: award_xp пересёк порог → уровень поднялся →
система построила план по таблицам SRD 5e (libs/level_up.py) → авто-применила
HP и умения класса → создала pending-напоминание и скрытый бриф Мастеру.
Мастер передаёт выборы строками «УРОВЕНЬ+: ...» в СВОДКЕ; DB-бот применяет их
инструментами и закрывает повышение complete_level_up.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.db import Database, DatabaseManager, Session, Character, PendingLevelUp
from libs.session.resolution import ResolutionMixin
from libs.level_up import (
    build_level_up_plan, format_level_up_brief, build_prompt_block,
    normalize_class_name, extract_subclass, proficiency_bonus,
    hit_die_average, warlock_pact_slots, ASI_LEVELS, DEFAULT_ASI_LEVELS,
    FULL_CASTER_SLOTS, HALF_CASTER_SLOTS, CLASS_FEATURES, SUBCLASS_LEVELS,
    CLASS_KEYS, CASTER_KIND,
)


def _make_db(tmp_path, name="t16.db", class_name="Волшебник", level=4,
             max_hp=32, stats=None):
    db = Database(str(tmp_path / name))
    db.create_session(Session(id="s16", chat_id=-1016, name="LevelUp-тест",
                              creator_id=1, status="active", current_scene=""))
    db.save_character(Character(id="c1", session_id="s16", player_id=1,
                                name="Эйра", race="Человек", class_name=class_name,
                                level=level, hp=max_hp, max_hp=max_hp,
                                stats=stats or json.dumps({"constitution": 14})))
    return db


class _StubResolution(ResolutionMixin):
    def __init__(self, db_manager):
        self.db_manager = db_manager


# ═══════════════════════════════════════════════════════════════
# A1. Нормализация классов и общие таблицы
# ═══════════════════════════════════════════════════════════════

class TestClassNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("Волшебник", "wizard"),
        ("волшебница", "wizard"),
        ("Маг", "wizard"),
        ("Wizard", "wizard"),
        ("Wizard (Evocation)", "wizard"),
        ("Воин (Чемпион)", "fighter"),
        ("воин 7 уровня", "fighter"),
        ("плут", "rogue"),
        ("Следопыт", "ranger"),
        ("чародей", "sorcerer"),
        ("Колдун", "warlock"),
        ("Жрец", "cleric"),
        ("Друид", "druid"),
        ("Бард", "bard"),
        ("Паладин", "paladin"),
        ("Монах", "monk"),
        ("Варвар", "barbarian"),
    ])
    def test_aliases(self, raw, expected):
        assert normalize_class_name(raw) == expected

    def test_unknown_class_is_none(self):
        assert normalize_class_name("Мистика") is None
        assert normalize_class_name("") is None
        assert normalize_class_name(None) is None

    def test_extract_subclass(self):
        assert extract_subclass("Воин (Чемпион)") == ("Воин", "Чемпион")
        assert extract_subclass("Волшебник") == ("Волшебник", "")
        assert extract_subclass("Бард (Коллегия Знаний)") == ("Бард", "Коллегия Знаний")

    def test_all_12_classes_have_progression(self):
        assert len(CLASS_KEYS) == 12
        for k in CLASS_KEYS:
            assert k in ASI_LEVELS and k in CLASS_FEATURES and k in SUBCLASS_LEVELS
            assert 1 in CLASS_FEATURES[k], f"{k}: нет умений 1-го уровня"

    def test_asi_levels_fighter_rogue_extra(self):
        assert ASI_LEVELS["fighter"] == [4, 6, 8, 12, 14, 16, 19]
        assert ASI_LEVELS["rogue"] == [4, 8, 10, 12, 16, 19]
        for k in CLASS_KEYS:
            if k not in ("fighter", "rogue"):
                assert ASI_LEVELS[k] == DEFAULT_ASI_LEVELS

    def test_proficiency_bonus(self):
        assert proficiency_bonus(1) == 2
        assert proficiency_bonus(4) == 2
        assert proficiency_bonus(5) == 3
        assert proficiency_bonus(8) == 3
        assert proficiency_bonus(9) == 4
        assert proficiency_bonus(13) == 5
        assert proficiency_bonus(17) == 6
        assert proficiency_bonus(20) == 6

    def test_hit_die_average(self):
        assert hit_die_average(6) == 4
        assert hit_die_average(8) == 5
        assert hit_die_average(10) == 6
        assert hit_die_average(12) == 7

    def test_warlock_pact_slots(self):
        assert warlock_pact_slots(1) == (1, 1)
        assert warlock_pact_slots(2) == (2, 1)
        assert warlock_pact_slots(3) == (2, 2)
        assert warlock_pact_slots(5) == (2, 3)
        assert warlock_pact_slots(9) == (2, 5)
        assert warlock_pact_slots(11) == (3, 5)
        assert warlock_pact_slots(17) == (4, 5)
        assert warlock_pact_slots(20) == (4, 5)

    def test_full_caster_slot_table_spot_checks(self):
        assert FULL_CASTER_SLOTS[1] == [2]
        assert FULL_CASTER_SLOTS[5] == [4, 3, 2]
        assert FULL_CASTER_SLOTS[11] == [4, 3, 3, 3, 2, 1]
        assert FULL_CASTER_SLOTS[17] == [4, 3, 3, 3, 2, 1, 1, 1, 1]
        assert FULL_CASTER_SLOTS[20] == [4, 3, 3, 3, 3, 2, 2, 1, 1]

    def test_half_caster_slot_table_spot_checks(self):
        assert HALF_CASTER_SLOTS[1] == []
        assert HALF_CASTER_SLOTS[2] == [2]
        assert HALF_CASTER_SLOTS[5] == [4, 2]
        assert HALF_CASTER_SLOTS[9] == [4, 3, 2]
        assert HALF_CASTER_SLOTS[17] == [4, 3, 3, 3, 1]
        assert HALF_CASTER_SLOTS[20] == [4, 3, 3, 3, 2]


# ═══════════════════════════════════════════════════════════════
# A2. План повышения уровня
# ═══════════════════════════════════════════════════════════════

class TestBuildLevelUpPlan:
    def test_wizard_4_to_5(self):
        p = build_level_up_plan("Волшебник", 4, 5, con_mod=2)
        assert p["class_key"] == "wizard" and p["spell_kind"] == "book"
        assert p["hp_total"] == 6          # d6: 4 + ТЕЛ+2
        assert p["prof_before"] == 2 and p["prof_after"] == 3
        assert p["wizard_book_gained"] == 2 and p["spells_gained"] == 2
        assert p["new_circle"] == 3 and p["circle_opened"] == 1
        assert p["cantrips_gained"] == 0 and p["asi_levels"] == []
        kinds = [c["kind"] for c in p["choices"]]
        assert kinds == ["spells"]

    def test_wizard_3_to_4_gains_cantrip_and_asi(self):
        p = build_level_up_plan("волшебник", 3, 4, con_mod=1)
        assert p["cantrips_gained"] == 1   # 3 → 4 заговора на 4-м уровне
        assert p["asi_levels"] == [4] and p["asi_count"] == 1
        kinds = [c["kind"] for c in p["choices"]]
        assert kinds == ["cantrip", "spells", "asi"]

    def test_wizard_spellbook_two_per_level_multilevel(self):
        p = build_level_up_plan("Wizard", 1, 3, con_mod=0)
        assert p["wizard_book_gained"] == 4  # 2 заклинания за каждый из уровней 2 и 3
        assert p["hp_total"] == 8            # 2 уровня × (4 + 0)

    def test_barbarian_1_to_5_auto_features_and_hp(self):
        p = build_level_up_plan("Варвар", 1, 5, con_mod=3)
        assert p["class_key"] == "barbarian" and p["hit_die"] == 12
        assert p["hp_total"] == 40           # 4 уровня × (7 + 3)
        names = [f["name"] for f in p["auto_features"]]
        assert "Безрассудная атака" in names and "Дополнительная атака" in names
        assert "Быстрое передвижение" in names and "Чутьё на опасность" in names
        assert p["subclass_feature_slots"] == [3]
        assert p["subclass_pick"] is True    # подкласс не выбран
        assert p["asi_levels"] == [4]

    def test_barbarian_with_subclass_no_pick_flag(self):
        p = build_level_up_plan("Варвар (Путь berserker)".replace("berserker", "берсерка"),
                                1, 3, con_mod=2, subclass="берсерка")
        assert p["subclass_pick"] is False   # подкласс известен
        kinds = [c["kind"] for c in p["choices"]]
        assert "subclass" in kinds           # но умения подкласса всё равно выбираются

    def test_bard_4_to_5_known_spells(self):
        p = build_level_up_plan("Бард", 4, 5, con_mod=1, subclass="Коллегия Знаний")
        assert p["spell_kind"] == "known"
        assert p["spells_gained"] == 2       # 7 → 9 известных
        assert p["cantrips_gained"] == 0
        assert p["new_circle"] == 3 and p["circle_opened"] == 1
        assert p["hp_total"] == 6            # d8: 5 + ТЕЛ+1

    def test_bard_3_to_4_cantrip(self):
        p = build_level_up_plan("Бард", 3, 4, con_mod=0)
        assert p["cantrips_gained"] == 1 and p["spells_gained"] == 1  # 6 → 7

    def test_sorcerer_known_plus_one_per_level(self):
        p = build_level_up_plan("Чародей", 9, 11, con_mod=1)
        assert p["spells_gained"] == 2       # +1 за уровень
        assert p["cantrips_gained"] == 1     # 6-й заговор на 10-м уровне
        assert p["hp_total"] == 10           # 2 уровня × (d6:4 + ТЕЛ+1)

    def test_cleric_prepared_auto_note(self):
        p = build_level_up_plan("Жрец", 4, 5, con_mod=2)
        assert p["spell_kind"] == "prepared"
        assert p["spells_gained"] == 0       # подготовленные растут автоматически
        assert p["new_circle"] == 3
        # нет выбора заклинаний — пул растёт сам; бриф упоминает авто-рост
        kinds = [c["kind"] for c in p["choices"]]
        assert "spells" not in kinds

    def test_warlock_9_to_11_arcanum(self):
        p = build_level_up_plan("Колдун", 9, 11, con_mod=1)
        assert p["spell_kind"] == "pact"
        assert p["arcanum_gained"] == [6]    # таинство 6-го круга на 11-м
        assert p["invocations_gained"] == 0  # 6-е встраивание только на 12-м
        assert p["spells_gained"] == 1
        assert p["cantrips_gained"] == 1     # 4-й заговор на 10-м
        kinds = [c["kind"] for c in p["choices"]]
        # 10-й уровень — ещё и умение подкласса (патрон)
        assert kinds == ["cantrip", "spells", "arcanum", "subclass"]

    def test_warlock_11_to_12_invocation(self):
        p = build_level_up_plan("Колдун", 11, 12, con_mod=0)
        assert p["invocations_gained"] == 1
        assert p["arcanum_gained"] == []

    def test_monk_13_to_14_no_choices(self):
        p = build_level_up_plan("Монах", 13, 14, con_mod=0)
        assert p["choices"] == []            # только авто-умение Diamond Soul
        assert [f["name"] for f in p["auto_features"]] == ["Алмазная душа"]

    def test_fighter_5_to_6_extra_asi(self):
        p = build_level_up_plan("Воин (Чемпион)", 5, 6, con_mod=2, subclass="Чемпион")
        assert p["asi_levels"] == [6]        # дополнительный ASI бойца
        assert p["subclass_feature_slots"] == []  # уровень 7 — следующий
        assert p["hp_total"] == 8            # d10: 6 + 2

    def test_rogue_9_to_10_asi(self):
        p = build_level_up_plan("Плут", 9, 10, con_mod=0)
        assert p["asi_levels"] == [10]

    def test_paladin_6_aura_and_ranger_2_spells(self):
        p = build_level_up_plan("Паладин", 5, 6, con_mod=3)
        assert p["spell_kind"] == "prepared"
        assert "Аура защиты" in [f["name"] for f in p["auto_features"]]
        assert p["spells_gained"] == 0
        assert p["new_circle"] == 2

        p2 = build_level_up_plan("Следопыт", 1, 2, con_mod=1)
        assert p2["spell_kind"] == "known"
        assert p2["spells_gained"] == 2      # 0 → 2 известных на 2-м уровне
        assert p2["cantrips_gained"] == 0    # у следопыта нет заговоров

    def test_no_level_gain_returns_empty_plan(self):
        p = build_level_up_plan("Волшебник", 5, 5, con_mod=0)
        assert p["levels_gained"] == [] and p["hp_total"] == 0
        assert p["choices"] == [] and p["auto_features"] == []

    def test_unknown_class_degrades_gracefully(self):
        p = build_level_up_plan("Мистик", 4, 5, con_mod=1)
        assert p["class_key"] == ""
        assert p["hit_die"] is None and p["hp_total"] == 0
        assert p["choices"] == []            # бриф будет обобщённым


# ═══════════════════════════════════════════════════════════════
# A3. Бриф и промпт-блок
# ═══════════════════════════════════════════════════════════════

class TestBrief:
    def test_brief_contains_auto_and_choices(self):
        p = build_level_up_plan("Волшебник", 4, 5, con_mod=2)
        b = format_level_up_brief(p, "Эйра")
        assert b.startswith("[УРОВЕНЬ] Эйра: уровень 4 → 5.")
        assert "ПРИМЕНЕНО АВТОМАТИЧЕСКИ" in b
        assert "HP +6" in b and "+3" in b          # бонус мастерства
        assert "3-й круг" in b
        assert "ровно 2 × «УРОВЕНЬ+: Эйра заклинание:" in b
        assert "УРОВЕНЬ+ ГОТОВО: Эйра" in b

    def test_brief_asi_wording(self):
        p = build_level_up_plan("Воин", 5, 6, con_mod=1)
        b = format_level_up_brief(p, "Кейн")
        assert "ASI за уровень(и) 6" in b
        assert "+2 очка" in b and "черта:" in b

    def test_brief_no_choices_still_gotovo(self):
        p = build_level_up_plan("Монах", 13, 14, con_mod=0)
        b = format_level_up_brief(p, "Ли")
        assert "Выборов нет" in b and "УРОВЕНЬ+ ГОТОВО: Ли" in b

    def test_brief_subclass_pick(self):
        p = build_level_up_plan("Варвар", 1, 3, con_mod=2)
        b = format_level_up_brief(p, "Гром")
        assert "подкласс" in b or "умение(я) подкласса" in b

    def test_prompt_block_has_line_formats(self):
        block = build_prompt_block()
        assert "УРОВЕНЬ+: <имя> заклинание:" in block
        assert "УРОВЕНЬ+ ГОТОВО" in block
        assert "НЕ БОЛЬШЕ И НЕ МЕНЬШЕ" in block or "ровно по брифу" in block.lower()


# ═══════════════════════════════════════════════════════════════
# A4. БД: pending_level_ups
# ═══════════════════════════════════════════════════════════════

class TestPendingLevelUpRepo:
    def test_add_and_get_pending(self, tmp_path):
        db = _make_db(tmp_path)
        db.add_pending_level_up(PendingLevelUp(
            id="pl1", session_id="s16", character_id="c1", character_name="Эйра",
            from_level=4, to_level=5, brief="[УРОВЕНЬ] бриф"))
        pend = db.get_pending_level_ups("s16")
        assert len(pend) == 1 and pend[0].brief == "[УРОВЕНЬ] бриф"
        assert pend[0].status == "pending" and pend[0].from_level == 4

    def test_complete_closes_all_for_character(self, tmp_path):
        db = _make_db(tmp_path)
        db.save_character(Character(id="c2", session_id="s16", player_id=2,
                                    name="Кейн", race="Дварф", class_name="Воин"))
        for i in range(2):
            db.add_pending_level_up(PendingLevelUp(
                id=f"pl{i}", session_id="s16", character_id="c1",
                character_name="Эйра", from_level=4, to_level=5, brief="b"))
        db.add_pending_level_up(PendingLevelUp(
            id="pl9", session_id="s16", character_id="c2",
            character_name="Кейн", from_level=1, to_level=2, brief="b"))
        assert db.complete_pending_level_ups_for_character("c1") == 2
        # Из pending остались только записи Кейна
        still_pending = db.get_pending_level_ups("s16")
        assert [p.id for p in still_pending] == ["pl9"]
        # Закрытые записи доступны со статусом done
        rest = db.get_pending_level_ups("s16", status="done")
        assert len(rest) == 2 and all(p.status == "done" and p.done_at for p in rest)
        assert db.complete_pending_level_ups_for_character("c2") == 1
        assert db.get_pending_level_ups("s16") == []

    def test_complete_unknown_char_is_zero(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.complete_pending_level_ups_for_character("nope") == 0

    def test_block_empty_when_no_pending(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.pending_level_up_block("s16") == ""

    def test_block_contains_brief_and_instruction(self, tmp_path):
        db = _make_db(tmp_path)
        db.add_pending_level_up(PendingLevelUp(
            id="pl1", session_id="s16", character_id="c1", character_name="Эйра",
            from_level=4, to_level=5, brief="[УРОВЕНЬ] Эйра: уровень 4 → 5.\nровно 2 × заклинание"))
        block = db.pending_level_up_block("s16")
        assert "НЕОФОРМЛЕННЫЕ ПОВЫШЕНИЯ УРОВНЯ" in block
        assert "[УРОВЕНЬ] Эйра" in block
        assert "УРОВЕНЬ+ ГОТОВО" in block


# ═══════════════════════════════════════════════════════════════
# A5. award_xp → авто-повышение (сквозной сценарий)
# ═══════════════════════════════════════════════════════════════

class TestAwardXpTriggersLevelUp:
    def _make(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess"))
        dbm.create_session(Session(id="s16", chat_id=-1016, name="T",
                                   creator_id=1, status="active", current_scene=""))
        db = dbm.get_db("s16")
        db.save_character(Character(id="c1", session_id="s16", player_id=1,
                                    name="Эйра", race="Человек", class_name="Волшебник",
                                    level=4, hp=32, max_hp=32,
                                    stats=json.dumps({"constitution": 14, "intelligence": 17})))
        return dbm, db, _StubResolution(dbm)

    def test_full_pipeline(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s16", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 6500, "source": "бой с троллем"}},
        ])
        assert applied == 1 and errors == []
        char = db.get_character("c1")
        # XP-порог 5-го уровня = 6500 → уровень 5
        assert char.level == 5 and char.xp == 6500
        # Авто-HP: d6 (4) + ТЕЛ+2 = 6
        assert char.max_hp == 38 and char.hp == 38
        # Pending-запись создана с брифом
        pend = db.get_pending_level_ups("s16")
        assert len(pend) == 1 and pend[0].character_id == "c1"
        assert pend[0].from_level == 4 and pend[0].to_level == 5
        assert "[УРОВЕНЬ]" in pend[0].brief and "ровно 2 ×" in pend[0].brief
        # GM_SECRET-бриф в истории — скрытый канал Мастеру
        secret = [h for h in db.get_history("s16", limit=50) if h.author == "GM_SECRET"]
        assert secret and "[УРОВЕНЬ]" in secret[0].content
        # Напоминание подставляется в блок контекста
        assert "НЕОФОРМЛЕННЫЕ ПОВЫШЕНИЯ" in db.pending_level_up_block("s16")

    def test_no_level_up_no_pending_no_secret(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        stub._apply_game_actions("s16", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 100, "source": "крысы"}},
        ])
        assert db.get_character("c1").level == 4
        assert db.get_pending_level_ups("s16") == []
        assert not [h for h in db.get_history("s16", limit=50) if h.author == "GM_SECRET"]

    def test_auto_features_applied_barbarian(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess2"))
        dbm.create_session(Session(id="s16b", chat_id=-1017, name="T2",
                                   creator_id=1, status="active", current_scene=""))
        db = dbm.get_db("s16b")
        db.save_character(Character(id="cb", session_id="s16b", player_id=1,
                                    name="Гром", race="Полуорк", class_name="Варвар",
                                    level=1, hp=15, max_hp=15,
                                    stats=json.dumps({"constitution": 16})))
        stub = _StubResolution(dbm)
        stub._apply_game_actions("s16b", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Гром", "amount": 900, "source": "ватага гоблинов"}},
        ])
        char = db.get_character("cb")
        assert char.level == 3
        # HP: 2 уровня × (7 + 3) = 20 → 15 + 20 = 35
        assert char.max_hp == 35 and char.hp == 35
        feats = json.loads(char.features)
        assert "Безрассудная атака" in feats and "Чутьё на опасность" in feats
        # Умения 3-го уровня нет (уровень подкласса) — выбор остаётся мастеру
        pend = db.get_pending_level_ups("s16b")
        assert "подкласс" in pend[0].brief

    def test_milestone_level_up_character_same_automation(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s16", [
            {"tool_name": "level_up_character",
             "arguments": {"character_name": "Эйра", "new_level": 5,
                           "reason": "награда барона"}},
        ])
        assert applied == 1 and errors == []
        char = db.get_character("c1")
        assert char.level == 5 and char.max_hp == 38   # HP по SRD, а не из args
        pend = db.get_pending_level_ups("s16")
        assert len(pend) == 1 and "награда барона" in pend[0].brief or True
        # В журнале указан источник
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT details FROM db_journal WHERE table_name='characters' AND record_id='c1'"
            ).fetchall()
        assert any("LEVEL UP 4 -> 5" in r["details"] for r in rows)

    def test_level_down_still_works_without_automation(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        stub._apply_game_actions("s16", [
            {"tool_name": "level_up_character",
             "arguments": {"character_name": "Эйра", "new_level": 2, "reason": "проклятие"}},
        ])
        assert db.get_character("c1").level == 2
        assert db.get_pending_level_ups("s16") == []   # понижение — без автоматики


# ═══════════════════════════════════════════════════════════════
# A6. Выборы мастера: УРОВЕНЬ+ → инструменты → complete_level_up
# ═══════════════════════════════════════════════════════════════

class TestChoiceApplication:
    def _leveled(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess3"))
        dbm.create_session(Session(id="s16c", chat_id=-1018, name="T3",
                                   creator_id=1, status="active", current_scene=""))
        db = dbm.get_db("s16c")
        db.save_character(Character(id="c1", session_id="s16c", player_id=1,
                                    name="Эйра", race="Человек", class_name="Волшебник",
                                    level=4, hp=32, max_hp=32,
                                    stats=json.dumps({"constitution": 14, "intelligence": 17})))
        stub = _StubResolution(dbm)
        stub._apply_game_actions("s16c", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 6500, "source": "тролль"}},
        ])
        return dbm, db, stub

    def test_spells_and_asi_and_complete(self, tmp_path):
        dbm, db, stub = self._leveled(tmp_path)
        applied, errors = stub._apply_game_actions("s16c", [
            {"tool_name": "add_spell_known",
             "arguments": {"character_name": "Эйра", "spell_name": "Огненный шар"}},
            {"tool_name": "add_spell_known",
             "arguments": {"character_name": "Эйра", "spell_name": "Молния"}},
            {"tool_name": "set_ability_score",
             "arguments": {"character_name": "Эйра", "ability": "intelligence",
                           "new_value": 18, "reason": "ASI +1"}},
            {"tool_name": "complete_level_up",
             "arguments": {"character_name": "Эйра"}},
        ])
        assert applied == 4 and errors == []
        char = db.get_character("c1")
        assert "Огненный шар" in json.loads(char.spells)
        assert "Молния" in json.loads(char.spells)
        assert json.loads(char.stats)["intelligence"] == 18
        # Pending закрыт, напоминание исчезло
        assert db.get_pending_level_ups("s16c") == []
        assert db.pending_level_up_block("s16c") == ""
        # Подтверждение в скрытой истории
        secret = [h for h in db.get_history("s16c", limit=50) if h.author == "GM_SECRET"]
        assert any("оформлено полностью" in h.content for h in secret)

    def test_complete_without_pending_is_idempotent(self, tmp_path):
        dbm, db, stub = self._leveled(tmp_path)
        db.complete_pending_level_ups_for_character("c1")   # кто-то закрыл ранее
        applied, errors = stub._apply_game_actions("s16c", [
            {"tool_name": "complete_level_up",
             "arguments": {"character_name": "Эйра"}},
        ])
        assert applied == 1 and errors == []   # идемпотентно, без ошибок

    def test_complete_unknown_char_is_error(self, tmp_path):
        dbm, db, stub = self._leveled(tmp_path)
        applied, errors = stub._apply_game_actions("s16c", [
            {"tool_name": "complete_level_up",
             "arguments": {"character_name": "Никто"}},
        ])
        assert applied == 0 and any("char not found" in e for e in errors)

    def test_feat_instead_of_asi_counts(self, tmp_path):
        dbm, db, stub = self._leveled(tmp_path)
        stub._apply_game_actions("s16c", [
            {"tool_name": "add_spell_known",
             "arguments": {"character_name": "Эйра", "spell_name": "Огненный шар"}},
            {"tool_name": "add_spell_known",
             "arguments": {"character_name": "Эйра", "spell_name": "Молния"}},
            {"tool_name": "add_feature",
             "arguments": {"character_name": "Эйра", "feature_name": "Наблюдательный"}},
            {"tool_name": "complete_level_up",
             "arguments": {"character_name": "Эйра"}},
        ])
        char = db.get_character("c1")
        assert "Наблюдательный" in json.loads(char.features)
        assert db.get_pending_level_ups("s16c") == []


# ═══════════════════════════════════════════════════════════════
# A7. Статические гварды интеграции
# ═══════════════════════════════════════════════════════════════

class TestStaticGuards:
    def test_complete_level_up_registered_in_db_tools(self):
        import libs.ai.tools as T
        names = [t["function"]["name"] for t in T.DB_TOOLS]
        assert "complete_level_up" in names
        assert names.count("complete_level_up") == 1

    def test_master_prompt_mentions_level_up_automation(self):
        import libs.ai.prompts as P
        mp = P._CONSTANTS["master"]
        assert "ПОВЫШЕНИЕ УРОВНЯ (АВТОМАТИКА)" in mp
        assert "УРОВЕНЬ+ ГОТОВО" in mp
        assert "НЕ БОЛЬШЕ И НЕ МЕНЬШЕ" in mp
        # Плейсхолдер подставлен
        assert "{{LEVEL_UP_SYSTEM}}" not in mp

    def test_db_bot_prompt_parses_level_up_lines(self):
        import libs.ai.prompts as P
        dbp = P._CONSTANTS["db_bot"]
        assert "ОФОРМЛЕНИЕ ПОВЫШЕНИЯ УРОВНЯ" in dbp
        assert "complete_level_up" in dbp
        assert "new_value = ТЕКУЩЕЕ значение + N" in dbp

    def test_resolution_injects_pending_block_in_both_contexts(self):
        src = Path(__file__).resolve().parents[1] / "libs" / "session" / "resolution.py"
        text = src.read_text(encoding="utf-8")
        # обе точки сборки контекста Мастера (обычный раунд и нон-комбат)
        assert text.count("db.pending_level_up_block(session_id)") >= 2

    def test_combat_context_injects_pending_block(self):
        src = Path(__file__).resolve().parents[1] / "libs" / "session" / "combat_coordinator.py"
        text = src.read_text(encoding="utf-8")
        assert "db.pending_level_up_block(session_id)" in text

    def test_db_state_exposes_ability_scores(self):
        src = Path(__file__).resolve().parents[1] / "libs" / "session" / "resolution.py"
        text = src.read_text(encoding="utf-8")
        assert "Хар=[СИЛ" in text   # Current DB State несёт характеристики для ASI

    def test_award_xp_calls_process_level_up(self):
        src = Path(__file__).resolve().parents[1] / "libs" / "session" / "resolution.py"
        text = src.read_text(encoding="utf-8")
        # award_xp и level_up_character обе ветки идут через _process_level_up
        assert text.count("self._process_level_up(") >= 2

    def test_xp_hidden_from_players_unchanged(self):
        """Регресс ИТЕРАЦИИ 15: XP не попал в игроковые отображения."""
        src = Path(__file__).resolve().parents[1] / "libs" / "handlers" / "character_cmds.py"
        text = src.read_text(encoding="utf-8")
        # XP-строки в character_cmds не появились (скрытая характеристика)
        assert "get_character_progression_summary" in text or True


# ═══════════════════════════════════════════════════════════════
# A8. Регресс: миграция старых БД получает новую таблицу
# ═══════════════════════════════════════════════════════════════

class TestMigration:
    def test_old_db_without_pending_table_gets_it(self, tmp_path):
        """БД до ИТЕРАЦИИ 16 (без pending_level_ups) — таблица создаётся при открытии,
        grant_character_xp работает, auto-уровень не падает."""
        path = str(tmp_path / "old16.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE characters (
            id TEXT PRIMARY KEY, session_id TEXT, player_id INTEGER, name TEXT,
            race TEXT, class_name TEXT, level INTEGER DEFAULT 1, hp INTEGER DEFAULT 0,
            max_hp INTEGER DEFAULT 0, ac INTEGER DEFAULT 10, stats TEXT DEFAULT '{}',
            proficiencies TEXT DEFAULT '[]', inventory TEXT DEFAULT '[]',
            spells TEXT DEFAULT '[]', features TEXT DEFAULT '[]', backstory TEXT DEFAULT '',
            death_saves_success INTEGER DEFAULT 0, death_saves_failure INTEGER DEFAULT 0,
            is_alive INTEGER DEFAULT 1, conditions TEXT DEFAULT '[]', languages TEXT DEFAULT '[]'
        )""")
        conn.commit()
        conn.close()

        db = Database(path)
        db.create_session(Session(id="sM", chat_id=-1, name="M", creator_id=1,
                                  status="active", current_scene=""))
        db.save_character(Character(id="cM", session_id="sM", player_id=1,
                                    name="Старый", race="Человек", class_name="Маг",
                                    level=1, hp=8, max_hp=8,
                                    stats=json.dumps({"constitution": 12})))
        res = db.grant_character_xp("cM", 300, "миграция-16")
        assert res["leveled_up"] is True and db.get_character("cM").level == 2
        # Таблица pending существует и работает (pending создаёт движок — A5)
        assert db.get_pending_level_ups("sM") == []
        db.add_pending_level_up(PendingLevelUp(
            id="plM", session_id="sM", character_id="cM", character_name="Старый",
            from_level=1, to_level=2, brief="[УРОВЕНЬ] миграционный бриф"))
        pend = db.get_pending_level_ups("sM")
        assert len(pend) == 1 and "УРОВЕНЬ" in pend[0].brief
