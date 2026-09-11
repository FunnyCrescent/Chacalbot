"""SrdRepoMixin — SRD cache + monsters/items/spells reference data."""
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
    SettingEntry, RoundMessageTracker, ValidationLog,
)

logger = logging.getLogger(__name__)


class SrdRepoMixin:
    """SrdRepoMixin — SRD cache + monsters/items/spells reference data."""

    def get_srd_cache(self, query: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response FROM srd_cache WHERE query = ?",
                (query.lower().strip(),)
            ).fetchone()
            return row["response"] if row else None


    def set_srd_cache(self, query: str, response: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO srd_cache (query, response) VALUES (?, ?)",
                (query.lower().strip(), response)
            )

    # ═══════════════════════════════════════════════════════════
    # Location Bindings (#16)
    # ═══════════════════════════════════════════════════════════


    def save_srd_monster(self, monster: SrdMonster):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO srd_monsters
                   (id, name, cr, type, size, ac, hp_avg, hp_formula, speed, stats, abilities, actions, legendary_actions, loot_table_id, lore_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (monster.id, monster.name, monster.cr, monster.type, monster.size, monster.ac, monster.hp_avg, monster.hp_formula, monster.speed, monster.stats, monster.abilities, monster.actions, monster.legendary_actions, monster.loot_table_id, monster.lore_id, monster.source)
            )


    def get_srd_monster(self, name: str) -> Optional[SrdMonster]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM srd_monsters WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                return SrdMonster(
                    id=row["id"], name=row["name"], cr=row["cr"], type=row["type"], size=row["size"],
                    ac=row["ac"], hp_avg=row["hp_avg"], hp_formula=row["hp_formula"], speed=row["speed"],
                    stats=row["stats"], abilities=row["abilities"], actions=row["actions"],
                    legendary_actions=row["legendary_actions"], loot_table_id=row["loot_table_id"],
                    lore_id=row["lore_id"], source=row["source"]
                )
            return None


    def get_srd_monsters(self, cr: str = None, type: str = None) -> List[SrdMonster]:
        with self._connect() as conn:
            query = "SELECT * FROM srd_monsters WHERE 1=1"
            params = []
            if cr:
                query += " AND cr = ?"
                params.append(cr)
            if type:
                query += " AND type = ?"
                params.append(type)
            rows = conn.execute(query, params).fetchall()
            return [SrdMonster(id=r["id"], name=r["name"], cr=r["cr"], type=r["type"], size=r["size"], ac=r["ac"], hp_avg=r["hp_avg"], hp_formula=r["hp_formula"], speed=r["speed"], stats=r["stats"], abilities=r["abilities"], actions=r["actions"], legendary_actions=r["legendary_actions"], loot_table_id=r["loot_table_id"], lore_id=r["lore_id"], source=r["source"]) for r in rows]


    def save_srd_item(self, item: SrdItem):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO srd_items
                   (id, name, category, rarity, type, description, mechanics, base_price_gp, weight, is_magical, attunement_required, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.id, item.name, item.category, item.rarity, item.type, item.description, item.mechanics, item.base_price_gp, item.weight, int(item.is_magical), int(item.attunement_required), item.source)
            )


    def get_srd_item(self, name: str) -> Optional[SrdItem]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM srd_items WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                return SrdItem(
                    id=row["id"], name=row["name"], category=row["category"], rarity=row["rarity"], type=row["type"],
                    description=row["description"], mechanics=row["mechanics"], base_price_gp=row["base_price_gp"],
                    weight=row["weight"], is_magical=bool(row["is_magical"]), attunement_required=bool(row["attunement_required"]), source=row["source"]
                )
            return None


    def save_srd_spell(self, spell: SrdSpell):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO srd_spells
                   (id, name, level, school, casting_time, range, duration, components, description, higher_levels, classes, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (spell.id, spell.name, spell.level, spell.school, spell.casting_time, spell.range, spell.duration, spell.components, spell.description, spell.higher_levels, spell.classes, spell.source)
            )


    def get_srd_spell(self, name: str) -> Optional[SrdSpell]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM srd_spells WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if row:
                return SrdSpell(
                    id=row["id"], name=row["name"], level=row["level"], school=row["school"], casting_time=row["casting_time"],
                    range=row["range"], duration=row["duration"], components=row["components"], description=row["description"],
                    higher_levels=row["higher_levels"], classes=row["classes"], source=row["source"]
                )
            return None

    # ═══════════════════════════════════════════════════════════
    # Validation Logs (anti-cheat)
    # ═══════════════════════════════════════════════════════════

    def save_validation_log(self, vlog: ValidationLog):
        """Save a validation log entry."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO validation_logs
                   (session_id, character_id, character_name, validation_type,
                    is_valid, severity, message, suggestion, details_json,
                    dm_override, override_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vlog.session_id, vlog.character_id, vlog.character_name,
                 vlog.validation_type, int(vlog.is_valid), vlog.severity,
                 vlog.message, vlog.suggestion, vlog.details_json,
                 int(vlog.dm_override), vlog.override_reason)
            )

    def get_validation_logs(self, session_id: str, character_id: str = "") -> List[ValidationLog]:
        """Get validation logs for a session, optionally filtered by character."""
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM validation_logs WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC",
                    (session_id, character_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM validation_logs WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,)
                ).fetchall()
            return [
                ValidationLog(
                    id=r["id"], session_id=r["session_id"],
                    character_id=r["character_id"], character_name=r["character_name"],
                    validation_type=r["validation_type"], is_valid=bool(r["is_valid"]),
                    severity=r["severity"], message=r["message"],
                    suggestion=r["suggestion"], details_json=r["details_json"],
                    dm_override=bool(r["dm_override"]), override_reason=r["override_reason"],
                    created_at=r["created_at"]
                )
                for r in rows
            ]

    def dm_override_validation(self, log_id: int, reason: str):
        """DM override: mark a validation log entry as overridden."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE validation_logs SET dm_override = 1, override_reason = ? WHERE id = ?",
                (reason, log_id)
            )

    # ═══════════════════════════════════════════════════════════
    # Locations
    # ═══════════════════════════════════════════════════════════


