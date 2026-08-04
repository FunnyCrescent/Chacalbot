"""EconomyRepoMixin — db_journal, market_prices, economic_events, timers, loot_tables."""
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


class EconomyRepoMixin:
    """EconomyRepoMixin — db_journal, market_prices, economic_events, timers, loot_tables."""

    def add_journal_entry(self, session_id: str, operation: str, table_name: str,
                          record_id: str = "", details: str = ""):
        """Log a database operation to the journal"""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO db_journal (session_id, operation, table_name, record_id, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, operation, table_name, record_id, details)
            )


    def get_journal_entries(self, session_id: str, limit: int = 30) -> List[DbJournalEntry]:
        """Get recent journal entries for AI context"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM db_journal WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit)
            ).fetchall()
            return [DbJournalEntry(
                id=r["id"], session_id=r["session_id"], operation=r["operation"],
                table_name=r["table_name"], record_id=r["record_id"],
                details=r["details"] or "", created_at=r["created_at"] or ""
            ) for r in reversed(rows)]


    def get_journal_summary(self, session_id: str) -> str:
        """Get a human-readable summary of recent DB operations"""
        entries = self.get_journal_entries(session_id, limit=20)
        if not entries:
            return ""
        lines = ["📊 Журнал БД (последние операции):"]
        for e in entries:
            lines.append(f"  [{e.operation}] {e.table_name}: {e.details}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════


    def set_market_price(self, price: MarketPrice):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO market_prices
                   (session_id, location_id, item_id, base_price_gp, current_price_gp, demand_factor, supply_factor, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (price.session_id, price.location_id, price.item_id, price.base_price_gp, price.current_price_gp, price.demand_factor, price.supply_factor)
            )
        self.add_journal_entry(price.session_id, "INSERT", "market_prices", "", f"Price: {price.item_id}")


    def get_market_prices(self, session_id: str, location_id: str = None) -> List[MarketPrice]:
        with self._connect() as conn:
            query = "SELECT * FROM market_prices WHERE session_id = ?"
            params = [session_id]
            if location_id:
                query += " AND location_id = ?"
                params.append(location_id)
            rows = conn.execute(query, params).fetchall()
            return [MarketPrice(id=r["id"], session_id=r["session_id"], location_id=r["location_id"], item_id=r["item_id"], base_price_gp=r["base_price_gp"], current_price_gp=r["current_price_gp"], demand_factor=r["demand_factor"], supply_factor=r["supply_factor"], last_updated=r["last_updated"] or "") for r in rows]


    def add_economic_event(self, event: EconomicEvent) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO economic_events
                   (session_id, name, description, affected_locations, price_multiplier, duration_days, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event.session_id, event.name, event.description, event.affected_locations, event.price_multiplier, event.duration_days, int(event.is_active))
            )
            event_id = cursor.lastrowid
        self.add_journal_entry(event.session_id, "INSERT", "economic_events", str(event_id), f"Economic event: {event.name}")
        return event_id


    def get_active_economic_events(self, session_id: str) -> List[EconomicEvent]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM economic_events WHERE session_id = ? AND is_active = 1", (session_id,)).fetchall()
            return [EconomicEvent(id=r["id"], session_id=r["session_id"], name=r["name"], description=r["description"], affected_locations=r["affected_locations"], price_multiplier=r["price_multiplier"], duration_days=r["duration_days"], is_active=bool(r["is_active"]), created_at=r["created_at"] or "") for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Active Effects & Timers
    # ═══════════════════════════════════════════════════════════


    def create_timer(self, timer: Timer) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO timers
                   (session_id, entity_type, entity_id, event_type, trigger_round, trigger_time, action, is_recurring)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (timer.session_id, timer.entity_type, timer.entity_id, timer.event_type, timer.trigger_round, timer.trigger_time, timer.action, int(timer.is_recurring))
            )
            timer_id = cursor.lastrowid
        self.add_journal_entry(timer.session_id, "INSERT", "timers", str(timer_id), f"Timer: {timer.event_type}")
        return timer_id


    def get_active_timers(self, session_id: str) -> List[Timer]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM timers WHERE session_id = ? AND is_active = 1", (session_id,)).fetchall()
            return [Timer(id=r["id"], session_id=r["session_id"], entity_type=r["entity_type"], entity_id=r["entity_id"], event_type=r["event_type"], trigger_round=r["trigger_round"], trigger_time=r["trigger_time"], action=r["action"], is_recurring=bool(r["is_recurring"]), is_active=bool(r["is_active"]), created_at=r["created_at"] or "") for r in rows]


    def tick_timer(self, timer_id: int):
        with self._connect() as conn:
            conn.execute("UPDATE timers SET is_active = 0 WHERE id = ?", (timer_id,))

    # ═══════════════════════════════════════════════════════════
    # Loot Tables
    # ═══════════════════════════════════════════════════════════


    def create_loot_table(self, name: str, min_cr: float, max_cr: float, loot_type: str, entries: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO loot_tables (name, min_cr, max_cr, loot_type, entries) VALUES (?, ?, ?, ?, ?)",
                (name, min_cr, max_cr, loot_type, entries)
            )
            return cursor.lastrowid


    def get_loot_table(self, cr: float, loot_type: str = "individual") -> Optional[LootTable]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM loot_tables WHERE min_cr <= ? AND max_cr >= ? AND loot_type = ? LIMIT 1",
                (cr, cr, loot_type)
            ).fetchone()
            if row:
                return LootTable(
                    id=row["id"], name=row["name"], min_cr=row["min_cr"], max_cr=row["max_cr"],
                    loot_type=row["loot_type"], entries=row["entries"], created_at=row["created_at"] or ""
                )
            return None

    # ═══════════════════════════════════════════════════════════
    # COMBAT ENCOUNTERS
    # ═══════════════════════════════════════════════════════════

