"""ИТЕРАЦИЯ 15 — два блока работ:

A. БАГ: после завершения сессии запрос не обрывался — игрок получал и бросок
   кубиков, и нарратив за ход, сделанный ДО /end.
   libs/session/generation_guard.py (реестр задач + отмена + гейт отправки),
   end_session/clear_session_runtime_state (отмена), resume_session (снятие
   запрета), гейты в engine.py (_send_combat_turn_result, _resolve_and_send,
   _resolve_non_combat_round, _run_db_bot_background, боевой цикл, авто-скип).

B. ДОПОЛНЕНИЕ: система опыта (XP) — скрытая характеристика (видят только
   Мастер-нейросеть и DB-бот): libs/xp_system.py (таблицы ТЗ), БД (колонка xp,
   миграция, grant/set, авто-уровневание), инструмент award_xp, промпты.
"""
import asyncio
import sqlite3
import pytest

from libs.db import DatabaseManager, HistoryEntry, Session
from libs.db.database import Database
from libs.db.models import Character
from libs.session import generation_guard as gg
from libs.session.generation_guard import (
    cancel_session_generations, clear_cancel, generation_task,
    is_cancelled, register, session_is_active, unregister, active_count,
)
from libs.session.resolution import ResolutionMixin
from libs.session.session_lifecycle import SessionLifecycleMixin
from libs.xp_system import (
    LEVEL_XP_THRESHOLDS, MONSTER_XP_BY_CR, DIFFICULTY_XP_BY_LEVEL,
    average_party_level, build_prompt_block, difficulty_award,
    level_for_xp, monster_xp, next_level_threshold, split_monster_xp,
)

from tests.conftest import FakeDB, FakeDBManager, FakeSession


# ═══════════════════════════════════════════════════════════════
# A1. Таблицы ТЗ — точные значения
# ═══════════════════════════════════════════════════════════════

class TestXpTables:
    def test_level_thresholds_exact(self):
        assert LEVEL_XP_THRESHOLDS == {
            1: 0, 2: 300, 3: 900, 4: 2700, 5: 6500,
            6: 14000, 7: 23000, 8: 34000, 9: 48000, 10: 64000,
            11: 85000, 12: 100000, 13: 120000, 14: 140000, 15: 165000,
            16: 195000, 17: 225000, 18: 265000, 19: 305000, 20: 355000,
        }

    def test_monster_xp_exact(self):
        assert MONSTER_XP_BY_CR["0"] == 10
        assert MONSTER_XP_BY_CR["1/8"] == 25
        assert MONSTER_XP_BY_CR["1/4"] == 50
        assert MONSTER_XP_BY_CR["1/2"] == 100
        assert MONSTER_XP_BY_CR["1"] == 200
        assert MONSTER_XP_BY_CR["5"] == 1800
        assert MONSTER_XP_BY_CR["13"] == 10000
        assert MONSTER_XP_BY_CR["20"] == 25000
        assert MONSTER_XP_BY_CR["30"] == 155000
        assert len(MONSTER_XP_BY_CR) == 34  # 0, 1/8, 1/4, 1/2 + 1..30

    def test_difficulty_table_exact(self):
        assert DIFFICULTY_XP_BY_LEVEL[1] == {"easy": 25, "medium": 50, "hard": 75, "deadly": 100}
        assert DIFFICULTY_XP_BY_LEVEL[5] == {"easy": 250, "medium": 500, "hard": 750, "deadly": 1100}
        assert DIFFICULTY_XP_BY_LEVEL[12] == {"easy": 1000, "medium": 2000, "hard": 3000, "deadly": 4500}
        assert DIFFICULTY_XP_BY_LEVEL[20] == {"easy": 2800, "medium": 5700, "hard": 8500, "deadly": 12700}

    def test_level_for_xp_boundaries(self):
        assert level_for_xp(0) == 1
        assert level_for_xp(299) == 1
        assert level_for_xp(300) == 2
        assert level_for_xp(899) == 2
        assert level_for_xp(900) == 3
        assert level_for_xp(2699) == 3
        assert level_for_xp(2700) == 4
        assert level_for_xp(354999) == 19
        assert level_for_xp(355000) == 20
        assert level_for_xp(400000) == 20
        assert level_for_xp(-5) == 1
        assert level_for_xp(None) == 1  # мусор → уровень 1, не падение

    def test_next_level_threshold(self):
        assert next_level_threshold(1) == 300
        assert next_level_threshold(2) == 900
        assert next_level_threshold(19) == 355000
        assert next_level_threshold(20) is None

    def test_monster_xp_lookup(self):
        assert monster_xp("1/4") == 50
        assert monster_xp("0.5") == 100  # дробная запись 1/2
        assert monster_xp(3) == 700
        assert monster_xp("31") == 155000  # неизвестный CR — ближайший снизу
        assert monster_xp("дракон") is None
        assert monster_xp(None) is None

    def test_difficulty_award_rus_english(self):
        assert difficulty_award(1, "Легкая") == 25
        assert difficulty_award(1, "лёгкая") == 25
        assert difficulty_award(5, "Средняя") == 500
        assert difficulty_award(5, "Сложная") == 750
        assert difficulty_award(20, "смертельная") == 12700
        assert difficulty_award(10, "deadly") == 2800
        assert difficulty_award(3, "неведомо") is None
        assert difficulty_award(99, "easy") == 2800  # клампинг уровня 25+ → 20

    def test_average_party_level(self):
        assert average_party_level([1, 3]) == 2
        assert average_party_level([1, 2, 3, 4]) == 3  # 2.5 → вверх
        assert average_party_level([5, 5, 5]) == 5
        assert average_party_level([]) == 1

    def test_split_monster_xp(self):
        assert split_monster_xp(450, 2) == 225
        assert split_monster_xp(200, 3) == 66  # вниз
        assert split_monster_xp(200, 0) == 0
        assert split_monster_xp(-5, 2) == 0

    def test_build_prompt_block_contains_tables_and_format(self):
        block = build_prompt_block()
        assert "300" in block and "355000" in block
        assert "CR 1/4 → 50" in block and "CR 30 → 155000" in block
        assert "Смертельная 12700" in block
        assert "award_xp" in block
        assert "СВОДКА" in block
        assert "ИГРОКИ НЕ ВИДЯТ XP" in block


# ═══════════════════════════════════════════════════════════════
# A2. БД: колонка xp, миграция, grant/set, авто-уровневание
# ═══════════════════════════════════════════════════════════════

def _make_db(tmp_path, name="t.db"):
    db = Database(str(tmp_path / name))
    db.create_session(Session(id="s15", chat_id=-1015, name="XP-тест",
                              creator_id=1, status="active", current_scene=""))
    db.save_character(Character(id="c1", session_id="s15", player_id=1,
                                name="Эйра", race="Человек", class_name="Маг",
                                level=1, hp=8, max_hp=8))
    return db


class TestXpRepo:
    def test_grant_accumulates(self, tmp_path):
        db = _make_db(tmp_path)
        res = db.grant_character_xp("c1", 200, "гоблин CR 1")
        assert res["xp_total"] == 200 and res["leveled_up"] is False
        assert res["new_level"] == 1 and res["next_threshold"] == 300
        assert db.get_character("c1").xp == 200

    def test_grant_crosses_threshold_levels_up_immediately(self, tmp_path):
        db = _make_db(tmp_path)
        db.grant_character_xp("c1", 200, "крыса")
        res = db.grant_character_xp("c1", 100, "ещё крыса")
        assert res["xp_total"] == 300
        assert res["leveled_up"] is True
        assert res["old_level"] == 1 and res["new_level"] == 2
        assert db.get_character("c1").level == 2

    def test_multi_level_jump(self, tmp_path):
        db = _make_db(tmp_path)
        res = db.grant_character_xp("c1", 355000, "сразился с ТАРОМ")
        assert res["new_level"] == 20 and res["leveled_up"] is True
        assert db.get_character("c1").level == 20

    def test_negative_clamps_to_zero(self, tmp_path):
        db = _make_db(tmp_path)
        db.grant_character_xp("c1", 100)
        res = db.grant_character_xp("c1", -500, "проклятие")
        assert res["xp_total"] == 0 and db.get_character("c1").level == 1

    def test_grant_unknown_char_returns_none(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.grant_character_xp("nope", 100) is None

    def test_set_character_xp_syncs_level_both_ways(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_character_xp("c1", 2699)
        assert db.get_character("c1").level == 3
        db.set_character_xp("c1", 100)
        assert db.get_character("c1").level == 1  # понижение уровня тоже синхронно

    def test_journal_records_award(self, tmp_path):
        db = _make_db(tmp_path)
        db.grant_character_xp("c1", 200, "гоблин")
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT details FROM db_journal WHERE table_name='characters' AND record_id='c1'"
            ).fetchall()
        assert any("XP" in r["details"] and "гоблин" in r["details"] for r in rows)

    def test_progression_summary_shows_xp_to_master_only_format(self, tmp_path):
        db = _make_db(tmp_path)
        db.grant_character_xp("c1", 200, "гоблин")
        summary = db.get_character_progression_summary("c1")
        assert "XP: 200 (до уровня 2: 300 XP)" in summary
        assert "уровень 1" in summary

    def test_progression_summary_zero_xp_quiet(self, tmp_path):
        db = _make_db(tmp_path)
        summary = db.get_character_progression_summary("c1")
        assert "XP: 0" in summary
        assert "до уровня" not in summary

    def test_save_character_roundtrips_xp(self, tmp_path):
        db = _make_db(tmp_path)
        char = db.get_character("c1")
        char.xp = 4321
        db.save_character(char)
        assert db.get_character("c1").xp == 4321

    def test_migration_old_schema_without_xp(self, tmp_path):
        """БД, созданная ДО ИТЕРАЦИИ 15 (нет колонки xp), мигрирует автоматически."""
        path = str(tmp_path / "old.db")
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
        conn.execute("INSERT INTO characters (id, session_id, player_id, name) VALUES ('c9','s9',1,'Старый')")
        conn.commit()
        conn.close()

        db = Database(path)
        char = db.get_character("c9")
        assert char is not None and char.xp == 0
        res = db.grant_character_xp("c9", 300, "миграция")
        assert res["leveled_up"] is True and res["new_level"] == 2


# ═══════════════════════════════════════════════════════════════
# A3. _apply_game_actions: инструмент award_xp
# ═══════════════════════════════════════════════════════════════

class _StubResolution(ResolutionMixin):
    def __init__(self, db_manager):
        self.db_manager = db_manager


class TestApplyAwardXp:
    def _make(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess"))
        dbm.create_session(Session(id="s15", chat_id=-1015, name="T",
                                   creator_id=1, status="active", current_scene=""))
        db = dbm.get_db("s15")
        db.save_character(Character(id="c1", session_id="s15", player_id=1,
                                    name="Эйра", race="Человек", class_name="Маг"))
        return dbm, db, _StubResolution(dbm)

    def test_award_xp_applied(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s15", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 200, "source": "гоблин CR 1"}},
        ])
        assert applied == 1 and errors == []
        assert db.get_character("c1").xp == 200

    def test_award_xp_level_up_writes_gm_secret_history(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        stub._apply_game_actions("s15", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 300, "source": "квест"}},
        ])
        assert db.get_character("c1").level == 2
        histories = db.get_history("s15", limit=50)
        # ИТЕРАЦИЯ 16: бриф теперь форматирует libs.level_up — маркер [УРОВЕНЬ]
        secret = [h for h in histories if h.author == "GM_SECRET" and "[УРОВЕНЬ]" in h.content]
        assert secret and "уровень 1 → 2" in secret[0].content and "ГОТОВО" in secret[0].content

    def test_award_xp_no_level_up_no_history(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        stub._apply_game_actions("s15", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": 50, "source": "мелочь"}},
        ])
        assert not [h for h in db.get_history("s15", limit=50) if h.author == "GM_SECRET"]

    def test_award_xp_unknown_char_is_error(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s15", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Никто", "amount": 100}},
        ])
        assert applied == 0 and any("char not found" in e for e in errors)

    def test_award_xp_bad_amount_is_error(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s15", [
            {"tool_name": "award_xp",
             "arguments": {"character_name": "Эйра", "amount": "много"}},
        ])
        assert applied == 0 and any("bad amount" in e for e in errors)

    def test_award_xp_zero_silently_skipped(self, tmp_path):
        dbm, db, stub = self._make(tmp_path)
        applied, errors = stub._apply_game_actions("s15", [
            {"tool_name": "award_xp", "arguments": {"character_name": "Эйра", "amount": 0}},
        ])
        assert applied == 0 and errors == []


# ═══════════════════════════════════════════════════════════════
# B1. generation_guard — реестр, отмена, гейт
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_guard():
    yield
    for sid in list(gg._active_tasks):
        gg._active_tasks.pop(sid, None)
    gg._cancelled.clear()


class TestGenerationGuard:
    async def test_cancel_kills_registered_task(self):
        started = asyncio.Event()
        async def worker():
            started.set()
            await asyncio.sleep(30)
        task = asyncio.create_task(worker())
        register("sx", task)
        assert active_count("sx") == 1
        await started.wait()
        assert cancel_session_generations("sx") == 1
        with pytest.raises(asyncio.CancelledError):
            await task
        assert is_cancelled("sx")

    async def test_generation_task_decorator_blocks_send_after_cancel(self):
        flag = {"sent": False}
        @generation_task
        async def gen(sid):
            await asyncio.sleep(0.03)
            flag["sent"] = True  # «отправка» игроку
        task = asyncio.create_task(gen("sy"))
        await asyncio.sleep(0.005)  # даём стартовать
        cancel_session_generations("sy")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert flag["sent"] is False  # отправка НЕ произошла

    async def test_refcount_nested_calls(self):
        async def work():
            await asyncio.sleep(0)
        task = asyncio.create_task(work())
        await asyncio.sleep(0)  # даём задаче стартовать
        register("sz", task)
        register("sz", task)  # вложенная рамка
        assert active_count("sz") == 2  # сумма рефов = 2 (одна задача)
        unregister("sz", task)
        assert active_count("sz") == 1  # внешний рамка ещё держит
        unregister("sz", task)
        assert active_count("sz") == 0

    async def test_cancel_from_inside_does_not_kill_itself(self):
        @generation_task
        async def gen(sid):
            assert cancel_session_generations(sid) == 0  # себя не отменяет
            await asyncio.sleep(0.01)
            return "done"
        assert await gen("sw") == "done"

    def test_session_is_active_db_states(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess"))
        dbm.create_session(Session(id="sA", chat_id=-1, name="T", creator_id=1,
                                   status="active", current_scene=""))
        assert session_is_active("sA", dbm) is True
        dbm.end_session("sA")
        assert session_is_active("sA", dbm) is False
        assert session_is_active("missing", dbm) is False

    def test_session_is_active_cancel_flag_overrides(self):
        dbm = FakeDBManager(FakeDB())
        assert session_is_active("s1", dbm) is True  # FakeSession.status="active"
        gg._cancelled.add("s1")
        assert session_is_active("s1", dbm) is False

    def test_session_is_active_survives_broken_db(self):
        class Boom:
            def get_db(self, sid):
                raise RuntimeError("boom")
        assert session_is_active("s1", Boom()) is False

    async def test_end_session_cancels_inflight_and_marks_ended(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess"))
        dbm.create_session(Session(id="sE", chat_id=-2, name="T", creator_id=1,
                                   status="active", current_scene=""))
        started = asyncio.Event()
        @generation_task
        async def resolve(sid):
            started.set()
            await asyncio.sleep(30)
        task = asyncio.create_task(resolve("sE"))
        await started.wait()

        class StubLife(SessionLifecycleMixin):
            def __init__(self, m):
                self.db_manager = m
        StubLife(dbm).end_session("sE")

        with pytest.raises(asyncio.CancelledError):
            await task
        assert dbm.get_db("sE").get_session("sE").status == "ended"
        assert is_cancelled("sE")

    async def test_resume_clears_cancel_flag(self, tmp_path):
        dbm = DatabaseManager(str(tmp_path / "sess"))
        dbm.create_session(Session(id="sR", chat_id=-3, name="T", creator_id=1,
                                   status="active", current_scene=""))
        # Запрет ставится в SessionManager.end_session (см. сквозной тест выше);
        # здесь выставляем напрямую и проверяем, что resume его снимает.
        cancel_session_generations("sR")
        assert is_cancelled("sR")
        resumed = dbm.resume_session("sR")
        assert resumed.status == "active"
        assert not is_cancelled("sR")

    async def test_double_cancel_is_idempotent(self):
        assert cancel_session_generations("sD") == 0
        assert cancel_session_generations("sD") == 0
        assert is_cancelled("sD")

    def test_clear_cancel_direct(self):
        gg._cancelled.add("sC")
        clear_cancel("sC")
        assert not is_cancelled("sC")


# ═══════════════════════════════════════════════════════════════
# B2. Гейты в engine.py — отправки в завершённую сессию не уходят
# ═══════════════════════════════════════════════════════════════

class FakeAsyncBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text="", **kwargs):
        self.sent.append(text)
        from types import SimpleNamespace
        return SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **kwargs):
        return True

    async def delete_message(self, **kwargs):
        return True


class TestEngineGates:
    async def test_combat_result_silent_when_session_ended(self, monkeypatch):
        from libs.handlers import engine
        monkeypatch.setattr(engine, "session_is_active", lambda sid, dbm: False)
        bot = FakeAsyncBot()
        handle = engine._ChatHandle(bot, -1, None)
        await engine._send_combat_turn_result(
            "s1", {"player_text": "Мастер описывает победу"}, handle)
        assert bot.sent == []  # ни нарратива, ни fallback

    async def test_combat_result_sends_when_active(self, monkeypatch):
        from libs.handlers import engine
        monkeypatch.setattr(engine, "session_is_active", lambda sid, dbm: True)
        bot = FakeAsyncBot()
        handle = engine._ChatHandle(bot, -1, None)
        await engine._send_combat_turn_result("s1", {"player_text": ""}, handle)
        assert any("Мастер задумался" in t for t in bot.sent)

    async def test_db_bot_background_skips_when_ended(self, monkeypatch):
        from libs.handlers import engine
        monkeypatch.setattr(engine, "session_is_active", lambda sid, dbm: False)
        calls = {"busy": 0, "phase": 0}

        async def strict_phase(*a, **k):
            calls["phase"] += 1
            return {}
        monkeypatch.setattr(engine.sessions, "run_db_bot_phase", strict_phase)

        orig_busy = engine.sessions.set_db_busy
        def busy(sid, flag):
            calls["busy"] += 1
        monkeypatch.setattr(engine.sessions, "set_db_busy", busy)

        await engine._run_db_bot_background("s1", "нарратив", "текст", None,
                                            FakeAsyncBot(), -1, None)
        assert calls["phase"] == 0      # DB-фаза не запускалась
        assert calls["busy"] >= 1       # флаг снят (не зависает «обновляется»)

    async def test_resolve_and_send_cancelled_by_end_no_send(self, monkeypatch):
        """Сквозной сценарий бага: игрок сходил, /end пришёл ПОКА Мастер думал —
        ни нарратива, ни бросков, ни «✅ Мир обновлён» после завершения."""
        from libs.handlers import engine

        fake_db = FakeDB()
        fake_db._session = FakeSession(session_id="sI", combat_active=False)
        fake_dbm = FakeDBManager(fake_db)
        monkeypatch.setattr(engine, "db_manager", fake_dbm)

        class StubSessions:
            def __init__(self):
                self.db_busy_set = False
            def get_roll_mode(self, sid):
                return "mixed"
            async def resolve_round_master_only(self, *a, **k):
                await asyncio.sleep(30)  # «Мастер думает»
                raise AssertionError("не должен дождаться")
            def set_db_busy(self, sid, flag):
                self.db_busy_set = flag
            def pop_pending_resolve(self, sid):
                return None
            def start_action_collection(self, sid):
                pass
        stub = StubSessions()
        monkeypatch.setattr(engine, "sessions", stub)

        bot = FakeAsyncBot()
        update = engine._ChatHandle(bot, -1, None)
        task = asyncio.create_task(engine._resolve_and_send("sI", update, None))
        await asyncio.sleep(0.05)  # резолв «думает»

        # /end в этот момент: end_session отменяет зарегистрированные задачи
        cancel_session_generations("sI")
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bot.sent == []            # ничего в чат не ушло
        assert stub.db_busy_set is False  # и флаг busy не включался

    def test_generation_functions_are_tracked(self):
        """Все 8 генеративных функций обёрнуты @generation_task (functools.wraps
        даёт __wrapped__). Регресс-гвард: новая генеративная функция без декоратора
        не переживёт /end."""
        from libs.handlers import engine
        tracked = [
            "_start_combat_turn_loop", "_resolve_npc_combat_turn",
            "_auto_resolve_npcs_then_pc", "_resolve_pc_combat_turn",
            "_send_translations", "_resolve_non_combat_round",
            "_resolve_and_send", "_run_db_bot_background",
        ]
        for name in tracked:
            fn = getattr(engine, name)
            assert hasattr(fn, "__wrapped__"), f"{name} не обёрнут @generation_task"

    def test_end_session_source_calls_guard(self):
        """Статическая проверка: end_session и clear_session_runtime_state зовут
        cancel_session_generations; resume_session — clear_cancel."""
        import inspect
        from libs.session.session_lifecycle import SessionLifecycleMixin
        src = inspect.getsource(SessionLifecycleMixin.end_session)
        assert "cancel_session_generations" in src
        from libs.session import base as session_base
        src2 = inspect.getsource(session_base.BaseSessionMixin.clear_session_runtime_state)
        assert "cancel_session_generations" in src2
        from libs.db import manager as db_manager_mod
        src3 = inspect.getsource(db_manager_mod.DatabaseManager.resume_session)
        assert "clear_cancel" in src3


# ═══════════════════════════════════════════════════════════════
# B3. Промпты: {{XP_SYSTEM}} подставляется, инструктаж DB-бота на месте
# ═══════════════════════════════════════════════════════════════

class TestXpPrompts:
    def test_master_prompt_has_xp_block_substituted(self):
        from libs.ai.prompts import get_prompt, MASTER_PROMPT
        for prompt in (MASTER_PROMPT, get_prompt("master")):
            assert "{{XP_SYSTEM}}" not in prompt
            assert "СИСТЕМА ОПЫТА" in prompt
            assert "award_xp" in prompt or "СВОДКА" in prompt
            assert "CR 1/4 → 50" in prompt

    def test_db_bot_prompt_instructs_award_xp(self):
        from libs.ai.prompts import DB_BOT_PROMPT
        assert "award_xp" in DB_BOT_PROMPT
        assert "всем участникам" in DB_BOT_PROMPT
        assert "{{XP_SYSTEM}}" not in DB_BOT_PROMPT

    def test_award_xp_tool_registered_in_tools(self):
        from libs.ai.tools import DB_TOOLS, ALL_TOOLS
        db_names = [t["function"]["name"] for t in DB_TOOLS]
        assert "award_xp" in db_names
        all_names = [t["function"]["name"] for t in ALL_TOOLS]
        assert "award_xp" in all_names

    def test_award_xp_tool_not_duplicated(self):
        from libs.ai import tools as tools_mod
        all_names = [t["function"]["name"] for t in tools_mod.ALL_TOOLS]
        assert all_names.count("award_xp") == 1
