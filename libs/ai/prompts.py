"""System prompts для LLM-агентов (на русском) — ТОНКИЙ ЗАГРУЗЧИК.

ИСТОЧНИК КОНТЕНТА: MD/prompts/*.md (см. ТЗ «консолидация MD + ready.md»).
Правки промптов делаются в .md-файлах; применение к новым сессиям — через
флаг MD/ready.md (см. libs/md_store.py). Этот модуль на импорте читает файлы,
подставляет плейсхолдеры {{EDITION_LABEL}} / {{EDITION_NOTE}} и выставляет
константы (MASTER_PROMPT и т.д.) для обратной совместимости.

ВО ВРЕМЯ ИГРЫ движок должен получать промпт через get_prompt(name, session_id):
она читает замороженную копию сессии data/sessions/<sid>/MD/prompts/*_copy.md
и только при её отсутствии падает обратно на глобальную константу (сессии,
созданные до внедрения MD; DM-режим; тесты).

Подстановка плейсхолдеров сделана через .replace(), НЕ через str.format():
в промптах десятки фигурных скобок из JSON-примеров — .format() на них падает.
"""

import logging
import os
from typing import Optional

from libs.config_legacy import PROMPTS_DIR
from libs.srd.edition_diff import EditionDiff

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# XP-СИСТЕМА (ИТЕРАЦИЯ 15): плейсхолдер {{XP_SYSTEM}} — единый источник правды
# в libs/xp_system.py. Числа в промпте и в коде (award_xp/уровневание) всегда
# совпадают, т.к. блок генерируется из тех же таблиц. МД-философия сохраняется:
# секция в master.md/db_bot.md редактируема, а таблицы подставляются рантаймом.
# ═══════════════════════════════════════════════════════════════
try:
    from libs.xp_system import build_prompt_block as _xp_block
    XP_PROMPT_BLOCK = _xp_block()
except Exception as _e:  # pragma: no cover — защита от частичной установки
    XP_PROMPT_BLOCK = ""
    logger.warning(f"[prompts] xp_system unavailable: {_e}")

# ═══════════════════════════════════════════════════════════════
# АВТОПОВЫШЕНИЕ УРОВНЯ (ИТЕРАЦИЯ 16): плейсхолдер {{LEVEL_UP_SYSTEM}} —
# единый источник правды в libs/level_up.py (формат строк «УРОВЕНЬ+: ...»
# и правило «ровно по брифу»). Таблицы классов живут в коде — промпту они
# не нужны: бриф повышения всегда несёт точные числа с собой.
# ═══════════════════════════════════════════════════════════════
try:
    from libs.level_up import build_prompt_block as _level_up_block
    LEVEL_UP_PROMPT_BLOCK = _level_up_block()
except Exception as _e:  # pragma: no cover
    LEVEL_UP_PROMPT_BLOCK = ""
    logger.warning(f"[prompts] level_up unavailable: {_e}")

# ═══════════════════════════════════════════════════════════════
# EDITION RESOLVER
# ═══════════════════════════════════════════════════════════════
_edition = EditionDiff()
EDITION_LABEL = _edition.edition_label
EDITION_NOTE = _edition.get_prompt_edition_note()

# Логическое имя → имя файла в MD/prompts/ (и в per-session копии с суффиксом _copy)
PROMPT_FILES = {
    "master": "master.md",
    "ask": "ask.md",
    "moder_ai": "moder_ai.md",
    "moder_ai_dispatch": "moder_ai_dispatch.md",
    "renderer": "renderer.md",
    "npc_ai": "npc_ai.md",
    "db_bot": "db_bot.md",
    "consolidator": "consolidator.md",
}


def _substitute(text: str) -> str:
    """Плейсхолдеры → реальные значения. Порядок не важен: значения не вложены."""
    text = (text
            .replace("{{EDITION_NOTE}}", EDITION_NOTE)
            .replace("{{EDITION_LABEL}}", EDITION_LABEL))
    if "{{XP_SYSTEM}}" in text:
        text = text.replace("{{XP_SYSTEM}}", XP_PROMPT_BLOCK)
    if "{{LEVEL_UP_SYSTEM}}" in text:
        text = text.replace("{{LEVEL_UP_SYSTEM}}", LEVEL_UP_PROMPT_BLOCK)
    return text


def _load_file(path: str, name: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _substitute(f.read())
    except FileNotFoundError:
        raise RuntimeError(
            f"[prompts] Не найден файл промпта '{name}': {path}. "
            f"Промпты хранятся в MD/prompts/ — восстановите файл из бэкапа "
            f"или запустите scripts/init_md.py для первичной инициализации."
        )


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPTS (глобальные константы из MD/prompts/*.md)
# ═══════════════════════════════════════════════════════════════
_CONSTANTS = {name: _load_file(os.path.join(PROMPTS_DIR, fname), name)
              for name, fname in PROMPT_FILES.items()}

MASTER_PROMPT = _CONSTANTS["master"]
ASK_PROMPT = _CONSTANTS["ask"]
MODER_AI_PROMPT = _CONSTANTS["moder_ai"]
MODER_AI_DISPATCH_PROMPT = _CONSTANTS["moder_ai_dispatch"]
RENDERER_PROMPT = _CONSTANTS["renderer"]
NPC_AI_PROMPT = _CONSTANTS["npc_ai"]
DB_BOT_PROMPT = _CONSTANTS["db_bot"]
CONSOLIDATOR_PROMPT = _CONSTANTS["consolidator"]


def get_prompt(name: str, session_id: Optional[str] = None) -> str:
    """Системный промпт с учётом per-session копии (Фаза 2).

    Порядок разрешения:
      1. session_id задан И существует
         data/sessions/<sid>/MD/prompts/<stem>_copy.md — читаем ЕЁ (замороженную
         на старте сессии версию; движок во время игры не читает MD/ источники).
      2. Иначе — глобальная константа (сессии до внедрения MD, DM-режим, тесты,
         или снапшот не удался при создании сессии — тогда честный фолбэк).
    """
    base = _CONSTANTS.get(name)
    if base is None:
        raise KeyError(f"Неизвестный промпт: {name}")
    if not session_id:
        return base
    try:
        from libs.md_store import get_session_copy_path
        path = get_session_copy_path(session_id, "prompts", PROMPT_FILES[name])
        if path:
            return _load_file(path, name)
    except Exception as e:
        logger.debug(f"[prompts] fallback to global '{name}' for session {session_id}: {e}")
    return base
