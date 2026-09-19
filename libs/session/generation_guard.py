"""GenerationGuard — реестр живых генеративных задач сессии + гейт «сессия активна?».

БАГ (ИТЕРАЦИЯ 15): после завершения сессии (/end, выход создателя, /dileu) сессия
помечалась ended в БД, но уже запущенные in-flight задачи продолжали жить:
резолв раунда, боевой цикл, ход NPC, DB-Bot background, авто-скип. Игрок
получал бросок кубиков и нарратив за ход, сделанный ДО завершения сессии,
а «✅ Мир обновлён» прилетал уже в мёртвый чат.

Механика (два независимых уровня защиты):
1. РЕЕСТР + ОТМЕНА. Каждая генеративная задача при старте регистрирует
   текущий asyncio.Task (декоратор @generation_task или register/unregister
   вручную для замыканий вроде _auto_skip_turn). end_session() /
   clear_session_runtime_state() вызывает cancel_session_generations(): все
   зарегистрированные задачи получают task.cancel() — CancelledError убивает
   их на ближайшем await (вызов LLM, sleep, отправка), поэтому ни бросков,
   ни нарратива после /end не уходит. CancelledError — BaseException, поэтому
   существующие `except Exception`-обработчики его не глотают и fallback-ов
   («⚠️ Ошибка обработки хода») в завершённую сессию тоже не будет.
2. ГЕЙТ ОТПРАВКИ. Перед отправкой нарратива/уведомлений вызывается
   session_is_active(): проверка флага отмены + живого статуса сессии в БД
   (status == "active"). Задачи, не попавшие в реестр (или запущенные между
   «/end обработан» и «задача создалась»), всё равно не отправят ничего
   в завершённую сессию.

Рефсчётчик: одна и та же задача может гнездить другую декорированную функцию
(_run_db_bot_background → await _resolve_pc_combat_turn) — задача снимается с
учёта только когда выйдут ВСЕ вложенные рамки.

clear_cancel() вызывается в resume_session: сессию возобновили — генерации
снова разрешены (id сессии при resume не меняется, а флаг отмены глобальный).
"""
import asyncio
import functools
import logging
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# session_id -> {task: refcount}
_active_tasks: Dict[str, Dict[asyncio.Task, int]] = {}
# session_id -> отменена (сессия завершена/удалена, генерации запрещены)
_cancelled: Set[str] = set()


class SessionCancelledError(RuntimeError):
    """Поднялась, когда операция пытается продолжить генерацию завершённой сессии."""


def register(session_id: str, task: Optional[asyncio.Task] = None) -> None:
    """Зарегистрировать задачу как генеративную для сессии (рефсчёт)."""
    if not session_id:
        return
    if task is None:
        task = asyncio.current_task()
    if task is None:
        return
    _active_tasks.setdefault(session_id, {})[task] = \
        _active_tasks.get(session_id, {}).get(task, 0) + 1


def unregister(session_id: str, task: Optional[asyncio.Task] = None) -> None:
    """Снять задачу с учёта (один реф)."""
    if not session_id:
        return
    if task is None:
        task = asyncio.current_task()
    if task is None:
        return
    bucket = _active_tasks.get(session_id)
    if not bucket:
        return
    count = bucket.get(task, 0) - 1
    if count <= 0:
        bucket.pop(task, None)
    else:
        bucket[task] = count
    if not bucket:
        _active_tasks.pop(session_id, None)


def active_count(session_id: str) -> int:
    """Сколько живых генеративных задач сейчас у сессии (для тестов/логов)."""
    return sum(_active_tasks.get(session_id, {}).values())


def cancel_session_generations(session_id: str) -> int:
    """Отменить ВСЕ живые генеративные задачи сессии и запретить новые отправки.

    Вызывается из end_session() / clear_session_runtime_state(). Возвращает
    число отменённых задач (для лога). Идемпотентно: повторный вызов не падает
    и просто подтверждает запрет (новых задач в реестре уже не будет — они
    успевают только зарегистрироваться перед первым await).
    """
    _cancelled.add(session_id)
    bucket = _active_tasks.pop(session_id, {})
    cancelled = 0
    for task, _refs in list(bucket.items()):
        if task is asyncio.current_task():
            # end_session теоретически может вызваться из генеративной задачи —
            # себя не отменяем, отменится сама через unregister/выход.
            continue
        if not task.done():
            task.cancel()
            cancelled += 1
    if cancelled:
        logger.info(
            f"[generation-guard] {session_id}: отменено {cancelled} активных "
            f"генеративных задач (сессия завершается)")
    return cancelled


def is_cancelled(session_id: str) -> bool:
    """True, если сессия завершена и генерации для неё запрещены флагом."""
    return session_id in _cancelled


def clear_cancel(session_id: str) -> None:
    """Снять запрет (resume_session: сессия снова активна)."""
    _cancelled.discard(session_id)


def session_is_active(session_id: str, db_manager) -> bool:
    """Гейт перед отправками: сессия существует, активна и не отменена.

    db_manager — DatabaseManager (или любой объект с .get_db(sid).get_session(sid)).
    Любая ошибка БД трактуется как «не активна» — молча не отправляем.
    """
    if not session_id or session_id in _cancelled:
        return False
    try:
        session = db_manager.get_db(session_id).get_session(session_id)
    except Exception:
        return False
    return bool(session) and getattr(session, "status", "") == "active"


def generation_task(func):
    """Декоратор для асинхронных генеративных функций.

    Требование: session_id — первый позиционный аргумент (или kwarg session_id).
    Регистрирует текущий asyncio.Task на время выполнения, снимает в finally
    (рефсчёт — вложенные декорированные вызовы одной задачи безопасны).
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        session_id = args[0] if args else kwargs.get("session_id")
        if not isinstance(session_id, str):
            session_id = str(session_id) if session_id is not None else ""
        register(session_id)
        try:
            return await func(*args, **kwargs)
        finally:
            unregister(session_id)

    return wrapper
