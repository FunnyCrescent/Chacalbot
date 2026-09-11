"""usage_ledger — учёт реально потреблённых LLM-токенов (ИТЕРАЦИЯ 10, Раздел 6).

Зачем: OpenAIClient теперь читает response["usage"]["total_tokens"] после
каждого вызова (см. libs/ai/client.py) и сообщает сюда. Раунд состоит из
НЕСКОЛЬКИХ ролей (Мастер, DB-Bot, Renderer, NPC-AI, Moder-AI, Memory,
переводчик) — реальная стоимость раунда это СУММА usage со всех вызовов.

Как usage привязывается к сессии: клиент не знает session_id, поэтому
используется contextvar — оркестраторы раунда в libs/handlers/engine.py
входят в `with usage_ledger.scope(session_id):` на входе; все задачи,
созданные через asyncio.create_task ВНУТРИ этого контекста (боевой цикл,
DB-Bot background, NPC-резолвы), наследуют контекст автоматически.

Подписчики: плагин billing (plugins/billing/) подписывается в setup() и
накапливает pending-токены по session_id; в точке «раунд полностью
завершён» (хук после set_db_busy(False) в _run_db_bot_background) плагин
списывает накопленное с баланса игроков по session.billing_mode.

Если плагина billing нет — рекорд просто никому не доставляется, memory
не течёт (dict в плагине, не здесь).
"""
import logging
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_current_session: ContextVar[Optional[str]] = ContextVar(
    "usage_ledger_session", default=None)

# Подписчики: callable(session_id: str, role: str, model: str, tokens: int)
_subscribers: List[Callable] = []


def subscribe(callback: Callable) -> None:
    """Подписаться на события расхода токенов (идемпотентно)."""
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe(callback: Callable) -> None:
    if callback in _subscribers:
        _subscribers.remove(callback)


def bind_session(session_id: str):
    """Привязать текущий async-контекст к сессии. Возвращает token для unbind()."""
    return _current_session.set(session_id)


def unbind(token) -> None:
    try:
        _current_session.reset(token)
    except Exception:
        pass


@contextmanager
def scope(session_id: str):
    """Context manager: with usage_ledger.scope(session_id): ... — все LLM-вызовы
    внутри (включая create_task-потомков) атрибутируются этой сессии."""
    token = bind_session(session_id)
    try:
        yield
    finally:
        unbind(token)


def current_session() -> Optional[str]:
    return _current_session.get()


def record(role: str, model: str, tokens: int) -> None:
    """Вызывается из OpenAIClient после каждого успешного вызова.
    Никогда не бросает исключений — биллинг не должен ломать игру."""
    try:
        tokens = int(tokens or 0)
    except (TypeError, ValueError):
        return
    if tokens <= 0:
        return
    session_id = _current_session.get()
    if not session_id:
        return  # вызов вне сессии (/creu, аудит, тесты) — биллингу не принадлежит
    for cb in tuple(_subscribers):
        try:
            cb(session_id, role, model, tokens)
        except Exception as e:
            logger.warning(f"[usage_ledger] subscriber failed: {e}")
