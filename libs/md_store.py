"""MD-хранилище — три слоя контента (см. ТЗ «консолидация MD + ready.md»).

  MD/{prompts,characters,settings}/        — ИСТОЧНИКИ. Правит человек, когда угодно.
  MD/ready.md                             — флаг ручного коммита ("False"/"True").
  MD/_applied/{prompts,characters,settings}/ — ВНУТРЕННИЙ кэш последнего
        подтверждённого (ready=True) состояния. Служебный: пользователь с ним
        напрямую не работает, он правит только источники.
  data/sessions/<sid>/MD/.../*_copy.md    — замороженная копия сессии. ЕДИНСТВЕННОЕ,
        что движок читает во время игры. Снимается ОДИН РАЗ при создании сессии
        (create_session) из MD/_applied/ и больше не меняется всю жизнь сессии.

Поток данных:
  MD/prompts/master.md ──(ready.md: False→True, строго после полного завершения
                          раунда оркестра — хук в engine._run_db_bot_background)──▶
  MD/_applied/prompts/master.md ──(create_session)──▶
  data/sessions/<sid>/MD/prompts/master_copy.md ──(читает движок этой сессии).

ГАРАНТИЯ СТАБИЛЬНОСТИ СЕССИИ: уже идущие сессии донашивают свою копию до конца,
даже если ready.md стал True посреди их игры. Это ОЖИДАЕМОЕ поведение, а не баг —
не «чинить». Обновить копию существующей сессии нельзя никак, только создать новую.

Атомарность: каждый файл копируется через временный файл РЯДОМ с целью +
os.replace — искусственное падение процесса посреди apply не оставляет ни один
файл в _applied/ битым или усечённым. Частично обновлённый НАБОР между падениями
допустим: следующий цикл с ready.md=True дольёт remaining файлы.
"""

import logging
import os
import shutil
from typing import Optional

from libs.config_legacy import (
    BASE_DIR,
    CHARACTERS_DIR,
    MD_DIR,
    PROMPTS_DIR,
    SETTINGS_DIR,
)

logger = logging.getLogger(__name__)

# Флаг-коммит
READY_FILE = os.path.join(MD_DIR, "ready.md")

# Внутренний кэш подтверждённого состояния
APPLIED_DIR = os.path.join(MD_DIR, "_applied")

# База per-session хранилищ (рядом с <sid>.db — см. libs/db/manager.py)
SESSIONS_BASE = os.path.join(BASE_DIR, "data", "sessions")

# Категория → каталог источников
SOURCE_DIRS = {
    "prompts": PROMPTS_DIR,
    "characters": CHARACTERS_DIR,
    "settings": SETTINGS_DIR,
}

# Служебные файлы, которые НЕ являются контентом: не переносятся apply-коммитом
# (registry.json — по ТЗ; ready.md лежит на уровне MD/ и в обзор не попадает).
SKIP_FILES = {"registry.json"}

# Расширения контента. .txt — потому что листы персонажей игроки грузят и в .txt.
CONTENT_EXTENSIONS = {".md", ".txt"}


# ═══════════════════════════════════════════════════════════════
# Layout / инициализация
# ═══════════════════════════════════════════════════════════════

def ensure_md_layout() -> None:
    """Создать структуру MD/ и проинициализировать _applied первичной копией
    источников, если кэша ещё нет (первый деплой). Идемпотентна.
    Вызывается при старте бота (handlers._state.init_state) и из scripts/init_md.py.
    """
    os.makedirs(MD_DIR, exist_ok=True)
    for d in SOURCE_DIRS.values():
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(SETTINGS_DIR, "custom"), exist_ok=True)
    os.makedirs(APPLIED_DIR, exist_ok=True)
    if not os.path.exists(READY_FILE):
        write_ready(False)
    # Первый деплой: _applied пуст → снимаем первичный слепок источников,
    # чтобы самой первой сессии было что читать.
    applied_prompts = os.path.join(APPLIED_DIR, "prompts")
    if not os.path.isdir(applied_prompts):
        n = 0
        for category, src_root in SOURCE_DIRS.items():
            dst_root = os.path.join(APPLIED_DIR, category)
            os.makedirs(dst_root, exist_ok=True)
            for src_abs, rel in _iter_content_files(src_root):
                _atomic_copy(src_abs, os.path.join(dst_root, rel))
                n += 1
        logger.info(f"[md_store] MD/_applied/ инициализирован первичной копией: {n} файлов")


# ═══════════════════════════════════════════════════════════════
# ready.md — флаг ручного коммита
# ═══════════════════════════════════════════════════════════════

def read_ready() -> bool:
    """True, если в MD/ready.md записано True (регистронезависимо)."""
    try:
        with open(READY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().lower() == "true"
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(f"[md_store] read_ready failed: {e}")
        return False


def write_ready(flag: bool) -> None:
    """Атомарно записать флаг-коммит."""
    os.makedirs(MD_DIR, exist_ok=True)
    tmp = READY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("True" if flag else "False")
    os.replace(tmp, READY_FILE)


# ═══════════════════════════════════════════════════════════════
# Apply: источники → _applied (по флагу ready.md)
# ═══════════════════════════════════════════════════════════════

def _iter_content_files(root: str):
    """Рекурсивный обход каталога категории: yields (abs_path, rel_path)
    для каждого файла-контента. Служебные файлы (registry.json) пропускаются."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in sorted(filenames):
            if fname in SKIP_FILES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CONTENT_EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, root)
            yield abs_path, rel_path


def _atomic_copy(src: str, dst: str) -> None:
    """Копия через tmp-файл РЯДОМ с целью + os.replace — цель никогда не бывает
    частичной. tmp-мусор при падении процесса перезаписывается следующей попыткой."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = f"{dst}.tmp.{os.getpid()}"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _cleanup_tmp_files() -> None:
    """Подмести tmp-остатки прошлых падений в _applied."""
    for dirpath, _dirnames, filenames in os.walk(APPLIED_DIR):
        for fname in filenames:
            if ".tmp." in fname:
                try:
                    os.remove(os.path.join(dirpath, fname))
                except OSError:
                    pass


def apply_md_changes_if_ready() -> bool:
    """Главный коммит Фазы 2. Вызывается из хука после ПОЛНОГО завершения раунда
    оркестра (Мастер + фоновый DB-Bot) — engine._run_db_bot_background, сразу после
    set_db_busy(session_id, False).

    Семантика по ТЗ:
      - ready.md не True → ничего не делать, return False;
      - ready.md True → каждый source .md/.txt из MD/{prompts,characters,settings}
        (кроме ready.md и registry.json) атомарно переносится в MD/_applied/;
      - ВСЕ копирования прошли → ready.md сбрасывается в False, return True;
      - хоть одно упало → ready.md ОСТАЁТСЯ True, ошибки залогированы по файлам,
        уже обновлённые файлы НЕ откатываются (каждый атомарен сам по себе,
        следующий цикл дольёт remaining).
    """
    if not read_ready():
        return False

    _cleanup_tmp_files()
    errors = []
    copied = 0
    for category, src_root in SOURCE_DIRS.items():
        dst_root = os.path.join(APPLIED_DIR, category)
        for src_abs, rel in _iter_content_files(src_root):
            if not os.path.exists(src_abs):
                continue
            try:
                _atomic_copy(src_abs, os.path.join(dst_root, rel))
                copied += 1
            except OSError as e:
                errors.append(f"{category}/{rel}: {e}")

    if errors:
        for e in errors:
            logger.error(f"[md_store] apply failed: {e}")
        logger.warning(
            f"[md_store] ready.md ОСТАВЛЕН True: {len(errors)} ошибок из {copied + len(errors)} "
            f"файлов — следующий завершённый раунд повторит попытку.")
        return False

    write_ready(False)
    logger.info(f"[md_store] ready.md применён: {copied} файлов → MD/_applied/")
    return True


# ═══════════════════════════════════════════════════════════════
# Per-session копии: _applied → data/sessions/<sid>/MD/
# ═══════════════════════════════════════════════════════════════

def session_md_root(session_id: str) -> str:
    return os.path.join(SESSIONS_BASE, session_id, "MD")


def get_session_copy_path(session_id: str, category: str, filename: str) -> Optional[str]:
    """Путь к per-session копии файла контента (<stem>_copy<ext>) или None, если её нет.
    Единственная точка, где строится имя *_copy.md — движок вызывает её через
    prompts.get_prompt() / settings_cmds._load_setting_md(..., session_id)."""
    if not session_id:
        return None
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(session_md_root(session_id), category, f"{stem}_copy{ext}")
    return candidate if os.path.exists(candidate) else None


def snapshot_for_session(session_id: str) -> int:
    """Снять замороженный слепок MD/_applied/ → data/sessions/<sid>/MD/.
    Вызывается ОДИН РАЗ из create_session; дальше копию сессии никто не меняет
    (см. гарантию стабильности в докмодуле). Возвращает число файлов."""
    applied_any = False
    for category in SOURCE_DIRS:
        if os.path.isdir(os.path.join(APPLIED_DIR, category)):
            applied_any = True
            break
    if not applied_any:
        # Первый деплой до старта бота: ensure_md_layout() создаст кэш из источников.
        ensure_md_layout()

    n = 0
    for category, src_root in SOURCE_DIRS.items():
        applied_root = os.path.join(APPLIED_DIR, category)
        if not os.path.isdir(applied_root):
            continue
        for src_abs, rel in _iter_content_files(applied_root):
            stem, ext = os.path.splitext(rel)
            dst = os.path.join(session_md_root(session_id), category, f"{stem}_copy{ext}")
            _atomic_copy(src_abs, dst)
            n += 1
    logger.info(f"[md_store] снапшот для сессии {session_id}: {n} файлов "
                f"(заморожено на момент создания сессии)")
    return n


def cleanup_session_md(session_id: str) -> bool:
    """Удалить data/sessions/<sid>/MD/ (вызывается при завершении/удалении сессии),
    чтобы копии не накапливались бесконечно. Возвращает True, если удалила."""
    root = session_md_root(session_id)
    if not os.path.isdir(root):
        return False
    try:
        shutil.rmtree(root, ignore_errors=True)
        # Если вместе с MD/ опустела вся папка сессии (drop_session) — убрать и её.
        parent = os.path.dirname(root)
        try:
            os.rmdir(parent)
        except OSError:
            pass  # там ещё живёт <sid>.db — норма для end_session
        logger.info(f"[md_store] per-session MD удалён: {session_id}")
        return True
    except Exception as e:
        logger.warning(f"[md_store] cleanup_session_md({session_id}) failed: {e}")
        return False
