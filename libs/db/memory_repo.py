"""MemoryRepoMixin — semantic memory entries (AI long-term context storage)."""
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


class MemoryRepoMixin:
    """MemoryRepoMixin — semantic memory entries (AI long-term context storage)."""

    def add_memory_entry(self, entry: MemoryEntry) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO memory_entries (session_id, content, source_type, embedding, confidence, score, last_used, usage_count)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0)""",
                (entry.session_id, entry.content, entry.source_type, entry.embedding, entry.confidence, entry.score)
            )
            return cursor.lastrowid


    def get_memory_entries(self, session_id: str, source_type: str = None) -> List[MemoryEntry]:
        with self._connect() as conn:
            query = "SELECT * FROM memory_entries WHERE session_id = ?"
            params = [session_id]
            if source_type:
                query += " AND source_type = ?"
                params.append(source_type)
            rows = conn.execute(query, params).fetchall()
            return [MemoryEntry(
                id=r["id"], session_id=r["session_id"], content=r["content"], source_type=r["source_type"],
                embedding=r["embedding"] or "[]", confidence=r["confidence"], score=r["score"],
                last_used=r["last_used"] or "", usage_count=r["usage_count"], created_at=r["created_at"] or ""
            ) for r in rows]


    def touch_memory_entries(self, ids: List[int]):
        """Bump last_used/usage_count for entries that were just retrieved and used."""
        if not ids:
            return
        with self._connect() as conn:
            for eid in ids:
                conn.execute(
                    "UPDATE memory_entries SET usage_count = usage_count + 1, last_used = CURRENT_TIMESTAMP WHERE id = ?",
                    (eid,)
                )


    def delete_memory_entries(self, ids: List[int]):
        if not ids:
            return
        with self._connect() as conn:
            qmarks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM memory_entries WHERE id IN ({qmarks})", ids)


    def count_raw_memory_entries(self, session_id: str) -> int:
        """Count non-consolidated entries — used to decide whether it's time to 'sleep'."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM memory_entries WHERE session_id = ? AND source_type != 'consolidated'",
                (session_id,)
            ).fetchone()
            return row["c"] if row else 0

    # ═══════════════════════════════════════════════════════════
    # Location Relations — what a LOCATION knows about a CHARACTER
    # (fame, wanted posters, reputation). Direction: location -> character.
    # ═══════════════════════════════════════════════════════════


