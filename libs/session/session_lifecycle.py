"""SessionLifecycleMixin — session CRUD, players, kick, transfer_creator, pvp toggle."""
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


class SessionLifecycleMixin:
    """SessionLifecycleMixin — session CRUD, players, kick, transfer_creator, pvp toggle."""

    def create_session(self, chat_id: int, name: str, creator_id: int,
                       creator_name: str) -> Session:
        """Create a new game session"""
        session_id = str(uuid.uuid4())[:8]

        session = Session(
            id=session_id,
            chat_id=chat_id,
            name=name,
            creator_id=creator_id,
            status="active",
            current_scene="",
        )

        self.db_manager.create_session(session)

        # MD-консолидация (Фаза 2): замороженный слепок MD/_applied/ →
        # data/sessions/<sid>/MD/.../*_copy.md. Снимается ОДИН РАЗ здесь; дальше
        # движок всю жизнь сессии читает ТОЛЬКО эту копию — ни MD/ источники,
        # ни MD/_applied/. Готовые позже коммиты (ready.md=True) НЕ трогают
        # эту копию — они попадут только в СЛЕДУЮЩИЕ create_session().
        try:
            from libs.md_store import snapshot_for_session
            snapshot_for_session(session_id)
        except Exception as e:
            # Некритично: движок честно упадёт обратно на глобальные константы
            # (prompts.get_prompt / _load_setting_md имеют фолбэк).
            logger.warning(f"[md_store] session snapshot failed for {session_id}: {e}")

        player = Player(
            user_id=creator_id,
            session_id=session_id,
            username=creator_name,
            display_name=creator_name,
            is_creator=True,
        )
        self.db_manager.get_db(session_id).add_player(player)

        logger.info(f"Created session {session_id}: {name}")
        return session


    def get_active_session(self, chat_id: int) -> Optional[Session]:
        """Get the active session for a chat"""
        return self.db_manager.get_session_by_chat(chat_id)


    def end_session(self, session_id: str):
        """End a session"""
        # ИТЕРАЦИЯ 15: обрыв in-flight генераций ДО пометки ended — резолвы,
        # боевой цикл, DB-Bot background и авто-скип получают task.cancel(),
        # поэтому игрок больше НЕ получает бросок кубиков и нарратив за ход,
        # сделанный до завершения сессии. Плюс страховочный гейт
        # session_is_active() в точках отправки (engine.py).
        try:
            from libs.session.generation_guard import cancel_session_generations
            cancelled = cancel_session_generations(session_id)
            if cancelled:
                logger.info(f"[end_session] {session_id}: aborted {cancelled} in-flight generation task(s)")
        except Exception as e:
            logger.warning(f"[end_session] generation cancel failed (non-fatal): {e}")
        self.db_manager.end_session(session_id)
        # MD-консолидация: per-session копия удаляется ВМЕСТЕ с завершением
        # сессии, чтобы копии не накапливались бесконечно. Если сессию потом
        # возобновят (/ailddechredu → resume_session), копия переснимается
        # из MD/_applied/ (состояние на момент возобновления).
        try:
            from libs.md_store import cleanup_session_md
            cleanup_session_md(session_id)
        except Exception as e:
            logger.warning(f"[md_store] cleanup failed for {session_id}: {e}")
        logger.info(f"Ended session {session_id}")

    # ═══════════════════════════════════════════════════════════
    # Player Management
    # ═══════════════════════════════════════════════════════════


    def get_players(self, session_id: str) -> List[Player]:
        """Get all players in a session"""
        return self.db_manager.get_db(session_id).get_players(session_id)


    def is_creator(self, user_id: int, session_id: str) -> bool:
        """Check if user is the session creator"""
        player = self.db_manager.get_db(session_id).get_player(user_id, session_id)
        return player is not None and player.is_creator

    # ═══════════════════════════════════════════════════════════
    # Combat & Initiative
    # ═══════════════════════════════════════════════════════════


    def transfer_creator(self, session_id: str, old_user_id: int, new_user_id: int) -> bool:
        """Transfer session creator rights to another player."""
        db = self.db_manager.get_db(session_id)
        with db._connect() as conn:
            conn.execute(
                "UPDATE players SET is_creator = 0 WHERE user_id = ? AND session_id = ?",
                (old_user_id, session_id)
            )
            conn.execute(
                "UPDATE players SET is_creator = 1 WHERE user_id = ? AND session_id = ?",
                (new_user_id, session_id)
            )
        return True



    def add_player(self, session_id: str, user_id: int, username: str,
                   display_name: str) -> Player:
        """Add a player to a session"""
        player = Player(
            user_id=user_id,
            session_id=session_id,
            username=username,
            display_name=display_name,
            is_creator=False,
        )
        self.db_manager.get_db(session_id).add_player(player)
        return player


    def add_player_to_queue(self, session_id: str, user_id: int):
        """If a round is collecting actions, add new player to waiting list."""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return
        if state.is_resolving:
            return  # Round is already resolving, they'll join next round
        waiting_for = json.loads(state.waiting_for)
        if user_id not in waiting_for:
            waiting_for.append(user_id)
            state.waiting_for = json.dumps(waiting_for)
            db.set_queue_state(state)


    def remove_player(self, session_id: str, user_id: int) -> bool:
        """Remove a player from a session. Returns True if this just completed the
        pending round (they were the last one waiting) — see db.remove_player."""
        return self.db_manager.get_db(session_id).remove_player(user_id, session_id)


    def kick_player(self, session_id: str, target_user_id: int) -> Tuple[bool, bool]:
        """Kick a player from session (DM only).
        Returns (existed, round_now_complete) — round_now_complete is True if this
        player was the last one the current round was waiting on, meaning the caller
        should resolve the round now instead of leaving it stuck forever."""
        db = self.db_manager.get_db(session_id)
        player = db.get_player(target_user_id, session_id)
        if not player:
            return False, False
        round_now_complete = db.remove_player(target_user_id, session_id)
        return True, round_now_complete


    def toggle_pvp(self, session_id: str):
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if session:
            session.pvp_active = not session.pvp_active
            db.update_session(session)


    def is_pvp(self, session_id: str) -> bool:
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        return session.pvp_active if session else False


    def toggle_autostart(self, session_id: str) -> bool:
        """Toggle auto-start feature. Returns new state."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return False
        current = getattr(session, 'autostart', False)
        new_state = not current
        session.autostart = new_state
        db.update_session(session)
        return new_state



