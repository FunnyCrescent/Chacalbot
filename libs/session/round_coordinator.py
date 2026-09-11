"""RoundCoordinatorMixin — action collection + non-combat queue."""
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


class RoundCoordinatorMixin:
    """RoundCoordinatorMixin — action collection + non-combat queue."""

    # ═══════════════════════════════════════════════════════════
    # Глобальный счётчик раундов (Фича 3)
    # ═══════════════════════════════════════════════════════════
    # Игроки задают вопросы вида «что я делал 5 раундов назад?», а нейросеть
    # не понимала нумерацию — в истории не было меток раундов. Теперь каждый
    # нарративный раунд получает монотонный номер: он хранится в БД сессии
    # (session_meta["global_round"]) и переживает перезапуск бота. Боевые
    # раунды штампуются номером инициативы (sessions.round_number) с пометкой
    # «бой», чтобы счётчик не скакал на +1 за каждый ход в бою.

    ROUND_META_KEY = "global_round"

    def get_current_round(self, session_id: str) -> int:
        """Текущий номер нарративного раунда (0 — мир ещё не разрешал ни одного раунда)."""
        try:
            db = self.db_manager.get_db(session_id)
            return int(db.get_meta(self.ROUND_META_KEY, "0") or 0)
        except Exception as e:
            logger.warning(f"[round] get_current_round failed: {e}")
            return 0

    def advance_round(self, session_id: str) -> int:
        """Инкрементирует и возвращает номер нового нарративного раунда.

        БОЙ-FIX: инкремент теперь атомарный — один UPSERT-стейтмент в
        db.increment_meta() вместо read-modify-write двумя запросами.
        Параллельные резолверы (боевой цикл × dual-narrative) больше не могут
        потерять обновление счётчика; вдобавок оба резолвера сериализуются
        session-wide локом get_resolver_lock() (см. ResolutionMixin).
        """
        try:
            db = self.db_manager.get_db(session_id)
            return int(db.increment_meta(self.ROUND_META_KEY, "0"))
        except Exception as e:
            logger.warning(f"[round] advance_round failed: {e}")
            return 0

    def start_action_collection(self, session_id: str) -> list:
        """Start collecting actions from ALL players for this round."""
        db = self.db_manager.get_db(session_id)
        players = db.get_players(session_id)
        waiting_for = [p.user_id for p in players]

        state = QueueState(
            session_id=session_id,
            waiting_for=json.dumps(waiting_for),
            collected_actions="{}",
            is_resolving=False,
        )
        db.set_queue_state(state)

        return waiting_for


    def submit_action(self, session_id: str, player_id: int,
                      action: str) -> Tuple[bool, Optional[str]]:
        """
        Submit a player action.
        Returns (is_complete, dm_response_or_none).
        """
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return False, None

        if state.is_resolving:
            return False, "*Resolving previous round... wait.*"

        waiting_for = json.loads(state.waiting_for)
        collected = json.loads(state.collected_actions)

        if str(player_id) in collected:
            return False, "*You already submitted your action for this round.*"

        if player_id not in waiting_for:
            return False, "*Not your turn to act, or you're not in this round.*"

        # Combat initiative blocking
        if COMBAT_INITIATIVE_ENABLED:
            db = self.db_manager.get_db(session_id)
            session = db.get_session(session_id)
            if session and session.combat_active:
                group = self.get_combat_action_group(session_id)
                blocked = group.get("blocked_players", [])
                if player_id in blocked:
                    return False, "Not your turn. Wait for your initiative."

        collected[str(player_id)] = action
        waiting_for.remove(player_id)

        state.waiting_for = json.dumps(waiting_for)
        state.collected_actions = json.dumps(collected)
        if not waiting_for:
            # Lock immediately, in the same write — see get_round_lock() comment for why
            # this alone isn't a full guarantee under true concurrency, but it closes
            # most of the previous window (which used to stay open for the entire time
            # between "detected complete" and resolve_round() actually being awaited).
            state.is_resolving = True
        db.set_queue_state(state)

        if not waiting_for:
            return True, None

        return False, None


    def cancel_action(self, session_id: str, player_id: int) -> Tuple[bool, str]:
        """Retract a player's already-submitted action for the current round, putting
        them back into waiting_for — only possible before the round starts resolving."""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return False, "Нет активного раунда."
        if state.is_resolving:
            return False, "Раунд уже разрешается, отменить действие больше нельзя."

        collected = json.loads(state.collected_actions)
        if str(player_id) not in collected:
            return False, "У тебя нет поданного действия в этом раунде."

        del collected[str(player_id)]
        waiting_for = json.loads(state.waiting_for)
        if player_id not in waiting_for:
            waiting_for.append(player_id)

        state.waiting_for = json.dumps(waiting_for)
        state.collected_actions = json.dumps(collected)
        db.set_queue_state(state)
        return True, "Действие отменено. Можешь написать новое `Дн.`"

    # ═══════════════════════════════════════════════════════════
    # /roll command (A4) — DB-Bot reads sheet+DB, rolls a REAL d20,
    # result gets attached to this player's next Дн. this round.
    # ═══════════════════════════════════════════════════════════


    def get_pending_players(self, session_id: str) -> List[str]:
        """Get list of display names still needed to act"""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return []

        waiting_for = json.loads(state.waiting_for)
        names = []
        for uid in waiting_for:
            player = db.get_player(uid, session_id)
            if player:
                char = db.get_character_by_player(uid, session_id)
                names.append(char.name if char else player.display_name)
        return names


    def force_reset_round(self, session_id: str) -> Dict:
        """DM safety valve for a round stuck after a crash mid-resolve (e.g. the
        LocationBinding hash bug, or any future exception). Unlike /clear, this does
        NOT wipe campaign history/memory — it only discards the current round's queue
        and starts a fresh one. Returns what was discarded so the DM can see the cost."""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        discarded_waiting, discarded_actions = [], []
        if state:
            try:
                waiting_for = json.loads(state.waiting_for)
            except Exception:
                waiting_for = []
            try:
                collected = json.loads(state.collected_actions)
            except Exception:
                collected = {}
            for uid in waiting_for:
                p = db.get_player(uid, session_id)
                discarded_waiting.append(p.display_name if p else str(uid))
            for uid_str in collected:
                p = db.get_player(int(uid_str), session_id)
                discarded_actions.append(p.display_name if p else uid_str)

        db.clear_queue_state(session_id)
        self.start_action_collection(session_id)
        return {"discarded_waiting": discarded_waiting, "discarded_actions": discarded_actions}


    def start_single_player_collection(self, session_id: str, player_id: int):
        """Set queue for a single player during their combat turn."""
        db = self.db_manager.get_db(session_id)
        state = QueueState(
            session_id=session_id,
            waiting_for=json.dumps([player_id]),
            collected_actions="{}",
            is_resolving=False,
        )
        db.set_queue_state(state)


    def get_non_combat_players(self, session_id: str) -> List[int]:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return []
        combatants = db.get_combatants(encounter.id)
        combat_player_ids = {c.player_id for c in combatants if c.player_id > 0}
        players = db.get_players(session_id)
        return [p.user_id for p in players if p.user_id not in combat_player_ids]

    # ═══════════════════════════════════════════════════════════════
    # DUAL NARRATIVE — non-combat player queue during combat
    # ═══════════════════════════════════════════════════════════════


    def is_non_combat_player(self, session_id: str, player_id: int) -> bool:
        """Check if a player is a non-combat participant during active combat."""
        return player_id in self.get_non_combat_players(session_id)


    def submit_non_combat_action(self, session_id: str, player_id: int, action: str):
        """Submit an action from a non-combat player during combat.
        Returns the number of actions in the queue after this submission."""
        if session_id not in self._non_combat_queue:
            self._non_combat_queue[session_id] = {}
        self._non_combat_queue[session_id][player_id] = action
        return len(self._non_combat_queue[session_id])


    def get_non_combat_queue(self, session_id: str) -> Dict[int, str]:
        """Get all queued non-combat actions for this session."""
        return dict(self._non_combat_queue.get(session_id, {}))


    def pop_non_combat_queue(self, session_id: str) -> Dict[int, str]:
        """Atomically pop and return all non-combat actions, clearing the queue."""
        actions = dict(self._non_combat_queue.pop(session_id, {}))
        return actions


    def clear_non_combat_queue(self, session_id: str):
        """Clear the non-combat queue (e.g. when combat ends)."""
        self._non_combat_queue.pop(session_id, None)


    def skip_player(self, session_id: str, player_id: int,
                    reason: str = "*[Turn skipped by DM]*") -> bool:
        """Skip a player's turn (creator only, or self-skip via /sgipio).

        `reason` lands in collected_actions as the player's "action" — the Master
        sees WHO skipped and WHY (admin skip vs player self-skip)."""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return False

        waiting_for = json.loads(state.waiting_for)
        collected = json.loads(state.collected_actions)

        if player_id in waiting_for:
            waiting_for.remove(player_id)
            collected[str(player_id)] = reason

            state.waiting_for = json.dumps(waiting_for)
            state.collected_actions = json.dumps(collected)
            if not waiting_for:
                state.is_resolving = True
            db.set_queue_state(state)

            return len(waiting_for) == 0

        return False

    # ═══════════════════════════════════════════════════════════
    # Out-of-turn questions
    # ═══════════════════════════════════════════════════════════


