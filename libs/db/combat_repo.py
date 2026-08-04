"""CombatRepoMixin — combat encounters, combatants, initiative, round messages."""
import json
import logging
import sqlite3
import os
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from .models import (
    Session, Player, Character, HistoryEntry, QueueState, CharacterSheet,
    HpLog, ConditionEntry, RestEntry, GoldEntry, QuestEntry, GameTime,
    FactionEntry, FactionRelation, WorldEvent, SrdCache, LocationBinding,
    CharacterResources, NpcMemory, SrdMonster, SrdItem, SrdSpell, Location,
    LocationPath, WorldNpc, NpcRelation, LoreArticle, MarketPrice,
    EconomicEvent, ActiveEffect, Timer, LootTable, DbJournalEntry,
    MemoryEntry, LocationRelation, CombatEncounter, Combatant, PlayerLanguage,
    SettingEntry, RoundMessageTracker,
)

logger = logging.getLogger(__name__)


class CombatRepoMixin:
    """CombatRepoMixin — combat encounters, combatants, initiative, round messages."""

    def create_combat_encounter(self, encounter: CombatEncounter):
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO combat_encounters VALUES (?,?,?,?,?,?,?)",
                        (encounter.id, encounter.session_id, encounter.reason,
                         encounter.location_name, encounter.round_number,
                         1 if encounter.is_active else 0, encounter.created_at))


    def get_active_combat_encounter(self, session_id: str) -> Optional[CombatEncounter]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM combat_encounters WHERE session_id=? AND is_active=1 ORDER BY created_at DESC LIMIT 1",
                        (session_id,)).fetchone()
            if row:
                return CombatEncounter(id=row["id"], session_id=row["session_id"], reason=row["reason"],
                                       location_name=row["location_name"], round_number=row["round_number"],
                                       is_active=bool(row["is_active"]), created_at=row["created_at"] or "")
        return None


    def end_combat_encounter(self, encounter_id: str):
        with self._connect() as conn:
            conn.execute("UPDATE combat_encounters SET is_active=0 WHERE id=?", (encounter_id,))


    def increment_combat_round(self, encounter_id: str):
        with self._connect() as conn:
            conn.execute("UPDATE combat_encounters SET round_number = round_number + 1 WHERE id=?", (encounter_id,))


    def add_combatant(self, combatant: Combatant):
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO combatants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (combatant.id, combatant.encounter_id, combatant.session_id,
                         combatant.name, combatant.entity_type, combatant.player_id,
                         combatant.initiative, combatant.natural_roll, combatant.dex_mod,
                         combatant.hp, combatant.max_hp, combatant.ac,
                         combatant.current_conditions, 1 if combatant.is_alive else 0,
                         combatant.traits, combatant.brief_context, combatant.sort_order))


    def get_combatants(self, encounter_id: str) -> List[Combatant]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM combatants WHERE encounter_id=? AND is_alive=1 ORDER BY sort_order DESC, initiative DESC",
                        (encounter_id,)).fetchall()
            return [Combatant(
                id=r["id"], encounter_id=r["encounter_id"], session_id=r["session_id"], name=r["name"],
                entity_type=r["entity_type"], player_id=r["player_id"], initiative=r["initiative"],
                natural_roll=r["natural_roll"], dex_mod=r["dex_mod"], hp=r["hp"], max_hp=r["max_hp"],
                ac=r["ac"], current_conditions=r["current_conditions"] or "[]", is_alive=bool(r["is_alive"]),
                traits=r["traits"] or "", brief_context=r["brief_context"] or "", sort_order=r["sort_order"]
            ) for r in rows]


    def get_alive_combatants_by_type(self, encounter_id: str, entity_type: str) -> List[Combatant]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM combatants WHERE encounter_id=? AND is_alive=1 AND entity_type=? ORDER BY sort_order DESC, initiative DESC",
                        (encounter_id, entity_type)).fetchall()
            return [Combatant(
                id=r["id"], encounter_id=r["encounter_id"], session_id=r["session_id"], name=r["name"],
                entity_type=r["entity_type"], player_id=r["player_id"], initiative=r["initiative"],
                natural_roll=r["natural_roll"], dex_mod=r["dex_mod"], hp=r["hp"], max_hp=r["max_hp"],
                ac=r["ac"], current_conditions=r["current_conditions"] or "[]", is_alive=bool(r["is_alive"]),
                traits=r["traits"] or "", brief_context=r["brief_context"] or "", sort_order=r["sort_order"]
            ) for r in rows]


    def update_combatant(self, combatant_id: str, **kwargs):
        """Update combatant fields. kwargs: hp, is_alive, current_conditions, etc."""
        if not kwargs:
            return
        set_clause = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values())
        # Convert booleans
        for i, v in enumerate(values):
            if isinstance(v, bool):
                values[i] = 1 if v else 0
            elif isinstance(v, list):
                values[i] = json.dumps(v)
        values.append(combatant_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE combatants SET {set_clause} WHERE id=?", values)


    def kill_combatant(self, combatant_id: str):
        with self._connect() as conn:
            conn.execute("UPDATE combatants SET is_alive=0 WHERE id=?", (combatant_id,))


    def get_initiative_order(self, encounter_id: str) -> List[Dict]:
        """Get full initiative order sorted by initiative descending."""
        with self._connect() as conn:
            rows = conn.execute("""SELECT name, entity_type, player_id, initiative, natural_roll, sort_order
                           FROM combatants WHERE encounter_id=? AND is_alive=1
                           ORDER BY initiative DESC, natural_roll DESC, sort_order DESC""", (encounter_id,)).fetchall()
            return [{"name": r["name"], "entity_type": r["entity_type"], "player_id": r["player_id"],
                     "initiative": r["initiative"], "natural_roll": r["natural_roll"], "sort_order": r["sort_order"]} for r in rows]

    # ═══════════════════════════════════════════════════════════
    # PLAYER LANGUAGES (translation)
    # ═══════════════════════════════════════════════════════════

    def track_round_message(self, session_id: str, message_id: int, chat_id: int,
                            player_id: int, message_type: str):
        with self._connect() as conn:
            conn.execute("INSERT INTO round_messages (session_id, message_id, chat_id, player_id, message_type) VALUES (?,?,?,?,?)",
                        (session_id, message_id, chat_id, player_id, message_type))


    def get_round_messages(self, session_id: str) -> List[RoundMessageTracker]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM round_messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            return [RoundMessageTracker(session_id=r["session_id"], message_id=r["message_id"], chat_id=r["chat_id"],
                                         player_id=r["player_id"], message_type=r["message_type"] or "dn", created_at=r["created_at"] or "") for r in rows]


    def clear_round_messages(self, session_id: str):
        with self._connect() as conn:
            conn.execute("DELETE FROM round_messages WHERE session_id=?", (session_id,))


