"""BaseDatabase — constructor + SQLite connection + schema bootstrap."""
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


class BaseDatabase:
    """Базовый класс Database — подключение и schema bootstrap."""

    def __init__(self, db_path: str = "data/campaigns.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    def _init_db(self):
        """Initialize database schema — ALL tables inside the with block"""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            # Core tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    creator_id INTEGER NOT NULL,
                    status TEXT DEFAULT 'active',
                    current_scene TEXT DEFAULT '',
                    combat_active INTEGER DEFAULT 0,
                    initiative_order TEXT DEFAULT '[]',
                    current_turn_index INTEGER DEFAULT 0,
                    round_number INTEGER DEFAULT 0,
                    pvp_active INTEGER DEFAULT 0,
                    autostart INTEGER DEFAULT 0,
                    summary TEXT DEFAULT '',
                    summary_at_count INTEGER DEFAULT 0,
                    genre TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Safe migration for DBs created before summary_at_count existed —
            # CREATE TABLE IF NOT EXISTS above won't add the column to an old file.
            try:
                existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
                if "summary_at_count" not in existing_cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN summary_at_count INTEGER DEFAULT 0")
                if "genre" not in existing_cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN genre TEXT DEFAULT ''")
                # BUG #2: persist message_thread_id so async background sends (combat
                # loop, DB-Bot phase, deferred resolves) land in the same forum topic
                # as the original command instead of falling back to the main group chat.
                if "message_thread_id" not in existing_cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN message_thread_id INTEGER DEFAULT 0")
                # ИТЕРАЦИЯ 10 (Разделы 6-7): способ оплаты сессии — «split» (поровну
                # между игроками) | «creator_pays» (полностью платит создатель).
                # Выбирается ОДИН раз в меню /newydd и фиксируется на жизнь сессии.
                if "billing_mode" not in existing_cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN billing_mode TEXT DEFAULT 'split'")
                # ИТЕРАЦИЯ 10 (Раздел 4): тай-брейк инициативы при равных значениях.
                if "priority" not in {r["name"] for r in conn.execute("PRAGMA table_info(combatants)").fetchall()}:
                    conn.execute("ALTER TABLE combatants ADD COLUMN priority INTEGER DEFAULT 0")
                # Custom currency columns
                for col in ["currency_name", "currency_plural", "currency_symbol",
                            "currency_sub_name", "currency_sub_plural", "currency_sub_symbol",
                            "currency_sub_value", "currency_super_name", "currency_super_plural",
                            "currency_super_symbol", "currency_super_value"]:
                    if col not in existing_cols:
                        default_val = "0.1" if col == "currency_sub_value" else ("10.0" if col == "currency_super_value" else "''")
                        conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT {default_val}")
            except Exception as e:
                logger.warning(f"summary_at_count/currency migration check failed (non-fatal): {e}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    user_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    username TEXT,
                    display_name TEXT NOT NULL,
                    is_creator INTEGER DEFAULT 0,
                    joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, session_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    player_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    race TEXT,
                    class_name TEXT,
                    level INTEGER DEFAULT 1,
                    hp INTEGER DEFAULT 0,
                    max_hp INTEGER DEFAULT 0,
                    ac INTEGER DEFAULT 10,
                    stats TEXT DEFAULT '{}',
                    proficiencies TEXT DEFAULT '[]',
                    inventory TEXT DEFAULT '[]',
                    spells TEXT DEFAULT '[]',
                    features TEXT DEFAULT '[]',
                    backstory TEXT DEFAULT '',
                    death_saves_success INTEGER DEFAULT 0,
                    death_saves_failure INTEGER DEFAULT 0,
                    is_alive INTEGER DEFAULT 1,
                    conditions TEXT DEFAULT '[]',
                    languages TEXT DEFAULT '[]',
                    xp INTEGER DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Migration: add languages/xp columns to existing characters tables
            # (xp — ИТЕРАЦИЯ 15: скрытый опыт, авто-уровневание по порогам libs.xp_system)
            try:
                char_cols = {r["name"] for r in conn.execute("PRAGMA table_info(characters)").fetchall()}
                if "languages" not in char_cols:
                    conn.execute("ALTER TABLE characters ADD COLUMN languages TEXT DEFAULT '[]'")
                if "xp" not in char_cols:
                    conn.execute("ALTER TABLE characters ADD COLUMN xp INTEGER DEFAULT 0")
            except Exception as e:
                logger.warning(f"characters migration check failed (non-fatal): {e}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    entry_type TEXT DEFAULT 'narrative',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue_state (
                    session_id TEXT PRIMARY KEY,
                    waiting_for TEXT DEFAULT '[]',
                    collected_actions TEXT DEFAULT '{}',
                    is_resolving INTEGER DEFAULT 0,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS character_sheets (
                    session_id TEXT NOT NULL,
                    player_id INTEGER NOT NULL,
                    sheet_text TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    PRIMARY KEY (session_id, player_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # HP & Conditions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hp_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    old_hp INTEGER NOT NULL,
                    new_hp INTEGER NOT NULL,
                    change INTEGER NOT NULL,
                    source TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conditions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    duration TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rest_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    rest_type TEXT NOT NULL,
                    hp_restored INTEGER DEFAULT 0,
                    hit_dice_used INTEGER DEFAULT 0,
                    abilities_recovered TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Economy
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gold_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    delta_cp INTEGER DEFAULT 0,
                    delta_sp INTEGER DEFAULT 0,
                    delta_ep INTEGER DEFAULT 0,
                    delta_gp INTEGER DEFAULT 0,
                    delta_pp INTEGER DEFAULT 0,
                    reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    description TEXT DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Quests & Time
            conn.execute("""
                CREATE TABLE IF NOT EXISTS quests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    assignee_id TEXT DEFAULT '',
                    assignee_name TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS game_time (
                    session_id TEXT PRIMARY KEY,
                    day INTEGER DEFAULT 1,
                    hour INTEGER DEFAULT 8,
                    minute INTEGER DEFAULT 0,
                    weather TEXT DEFAULT 'clear',
                    season TEXT DEFAULT 'summer',
                    temperature TEXT DEFAULT 'mild',
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Factions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    reputation INTEGER DEFAULT 0,
                    attitude TEXT DEFAULT 'neutral',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS faction_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    faction_id INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    standing INTEGER DEFAULT 0,
                    notes TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # World events
            conn.execute("""
                CREATE TABLE IF NOT EXISTS world_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    is_resolved INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # SRD cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS srd_cache (
                    query TEXT PRIMARY KEY,
                    response TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Locations & bindings
            conn.execute("""
                CREATE TABLE IF NOT EXISTS location_bindings (
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    location_name TEXT DEFAULT '',
                    location_description TEXT DEFAULT '',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, character_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Resources
            conn.execute("""
                CREATE TABLE IF NOT EXISTS character_resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    resource_name TEXT NOT NULL,
                    current INTEGER DEFAULT 0,
                    maximum INTEGER DEFAULT 0,
                    short_rest_recover INTEGER DEFAULT 0,
                    long_rest_recover INTEGER DEFAULT 1,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # NPC memory
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    npc_name TEXT NOT NULL,
                    personality_pattern TEXT DEFAULT '',
                    known_facts TEXT DEFAULT '',
                    relationships TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Roll mode
            conn.execute("""
                CREATE TABLE IF NOT EXISTS roll_mode (
                    session_id TEXT PRIMARY KEY,
                    mode TEXT DEFAULT 'mixed',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # DB Journal — NEW!
            conn.execute("""
                CREATE TABLE IF NOT EXISTS db_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    operation TEXT,
                    table_name TEXT,
                    record_id TEXT,
                    details TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # World memory — semantic diary (embedding-based retrieval)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_type TEXT DEFAULT 'narrative',
                    embedding TEXT DEFAULT '[]',
                    confidence REAL DEFAULT 0.0,
                    score REAL DEFAULT 0.0,
                    last_used TEXT DEFAULT CURRENT_TIMESTAMP,
                    usage_count INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Location -> Character relation: what the CITY knows about the PC
            # (fame, wanted posters, reputation) — direction is location -> character.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS location_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    location_name TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    fame INTEGER DEFAULT 0,
                    reputation INTEGER DEFAULT 0,
                    is_wanted INTEGER DEFAULT 0,
                    notoriety TEXT DEFAULT '',
                    last_interaction TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # Personal goals — distinct from quests. A quest needs a narratively
            # confirmed NPC-given task; a goal is what the CHARACTER already wants
            # (from backstory, or a stated intention/thought in play) with no NPC
            # confirmation required. Never auto-promoted to a quest.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS character_goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    source TEXT DEFAULT 'session',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            # SRD tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS srd_monsters (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    cr TEXT,
                    type TEXT,
                    size TEXT,
                    ac INTEGER,
                    hp_avg INTEGER,
                    hp_formula TEXT,
                    speed TEXT,
                    stats TEXT DEFAULT '{}',
                    abilities TEXT DEFAULT '[]',
                    actions TEXT DEFAULT '[]',
                    legendary_actions TEXT DEFAULT '[]',
                    loot_table_id TEXT,
                    lore_id TEXT,
                    source TEXT DEFAULT 'SRD'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS srd_items (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    rarity TEXT DEFAULT 'common',
                    type TEXT,
                    description TEXT,
                    mechanics TEXT DEFAULT '{}',
                    base_price_gp INTEGER DEFAULT 0,
                    weight REAL DEFAULT 0,
                    is_magical INTEGER DEFAULT 0,
                    attunement_required INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'SRD'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS srd_spells (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    level INTEGER,
                    school TEXT,
                    casting_time TEXT,
                    range TEXT,
                    duration TEXT,
                    components TEXT,
                    description TEXT,
                    higher_levels TEXT,
                    classes TEXT DEFAULT '[]',
                    source TEXT DEFAULT 'SRD'
                )
            """)
            # Dynamic world tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    type TEXT DEFAULT 'wilderness',
                    parent_location_id TEXT,
                    danger_level INTEGER DEFAULT 1,
                    discovered_items TEXT DEFAULT '[]',
                    current_occupants TEXT DEFAULT '[]',
                    weather_effect TEXT DEFAULT '',
                    is_discovered INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS location_paths (
                    id TEXT PRIMARY KEY,
                    from_location_id TEXT NOT NULL,
                    to_location_id TEXT NOT NULL,
                    travel_hours INTEGER DEFAULT 1,
                    danger_encounters TEXT DEFAULT '[]',
                    is_blocked INTEGER DEFAULT 0,
                    block_reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npcs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    race TEXT DEFAULT '',
                    occupation TEXT DEFAULT '',
                    location_id TEXT,
                    personality TEXT DEFAULT '{}',
                    schedule TEXT DEFAULT '{}',
                    is_alive INTEGER DEFAULT 1,
                    backstory TEXT DEFAULT '',
                    secrets TEXT DEFAULT '[]',
                    faction_id INTEGER,
                    traits TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS npc_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    npc_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    reputation INTEGER DEFAULT 0,
                    attitude TEXT DEFAULT 'neutral',
                    known_facts TEXT DEFAULT '',
                    last_interaction TEXT DEFAULT '',
                    grudges TEXT DEFAULT '[]',
                    debts TEXT DEFAULT '[]',
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lore_articles (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    content TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    related_article_ids TEXT DEFAULT '[]',
                    discovered_by_session TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    location_id TEXT,
                    item_id TEXT NOT NULL,
                    base_price_gp INTEGER DEFAULT 0,
                    current_price_gp INTEGER DEFAULT 0,
                    demand_factor REAL DEFAULT 1.0,
                    supply_factor REAL DEFAULT 1.0,
                    last_updated TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS economic_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    affected_locations TEXT DEFAULT '[]',
                    price_multiplier REAL DEFAULT 1.0,
                    duration_days INTEGER DEFAULT 7,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_effects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    effect_type TEXT DEFAULT 'curse',
                    source TEXT DEFAULT '',
                    duration_type TEXT DEFAULT 'permanent',
                    remaining INTEGER DEFAULT 0,
                    mechanics TEXT DEFAULT '{}',
                    is_removable INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS timers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    trigger_round INTEGER,
                    trigger_time TEXT,
                    action TEXT DEFAULT '{}',
                    is_recurring INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS loot_tables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    min_cr REAL DEFAULT 0,
                    max_cr REAL DEFAULT 0,
                    loot_type TEXT DEFAULT 'individual',
                    entries TEXT DEFAULT '[]',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ── Combat Encounters ─────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS combat_encounters (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    location_name TEXT DEFAULT '',
                    round_number INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS combatants (
                    id TEXT PRIMARY KEY,
                    encounter_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entity_type TEXT DEFAULT 'pc',
                    player_id INTEGER DEFAULT 0,
                    initiative INTEGER DEFAULT 0,
                    natural_roll INTEGER DEFAULT 0,
                    dex_mod INTEGER DEFAULT 0,
                    hp INTEGER DEFAULT 0,
                    max_hp INTEGER DEFAULT 0,
                    ac INTEGER DEFAULT 10,
                    current_conditions TEXT DEFAULT '[]',
                    is_alive INTEGER DEFAULT 1,
                    traits TEXT DEFAULT '',
                    brief_context TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    priority INTEGER DEFAULT 0,
                    FOREIGN KEY (encounter_id) REFERENCES combat_encounters(id)
                )
            """)
            # ── Player Languages ───────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS player_languages (
                    session_id TEXT NOT NULL,
                    player_id INTEGER NOT NULL,
                    language TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 0,
                    PRIMARY KEY (session_id, player_id)
                )
            """)
            # ── Settings ────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    complexity_score REAL DEFAULT 0.0,
                    summary TEXT DEFAULT '',
                    full_description TEXT DEFAULT '',
                    lore TEXT DEFAULT '',
                    is_builtin INTEGER DEFAULT 1,
                    is_verified INTEGER DEFAULT 1,
                    created_by INTEGER DEFAULT 0,
                    file_path TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]'
                )
            """)
            # ── Round Message Tracker (anti-spam) ──────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS round_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    player_id INTEGER DEFAULT 0,
                    message_type TEXT DEFAULT 'dn',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ── Session meta (key-value) — глобальный счётчик раундов и пр. ──
            # Фича 3: нейросеть должна понимать нумерацию раундов. Храним
            # монотонный счётчик нарративных раундов в самой БД сессии,
            # чтобы он переживал перезапуск бота.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)
            # ── Validation Logs (anti-cheat) ──────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS validation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT DEFAULT '',
                    validation_type TEXT DEFAULT 'full',
                    is_valid INTEGER DEFAULT 1,
                    severity TEXT DEFAULT 'info',
                    message TEXT DEFAULT '',
                    suggestion TEXT DEFAULT '',
                    details_json TEXT DEFAULT '{}',
                    dm_override INTEGER DEFAULT 0,
                    override_reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_session ON history(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_players_session ON players(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_characters_session ON characters(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hp_log_session ON hp_log(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conditions_session ON conditions(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quests_session ON quests(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_session ON inventory(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_factions_session ON factions(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_world_events_session ON world_events(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_npc_memory_session ON npc_memory(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_srd_monsters_name ON srd_monsters(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_srd_items_name ON srd_items(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_srd_spells_name ON srd_spells(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_locations_session ON locations(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_npcs_session ON npcs(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_npcs_location ON npcs(location_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lore_session ON lore_articles(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_market_session ON market_prices(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_economic_session ON economic_events(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_effects_session ON active_effects(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timers_session ON timers(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_db_journal_session ON db_journal(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_entries_session ON memory_entries(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_location_relations_session ON location_relations(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_location_relations_char ON location_relations(character_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_character_goals_session ON character_goals(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_character_goals_char ON character_goals(character_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_logs_session ON validation_logs(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_logs_char ON validation_logs(character_id)")

    # ═══════════════════════════════════════════════════════════
    # Session meta (key-value store) — фича 3: нумерация раундов
    # ═══════════════════════════════════════════════════════════

    def get_meta(self, key: str, default: str = "") -> str:
        """Read a value from the per-session key-value store."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM session_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row and row["value"] is not None else default

    def set_meta(self, key: str, value: str) -> None:
        """Write a value into the per-session key-value store (upsert)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def increment_meta(self, key: str, default: str = "0") -> int:
        """Atomically increment an integer meta value and return the NEW value.

        БОЙ-FIX (гонка раундов): advance_round() раньше делал read-modify-write
        двумя отдельными запросами (get_meta → +1 в Python → set_meta). Когда
        боевой цикл и dual-narrative резолвер работали параллельно, оба читали
        одно и то же значение и оба писали одно и то же «+1» — классический
        lost update, отсюда дублирующиеся и скачущие номера раундов в истории
        ([Раунд 2] дважды, 1→3→5→7→8→6→2). Теперь инкремент выполняется ОДНИМ
        UPSERT-стейтментом внутри одной транзакции: SQLite держит блокировку
        записи до коммита, поэтому параллельный писатель физически не может
        вклиниться между чтением и записью.

        Нечисловое старое значение («banana», битая запись после падения)
        трактуется SQLite CAST-ом как 0 — счётчик самовосстанавливается.
        """
        try:
            base = int(default)
        except (TypeError, ValueError):
            base = 0
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO session_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
                (key, str(base + 1)),
            )
            row = conn.execute(
                "SELECT value FROM session_meta WHERE key = ?", (key,)
            ).fetchone()
        try:
            return int(row["value"]) if row and row["value"] is not None else base
        except (TypeError, ValueError):
            return base

    # ═══════════════════════════════════════════════════════════
    # DB Journal — NEW METHODS
    # ═══════════════════════════════════════════════════════════


