"""BaseSessionMixin — constructor + concurrency primitives (locks, db_busy, pending_resolve)."""
import json
import logging
import uuid
import asyncio
import random
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from libs.ai_client import DMEngine, strip_stray_tags
from libs.db import (
    Database, DatabaseManager, HistoryEntry, Player, QueueState, Session,
    Location, LocationPath, WorldNpc, NpcRelation, LoreArticle,
    MarketPrice, EconomicEvent, ActiveEffect, Timer, LocationRelation,
    CombatEncounter, Combatant, PlayerLanguage,
)
from libs.config_legacy import (
    COMBAT_INITIATIVE_ENABLED, DUAL_NARRATIVE_ENABLED,
    NPC_AI_ENABLED, NPC_AI_MODE,
)
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class BaseSessionMixin:
    """BaseSessionMixin — constructor + concurrency primitives (locks, db_busy, pending_resolve)."""

    def __init__(self, db_manager: DatabaseManager, dm_engine: DMEngine):
        self.db_manager = db_manager
        self.dm = dm_engine
        self.memory_store = MemoryStore(db_manager, dm_engine)
        # A4: /roll results, waiting to be attached to this player's next Дн. this round.
        # Same in-memory-only caveat as the button-wait futures in bot.py — does not
        # survive a bot restart mid-round, which is an accepted, disclosed limitation.
        self._pending_manual_rolls: Dict[str, Dict[int, List[str]]] = {}  # session_id -> player_id -> [display strings]
        self._pending_manual_roll_skills: Dict[str, Dict[int, List[str]]] = {}  # session_id -> player_id -> [check_name (lowered)]

        # Round-completion race fix: this is a single-process asyncio app, so an
        # asyncio.Lock per session is enough to fully serialize "detect the round just
        # became complete, then decide to resolve it" across handle_message / /skip /
        # /kick — closing the window where, e.g., a DM /skip on the last pending player
        # and that same player's own Дн. arriving at nearly the same instant could both
        # independently see an empty queue and both trigger a duplicate Master call.
        self._round_locks: Dict[str, asyncio.Lock] = {}

        # Async Master/DB-Bot split: once the Master's narrative is sent, the DB-Bot
        # pass runs in the background. While db_busy[session_id] is True, read commands
        # that could see half-applied state (HP, inventory, quests, NPCs...) should warn
        # instead of answering, and a round that finishes collecting during this window
        # is deferred (queued in _pending_resolve) instead of calling the Master with
        # stale pre-update character state. In-memory only — does not survive a restart.
        self._db_busy: Dict[str, bool] = {}
        self._pending_resolve: Dict[str, dict] = {}  # session_id -> {"chat_id": int, "ctx": PTB context}

        # ── DUAL NARRATIVE: separate queue for non-combat players during combat ──
        # When combat is active, non-combat players have their own action queue
        # and are resolved by a SEPARATE Master call — so they don't wait for
        # the combat turn loop. Key: session_id -> {player_id: action_text}
        self._non_combat_queue: Dict[str, Dict[int, str]] = {}
        # Lock for non-combat resolution to prevent race conditions
        self._non_combat_locks: Dict[str, asyncio.Lock] = {}

        # ── БОЙ-FIX: session-wide resolver mutex ──
        # Раньше боевой цикл (NPC/PC ходы), dual-narrative (non-combat очередь)
        # и обычный раунд могли вызывать Мастера ПАРАЛЛЕЛЬНО — каждый со своим
        # контекстом, своими бросками костей и своим мнением об HP и состояниях.
        # Результат в реальном логе: два разных GM_SECRET+нарратива на один и
        # тот же раунд, «воскресшие» стражники, прыгающие номера раундов
        # (1→3→5→7→8→6→2→2→4). Теперь ЛЮБОЙ вызов Мастера для сессии обязан
        # держать этот лок: в каждый момент времени у сессии существует
        # максимум ОДИН активный нарратив. См. ResolutionMixin.
        self._resolver_locks: Dict[str, asyncio.Lock] = {}

        # ── БОЙ-FIX v2: вступления в бой (ymuno_ymladd) ──
        # Когда Мастер добавляет игрока в идущий бой, факт вступления
        # запоминается здесь, а движок анонсирует его после отправки
        # нарратива («⚔️ Рими вступает в бой! Инициатива: N»). In-memory —
        # теряется при рестарте, что допустимо: анонс чисто информационный.
        self._pending_combat_joins: Dict[str, List[dict]] = {}


    def get_round_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._round_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._round_locks[session_id] = lock
        return lock


    def is_db_busy(self, session_id: str) -> bool:
        return self._db_busy.get(session_id, False)


    def set_db_busy(self, session_id: str, busy: bool):
        self._db_busy[session_id] = busy


    def mark_pending_resolve(self, session_id: str, chat_id: int, ctx) -> None:
        """Record that a round is fully collected but must wait for the previous
        round's background DB-Bot pass to finish before the Master can be called."""
        self._pending_resolve[session_id] = {"chat_id": chat_id, "ctx": ctx}


    def pop_pending_resolve(self, session_id: str) -> Optional[dict]:
        return self._pending_resolve.pop(session_id, None)

    # ═══════════════════════════════════════════════════════════
    # Session Lifecycle
    # ═══════════════════════════════════════════════════════════


    def get_non_combat_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._non_combat_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._non_combat_locks[session_id] = lock
        return lock


    def get_resolver_lock(self, session_id: str) -> asyncio.Lock:
        """Session-wide mutex for EVERY Master round resolution.

        БОЙ-FIX: гарантирует «один нарратив за раз» для сессии. Держится
        вокруг: обычного раунда (resolve_round_master_only), dual-narrative
        (resolve_non_combat_round), хода NPC (_resolve_npc_combat_turn) и
        хода игрока в бою (_resolve_pc_combat_turn). Пока один из них идёт,
        остальные ждут — их действия копятся в своих очередях и разрешаются
        следующим по очереди, ничего не теряется и не дублируется.
        """
        lock = self._resolver_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._resolver_locks[session_id] = lock
        return lock


    def is_resolver_busy(self, session_id: str) -> bool:
        """True, если для этой сессии прямо сейчас резолвится нарратив (кем угодно)."""
        lock = self._resolver_locks.get(session_id)
        return bool(lock and lock.locked())


    # ═══════════════════════════════════════════════════════════
    # BUG #7 & #10: full runtime-state cleanup on /dileu
    # ═══════════════════════════════════════════════════════════

    def clear_session_runtime_state(self, session_id: str) -> None:
        """BUG #7 + #10: drop ALL in-memory per-session state so the deleted session
        stops accepting rolls, cannot be auto-resolved by a pending task, cannot be
        resumed via /ailddechrau, and the combat loop has no way to send another
        message. Must be called BEFORE db_manager.end_session() so the order of
        operations is:
          1. clear_session_runtime_state(session_id) — in-memory caches
          2. db_manager.end_session(session_id)        — DB row + queue_state
          3. db_manager.drop_session(session_id)       — delete the .db file itself
        After this, /ailddechrau (resume) will fail to find the session (no .db file,
        no _chat_to_session mapping) — exactly what the user expects from "deleted".
        """
        # Stop accepting manual roll submissions.
        self._pending_manual_rolls.pop(session_id, None)
        self._pending_manual_roll_skills.pop(session_id, None)
        # Cancel any deferred resolve — if this fires after delete it would call
        # the Master with stale state.
        self._pending_resolve.pop(session_id, None)
        # Clear db_busy so future read commands in a NEW session in the same chat
        # don't get a spurious "Mир ещё обновляется" warning.
        self._db_busy.pop(session_id, None)
        # Drop the round locks — they're tied to a specific session_id and would
        # otherwise linger forever in memory.
        self._round_locks.pop(session_id, None)
        # Drop the non-combat queue + lock (DUAL NARRATIVE).
        self._non_combat_queue.pop(session_id, None)
        self._non_combat_locks.pop(session_id, None)
        # Drop the session-wide resolver mutex (БОЙ-FIX).
        self._resolver_locks.pop(session_id, None)
        # Drop pending combat-join announcements (БОЙ-FIX v2).
        self._pending_combat_joins.pop(session_id, None)

    # ═══════════════════════════════════════════════════════════
    # End of runtime-state cleanup
    # ═══════════════════════════════════════════════════════════


