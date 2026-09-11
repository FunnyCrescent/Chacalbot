"""WorldRepoMixin — locations, NPCs, npc_relations, npc_memory, lore, world_events, factions, game_time."""
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


class WorldRepoMixin:
    """WorldRepoMixin — locations, NPCs, npc_relations, npc_memory, lore, world_events, factions, game_time."""

    def get_game_time(self, session_id: str) -> GameTime:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM game_time WHERE session_id = ?", (session_id,)).fetchone()
            if row:
                return GameTime(
                    session_id=row["session_id"], day=row["day"], hour=row["hour"],
                    minute=row["minute"], weather=row["weather"] or "clear",
                    season=row["season"] or "summer", temperature=row["temperature"] or "mild",
                    last_updated=row["last_updated"] or ""
                )
            else:
                gt = GameTime(session_id=session_id)
                conn.execute(
                    "INSERT INTO game_time (session_id) VALUES (?)",
                    (session_id,)
                )
                return gt


    def update_game_time(self, session_id: str, day: int = None, hour: int = None,
                         minute: int = None, weather: str = None, season: str = None, temperature: str = None):
        with self._connect() as conn:
            fields = []
            params = []
            if day is not None:
                fields.append("day = ?")
                params.append(day)
            if hour is not None:
                fields.append("hour = ?")
                params.append(hour)
            if minute is not None:
                fields.append("minute = ?")
                params.append(minute)
            if weather is not None:
                fields.append("weather = ?")
                params.append(weather)
            if season is not None:
                fields.append("season = ?")
                params.append(season)
            if temperature is not None:
                fields.append("temperature = ?")
                params.append(temperature)
            if fields:
                fields.append("last_updated = CURRENT_TIMESTAMP")
                params.append(session_id)
                conn.execute(f"UPDATE game_time SET {', '.join(fields)} WHERE session_id = ?", params)


    def advance_time(self, session_id: str, minutes: int = 0, hours: int = 0):
        gt = self.get_game_time(session_id)
        total_minutes = gt.minute + minutes + (hours * 60)
        new_minute = total_minutes % 60
        total_hours = gt.hour + (total_minutes // 60)
        new_hour = total_hours % 24
        new_day = gt.day + (total_hours // 24)
        self.update_game_time(session_id, day=new_day, hour=new_hour, minute=new_minute)
        return new_day, new_hour, new_minute

    # ═══════════════════════════════════════════════════════════
    # Factions (#12)
    # ═══════════════════════════════════════════════════════════


    def add_faction(self, session_id: str, name: str, description: str = "",
                    reputation: int = 0, attitude: str = "neutral") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO factions (session_id, name, description, reputation, attitude)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, name, description, reputation, attitude)
            )
            faction_id = cursor.lastrowid
        self.add_journal_entry(session_id, "INSERT", "factions", str(faction_id), f"Faction added: {name}")
        return faction_id


    def get_factions(self, session_id: str) -> List[FactionEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM factions WHERE session_id = ? ORDER BY name",
                (session_id,)
            ).fetchall()
            return [FactionEntry(
                id=r["id"], session_id=r["session_id"], name=r["name"],
                description=r["description"] or "", reputation=r["reputation"],
                attitude=r["attitude"] or "neutral", created_at=r["created_at"] or ""
            ) for r in rows]


    def update_faction_reputation(self, session_id: str, faction_id: int, delta: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE factions SET reputation = reputation + ? WHERE id = ? AND session_id = ?",
                (delta, faction_id, session_id)
            )
            row = conn.execute(
                "SELECT reputation FROM factions WHERE id = ?", (faction_id,)
            ).fetchone()
            if row:
                rep = row["reputation"]
                if rep >= 20:
                    attitude = "friendly"
                elif rep >= 10:
                    attitude = "helpful"
                elif rep > -10:
                    attitude = "neutral"
                elif rep > -20:
                    attitude = "unfriendly"
                else:
                    attitude = "hostile"
                conn.execute(
                    "UPDATE factions SET attitude = ? WHERE id = ?",
                    (attitude, faction_id)
                )
        self.add_journal_entry(session_id, "UPDATE", "factions", str(faction_id), f"Reputation {delta:+d}")

    # ═══════════════════════════════════════════════════════════
    # World Events (#22)
    # ═══════════════════════════════════════════════════════════


    def add_world_event(self, session_id: str, event_type: str, description: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO world_events (session_id, event_type, description) VALUES (?, ?, ?)",
                (session_id, event_type, description)
            )
            event_id = cursor.lastrowid
        self.add_journal_entry(session_id, "INSERT", "world_events", str(event_id), f"Event: {event_type}")
        return event_id


    def get_world_events(self, session_id: str, unresolved_only: bool = False) -> List[WorldEvent]:
        with self._connect() as conn:
            query = "SELECT * FROM world_events WHERE session_id = ?"
            params = [session_id]
            if unresolved_only:
                query += " AND is_resolved = 0"
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [WorldEvent(
                id=r["id"], session_id=r["session_id"], event_type=r["event_type"],
                description=r["description"], is_resolved=bool(r["is_resolved"]),
                created_at=r["created_at"] or "", resolved_at=r["resolved_at"] or ""
            ) for r in rows]


    def resolve_world_event(self, event_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE world_events SET is_resolved = 1, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                (event_id,)
            )
        self.add_journal_entry("", "UPDATE", "world_events", str(event_id), "Event resolved")

    # ═══════════════════════════════════════════════════════════
    # SRD Cache (#14)
    # ═══════════════════════════════════════════════════════════


    def add_npc(self, session_id: str, npc_name: str, personality_pattern: str = "",
                known_facts: str = "", relationships: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO npc_memory (session_id, npc_name, personality_pattern, known_facts, relationships, updated_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (session_id, npc_name, personality_pattern, known_facts, relationships)
            )
        self.add_journal_entry(session_id, "INSERT", "npc_memory", "", f"NPC: {npc_name}")


    def get_npc(self, session_id: str, npc_name: str) -> Optional[NpcMemory]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM npc_memory WHERE session_id = ? AND npc_name = ?",
                (session_id, npc_name)
            ).fetchone()
            if row:
                return NpcMemory(
                    id=r["id"], session_id=r["session_id"], npc_name=r["npc_name"],
                    personality_pattern=r["personality_pattern"] or "", known_facts=r["known_facts"] or "",
                    relationships=r["relationships"] or "", created_at=r["created_at"] or "",
                    updated_at=r["updated_at"] or ""
                )
            return None


    def get_all_npcs(self, session_id: str) -> List[NpcMemory]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM npc_memory WHERE session_id = ? ORDER BY npc_name",
                (session_id,)
            ).fetchall()
            return [NpcMemory(
                id=r["id"], session_id=r["session_id"], npc_name=r["npc_name"],
                personality_pattern=r["personality_pattern"] or "", known_facts=r["known_facts"] or "",
                relationships=r["relationships"] or "", created_at=r["created_at"] or "",
                updated_at=r["updated_at"] or ""
            ) for r in rows]


    def update_npc_facts(self, session_id: str, npc_name: str, new_facts: str):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT known_facts FROM npc_memory WHERE session_id = ? AND npc_name = ?",
                (session_id, npc_name)
            ).fetchone()
            if existing:
                facts = (existing["known_facts"] or "") + "\n" + new_facts
                conn.execute(
                    "UPDATE npc_memory SET known_facts = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ? AND npc_name = ?",
                    (facts, session_id, npc_name)
                )
        self.add_journal_entry(session_id, "UPDATE", "npc_memory", "", f"NPC facts updated: {npc_name}")

    # ═══════════════════════════════════════════════════════════
    # Roll Mode (#6)
    # ═══════════════════════════════════════════════════════════


    def set_location_relation(self, relation: LocationRelation) -> int:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM location_relations WHERE session_id = ? AND location_id = ? AND character_id = ?",
                (relation.session_id, relation.location_id, relation.character_id)
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE location_relations SET fame = ?, reputation = ?, is_wanted = ?, notoriety = ?,
                       last_interaction = CURRENT_TIMESTAMP WHERE id = ?""",
                    (relation.fame, relation.reputation, int(relation.is_wanted), relation.notoriety, existing["id"])
                )
                rid = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO location_relations
                       (session_id, location_id, location_name, character_id, character_name, fame, reputation, is_wanted, notoriety, last_interaction)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (relation.session_id, relation.location_id, relation.location_name, relation.character_id,
                     relation.character_name, relation.fame, relation.reputation, int(relation.is_wanted), relation.notoriety)
                )
                rid = cursor.lastrowid
        self.add_journal_entry(relation.session_id, "UPDATE", "location_relations", str(rid),
                               f"{relation.location_name} о {relation.character_name}: fame={relation.fame}, wanted={relation.is_wanted}")
        return rid


    def adjust_location_relation(self, session_id: str, location_id: str, location_name: str,
                                  character_id: str, character_name: str,
                                  fame_delta: int = 0, reputation_delta: int = 0,
                                  is_wanted: Optional[bool] = None, notoriety_note: str = ""):
        """Incremental version — adds to existing fame/reputation instead of overwriting."""
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM location_relations WHERE session_id = ? AND location_id = ? AND character_id = ?",
                (session_id, location_id, character_id)
            ).fetchone()
            if existing:
                new_fame = existing["fame"] + fame_delta
                new_rep = existing["reputation"] + reputation_delta
                new_wanted = int(is_wanted) if is_wanted is not None else existing["is_wanted"]
                new_notoriety = (existing["notoriety"] + "; " + notoriety_note).strip("; ") if notoriety_note else existing["notoriety"]
                conn.execute(
                    """UPDATE location_relations SET fame = ?, reputation = ?, is_wanted = ?, notoriety = ?,
                       last_interaction = CURRENT_TIMESTAMP WHERE id = ?""",
                    (new_fame, new_rep, new_wanted, new_notoriety, existing["id"])
                )
            else:
                conn.execute(
                    """INSERT INTO location_relations
                       (session_id, location_id, location_name, character_id, character_name, fame, reputation, is_wanted, notoriety, last_interaction)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (session_id, location_id, location_name, character_id, character_name,
                     fame_delta, reputation_delta, int(bool(is_wanted)), notoriety_note)
                )
        self.add_journal_entry(session_id, "UPDATE", "location_relations", location_id,
                               f"{location_name} о {character_name}: fame{fame_delta:+d}" + (f", {notoriety_note}" if notoriety_note else ""))


    def get_location_relations(self, session_id: str, character_id: str = None, location_id: str = None) -> List[LocationRelation]:
        with self._connect() as conn:
            query = "SELECT * FROM location_relations WHERE session_id = ?"
            params = [session_id]
            if character_id:
                query += " AND character_id = ?"
                params.append(character_id)
            if location_id:
                query += " AND location_id = ?"
                params.append(location_id)
            rows = conn.execute(query, params).fetchall()
            return [LocationRelation(
                id=r["id"], session_id=r["session_id"], location_id=r["location_id"], location_name=r["location_name"],
                character_id=r["character_id"], character_name=r["character_name"], fame=r["fame"], reputation=r["reputation"],
                is_wanted=bool(r["is_wanted"]), notoriety=r["notoriety"] or "", last_interaction=r["last_interaction"] or "",
                created_at=r["created_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # SRD — Static Reference Data
    # ═══════════════════════════════════════════════════════════


    def get_known_npcs_for_character(self, session_id: str, character_id: str) -> List[dict]:
        """NPCs this specific character has a relationship with — what THEY know/feel about the PC.
        Used for the player-facing /npc command (only NPCs actually met, not the full world registry)."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT npc_relations.*, npcs.name as npc_name, npcs.race as npc_race, npcs.occupation as npc_occupation
                   FROM npc_relations JOIN npcs ON npc_relations.npc_id = npcs.id
                   WHERE npc_relations.session_id = ? AND npc_relations.character_id = ?
                   ORDER BY npc_relations.last_interaction DESC""",
                (session_id, character_id)
            ).fetchall()
            return [{
                "npc_name": r["npc_name"], "npc_race": r["npc_race"] or "", "npc_occupation": r["npc_occupation"] or "",
                "attitude": r["attitude"] or "neutral", "reputation": r["reputation"] or 0,
                "known_facts": r["known_facts"] or "", "last_interaction": r["last_interaction"] or "",
                "grudges": "" if (r["grudges"] or "") == "[]" else (r["grudges"] or ""),
                "debts": "" if (r["debts"] or "") == "[]" else (r["debts"] or ""),
            } for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Lore
    # ═══════════════════════════════════════════════════════════


    def create_location(self, location: Location):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO locations
                   (id, session_id, name, description, type, parent_location_id, danger_level, discovered_items, current_occupants, weather_effect, is_discovered)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (location.id, location.session_id, location.name, location.description, location.type, location.parent_location_id, location.danger_level, location.discovered_items, location.current_occupants, location.weather_effect, int(location.is_discovered))
            )
        self.add_journal_entry(location.session_id, "INSERT", "locations", location.id, f"Location: {location.name}")


    def get_location_by_id(self, location_id: str) -> Optional[Location]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
            if row:
                return Location(
                    id=row["id"], session_id=row["session_id"], name=row["name"], description=row["description"],
                    type=row["type"], parent_location_id=row["parent_location_id"], danger_level=row["danger_level"],
                    discovered_items=row["discovered_items"], current_occupants=row["current_occupants"],
                    weather_effect=row["weather_effect"], is_discovered=bool(row["is_discovered"]), created_at=row["created_at"] or ""
                )
            return None


    def get_locations(self, session_id: str, type: str = None) -> List[Location]:
        with self._connect() as conn:
            query = "SELECT * FROM locations WHERE session_id = ?"
            params = [session_id]
            if type:
                query += " AND type = ?"
                params.append(type)
            rows = conn.execute(query, params).fetchall()
            return [Location(id=r["id"], session_id=r["session_id"], name=r["name"], description=r["description"], type=r["type"], parent_location_id=r["parent_location_id"], danger_level=r["danger_level"], discovered_items=r["discovered_items"], current_occupants=r["current_occupants"], weather_effect=r["weather_effect"], is_discovered=bool(r["is_discovered"]), created_at=r["created_at"] or "") for r in rows]


    def create_location_path(self, path: LocationPath):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO location_paths
                   (id, from_location_id, to_location_id, travel_hours, danger_encounters, is_blocked, block_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (path.id, path.from_location_id, path.to_location_id, path.travel_hours, path.danger_encounters, int(path.is_blocked), path.block_reason)
            )


    def get_location_paths(self, from_id: str) -> List[LocationPath]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM location_paths WHERE from_location_id = ?", (from_id,)).fetchall()
            return [LocationPath(id=r["id"], from_location_id=r["from_location_id"], to_location_id=r["to_location_id"], travel_hours=r["travel_hours"], danger_encounters=r["danger_encounters"], is_blocked=bool(r["is_blocked"]), block_reason=r["block_reason"], created_at=r["created_at"] or "") for r in rows]

    # ═══════════════════════════════════════════════════════════
    # NPCs
    # ═══════════════════════════════════════════════════════════


    def create_npc(self, npc: WorldNpc):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO npcs
                   (id, session_id, name, race, occupation, location_id, personality, schedule, is_alive, backstory, secrets, faction_id, traits, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (npc.id, npc.session_id, npc.name, npc.race, npc.occupation, npc.location_id, npc.personality, npc.schedule, int(npc.is_alive), npc.backstory, npc.secrets, npc.faction_id, npc.traits)
            )
        self.add_journal_entry(npc.session_id, "INSERT", "npcs", npc.id, f"NPC: {npc.name}")


    def get_npc_by_id(self, npc_id: str) -> Optional[WorldNpc]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM npcs WHERE id = ?", (npc_id,)).fetchone()
            if row:
                return WorldNpc(
                    id=row["id"], session_id=row["session_id"], name=row["name"], race=row["race"], occupation=row["occupation"],
                    location_id=row["location_id"], personality=row["personality"], schedule=row["schedule"], is_alive=bool(row["is_alive"]),
                    backstory=row["backstory"], secrets=row["secrets"], faction_id=row["faction_id"], traits=row["traits"] or "[]", created_at=row["created_at"] or "", updated_at=row["updated_at"] or ""
                )
            return None


    def get_npcs(self, session_id: str, location_id: str = None, alive_only: bool = True) -> List[WorldNpc]:
        with self._connect() as conn:
            query = "SELECT * FROM npcs WHERE session_id = ?"
            params = [session_id]
            if location_id:
                query += " AND location_id = ?"
                params.append(location_id)
            if alive_only:
                query += " AND is_alive = 1"
            rows = conn.execute(query, params).fetchall()
            return [WorldNpc(id=r["id"], session_id=r["session_id"], name=r["name"], race=r["race"], occupation=r["occupation"], location_id=r["location_id"], personality=r["personality"], schedule=r["schedule"], is_alive=bool(r["is_alive"]), backstory=r["backstory"], secrets=r["secrets"], faction_id=r["faction_id"], traits=r["traits"] or "[]", created_at=r["created_at"] or "", updated_at=r["updated_at"] or "") for r in rows]


    def get_npcs_at_location(self, session_id: str, location_id: str, alive_only: bool = True) -> List[WorldNpc]:
        """Get all NPCs currently at a specific location by location_id.
        Convenience wrapper around get_npcs with location_id filter."""
        return self.get_npcs(session_id, location_id=location_id, alive_only=alive_only)


    def get_npcs_by_occupation(self, session_id: str, occupation: str, alive_only: bool = True) -> List[WorldNpc]:
        """Get all NPCs with a specific occupation in a session."""
        with self._connect() as conn:
            query = "SELECT * FROM npcs WHERE session_id = ? AND occupation = ?"
            params = [session_id, occupation]
            if alive_only:
                query += " AND is_alive = 1"
            rows = conn.execute(query, params).fetchall()
            return [WorldNpc(id=r["id"], session_id=r["session_id"], name=r["name"], race=r["race"], occupation=r["occupation"], location_id=r["location_id"], personality=r["personality"], schedule=r["schedule"], is_alive=bool(r["is_alive"]), backstory=r["backstory"], secrets=r["secrets"], faction_id=r["faction_id"], traits=r["traits"] or "[]", created_at=r["created_at"] or "", updated_at=r["updated_at"] or "") for r in rows]


    def set_npc_relation(self, relation: NpcRelation) -> int:
        """Upsert on (session_id, npc_id, character_id) — the old version was
        'INSERT OR REPLACE' with no matching UNIQUE constraint on those columns, so it
        silently inserted a NEW row every time instead of updating, duplicating the same
        NPC relationship endlessly (visible as the same NPC listed many times in /npc).

        grudges/debts: previously dead columns (schema had them, nothing ever wrote to
        them). Treated as free-text, semicolon-appended lists — same convention as
        known_facts — not real JSON; the '[]' default is a leftover placeholder and is
        normalized to blank the first time either column is touched."""
        def _clean(v: str) -> str:
            v = (v or "").strip()
            return "" if v == "[]" else v

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM npc_relations WHERE session_id = ? AND npc_id = ? AND character_id = ?",
                (relation.session_id, relation.npc_id, relation.character_id)
            ).fetchone()
            if existing:
                new_reputation = existing["reputation"] + relation.reputation
                new_attitude = relation.attitude if relation.attitude else existing["attitude"]
                new_facts = existing["known_facts"] or ""
                if relation.known_facts:
                    new_facts = (new_facts + "; " + relation.known_facts).strip("; ")

                new_grudges = _clean(existing["grudges"])
                new_grudge = _clean(relation.grudges)
                if new_grudge and new_grudge not in new_grudges:
                    new_grudges = (new_grudges + "; " + new_grudge).strip("; ")

                new_debts = _clean(existing["debts"])
                new_debt = _clean(relation.debts)
                if new_debt and new_debt not in new_debts:
                    new_debts = (new_debts + "; " + new_debt).strip("; ")

                conn.execute(
                    """UPDATE npc_relations SET reputation = ?, attitude = ?, known_facts = ?,
                       grudges = ?, debts = ?, last_interaction = CURRENT_TIMESTAMP WHERE id = ?""",
                    (new_reputation, new_attitude, new_facts, new_grudges, new_debts, existing["id"])
                )
                return existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO npc_relations
                       (session_id, npc_id, character_id, reputation, attitude, known_facts, last_interaction, grudges, debts)
                       VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)""",
                    (relation.session_id, relation.npc_id, relation.character_id, relation.reputation,
                     relation.attitude or "neutral", relation.known_facts,
                     _clean(relation.grudges), _clean(relation.debts))
                )
                return cursor.lastrowid


    def get_npc_relations(self, session_id: str, npc_id: str = None, character_id: str = None) -> List[NpcRelation]:
        with self._connect() as conn:
            query = "SELECT * FROM npc_relations WHERE session_id = ?"
            params = [session_id]
            if npc_id:
                query += " AND npc_id = ?"
                params.append(npc_id)
            if character_id:
                query += " AND character_id = ?"
                params.append(character_id)
            rows = conn.execute(query, params).fetchall()
            return [NpcRelation(id=r["id"], session_id=r["session_id"], npc_id=r["npc_id"], character_id=r["character_id"], reputation=r["reputation"], attitude=r["attitude"], known_facts=r["known_facts"], last_interaction=r["last_interaction"], grudges=r["grudges"], debts=r["debts"]) for r in rows]


    def create_lore(self, article: LoreArticle):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO lore_articles
                   (id, session_id, title, category, content, tags, related_article_ids, discovered_by_session)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (article.id, article.session_id, article.title, article.category, article.content, article.tags, article.related_article_ids, article.discovered_by_session)
            )
        self.add_journal_entry(article.session_id, "INSERT", "lore_articles", article.id, f"Lore: {article.title}")


    def get_lore_by_id(self, article_id: str) -> Optional[LoreArticle]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM lore_articles WHERE id = ?", (article_id,)).fetchone()
            if row:
                return LoreArticle(
                    id=row["id"], session_id=row["session_id"], title=row["title"], category=row["category"],
                    content=row["content"], tags=row["tags"], related_article_ids=row["related_article_ids"],
                    discovered_by_session=row["discovered_by_session"], created_at=row["created_at"] or ""
                )
            return None


    def get_lore_by_tag(self, session_id: str, tag: str) -> List[LoreArticle]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM lore_articles WHERE session_id = ? AND tags LIKE ?", (session_id, f'%{tag}%')).fetchall()
            return [LoreArticle(id=r["id"], session_id=r["session_id"], title=r["title"], category=r["category"], content=r["content"], tags=r["tags"], related_article_ids=r["related_article_ids"], discovered_by_session=r["discovered_by_session"], created_at=r["created_at"] or "") for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Market & Economy
    # ═══════════════════════════════════════════════════════════


