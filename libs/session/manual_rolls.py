"""ManualRollsMixin — /roll command + pending roll registry."""
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
    NPC_AI_ENABLED, NPC_AI_MODE, MAX_MANUAL_ROLLS_PER_ROUND,
)
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class ManualRollsMixin:
    """ManualRollsMixin — /roll command + pending roll registry."""

    def _add_pending_manual_roll(self, session_id: str, player_id: int, check_name: str, display: str):
        """Store a verified roll result. Also tracks the skill name for duplicate/limit checks."""
        self._pending_manual_rolls.setdefault(session_id, {}).setdefault(player_id, []).append(display)
        self._pending_manual_roll_skills.setdefault(session_id, {}).setdefault(player_id, []).append(check_name.lower().strip())

    def _get_manual_roll_count(self, session_id: str, player_id: int) -> int:
        """How many rolls this player has submitted this round."""
        return len(self._pending_manual_roll_skills.get(session_id, {}).get(player_id, []))

    def _has_skill_roll(self, session_id: str, player_id: int, check_name: str) -> bool:
        """Has this player already rolled this exact skill this round?"""
        normalized = check_name.lower().strip()
        return normalized in self._pending_manual_roll_skills.get(session_id, {}).get(player_id, [])

    def _pop_pending_manual_rolls(self, session_id: str) -> Dict[int, List[str]]:
        """Consume (and clear) all pending /roll results for this session."""
        self._pending_manual_roll_skills.pop(session_id, None)
        return self._pending_manual_rolls.pop(session_id, {})


    async def resolve_manual_roll(self, session_id: str, player_id: int, check_name: str) -> Dict:
        """Handle /roll with MAX_MANUAL_ROLLS_PER_ROUND limit and duplicate skill check."""
        db = self.db_manager.get_db(session_id)
        char = db.get_character_by_player(player_id, session_id)
        if not char:
            return {"ok": False, "error": "У тебя нет персонажа в этой сессии."}

        current_count = self._get_manual_roll_count(session_id, player_id)
        if current_count >= MAX_MANUAL_ROLLS_PER_ROUND:
            return {"ok": False, "error": f"Лимит бросков: {MAX_MANUAL_ROLLS_PER_ROUND} за раунд. Ты уже кинул {current_count}."}

        if self._has_skill_roll(session_id, player_id, check_name):
            existing_skills = self._pending_manual_roll_skills.get(session_id, {}).get(player_id, [])
            return {"ok": False, "error": f"Ты уже кидал «{check_name}» в этом раунде. Каждая характеристика — только один бросок.\nУже кинул: {', '.join(existing_skills)}"}

        sheet_text = db.get_character_sheet(session_id, player_id) or "(лист не загружен, используй только базовые правила)"
        progression_text = db.get_character_progression_summary(char.id)

        result = await self.dm.resolve_manual_roll(char.name, check_name, sheet_text, progression_text)
        if result.get("ok"):
            self._add_pending_manual_roll(session_id, player_id, check_name, f"{check_name} — {result['display']}")
            new_count = self._get_manual_roll_count(session_id, player_id)
            remaining = MAX_MANUAL_ROLLS_PER_ROUND - new_count
            result["remaining_rolls"] = remaining
            result["roll_number"] = new_count
        return result
