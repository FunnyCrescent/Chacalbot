"""WorldServiceMixin — NPCs/time/weather/factions/locations/events."""
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


class WorldServiceMixin:
    """WorldServiceMixin — NPCs/time/weather/factions/locations/events."""

    def add_npc(self, session_id: str, npc_name: str, personality: str = "",
                facts: str = "", relationships: str = ""):
        self.db_manager.get_db(session_id).add_npc(session_id, npc_name, personality, facts, relationships)


    def get_npc(self, session_id: str, npc_name: str):
        return self.db_manager.get_db(session_id).get_npc(session_id, npc_name)


    def update_npc_facts(self, session_id: str, npc_name: str, new_facts: str):
        self.db_manager.get_db(session_id).update_npc_facts(session_id, npc_name, new_facts)


    def add_world_event(self, session_id: str, event_type: str, description: str) -> int:
        return self.db_manager.get_db(session_id).add_world_event(session_id, event_type, description)


    def get_world_events(self, session_id: str, unresolved_only: bool = False) -> list:
        return self.db_manager.get_db(session_id).get_world_events(session_id, unresolved_only)


    def resolve_world_event(self, event_id: int):
        self.db_manager.get_db("").resolve_world_event(event_id)

    # ═══════════════════════════════════════════════════════════
    # Location (#16)
    # ═══════════════════════════════════════════════════════════


    def set_location(self, session_id: str, character_id: str, name: str, description: str = ""):
        self.db_manager.get_db(session_id).set_location(session_id, character_id, name, description)


    def get_location(self, session_id: str, character_id: str):
        return self.db_manager.get_db(session_id).get_location(session_id, character_id)


    def get_location_relations_for_character(self, session_id: str, character_id: str) -> List:
        """H8: what LOCATIONS know about this character — fame, notoriety, wanted status.
        Direction is location -> character (used by /city, personal view)."""
        return self.db_manager.get_db(session_id).get_location_relations(session_id, character_id=character_id)


    def get_all_locations_brief(self, session_id: str) -> List:
        """/city all — every location in the world registry, regardless of whether the
        character has been there. Same reasoning as get_all_npcs_brief: existence of a
        place isn't metagaming since players already saw the lore text; only what a
        SPECIFIC location personally thinks of a character is gated (see above)."""
        return self.db_manager.get_db(session_id).get_locations(session_id)

    # ═══════════════════════════════════════════════════════════
    # SRD (#14)
    # ═══════════════════════════════════════════════════════════


    def add_faction(self, session_id: str, name: str, description: str = "",
                    reputation: int = 0, attitude: str = "neutral") -> int:
        return self.db_manager.get_db(session_id).add_faction(session_id, name, description, reputation, attitude)


    def get_factions(self, session_id: str) -> list:
        return self.db_manager.get_db(session_id).get_factions(session_id)


    def change_reputation(self, session_id: str, faction_id: int, delta: int):
        self.db_manager.get_db(session_id).update_faction_reputation(session_id, faction_id, delta)

    # ═══════════════════════════════════════════════════════════
    # Resources (#20)
    # ═══════════════════════════════════════════════════════════


    def get_time(self, session_id: str) -> dict:
        db = self.db_manager.get_db(session_id)
        gt = db.get_game_time(session_id)
        return {
            "day": gt.day, "hour": gt.hour, "minute": gt.minute,
            "time_str": f"День {gt.day}, {gt.hour:02d}:{gt.minute:02d}",
            "weather": gt.weather, "season": gt.season, "temperature": gt.temperature,
        }


    def advance_time(self, session_id: str, minutes: int = 0, hours: int = 0,
                     weather: str = None, temperature: str = None):
        db = self.db_manager.get_db(session_id)
        day, hour, minute = db.advance_time(session_id, minutes, hours)
        if weather:
            db.update_game_time(session_id, weather=weather)
        if temperature:
            db.update_game_time(session_id, temperature=temperature)
        return {"day": day, "hour": hour, "minute": minute}

    # ═══════════════════════════════════════════════════════════
    # Factions (#12)
    # ═══════════════════════════════════════════════════════════


    def get_world_status(self, session_id: str) -> dict:
        """Get world generation status for validation"""
        db = self.db_manager.get_db(session_id)
        locations = db.get_locations(session_id)
        npcs = db.get_npcs(session_id)
        lore = db.get_lore_by_tag(session_id, "")
        events = db.get_world_events(session_id)
        factions = db.get_factions(session_id)

        return {
            "locations_count": len(locations),
            "locations": [l.name for l in locations[:10]],
            "npcs_count": len(npcs),
            "npcs": [n.name for n in npcs[:10]],
            "lore_count": len(lore),
            "events_count": len(events),
            "factions_count": len(factions),
        }


    def get_known_npcs_for_character(self, session_id: str, character_id: str) -> List[dict]:
        """H1: NPCs THIS character has actually met — what they know/feel about the PC,
        including grudges/debts. Used for the player-facing /npc (relationships)."""
        return self.db_manager.get_db(session_id).get_known_npcs_for_character(session_id, character_id)


    def get_all_npcs_brief(self, session_id: str) -> List:
        """/npc all — every named NPC in the world registry (the npcs/WorldNpc table
        that /dndstart world-gen and set_npc_relation actually use), regardless of
        whether any character has met them. Showing WHO EXISTS isn't metagaming here —
        players already saw the same lore text in the group chat when the world was
        generated; only what a specific NPC personally knows/feels about a character
        (get_known_npcs_for_character above) is gated to actual in-character contact."""
        db = self.db_manager.get_db(session_id)
        return db.get_npcs(session_id, alive_only=False)

    # ═══════════════════════════════════════════════════════════
    # Personal Goals — distinct from quests, see db.py character_goals comment
    # ═══════════════════════════════════════════════════════════


    async def srd_lookup(self, query: str) -> str:
        # SRD cache is global-ish but per-session DB; we use a dummy session for cache
        # Actually SRD cache should probably be global, but for now we pick first available DB
        # or create a dedicated cache. For simplicity, we'll use a fixed "_srd" session DB.
        # Better: keep SRD in the first active session or a dedicated global DB.
        # For this refactor, SRD queries are rare; we'll use the first active session's DB.
        active = self.db_manager.get_all_active_sessions()
        if active:
            db = self.db_manager.get_db(active[0].id)
        else:
            db = self.db_manager.get_db("_srd")
        cached = db.get_srd_cache(query)
        if cached:
            return f"📖 [Кэш] {cached}"

        result = await self.dm.srd_lookup(query)
        db.set_srd_cache(query, result)
        return result


