"""ИТЕРАЦИЯ 10 — тесты сводного ТЗ (8 разделов).

Раздел 1: бой не съедает заявленное действие (промпт-фикс)
Раздел 2: смена персонажа запрещена после создания мира
Раздел 3: апелляция DB-боту при нулевой истории отклоняется
Раздел 4: тай-брейк инициативы (priority, меньше = раньше)
Раздел 5: анти-рестарт (консенсус игроков не меняет историю — промпт-фикс)
Раздел 6: монетизация (грант 30кк, реальный usage, settle по billing_mode)
Раздел 7: плагин billing удаляем без краша (get_plugin-паттерн)
Раздел 8: перебор провайдер → модели без памяти между запросами
"""
import asyncio
import json
import os
import sqlite3

import pytest

from tests.conftest import FakeDB, FakeSession, FakeChar, FakePlayer

from libs.db import Database, Session, Player, Character, Combatant, CombatEncounter
from libs.db.billing_repo import BillingRepo
from libs.session.combat_coordinator import CombatCoordinatorMixin


# ═══════════════════════════════════════════════════════════════
# Разделы 1 и 5: промпт-фиксы мастера
# ═══════════════════════════════════════════════════════════════

class TestPromptFixes:
    def test_master_prompt_has_combat_not_eating_action(self):
        from libs.ai.prompts import MASTER_PROMPT
        assert "НАЧАЛО БОЯ НЕ СТИРАЕТ ЗАЯВЛЕННОЕ ДЕЙСТВИЕ" in MASTER_PROMPT
        assert "НА РЕЗУЛЬТАТ этого действия, а не ВМЕСТО него" in MASTER_PROMPT
        # оба примера пользователя присутствуют
        assert "гоблин" in MASTER_PROMPT
        assert "иду искать таверну" in MASTER_PROMPT

    def test_master_prompt_has_anti_restart(self):
        from libs.ai.prompts import MASTER_PROMPT
        assert "АНТИ-РЕСТАРТ" in MASTER_PROMPT
        assert "это не голосование" in MASTER_PROMPT
        assert "НЕ ЯВЛЯЮТСЯ командой" in MASTER_PROMPT

    def test_master_md_and_applied_in_sync(self):
        """MD/ — источник, MD/_applied/ — кэш последнего применённого состояния.
        Шипим их одинаковыми, иначе новая сессия получит старый промпт."""
        from libs.config_legacy import MD_DIR
        src = open(os.path.join(MD_DIR, "prompts", "master.md"), encoding="utf-8").read()
        applied = open(os.path.join(MD_DIR, "_applied", "prompts", "master.md"), encoding="utf-8").read()
        assert src == applied
        assert "НАЧАЛО БОЯ НЕ СТИРАЕТ" in applied
        assert "АНТИ-РЕСТАРТ" in applied

    def test_tool_schema_has_priorities(self):
        from libs.ai.tools import MASTER_TOOLS
        spec = next(t for t in MASTER_TOOLS
                    if t["function"]["name"] == "dechrauymladd")
        props = spec["function"]["parameters"]["properties"]
        assert "priorities" in props
        assert props["priorities"]["type"] == "object"


# ═══════════════════════════════════════════════════════════════
# Раздел 2: смена персонажа после создания мира
# ═══════════════════════════════════════════════════════════════

class FakeSessionFull:
    """FakeSession + current_scene (персистентный маркер «мир создан»)."""
    def __init__(self, session_id="s1", world_created=False):
        self.id = session_id
        self.current_scene = "Таверна у дороги" if world_created else ""
        self.combat_active = False


class CharChangeDB(FakeDB):
    def __init__(self, char=None):
        super().__init__()
        self._char = char

    def get_character_by_player(self, uid, sid):
        return self._char


def make_char(hp=20, is_alive=True, name="Хравна"):
    c = FakeChar(1, 7, name)
    c.hp = hp
    c.is_alive = is_alive
    return c


class TestCharChangeGuard:
    def _blocked(self, world_created, char):
        from libs.handlers.character_cmds import _char_change_blocked
        return _char_change_blocked(CharChangeDB(char), FakeSessionFull(world_created=world_created), 7)

    def test_new_player_allowed(self):
        # игрок новый (персонажа нет) — менять можно всегда
        assert self._blocked(True, None) is None

    def test_world_not_created_alive_char_allowed(self):
        # мир ещё не создан — перегенерация листа разрешена
        assert self._blocked(False, make_char(hp=20)) is None

    def test_world_created_alive_char_blocked(self):
        msg = self._blocked(True, make_char(hp=20))
        assert msg is not None
        assert "Смена персонажа запрещена" in msg
        assert "Хравна" in msg

    def test_world_created_dead_char_allowed(self):
        # персонаж погиб (hp=0) — замена разрешена
        assert self._blocked(True, make_char(hp=0)) is None

    def test_world_created_killed_flag_allowed(self):
        # is_alive=False (0 HP -> выпал из жизни) — замена разрешена
        assert self._blocked(True, make_char(hp=0, is_alive=False)) is None


# ═══════════════════════════════════════════════════════════════
# Раздел 3: апелляция при нулевой истории
# ═══════════════════════════════════════════════════════════════

class TestZeroRoundAppealGuard:
    @pytest.fixture
    def db(self, tmp_path):
        d = Database(str(tmp_path / "appeal.db"))
        d.create_session(Session(id="s1", chat_id=1, name="t", creator_id=7,
                                 status="active", current_scene="мир"))
        return d

    def _has_history(self, db):
        from libs.handlers import utils as u
        orig = u.db_manager
        class M:
            def get_db(self, sid):
                return db
        u.db_manager = M()
        try:
            return u.has_any_round_history("s1")
        finally:
            u.db_manager = orig

    def test_no_history_at_all(self, db):
        assert self._has_history(db) is False

    def test_system_entry_only_is_not_a_round(self, db):
        """Окно «мир создан, но ходов ещё не было»: [DNDSTART]/[AUTO_START]
        записи НЕ считаются ходами — апелляция должна быть отклонена."""
        db.add_history(type("H", (), {"session_id": "s1", "author": "SYSTEM",
                                      "content": "[DNDSTART] World generated.",
                                      "entry_type": "system", "id": 0, "created_at": ""})())
        assert self._has_history(db) is False

    def test_narrative_entry_is_a_round(self, db):
        db.add_history(type("H", (), {"session_id": "s1", "author": "DM",
                                      "content": "[Раунд 1] ты входишь в таверну",
                                      "entry_type": "narrative", "id": 0, "created_at": ""})())
        assert self._has_history(db) is True


# ═══════════════════════════════════════════════════════════════
# Раздел 4: тай-брейк инициативы (priority, меньше = раньше)
# ═══════════════════════════════════════════════════════════════

class CombatDB(FakeDB):
    """FakeDB + реальная SQL-поверхность боевых таблиц поверх SQLite.
    Тестирует РЕАЛЬНЫЕ ORDER BY / миграцию, а не фейки."""

    def __init__(self, real: Database):
        super().__init__()
        self._real = real

    def get_session(self, sid):
        return self._real.get_session(sid)

    def update_session(self, session):
        self._real.update_session(session)

    def get_players(self, sid):
        return self._real.get_players(sid)

    def get_session_characters(self, sid):
        return self._real.get_session_characters(sid)

    def create_combat_encounter(self, enc):
        self._real.create_combat_encounter(enc)

    def add_combatant(self, c):
        self._real.add_combatant(c)

    def get_initiative_order(self, encounter_id):
        return self._real.get_initiative_order(encounter_id)

    def add_history(self, entry):
        self._real.add_history(entry)


@pytest.fixture
def combat_env(tmp_path):
    real = Database(str(tmp_path / "prio.db"))
    real.create_session(Session(id="s1", chat_id=1, name="t", creator_id=7,
                                status="active", current_scene="мир"))
    real.add_player(Player(user_id=7, session_id="s1", username="u7",
                           display_name="Хравна", is_creator=True))
    real.save_character(Character(
        id="c7", session_id="s1", player_id=7, name="Хравна",
        race="Человек", class_name="Воин", level=1, hp=10, max_hp=12, ac=14,
        stats='{"dexterity": 10}', proficiencies="[]", inventory="[]",
        spells="[]", features="[]", backstory="", languages="[]"))
    db = CombatDB(real)
    host = type("Host", (CombatCoordinatorMixin,), {})()
    host.db_manager = type("M", (), {"get_db": lambda self, sid: db})()
    return host, real


class TestInitiativePriority:
    def test_tie_break_by_priority_python_sort(self, combat_env):
        """Равные инициативы: priority МЕНЬШЕ ходит РАНЬШЕ ( Раздел 4)."""
        host, real = combat_env
        import libs.session.combat_coordinator as cc
        # фиксируем броски: Хравна и Колючка получают одинаковые 15
        rolls = iter([15, 15])
        monkey_rng = lambda a, b: next(rolls)
        orig = cc.random.randint
        cc.random.randint = lambda a, b: next(rolls)
        try:
            text = host.start_combat_for_participants(
                "s1", ["Хравна", "Колючка"], "тест",
                priorities={"Хравна": 1, "Колючка": 2})
        finally:
            cc.random.randint = orig
        order = json.loads(real.get_session("s1").initiative_order)
        assert order[0]["name"] == "Хравна"   # priority 1 < 2
        assert order[1]["name"] == "Колючка"
        assert "First to act: Хравна" in text

    def test_higher_initiative_beats_priority(self, combat_env):
        """Инициатива главнее: больший бросок ходит первым независимо от priority."""
        host, real = combat_env
        import libs.session.combat_coordinator as cc
        rolls = iter([18, 10])
        orig = cc.random.randint
        cc.random.randint = lambda a, b: next(rolls)
        try:
            host.start_combat_for_participants(
                "s1", ["Хравна", "Колючка"], "тест",
                priorities={"Хравна": 5, "Колючка": 0})
        finally:
            cc.random.randint = orig
        order = json.loads(real.get_session("s1").initiative_order)
        assert order[0]["name"] == "Хравна"  # 18 > 10, priority не важен

    def test_sql_order_uses_priority(self, combat_env, tmp_path):
        """SQL get_initiative_order: initiative DESC, priority ASC."""
        real = combat_env[1]
        enc = CombatEncounter(id="e1", session_id="s1", reason="r",
                              created_at="now")
        real.create_combat_encounter(enc)
        for name, initiative, priority in [
            ("А", 15, 3), ("Б", 15, 1), ("В", 15, 0), ("Г", 20, 9),
        ]:
            real.add_combatant(Combatant(
                id=name, encounter_id="e1", session_id="s1", name=name,
                entity_type="npc", player_id=0, initiative=initiative,
                natural_roll=initiative, dex_mod=0, sort_order=0,
                priority=priority))
        order = real.get_initiative_order("e1")
        names = [e["name"] for e in order]
        assert names == ["Г", "В", "Б", "А"]  # 20; равные 15 → priority 0,1,3

    def test_default_priority_zero(self):
        c = Combatant(id="x", encounter_id="e", session_id="s", name="n")
        assert c.priority == 0

    def test_migration_adds_priority_column(self, tmp_path):
        """Старая БД без колонки priority мигрирует ALTER'ом при открытии."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE combatants (
            id TEXT PRIMARY KEY, encounter_id TEXT NOT NULL,
            session_id TEXT NOT NULL, name TEXT NOT NULL,
            entity_type TEXT DEFAULT 'pc', player_id INTEGER DEFAULT 0,
            initiative INTEGER DEFAULT 0, natural_roll INTEGER DEFAULT 0,
            dex_mod INTEGER DEFAULT 0, hp INTEGER DEFAULT 0,
            max_hp INTEGER DEFAULT 0, ac INTEGER DEFAULT 10,
            current_conditions TEXT DEFAULT '[]', is_alive INTEGER DEFAULT 1,
            traits TEXT DEFAULT '', brief_context TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0)""")
        conn.commit()
        conn.close()
        Database(path)  # открытие мигрирует
        conn = sqlite3.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(combatants)")}
        conn.close()
        assert "priority" in cols

    def test_legacy_start_initiative_combat_priorities(self, combat_env):
        host, real = combat_env
        import libs.session.combat_coordinator as cc
        rolls = iter([10, 10, 10])
        orig = cc.random.randint
        cc.random.randint = lambda a, b: next(rolls)
        try:
            host.start_initiative_combat(
                "s1", ["Хравна", "Гоблин"], "тест",
                priorities={"Гоблин": -1})  # гоблин раньше при равных
        finally:
            cc.random.randint = orig
        order = real.get_initiative_order(
            real.get_active_combat_encounter("s1").id)
        assert order[0]["name"] == "Гоблин"  # priority -1 < 0


# ═══════════════════════════════════════════════════════════════
# Раздел 8: перебор провайдер → модели БЕЗ памяти между запросами
# ═══════════════════════════════════════════════════════════════

class TestProvidersConfig:
    def test_parse_valid(self):
        from libs.config_legacy import _parse_providers_json
        raw = json.dumps([
            {"name": "p1", "base_url": "http://a/v1/", "api_key": "k1",
             "models": ["m1", "m2"]},
            {"name": "p2", "base_url": "http://b/v1", "api_key": "k2",
             "models": ["m3"]},
        ])
        provs = _parse_providers_json(raw)
        assert len(provs) == 2
        assert provs[0]["base_url"] == "http://a/v1"  # trailing slash trimmed
        assert provs[0]["models"] == ["m1", "m2"]

    def test_parse_invalid_json_returns_empty(self, capsys):
        from libs.config_legacy import _parse_providers_json
        assert _parse_providers_json("не json") == []
        assert _parse_providers_json("") == []
        assert _parse_providers_json('{"name": "x"}') == []  # корень не массив

    def test_parse_skips_entries_without_base_url(self):
        from libs.config_legacy import _parse_providers_json
        raw = json.dumps([
            {"name": "broken", "models": ["m"]},
            {"name": "ok", "base_url": "http://x/v1", "models": ["m"]},
        ])
        provs = _parse_providers_json(raw)
        assert len(provs) == 1 and provs[0]["name"] == "ok"

    def test_role_env_override(self, monkeypatch):
        from libs.config_legacy import get_providers_for_role
        monkeypatch.setenv("SOMEROLE_PROVIDERS", json.dumps([
            {"name": "role-prov", "base_url": "http://role/v1",
             "api_key": "rk", "models": ["rm1", "rm2"]}]))
        provs = get_providers_for_role("somerole", "dm", "http://d/v1", "dk")
        assert provs[0]["name"] == "role-prov"
        assert provs[0]["models"] == ["rm1", "rm2"]

    def test_llm_providers_fallback(self, monkeypatch):
        from libs.config_legacy import get_providers_for_role
        monkeypatch.setenv("SOMEROLE2_PROVIDERS", "")
        monkeypatch.setattr("libs.config_legacy.LLM_PROVIDERS", json.dumps([
            {"name": "global", "base_url": "http://g/v1",
             "api_key": "gk", "models": ["gm"]}]))
        provs = get_providers_for_role("somerole2", "dm", "http://d/v1", "dk")
        assert provs[0]["name"] == "global"

    def test_default_single_provider(self, monkeypatch):
        from libs.config_legacy import get_providers_for_role
        monkeypatch.setenv("SOMEROLE3_PROVIDERS", "")
        monkeypatch.setattr("libs.config_legacy.LLM_PROVIDERS", "")
        provs = get_providers_for_role("somerole3", "my-model", "http://d/v1", "dk")
        assert provs == [{"name": "default", "base_url": "http://d/v1",
                          "api_key": "dk", "models": ["my-model"]}]


class TestProviderSwitchLoop:
    """Вложенный цикл chat(): переключение пары при 401/402/403 и исчерпании
    ретраев; 400 — сразу наружу; всё исчерпано — AllProvidersExhausted."""

    def _patch_providers(self, monkeypatch, providers):
        monkeypatch.setattr(
            "libs.ai.client.get_providers_for_role",
            lambda role, **kw: providers)

    def test_switch_on_401_then_success(self, monkeypatch):
        """Сигнал переключения (_chat_once конвертирует 401 в _ProviderSwitch)
        двигает перебор к следующей модели/провайдеру."""
        from libs.ai.client import OpenAIClient, _ProviderSwitch
        providers = [
            {"name": "p1", "base_url": "http://a/v1", "api_key": "k1",
             "models": ["m1", "m2"]},
            {"name": "p2", "base_url": "http://b/v1", "api_key": "k2",
             "models": ["m3"]},
        ]
        self._patch_providers(monkeypatch, providers)
        c = OpenAIClient("m1", role="master", base_url="http://d/v1", api_key="dk")
        tried = []

        async def fake_once(provider, model, payload, retries):
            tried.append((provider["name"], model))
            if provider["name"] == "p1":
                raise _ProviderSwitch(provider["name"], model, "HTTP 401: unauthorized")
            return {"ok": True, "model": model}

        c._chat_once = fake_once
        result = asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        assert result["ok"] is True
        assert tried == [("p1", "m1"), ("p1", "m2"), ("p2", "m3")]

    def test_switch_on_retry_exhaustion(self, monkeypatch):
        from libs.ai.client import OpenAIClient, _ProviderSwitch
        providers = [{"name": "p1", "base_url": "http://a/v1",
                      "api_key": "k1", "models": ["m1", "m2"]}]
        self._patch_providers(monkeypatch, providers)
        c = OpenAIClient("m1", role="master")

        async def fake_once(provider, model, payload, retries):
            raise _ProviderSwitch(provider["name"], model, "retries exhausted (network)")

        c._chat_once = fake_once
        from libs.ai.client import AllProvidersExhausted
        with pytest.raises(AllProvidersExhausted) as exc:
            asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        assert exc.value.attempts == 2

    def test_400_does_not_switch(self, monkeypatch):
        from libs.ai.client import OpenAIClient, ApiError
        providers = [
            {"name": "p1", "base_url": "http://a/v1", "api_key": "k1",
             "models": ["m1"]},
            {"name": "p2", "base_url": "http://b/v1", "api_key": "k2",
             "models": ["m2"]},
        ]
        self._patch_providers(monkeypatch, providers)
        c = OpenAIClient("m1", role="master")
        tried = []

        async def fake_once(provider, model, payload, retries):
            tried.append((provider["name"], model))
            raise ApiError(400, "bad request — наша ошибка, не провайдера")

        c._chat_once = fake_once
        with pytest.raises(ApiError):
            asyncio.run(c.chat([{"role": "user", "content": "hi"}]))
        assert tried == [("p1", "m1")]  # p2 НЕ пробовался

    def test_no_memory_between_calls(self, monkeypatch):
        """Каждый вызов начинает перебор ЗАНОВО с пары №1 (Раздел 8)."""
        from libs.ai.client import OpenAIClient, _ProviderSwitch
        providers = [
            {"name": "p1", "base_url": "http://a/v1", "api_key": "k1",
             "models": ["m1"]},
            {"name": "p2", "base_url": "http://b/v1", "api_key": "k2",
             "models": ["m2"]},
        ]
        self._patch_providers(monkeypatch, providers)
        c = OpenAIClient("m1", role="master")
        calls = []

        async def fake_once(provider, model, payload, retries):
            calls.append(provider["name"])
            if provider["name"] == "p1":
                raise _ProviderSwitch(provider["name"], model, "HTTP 401")
            return {"ok": model}

        c._chat_once = fake_once
        r1 = asyncio.run(c.chat([{"role": "user", "content": "1"}]))
        r2 = asyncio.run(c.chat([{"role": "user", "content": "2"}]))
        # ОБА вызова прошли полный путь p1(фейл) → p2(ок) — без «залипания» на p2
        assert calls == ["p1", "p2", "p1", "p2"]
        assert r1["ok"] == "m2" and r2["ok"] == "m2"


# ═══════════════════════════════════════════════════════════════
# Разделы 6-7: биллинг — грант, реальное списание, гейты, плагин
# ═══════════════════════════════════════════════════════════════

class TestBillingRepo:
    @pytest.fixture
    def repo(self, tmp_path):
        return BillingRepo(str(tmp_path / "billing.db"))

    def test_grant_issued_once(self, repo):
        assert repo.ensure_grant(42, 30_000_000) == 30_000_000
        assert repo.has_received_grant(42)
        # повторный вызов (вход в другую сессию/чат) НЕ пересоздаёт грант —
        # эксплойт «вышел-зашёл» невозможен (баланс глобален по user_id)
        assert repo.ensure_grant(42, 30_000_000) == 30_000_000

    def test_charge_reduces_balance(self, repo):
        repo.ensure_grant(42, 1000)
        assert repo.charge(42, 300, "s1") == 700
        assert repo.charge(42, 200, "s1") == 500
        assert repo.get_balance(42) == 500

    def test_charge_never_negative(self, repo):
        repo.ensure_grant(42, 100)
        assert repo.charge(42, 10_000) == 0  # списывает остаток, не уходит в минус

    def test_charge_unknown_user_safe(self, repo):
        assert repo.charge(999, 500) == 0
        assert repo.get_balance(999) == 0

    def test_topup_and_log(self, repo):
        repo.ensure_grant(42, 1000)
        repo.charge(42, 100, "s1", note="раунд 1")
        repo.topup(111, 42, 5000, note="ручное пополнение")
        log = repo.get_log()
        kinds = [e["kind"] for e in log]
        assert kinds == ["manual_topup", "round_charge", "grant"]
        topup_entry = log[0]
        assert topup_entry["actor_id"] == 111
        assert topup_entry["user_id"] == 42
        assert topup_entry["amount"] == 5000

    def test_balances_independent_per_user(self, repo):
        repo.ensure_grant(1, 100)
        repo.ensure_grant(2, 200)
        repo.charge(1, 40)
        assert repo.get_balance(1) == 60
        assert repo.get_balance(2) == 200


class TestSessionBillingMode:
    def test_billing_mode_roundtrip(self, tmp_path):
        db = Database(str(tmp_path / "bm.db"))
        db.create_session(Session(id="s1", chat_id=1, name="t", creator_id=7,
                                  status="active", current_scene=""))
        s = db.get_session("s1")
        assert s.billing_mode == "split"  # дефолт
        s.billing_mode = "creator_pays"
        db.update_session(s)
        assert db.get_session("s1").billing_mode == "creator_pays"

    def test_billing_mode_migration(self, tmp_path):
        """Старая БД без billing_mode мигрирует и отдаёт дефолт 'split'."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, name TEXT NOT NULL,
            creator_id INTEGER NOT NULL, status TEXT DEFAULT 'active',
            current_scene TEXT DEFAULT '', combat_active INTEGER DEFAULT 0,
            initiative_order TEXT DEFAULT '[]', current_turn_index INTEGER DEFAULT 0,
            round_number INTEGER DEFAULT 0, pvp_active INTEGER DEFAULT 0,
            autostart INTEGER DEFAULT 0, summary TEXT DEFAULT '',
            summary_at_count INTEGER DEFAULT 0, genre TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO sessions (id, chat_id, name, creator_id) "
                     "VALUES ('old1', 1, 'n', 7)")
        conn.commit()
        conn.close()
        db = Database(path)
        s = db.get_session("old1")
        assert s is not None and s.billing_mode == "split"


class _FakeChat:
    def __init__(self, id, type):
        self.id, self.type = id, type


class _FakeUser:
    def __init__(self, id):
        self.id = id


class _FakeUpd:
    def __init__(self, chat, user):
        self.effective_chat, self.effective_user = chat, user


class _Sess:
    def __init__(self, id="s1", creator_id=7, billing_mode="split"):
        self.id = id
        self.creator_id = creator_id
        self.billing_mode = billing_mode
        self.current_scene = "мир"
        self.round_number = 3


def make_plugin(tmp_path, testers=(111,), active=True):
    from plugins.billing.plugin import BillingPlugin
    plg = BillingPlugin.__new__(BillingPlugin)
    BillingPlugin.__init__(plg)
    plg._active = active
    plg._testers = set(testers)
    plg._repo = BillingRepo(str(tmp_path / "gate.db"))
    return plg


class TestBillingGate:
    @pytest.mark.asyncio
    async def test_inactive_passes_everywhere(self, tmp_path, monkeypatch):
        """COMMERCIAL_MODE=false → игра бесплатна везде, ЛС открыта."""
        plg = make_plugin(tmp_path, active=False)
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(1, "private"), _FakeUser(42)), None)
        assert ok
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(777, "supergroup"), _FakeUser(42)), None,
            session=_Sess())
        assert ok

    @pytest.mark.asyncio
    async def test_dm_blocked_for_non_tester(self, tmp_path):
        plg = make_plugin(tmp_path)
        ok, msg = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(1, "private"), _FakeUser(42)), None)
        assert not ok
        assert "ЛС" in msg

    @pytest.mark.asyncio
    async def test_dm_allowed_for_tester(self, tmp_path):
        plg = make_plugin(tmp_path)
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(1, "private"), _FakeUser(111)), None)
        assert ok

    @pytest.mark.asyncio
    async def test_main_chat_free(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 500)
        plg = make_plugin(tmp_path)
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(500, "supergroup"), _FakeUser(42)), None,
            session=_Sess())
        assert ok

    @pytest.mark.asyncio
    async def test_paid_chat_zero_balance_blocked(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        plg = make_plugin(tmp_path)
        plg._repo.ensure_grant(42, 100)
        plg._repo.charge(42, 100)  # обнуляем
        ok, msg = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(777, "supergroup"), _FakeUser(42)), None,
            session=_Sess())
        assert not ok
        assert "Токены исчерпаны" in msg
        assert "ychwanegu" in msg  # подсказка про пополнение

    @pytest.mark.asyncio
    async def test_paid_chat_with_balance_grants_once(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        plg = make_plugin(tmp_path)
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(777, "supergroup"), _FakeUser(42)), None,
            session=_Sess())
        assert ok
        assert plg._repo.get_balance(42) == 30_000_000
        # грант ОДИН раз: повторный гейт не добавляет
        await plg.check_game_allowed(
            _FakeUpd(_FakeChat(777, "supergroup"), _FakeUser(42)), None,
            session=_Sess())
        assert plg._repo.get_balance(42) == 30_000_000

    @pytest.mark.asyncio
    async def test_creator_pays_tester_creator_skips_gate(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        plg = make_plugin(tmp_path)
        # создатель — тестер (безлимит): гейт пропускает, игрок платит 0
        ok, _ = await plg.check_game_allowed(
            _FakeUpd(_FakeChat(777, "supergroup"), _FakeUser(42)), None,
            session=_Sess(billing_mode="creator_pays", creator_id=111))
        assert ok
        # грант новичку при входе в платный чат выдан (он зашёл — грант его),
        # но ПЛАТИТЬ будет тестер-создатель, чей баланс неограничен
        assert plg._repo.get_balance(42) == 30_000_000
        assert plg._repo.has_received_grant(111) is False

    def test_resolve_payer_modes(self, tmp_path):
        plg = make_plugin(tmp_path)
        assert plg.resolve_payer(_Sess(billing_mode="split"), 42) == 42
        assert plg.resolve_payer(_Sess(billing_mode="creator_pays", creator_id=7), 42) == 7
        # мусор в billing_mode → дефолт split
        assert plg.resolve_payer(_Sess(billing_mode="banana"), 42) == 42


class TestSettleRound:
    def _setup_session(self, tmp_path, players, billing_mode="split",
                       creator_id=None, chat_id=777):
        real = Database(str(tmp_path / f"settle_{chat_id}.db"))
        real.create_session(Session(id="s1", chat_id=chat_id, name="t",
                                    creator_id=creator_id or players[0],
                                    status="active", current_scene="мир",
                                    billing_mode=billing_mode))
        for pid in players:
            real.add_player(Player(user_id=pid, session_id="s1",
                                   username=f"u{pid}", display_name=f"P{pid}"))
        plg = make_plugin(tmp_path, testers=(111,))
        # Гранты игрокам уже выданы гейтом Дн. до раунда (реальный флоу).
        for pid in players:
            if pid not in plg._testers:
                plg._repo.ensure_grant(pid, 30_000_000)
        plg._real = real
        return plg, real

    @pytest.mark.asyncio
    async def test_split_charges_every_player(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        import libs.handlers._state as st
        plg, real = self._setup_session(tmp_path, [42, 43])
        orig = st.db_manager
        st.db_manager = type("M", (), {"get_db": lambda self, sid: real})()
        try:
            plg._pending["s1"] = 1000
            await plg.settle_round("s1", bot_obj=None, chat_id=777)
        finally:
            st.db_manager = orig
        # 1000 // 2 = 500 каждому
        assert plg._repo.get_balance(42) == 30_000_000 - 500
        assert plg._repo.get_balance(43) == 30_000_000 - 500
        assert plg.pending_usage("s1") == 0  # списано и очищено

    @pytest.mark.asyncio
    async def test_creator_pays_charges_only_creator(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        import libs.handlers._state as st
        plg, real = self._setup_session(tmp_path, [42, 43],
                                        billing_mode="creator_pays",
                                        creator_id=7)
        # создатель тоже заходил в платный чат раньше — у него грант
        plg._repo.ensure_grant(7, 30_000_000)
        orig = st.db_manager
        st.db_manager = type("M", (), {"get_db": lambda self, sid: real})()
        try:
            plg._pending["s1"] = 1000
            await plg.settle_round("s1", bot_obj=None, chat_id=777)
        finally:
            st.db_manager = orig
        assert plg._repo.get_balance(7) == 30_000_000 - 1000
        assert plg._repo.get_balance(42) == 30_000_000  # игроки не платят
        assert plg._repo.get_balance(43) == 30_000_000

    @pytest.mark.asyncio
    async def test_tester_not_charged(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        import libs.handlers._state as st
        plg, real = self._setup_session(tmp_path, [42, 111])  # 111 — тестер
        orig = st.db_manager
        st.db_manager = type("M", (), {"get_db": lambda self, sid: real})()
        try:
            plg._pending["s1"] = 1000
            await plg.settle_round("s1", bot_obj=None, chat_id=777)
        finally:
            st.db_manager = orig
        # 1000 // 2 = 500, но тестер НЕ списывается
        assert plg._repo.get_balance(42) == 30_000_000 - 500
        assert plg._repo.get_balance(111) == 0  # тестеру грант даже не выдавался

    @pytest.mark.asyncio
    async def test_main_chat_not_charged(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 500)
        import libs.handlers._state as st
        plg, real = self._setup_session(tmp_path, [42], chat_id=500)
        orig = st.db_manager
        st.db_manager = type("M", (), {"get_db": lambda self, sid: real})()
        try:
            plg._pending["s1"] = 1000
            await plg.settle_round("s1", bot_obj=None, chat_id=500)
        finally:
            st.db_manager = orig
        assert plg._repo.get_balance(42) == 30_000_000  # общий чат бесплатен

    @pytest.mark.asyncio
    async def test_inactive_settle_noop(self, tmp_path, monkeypatch):
        import libs.config_legacy as cfg
        monkeypatch.setattr(cfg, "MAIN_CHAT_ID", 0)
        plg, real = self._setup_session(tmp_path, [42])
        plg._active = False
        plg._pending["s1"] = 1000
        await plg.settle_round("s1", bot_obj=None, chat_id=777)
        assert plg._repo.get_balance(42) == 30_000_000  # не списано


class TestUsageLedger:
    def test_scope_attributes_usage(self):
        from libs.ai import usage_ledger
        got = []
        cb = lambda sid, role, model, tokens: got.append((sid, role, tokens))
        usage_ledger.subscribe(cb)
        try:
            with usage_ledger.scope("sess-A"):
                usage_ledger.record("master", "m", 100)
                usage_ledger.record("db", "m", 50)
            usage_ledger.record("master", "m", 999)  # вне сессии
            with usage_ledger.scope("sess-B"):
                usage_ledger.record("master", "m", 7)
        finally:
            usage_ledger.unsubscribe(cb)
        assert got == [("sess-A", "master", 100), ("sess-A", "db", 50),
                       ("sess-B", "master", 7)]

    def test_bad_tokens_ignored(self):
        from libs.ai import usage_ledger
        got = []
        cb = lambda *a: got.append(a)
        usage_ledger.subscribe(cb)
        try:
            usage_ledger.record("master", "m", 0)
            usage_ledger.record("master", "m", -5)
            usage_ledger.record("master", "m", "banana")
            usage_ledger.record("master", "m", None)
        finally:
            usage_ledger.unsubscribe(cb)
        assert got == []

    def test_subscriber_error_does_not_break(self):
        from libs.ai import usage_ledger

        def bad(*a):
            raise RuntimeError("boom")
        usage_ledger.subscribe(bad)
        try:
            with usage_ledger.scope("s"):
                usage_ledger.record("master", "m", 10)  # не должно бросить
        finally:
            usage_ledger.unsubscribe(bad)

    def test_client_reports_usage(self, monkeypatch):
        """OpenAIClient._report_usage извлекает total_tokens и отдаёт в ledger."""
        from libs.ai.client import OpenAIClient
        from libs.ai import usage_ledger
        got = []
        cb = lambda sid, role, model, tokens: got.append((sid, role, tokens))
        usage_ledger.subscribe(cb)
        try:
            c = OpenAIClient("m1", role="master")
            with usage_ledger.scope("sess-X"):
                c._report_usage({"usage": {"total_tokens": 123}}, "m1")
                c._report_usage({}, "m1")                    # без usage — тихо
                c._report_usage({"usage": {"total_tokens": "banana"}}, "m1")
        finally:
            usage_ledger.unsubscribe(cb)
        assert got == [("sess-X", "master", 123)]


class TestPluginRemovability:
    """Раздел 7: удаление плагина не должно ломать бота — get_plugin → None."""

    def test_get_billing_plugin_without_bot_data(self):
        from libs.handlers.utils import get_billing_plugin
        assert get_billing_plugin(None) is None

    def test_get_billing_plugin_empty_bot_data(self):
        from libs.handlers.utils import get_billing_plugin

        class Ctx:
            bot_data = {}
        assert get_billing_plugin(Ctx()) is None

    def test_get_billing_plugin_missing_plugin(self):
        """Менеджер есть, плагина billing нет (удалён) → None, не исключение."""
        from libs.handlers.utils import get_billing_plugin

        class Manager:
            def get_plugin(self, name):
                return None
        assert get_billing_plugin(None) is None  # ctx None
        class Ctx:
            bot_data = {"plugin_manager": Manager()}
        assert get_billing_plugin(Ctx()) is None

    def test_billing_plugin_registered(self):
        """Плагин описан корректно: имя/зависимости/версия (для plugins.toml)."""
        from plugins.billing.plugin import BillingPlugin
        p = BillingPlugin()
        assert p.name == "billing"
        assert "persistence" in p.depends_on
        assert "session-core" in p.depends_on

    def test_no_reverse_dependency_on_billing(self):
        """Раздел 7: lobby-session НЕ зависит от billing; ни один модуль в
        libs/ не импортирует plugins.billing напрямую (только get_plugin)."""
        # папка lobby-session с дефисом — читаем манифест текстом
        base = os.path.dirname(os.path.dirname(__file__))
        ls_src = open(os.path.join(base, "plugins", "lobby-session", "plugin.py"),
                      encoding="utf-8").read()
        assert '"billing"' not in ls_src.replace("'", '"') or "depends_on" not in ls_src or \
            "billing" not in ls_src.split("depends_on")[1].split("]")[0]

        libs_dir = os.path.join(base, "libs")
        for root, _dirs, files in os.walk(libs_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                assert "from plugins.billing" not in src, path
                assert "import plugins.billing" not in src, path
