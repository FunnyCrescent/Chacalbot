"""Тесты MD-консолидации (ТЗ: Фаза 1 перенос + Фаза 2 ready.md/per-session копии).

Проверяются инварианты ТЗ:
  - ready.md=False → apply ничего не делает;
  - ready.md=True → источники атомарно уезжают в _applied, флаг сбрасывается;
  - падение одного файла НЕ сбрасывает флаг и НЕ портит уже скопированные;
  - registry.json исключён из apply-коммита;
  - снапшот сессии снимается ОДИН РАЗ из _applied и живёт своей жизнью;
  - уже идущая сессия не видит последующих коммитов (осознанное поведение);
  - cleanup удаляет копию сессии (end/drop), get_prompt читает копию сессии.
"""
import os
from types import SimpleNamespace

import pytest

import libs.md_store as mds
from libs.md_store import (
    apply_md_changes_if_ready,
    cleanup_session_md,
    ensure_md_layout,
    get_session_copy_path,
    read_ready,
    snapshot_for_session,
    write_ready,
)
from libs.session.session_lifecycle import SessionLifecycleMixin


# ═══════════════════════════════════════════════════════════════
# Фикстура: изолированное MD-хранилище в tmp_path
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def md_env(tmp_path, monkeypatch):
    md_root = tmp_path / "MD"
    src = {
        "prompts": md_root / "prompts",
        "characters": md_root / "characters",
        "settings": md_root / "settings",
    }
    for d in src.values():
        d.mkdir(parents=True)
    (src["prompts"] / "master.md").write_text("мастер v1", encoding="utf-8")
    (src["prompts"] / "db_bot.md").write_text("дб v1", encoding="utf-8")
    (src["characters"] / "hero.md").write_text("лист героя", encoding="utf-8")
    (src["characters"] / "imported.txt").write_text("лист в txt", encoding="utf-8")
    (src["settings"] / "medieval_fantasy.md").write_text("сеттинг v1", encoding="utf-8")
    (src["settings"] / "registry.json").write_text('{"categories": {}}', encoding="utf-8")

    sessions_base = tmp_path / "data" / "sessions"

    monkeypatch.setattr(mds, "MD_DIR", str(md_root))
    monkeypatch.setattr(mds, "APPLIED_DIR", str(md_root / "_applied"))
    monkeypatch.setattr(mds, "READY_FILE", str(md_root / "ready.md"))
    monkeypatch.setattr(mds, "SESSIONS_BASE", str(sessions_base))
    monkeypatch.setattr(mds, "SOURCE_DIRS", {k: str(v) for k, v in src.items()})
    monkeypatch.setattr(mds, "PROMPTS_DIR", str(src["prompts"]))
    monkeypatch.setattr(mds, "CHARACTERS_DIR", str(src["characters"]))
    monkeypatch.setattr(mds, "SETTINGS_DIR", str(src["settings"]))

    return SimpleNamespace(
        md_root=md_root, src=src, applied=md_root / "_applied",
        sessions_base=sessions_base,
    )


def _session_md(sid: str) -> str:
    return mds.session_md_root(sid)


# ═══════════════════════════════════════════════════════════════
# Layout / ready-флаг
# ═══════════════════════════════════════════════════════════════

def test_ensure_md_layout_creates_structure_and_ready(md_env):
    ensure_md_layout()
    assert (md_env.md_root / "ready.md").read_text(encoding="utf-8") == "False"
    assert (md_env.applied / "prompts" / "master.md").exists()
    assert (md_env.applied / "settings" / "medieval_fantasy.md").exists()
    # registry.json — служебный, в _applied не переносится
    assert not (md_env.applied / "settings" / "registry.json").exists()


def test_ensure_md_layout_is_idempotent_and_does_not_reset_applied(md_env):
    ensure_md_layout()
    # Админ отредактировал источник, но ready ещё не коммитил —
    # повторный ensure НЕ должен перезатереть подтверждённый кэш источником.
    (md_env.src["prompts"] / "master.md").write_text("мастер v2 (не закоммичено)", encoding="utf-8")
    ensure_md_layout()
    assert (md_env.applied / "prompts" / "master.md").read_text(encoding="utf-8") == "мастер v1"


def test_read_ready_missing_file_and_case_insensitivity(md_env):
    assert read_ready() is False  # файла нет → False
    (md_env.md_root / "ready.md").write_text("True", encoding="utf-8")
    assert read_ready() is True
    (md_env.md_root / "ready.md").write_text("  true\n", encoding="utf-8")
    assert read_ready() is True
    (md_env.md_root / "ready.md").write_text("False", encoding="utf-8")
    assert read_ready() is False


def test_write_ready_is_atomic_and_flips(md_env):
    ensure_md_layout()
    write_ready(True)
    assert read_ready() is True
    assert not list(md_env.md_root.glob("ready.md.tmp*"))


# ═══════════════════════════════════════════════════════════════
# apply_md_changes_if_ready
# ═══════════════════════════════════════════════════════════════

def test_apply_noop_when_not_ready(md_env):
    ensure_md_layout()
    (md_env.src["prompts"] / "master.md").write_text("мастер v2", encoding="utf-8")
    assert apply_md_changes_if_ready() is False
    assert (md_env.applied / "prompts" / "master.md").read_text(encoding="utf-8") == "мастер v1"


def test_apply_copies_all_sources_and_resets_flag(md_env):
    ensure_md_layout()
    (md_env.src["prompts"] / "master.md").write_text("мастер v2", encoding="utf-8")
    (md_env.src["settings"] / "medieval_fantasy.md").write_text("сеттинг v2", encoding="utf-8")
    write_ready(True)

    assert apply_md_changes_if_ready() is True
    assert read_ready() is False
    assert (md_env.applied / "prompts" / "master.md").read_text(encoding="utf-8") == "мастер v2"
    assert (md_env.applied / "settings" / "medieval_fantasy.md").read_text(encoding="utf-8") == "сеттинг v2"
    # .txt-листы тоже контент (игроки грузят и txt)
    assert (md_env.applied / "characters" / "imported.txt").read_text(encoding="utf-8") == "лист в txt"
    # tmp-мусора нет
    leftovers = [p for p in md_env.applied.rglob("*.tmp*")]
    assert leftovers == []


def test_apply_failure_keeps_ready_true_and_good_files_intact(md_env):
    ensure_md_layout()
    (md_env.src["prompts"] / "broken.md").write_text("сломанный", encoding="utf-8")
    (md_env.src["prompts"] / "master.md").write_text("мастер v2", encoding="utf-8")
    write_ready(True)
    # ломаем ОДИН исходник — чтение упадёт уже после первых успешных копий
    broken = md_env.src["prompts"] / "broken.md"
    os.chmod(broken, 0o000)
    try:
        result = apply_md_changes_if_ready()
    finally:
        os.chmod(broken, 0o644)

    assert result is False
    assert read_ready() is True  # флаг НЕ сброшен — следующий цикл дольёт remaining
    # часть файлов могла уже уехать; при этом ни один не битый
    allowed = {"мастер v1", "мастер v2", "дб v1", "сломанный",
               "лист героя", "лист в txt", "сеттинг v1"}
    for f in list(md_env.applied.rglob("*.md")) + list(md_env.applied.rglob("*.txt")):
        assert f.read_text(encoding="utf-8") in allowed

    # Повторная попытка (ready всё ещё True) — теперь всё проходит
    assert apply_md_changes_if_ready() is True
    assert read_ready() is False
    assert (md_env.applied / "prompts" / "broken.md").read_text(encoding="utf-8") == "сломанный"


def test_apply_cleans_stale_tmp_files(md_env):
    ensure_md_layout()
    stale = md_env.applied / "prompts" / "master.md.tmp.999"
    stale.write_text("мусор от прошлого падения", encoding="utf-8")
    write_ready(True)
    apply_md_changes_if_ready()
    assert not stale.exists()


# ═══════════════════════════════════════════════════════════════
# Per-session снапшот и изоляция
# ═══════════════════════════════════════════════════════════════

def test_snapshot_creates_copy_files(md_env):
    ensure_md_layout()
    sid = "abc12345"
    n = snapshot_for_session(sid)
    assert n >= 4
    p = get_session_copy_path(sid, "prompts", "master.md")
    assert p is not None and os.path.basename(p) == "master_copy.md"
    assert "сеттинг v1" == open(get_session_copy_path(sid, "settings", "medieval_fantasy.md"), encoding="utf-8").read()
    # registry.json в сессию не попадает
    assert get_session_copy_path(sid, "settings", "registry.json") is None


def test_session_copy_is_frozen_against_later_commits(md_env):
    """КЛЮЧЕВОЙ инвариант ТЗ: ready-коммит не трогает копии уже созданных сессий;
    новые сессии получают обновлённый слепок."""
    ensure_md_layout()
    old_sid, new_sid = "old0001", "new0002"
    snapshot_for_session(old_sid)

    # коммит новой версии промпта
    (md_env.src["prompts"] / "master.md").write_text("мастер v2", encoding="utf-8")
    write_ready(True)
    assert apply_md_changes_if_ready() is True

    snapshot_for_session(new_sid)

    old_copy = open(get_session_copy_path(old_sid, "prompts", "master.md"), encoding="utf-8").read()
    new_copy = open(get_session_copy_path(new_sid, "prompts", "master.md"), encoding="utf-8").read()
    assert old_copy == "мастер v1", "уже идущая сессия не должна получить коммит"
    assert new_copy == "мастер v2", "новая сессия получает подтверждённое состояние"


def test_snapshot_without_prior_ensure_self_bootstraps(md_env):
    # Первый деплой: _applied ещё нет, сессию создают — снапшот сам инициализирует кэш
    sid = "boot0001"
    n = snapshot_for_session(sid)
    assert n > 0
    assert get_session_copy_path(sid, "prompts", "db_bot.md") is not None


def test_cleanup_session_md(md_env):
    ensure_md_layout()
    sid = "dead0001"
    snapshot_for_session(sid)
    assert os.path.isdir(_session_md(sid))
    assert cleanup_session_md(sid) is True
    assert not os.path.isdir(_session_md(sid))
    assert cleanup_session_md(sid) is False  # идемпотентно


def test_get_session_copy_path_missing_session(md_env):
    assert get_session_copy_path("", "prompts", "master.md") is None
    assert get_session_copy_path("nosuch", "prompts", "master.md") is None


# ═══════════════════════════════════════════════════════════════
# get_prompt: per-session промпты
# ═══════════════════════════════════════════════════════════════

def test_get_prompt_uses_session_copy(md_env):
    ensure_md_layout()
    sid = "prmt0001"
    snapshot_for_session(sid)
    # правим копию сессии вручную (как будто сессия заморожена со старым текстом)
    copy = md_env.sessions_base / sid / "MD" / "prompts" / "master_copy.md"
    copy.write_text("СТАРЫЙ ПРОМПТ СЕССИИ {{EDITION_LABEL}}", encoding="utf-8")

    from libs.ai.prompts import get_prompt, EDITION_LABEL

    # копия сессии имеет приоритет; плейсхолдеры подставляются и в копии
    assert get_prompt("master", sid) == f"СТАРЫЙ ПРОМПТ СЕССИИ {EDITION_LABEL}"
    # без session_id — глобальная константа из MD/prompts/master.md
    assert get_prompt("master") != f"СТАРЫЙ ПРОМПТ СЕССИИ {EDITION_LABEL}"
    # неизвестное имя
    with pytest.raises(KeyError):
        get_prompt("nosuch")


def test_get_prompt_falls_back_to_global_when_no_copy(md_env):
    ensure_md_layout()
    from libs.ai.prompts import get_prompt
    # сессия без снапшота (создана до внедрения MD) → глобальный промпт
    assert get_prompt("db_bot", "legacy01") == get_prompt("db_bot")


# ═══════════════════════════════════════════════════════════════
# Хуки жизненного цикла сессии
# ═══════════════════════════════════════════════════════════════

class _StubDBManager:
    def __init__(self):
        self.created, self.ended = [], []

    def create_session(self, session):
        self.created.append(session.id)

    def get_db(self, session_id):
        return SimpleNamespace(add_player=lambda player: None)

    def end_session(self, session_id):
        self.ended.append(session_id)


class _LifecycleHost(SessionLifecycleMixin):
    def __init__(self, db_manager):
        self.db_manager = db_manager


def test_create_session_takes_snapshot_once(md_env):
    ensure_md_layout()
    host = _LifecycleHost(_StubDBManager())
    session = host.create_session(chat_id=-100, name="test", creator_id=1, creator_name="creator")
    assert session.id in host.db_manager.created
    copy = get_session_copy_path(session.id, "prompts", "master.md")
    assert copy is not None


def test_end_session_cleans_md_copy(md_env):
    ensure_md_layout()
    host = _LifecycleHost(_StubDBManager())
    session = host.create_session(chat_id=-100, name="test", creator_id=1, creator_name="creator")
    assert os.path.isdir(_session_md(session.id))
    host.end_session(session.id)
    assert not os.path.isdir(_session_md(session.id))


def test_snapshot_tolerates_source_dir_absence(md_env):
    """Сломанное окружение (кто-то удалил MD/characters) не роняет создание сессии:
    снапшот читает из _applied, а не из источников."""
    ensure_md_layout()
    import shutil
    shutil.rmtree(md_env.src["characters"])
    sid = "tolr0001"
    n = snapshot_for_session(sid)  # не должен упасть
    assert n > 0
    assert get_session_copy_path(sid, "prompts", "master.md") is not None
    # hero уехал в копию из _applied — источник не нужен
    assert get_session_copy_path(sid, "characters", "hero.md") is not None
