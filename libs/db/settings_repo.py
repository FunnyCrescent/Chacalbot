"""SettingsRepoMixin — player_languages + global settings table."""
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


class SettingsRepoMixin:
    """SettingsRepoMixin — player_languages + global settings table."""

    def set_player_language(self, session_id: str, player_id: int, language: str, enabled: bool = True):
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO player_languages VALUES (?,?,?,?)",
                        (session_id, player_id, language, 1 if enabled else 0))


    def get_player_language(self, session_id: str, player_id: int) -> Optional[PlayerLanguage]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM player_languages WHERE session_id=? AND player_id=?",
                        (session_id, player_id)).fetchone()
            if row:
                return PlayerLanguage(session_id=row["session_id"], player_id=row["player_id"],
                                      language=row["language"] or "", enabled=bool(row["enabled"]))
        return None


    def get_enabled_translations(self, session_id: str) -> List[PlayerLanguage]:
        """Get all players with translation enabled, grouped by language."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM player_languages WHERE session_id=? AND enabled=1", (session_id,)).fetchall()
            return [PlayerLanguage(session_id=r["session_id"], player_id=r["player_id"],
                                   language=r["language"] or "", enabled=True) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # SETTINGS
    # ═══════════════════════════════════════════════════════════

    def add_setting(self, setting: SettingEntry):
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO settings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (setting.id, setting.name, setting.category,
                         setting.complexity_score, setting.summary,
                         setting.full_description, setting.lore,
                         1 if setting.is_builtin else 0,
                         1 if setting.is_verified else 0,
                         setting.created_by, setting.file_path, setting.tags))


    def get_setting(self, setting_id: str) -> Optional[SettingEntry]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id=?", (setting_id,)).fetchone()
            if row:
                return SettingEntry(id=row["id"], name=row["name"], category=row["category"] or "",
                                    complexity_score=row["complexity_score"], summary=row["summary"] or "",
                                    full_description=row["full_description"] or "", lore=row["lore"] or "",
                                    is_builtin=bool(row["is_builtin"]), is_verified=bool(row["is_verified"]),
                                    created_by=row["created_by"], file_path=row["file_path"] or "", tags=row["tags"] or "[]")
        return None


    def get_settings_by_category(self, category: str) -> List[SettingEntry]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM settings WHERE category=? AND is_verified=1 ORDER BY name", (category,)).fetchall()
            return [SettingEntry(id=r["id"], name=r["name"], category=r["category"] or "",
                                  complexity_score=r["complexity_score"], summary=r["summary"] or "",
                                  full_description=r["full_description"] or "", lore=r["lore"] or "",
                                  is_builtin=bool(r["is_builtin"]), is_verified=bool(r["is_verified"]),
                                  created_by=r["created_by"], file_path=r["file_path"] or "", tags=r["tags"] or "[]") for r in rows]


    def search_settings(self, query: str, limit: int = 5) -> List[SettingEntry]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM settings WHERE is_verified=1 AND (name LIKE ? OR category LIKE ? OR summary LIKE ?) ORDER BY name LIMIT ?",
                        (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()
            return [SettingEntry(id=r["id"], name=r["name"], category=r["category"] or "",
                                  complexity_score=r["complexity_score"], summary=r["summary"] or "",
                                  full_description=r["full_description"] or "", lore=r["lore"] or "",
                                  is_builtin=bool(r["is_builtin"]), is_verified=bool(r["is_verified"]),
                                  created_by=r["created_by"], file_path=r["file_path"] or "", tags=r["tags"] or "[]") for r in rows]


    def get_all_categories(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT category FROM settings WHERE is_verified=1 ORDER BY category").fetchall()
            return [r["category"] for r in rows]

    # ═══════════════════════════════════════════════════════════
    # ROUND MESSAGE TRACKER (anti-spam)
    # ═══════════════════════════════════════════════════════════

