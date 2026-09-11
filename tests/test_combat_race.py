"""Regression tests for the COMBAT RACE fix (БОЙ-FIX, итерация 6).

Реальный инцидент: во время боя раунды шли 1→3→5→7→8→6→2→2→4, одни и те же
ходы дублировались дословно, HP/статусы противоречили друг другу (стражники
то мертвы, то снова 12 HP; Храфна то без сознания, то 15/15).

Корневые причины, покрытые здесь:
1. advance_round() делал read-modify-write двумя SQL-запросами → lost update
   между параллельными резолверами → прыгающие/дублирующиеся номера раундов.
   Фикс: атомарный UPSERT (db.increment_meta) — проверен и на фейке, и на
   РЕАЛЬНОМ SQLite (в т.ч. между потоками).
2. Боевой цикл (NPC/PC ходы), dual-narrative и обычный раунд вызывали
   Мастера ПАРАЛЛЕЛЬНО → два противоречащих нарратива на одну сцену.
   Фикс: session-wide резолвер-мьютекс (get_resolver_lock) — тест
   доказывает, что перекрытие вызовов Мастера невозможно.
3. Действия non-combat игроков, оставшиеся в очереди на момент конца боя,
   терялись (end_initiative_combat звал clear_non_combat_queue). Фикс:
   очередь переживает конец боя и подмешивается к первому обычному раунду.
4. Игроки корректно добавляются в бой: имя участника из нарратива Мастера
   («Храфна Морвен», @username) сопоставляется с персонажем сессии; игрок
   больше НЕ регистрируется как NPC с player_id=0.
"""
import asyncio

import pytest

import libs.session.resolution as resolution_module
from libs.session.base import BaseSessionMixin
from libs.session.resolution import ResolutionMixin
from libs.session.round_coordinator import RoundCoordinatorMixin
from libs.session.manual_rolls import ManualRollsMixin
from libs.session.combat_coordinator import (
    CombatCoordinatorMixin, match_participant_to_character,
)
from libs.db import HistoryEntry
from tests.conftest import (
    FakeDB, FakeDBManager, FakePlayer, FakeChar, make_queue,
)


# ═══════════════════════════════════════════════════════════════
# Fakes: DM engine / memory store / host class
# ═══════════════════════════════════════════════════════════════

class FakeDM:
    """Считает ПАРАЛЛЕЛЬНЫЕ вызовы process_master_turn.

    max_active > 1 в любой момент = два нарратива пишутся одновременно =
    та самая гонка из боевого лога."""

    def __init__(self, delay: float = 0.03):
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def process_master_turn(self, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
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


class Host(BaseSessionMixin, ResolutionMixin, RoundCoordinatorMixin, ManualRollsMixin):
    """Полноценный хост: BaseSessionMixin даёт НАСТОЯЩИЕ get_resolver_lock /
    is_resolver_busy / очереди; ManualRollsMixin — /roll-буфер;
    MemoryStore подменяется фейком."""

    def __init__(self, db: FakeDB, dm: FakeDM = None):
        BaseSessionMixin.__init__(self, FakeDBManager(db), dm or FakeDM())
        self.memory_store = FakeMemoryStore()


# ═══════════════════════════════════════════════════════════════
# 1. Атомарный счётчик раундов
# ═══════════════════════════════════════════════════════════════

class TestAtomicAdvanceRound:

    def test_sequential_increments(self, db):
        rc = Host(db)
        assert rc.advance_round("s1") == 1
        assert rc.advance_round("s1") == 2
        assert rc.get_current_round("s1") == 2

    def test_ten_advances_unique_and_monotonic(self, db):
        """Счётчик монотонен и без пропусков. Параллельная атомарность
        доказана на реальном SQLite (TestRealSqliteIncrementMeta::
        test_increment_via_threads) и на уровне резолверов (ниже)."""
        rc = Host(db)
        results = [rc.advance_round("s1") for _ in range(10)]
        assert results == list(range(1, 11))
        assert rc.get_current_round("s1") == 10

    @pytest.mark.asyncio
    async def test_parallel_resolvers_get_unique_round_numbers(self, db):
        """Симуляция реальной гонки: обычный раунд × dual-narrative × ещё один
        обычный раунд одновременно. Нарративы обязаны получить уникальные
        номера раундов — без дублей вида «[Раунд 2] дважды»."""
        db.players = [FakePlayer(5, username="rimi", display_name="Рими")]
        host = Host(db)
        db._queue = make_queue([])
        host.submit_non_combat_action("s1", 5, "ищу травы")

        results = await asyncio.gather(
            host.resolve_round_master_only("s1"),
            host.resolve_non_combat_round("s1"),
            host.resolve_round_master_only("s1"),
        )
        assert host.dm.max_active == 1, "перекрытие вызовов Мастера — гонка вернулась"
        # Минимум один обычный раунд обязан дойти до Мастера (очередь создана),
        # и его нарратив должен попасть в историю с корректным номером.
        narratives = [
            h.content for h in getattr(db, "_history", [])
            if h.entry_type == "narrative"
        ]
        assert narratives, "обычный раунд не дошёл до Мастера"
        numbers = [c.split("]")[0] for c in narratives]
        assert len(numbers) == len(set(numbers)), f"дубликаты раундов: {numbers}"

    def test_banana_recovers(self, db):
        db.set_meta(RoundCoordinatorMixin.ROUND_META_KEY, "banana")
        assert Host(db).advance_round("s1") == 1


class TestRealSqliteIncrementMeta:
    """db.increment_meta на РЕАЛЬНОМ SQLite — здесь живёт сама атомарность."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from libs.db import Database
        return Database(str(tmp_path / "race.db"))

    def test_increment_returns_new_value(self, real_db):
        assert real_db.increment_meta("global_round") == 1
        assert real_db.increment_meta("global_round") == 2
        assert real_db.increment_meta("global_round") == 3
        assert real_db.get_meta("global_round") == "3"

    def test_banana_casts_to_zero(self, real_db):
        real_db.set_meta("global_round", "banana")
        assert real_db.increment_meta("global_round") == 1

    def test_separate_keys_independent(self, real_db):
        assert real_db.increment_meta("a") == 1
        assert real_db.increment_meta("b", "5") == 6
        assert real_db.increment_meta("a") == 2

    def test_increment_via_threads(self, real_db):
        """SQLite-UPSERT атомарен даже между потоками/соединениями —
        5 потоков × 20 инкрементов дают ровно 100 уникальных значений."""
        import threading
        results = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                v = real_db.increment_meta("global_round")
                with lock:
                    results.append(v)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == len(set(results)) == 100
        assert real_db.get_meta("global_round") == "100"


# ═══════════════════════════════════════════════════════════════
# 2. Резолвер-мьютекс: один нарратив за раз
# ═══════════════════════════════════════════════════════════════

class TestResolverMutex:

    @pytest.fixture(autouse=True)
    def dual_enabled(self, monkeypatch):
        monkeypatch.setattr(resolution_module, "DUAL_NARRATIVE_ENABLED", True)

    @pytest.mark.asyncio
    async def test_normal_and_dual_narrative_never_overlap(self, db):
        """Dual-narrative и обычный раунд обязаны сериализоваться."""
        db.players = [FakePlayer(5, username="rimi", display_name="Рими")]
        host = Host(db)
        db._queue = make_queue([])
        host.submit_non_combat_action("s1", 5, "ищу травы")

        await asyncio.gather(
            host.resolve_round_master_only("s1"),
            host.resolve_non_combat_round("s1"),
        )
        assert host.dm.max_active == 1, "гонка: два нарратива одновременно"
        # Действие non-combat игрока разрешено ровно один раз
        # (либо в dual-narrative, либо подмешано в обычный раунд —
        # но не в обоих и не потеряно).
        rimi_entries = [list(c.values()) for c in host.dm.calls]
        flat = [a for sub in rimi_entries for a in sub
                if "ищу травы" in str(a)]
        assert len(flat) == 1, f"действие разрешено {len(flat)} раз"

    @pytest.mark.asyncio
    async def test_two_normal_rounds_never_overlap(self, db):
        """Два обычных раунда подряд (deferred resolve + новый сабмит) —
        реальный источник цепочек 1,3,5,7 / 2,4,6,8 из боевого лога."""
        host = Host(db)
        db._queue = make_queue([])
        first, second = await asyncio.gather(
            host.resolve_round_master_only("s1"),
            host.resolve_round_master_only("s1"),
        )
        assert host.dm.max_active == 1
        assert len(host.dm.calls) == 2
        # Ни один из двух не упал с "No queue state": после первого раунда
        # start_action_collection обязан был открыть очередь второму.
        assert "Error" not in first["player_text"]
        assert "Error" not in second["player_text"]

    @pytest.mark.asyncio
    async def test_duplicate_non_combat_tasks_are_safe(self, db):
        """Каждая подача действия спавнит задачу-резолвер; 4 параллельных
        задачи на 2 действия: атомарный pop собирает оба действия ОДНИМ
        пакетом, дубликаты выходят по пустому pop без вызова Мастера."""
        db.players = [
            FakePlayer(5, username="rimi", display_name="Рими"),
            FakePlayer(6, username="crescent", display_name="Храфна"),
        ]
        host = Host(db)
        host.submit_non_combat_action("s1", 5, "крадусь вдоль стены")
        host.submit_non_combat_action("s1", 6, "слушаю у дверей")

        await asyncio.gather(*[
            host.resolve_non_combat_round("s1") for _ in range(4)
        ])
        assert host.dm.max_active == 1
        assert len(host.dm.calls) == 1, "пустые попы не должны вызывать Мастера"
        # ник = username or display_name (как в resolve_non_combat_round)
        players_in_call = set(host.dm.calls[0].keys())
        assert players_in_call == {"rimi", "crescent"}, host.dm.calls
        actions = set(host.dm.calls[0].values())
        assert actions == {"Дн. крадусь вдоль стены", "Дн. слушаю у дверей"}

    @pytest.mark.asyncio
    async def test_round_lock_held_during_resolution(self, db):
        db._queue = make_queue([])
        host = Host(db)
        task = asyncio.create_task(host.resolve_round_master_only("s1"))
        await asyncio.sleep(0.005)  # резолвер внутри лока (dm delay 0.03)
        assert host.is_resolver_busy("s1")
        await task
        assert not host.is_resolver_busy("s1")

    @pytest.mark.asyncio
    async def test_error_releases_lock(self, db):
        class BoomDM(FakeDM):
            async def process_master_turn(self, **kwargs):
                raise RuntimeError("мастер упал")

        db._queue = make_queue([])
        host = Host(db, dm=BoomDM())
        result = await host.resolve_round_master_only("s1")
        assert result.get("error")
        assert not host.is_resolver_busy("s1"), "лок утёк после исключения"


# ═══════════════════════════════════════════════════════════════
# 3. Non-combat очередь переживает конец боя
# ═══════════════════════════════════════════════════════════════

class SimpleEncounter:
    id = "e1"
    session_id = "s1"


class CombatEndDB(FakeDB):
    """FakeDB + минимум боевого покрытия для end_initiative_combat."""

    def __init__(self):
        super().__init__()
        self.encounter = SimpleEncounter()

    def get_active_combat_encounter(self, session_id):
        return self.encounter

    def end_combat_encounter(self, encounter_id):
        self.encounter = None

    def update_session(self, session):
        pass


class Bot(CombatCoordinatorMixin, RoundCoordinatorMixin):
    def __init__(self, db):
        self.db_manager = FakeDBManager(db)
        self._non_combat_queue = {}
        self._non_combat_locks = {}
        self._resolver_locks = {}


class CombinedHost(BaseSessionMixin, ResolutionMixin, RoundCoordinatorMixin,
                   CombatCoordinatorMixin, ManualRollsMixin):
    """Один объект = и резолвер, и боевой координатор — как настоящий
    SessionManager в проде (в отличие от пары Bot+Host, у которых
    очереди раздельные)."""

    def __init__(self, db: FakeDB, dm: FakeDM = None):
        BaseSessionMixin.__init__(self, FakeDBManager(db), dm or FakeDM())
        self.memory_store = FakeMemoryStore()


class TestLeftoverNonCombatQueue:

    @pytest.mark.asyncio
    async def test_leftovers_survive_combat_end(self):
        """end_initiative_combat больше НЕ чистит non-combat очередь:
        действия доживают до первого обычного раунда."""
        db = CombatEndDB()
        db.players = [FakePlayer(5, username="rimi", display_name="Рими")]
        db._session.combat_active = True
        bot = Bot(db)
        bot.submit_non_combat_action("s1", 5, "договариваюсь с купцом")

        bot.end_initiative_combat("s1", "стража отступила")
        assert bot.get_non_combat_queue("s1"), "очередь потеряна при конце боя"

    @pytest.mark.asyncio
    async def test_leftovers_merged_into_next_normal_round(self):
        db = CombatEndDB()
        db.players = [FakePlayer(5, username="rimi", display_name="Рими")]
        db._session.combat_active = True
        host = CombinedHost(db)
        host.submit_non_combat_action("s1", 5, "договариваюсь с купцом")
        host.end_initiative_combat("s1", "стража отступила")
        assert host.get_non_combat_queue("s1"), "очередь потеряна при конце боя"

        db._queue = make_queue([1])           # новый обычный раунд ждёт других
        db.players.append(FakePlayer(1, username="leha", display_name="Лёха"))
        await host.resolve_round_master_only("s1")

        assert len(host.dm.calls) == 1
        # Действие non-combat игрока попало к Мастеру вместе с обычным раундом
        # (ник = username or display_name, как в resolve_round_master_only)
        assert host.dm.calls[0].get("rimi") == "Дн. договариваюсь с купцом"

    @pytest.mark.asyncio
    async def test_leftover_not_duplicated_when_player_also_in_queue(self):
        """Если игрок успел подать действие и в основной раунд — берём его
        (новее), остаток non-combat очереди НЕ дублируется."""
        db = CombatEndDB()
        db.players = [FakePlayer(5, username="rimi", display_name="Рими")]
        host = CombinedHost(db)
        host.submit_non_combat_action("s1", 5, "старое действие из боя")

        db._queue = make_queue([5])
        is_complete, _msg = host.submit_action("s1", 5, "новое действие после боя")
        assert is_complete
        await host.resolve_round_master_only("s1")

        actions = host.dm.calls[0]
        assert actions.get("rimi") == "Дн. новое действие после боя"
        assert "Дн. старое действие из боя" not in actions.values(), \
            "остаток продублировал действие"


# ═══════════════════════════════════════════════════════════════
# 4. Матчинг участников боя (игроки корректно добавляются в бой)
# ═══════════════════════════════════════════════════════════════

class TestParticipantMatching:

    def make_chars_players(self):
        chars = [
            FakeChar(10, 1, "Храфна", race="Шадар-кай"),
            FakeChar(11, 2, "Стрикс", race="Совин"),
            FakeChar(12, 3, "СЛИЗЬ", race="Слизь"),
        ]
        players = [
            FakePlayer(1, username="hrafn", display_name="Храфна"),
            FakePlayer(2, username="owl", display_name="Оулич"),
            FakePlayer(3, username="sliz", display_name="Слизень"),
        ]
        return chars, players

    def test_exact_match(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("Храфна", chars, players) is chars[0]

    def test_case_insensitive(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("храфна", chars, players) is chars[0]

    def test_full_name_from_narrative(self):
        """«Храфна Морвен» из нарратива Мастера → персонаж «Храфна» (0.5 покрытия)."""
        chars, players = self.make_chars_players()
        assert match_participant_to_character("Храфна Морвен", chars, players) is chars[0]

    def test_username_match(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("@hrafn", chars, players) is chars[0]

    def test_punctuation_ignored(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("Стрикс!", chars, players) is chars[1]

    def test_monster_not_pulled_to_pc(self):
        """Регресс BUG #3-семьи: «Воин с мечом СЛИЗЬ» — НЕ игрок СЛИЗЬ."""
        chars, players = self.make_chars_players()
        assert match_participant_to_character("Воин с мечом СЛИЗЬ", chars, players) is None

    def test_plain_monster_is_none(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("Гоблин-перебежчик", chars, players) is None

    def test_empty_and_none(self):
        chars, players = self.make_chars_players()
        assert match_participant_to_character("", chars, players) is None
        assert match_participant_to_character("Храфна", [], players) is None

    def test_ambiguous_token_match_is_none(self):
        """Два одинаковых имени с равным покрытием → неоднозначность → None."""
        chars2 = [FakeChar(1, 1, "Близнец"), FakeChar(2, 2, "Близнец")]
        players2 = [FakePlayer(1, "b1", "Б1"), FakePlayer(2, "b2", "Б2")]
        assert match_participant_to_character("Близнец Злой", chars2, players2) is None

    def test_monster_with_pc_name_inside_stays_npc_in_full_combat(self):
        chars, players = self.make_chars_players()
        # Точный матч только для полного имени; монстр с именем игрока
        # внутри — остаётся NPC (player_id=0 у комбатанта создаёт вызывающий).
        assert match_participant_to_character(
            "СЛИЗЬ-охотник за приключенцами", chars, players) is None


class TestStartCombatRegistersPlayersCorrectly:

    class CombatStartDB(FakeDB):
        def __init__(self):
            super().__init__()
            self.encounter = None
            self.combatants = []

        def get_session_characters(self, session_id):
            return list(self.chars)

        def create_combat_encounter(self, enc):
            self.encounter = enc

        def add_combatant(self, c):
            self.combatants.append(c)

        def update_session(self, session):
            pass

        def add_history(self, entry: HistoryEntry):
            if not hasattr(self, "_history"):
                self._history = []
            self._history.append(entry)

    def test_pc_matched_by_full_narrative_name(self):
        db = self.CombatStartDB()
        db.chars = [FakeChar(10, 1, "Храфна")]
        db.players = [FakePlayer(1, "hrafn", "Храфна")]
        db._session.combat_active = False

        Bot(db).start_combat_for_participants(
            "s1", ["Храфна Морвен", "Главный стражник"], "стычка у ворот")

        by_type = {}
        for c in db.combatants:
            by_type.setdefault(c.entity_type, []).append(c)
        assert len(by_type["pc"]) == 1
        pc = by_type["pc"][0]
        assert pc.player_id == 1, "игрок зарегистрирован как NPC — гонка вернётся"
        # Комбатант получает каноническое имя ПЕРСОНАЖА (не нарративный алиас),
        # чтобы get_combat_context/update_combatant находили его по листу.
        assert pc.name == "Храфна"
        npc = by_type["npc"][0]
        assert npc.player_id == 0 and npc.name == "Главный стражник"

    def test_pc_matched_by_username(self):
        db = self.CombatStartDB()
        db.chars = [FakeChar(10, 1, "Анджей")]
        db.players = [FakePlayer(1, "leha", "Лёха")]
        db._session.combat_active = False

        Bot(db).start_combat_for_participants("s1", ["@leha"], "драка")
        assert db.combatants[0].entity_type == "pc"
        assert db.combatants[0].player_id == 1

    def test_all_session_pcs_never_become_npcs(self):
        db = self.CombatStartDB()
        db.chars = [
            FakeChar(10, 1, "Калючка"),
            FakeChar(11, 2, "Стрикс"),
            FakeChar(12, 3, "Храфна"),
            FakeChar(13, 4, "Рими"),
        ]
        db.players = [
            FakePlayer(1, "p1", "Калючка"), FakePlayer(2, "p2", "Стрикс"),
            FakePlayer(3, "p3", "Храфна"), FakePlayer(4, "p4", "Рими"),
        ]
        db._session.combat_active = False

        Bot(db).start_combat_for_participants(
            "s1",
            ["Калючка", "Стрикс Совин", "Храфна Морвен", "Рими", "Стражники ворот"],
            "бой у ворот")

        pc_ids = {c.player_id for c in db.combatants if c.entity_type == "pc"}
        assert pc_ids == {1, 2, 3, 4}, (
            f"не все игроки в бою: "
            f"{[(c.name, c.entity_type, c.player_id) for c in db.combatants]}"
        )
        npcs = [c for c in db.combatants if c.entity_type == "npc"]
        assert [n.name for n in npcs] == ["Стражники ворот"]

    def test_initiative_combat_legacy_path_also_matches(self):
        db = self.CombatStartDB()
        db.chars = [FakeChar(10, 1, "Храфна")]
        db.players = [FakePlayer(1, "hrafn", "Храфна")]
        db._session.combat_active = False
        db.get_initiative_order = lambda encounter_id: sorted(
            ([{"name": c.name, "entity_type": c.entity_type,
               "player_id": c.player_id, "initiative": c.initiative}
              for c in db.combatants]),
            key=lambda e: -e["initiative"])

        Bot(db).start_initiative_combat("s1", ["Храфна Морвен"], "dechrauymladd")
        assert db.combatants[0].entity_type == "pc"
        assert db.combatants[0].player_id == 1
