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


