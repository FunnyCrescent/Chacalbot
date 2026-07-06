"""
Database Layer — SQLite for long campaigns
Tables: sessions, players, characters, history, queue_state, db_journal
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """Game session"""
    id: str
    chat_id: int
    name: str
    creator_id: int
    status: str
    current_scene: str
    combat_active: bool = False
    initiative_order: str = ""
    current_turn_index: int = 0
    round_number: int = 0
    pvp_active: bool = False
    autostart: bool = False
    summary: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Player:
    """Player in a session"""
    user_id: int
    session_id: str
    username: str
    display_name: str
    is_creator: bool = False
    joined_at: str = ""


@dataclass
class Character:
    """D&D Character"""
    id: str
    session_id: str
    player_id: int
    name: str
    race: str
    class_name: str
    level: int = 1
    hp: int = 0
    max_hp: int = 0
    ac: int = 10
    stats: str = ""
    proficiencies: str = ""
    inventory: str = ""
    spells: str = ""
    features: str = ""
    backstory: str = ""
    death_saves_success: int = 0
    death_saves_failure: int = 0
    is_alive: bool = True
    conditions: str = ""


@dataclass
class HistoryEntry:
    """Session history entry"""
    session_id: str
    author: str
    content: str
    entry_type: str = "narrative"
    id: int = 0
    created_at: str = ""


@dataclass
class QueueState:
    """Current queue state for a session"""
    session_id: str
    waiting_for: str = ""
    collected_actions: str = ""
    is_resolving: bool = False


@dataclass
class CharacterSheet:
    """Full character sheet text uploaded by player"""
    session_id: str
    player_id: int
    sheet_text: str = ""
    file_name: str = ""


@dataclass
class HpLog:
    id: int
    session_id: str
    character_id: str
    character_name: str
    old_hp: int
    new_hp: int
    change: int
    source: str
    created_at: str


@dataclass
class ConditionEntry:
    id: int
    session_id: str
    character_id: str
    character_name: str
    condition: str
    source: str
    duration: str
    created_at: str
    expires_at: str = ""


@dataclass
class RestEntry:
    id: int
    session_id: str
    character_id: str
    character_name: str
    rest_type: str
    hp_restored: int
    hit_dice_used: int
    abilities_recovered: str
    created_at: str


@dataclass
class GoldEntry:
    id: int
    session_id: str
    character_id: str
    character_name: str
    delta_cp: int = 0
    delta_sp: int = 0
    delta_ep: int = 0
    delta_gp: int = 0
    delta_pp: int = 0
    reason: str = ""
    created_at: str = ""


@dataclass
class QuestEntry:
    id: int
    session_id: str
    assignee_id: str
    assignee_name: str
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str = ""


@dataclass
class GameTime:
    session_id: str
    day: int = 1
    hour: int = 8
    minute: int = 0
    weather: str = "clear"
    season: str = "summer"
    temperature: str = "mild"
    last_updated: str = ""


@dataclass
class FactionEntry:
    id: int
    session_id: str
    name: str
    description: str
    reputation: int
    attitude: str
    created_at: str


@dataclass
class FactionRelation:
    id: int
    session_id: str
    faction_id: int
    character_id: str
    standing: int
    notes: str
    created_at: str


@dataclass
class WorldEvent:
    id: int
    session_id: str
    event_type: str
    description: str
    is_resolved: bool
    created_at: str
    resolved_at: str = ""


@dataclass
class SrdCache:
    query: str
    response: str
    created_at: str


@dataclass
class LocationBinding:
    session_id: str
    character_id: str
    location_name: str
    location_description: str
    updated_at: str


@dataclass
class CharacterResources:
    id: int
    session_id: str
    character_id: str
    resource_name: str
    current: int
    maximum: int
    short_rest_recover: bool
    long_rest_recover: bool
    updated_at: str




@dataclass
class SrdMonster:
    id: str
    name: str
    cr: str = ""
    type: str = ""
    size: str = ""
    ac: int = 10
    hp_avg: int = 0
    hp_formula: str = ""
    speed: str = ""
    stats: str = "{}"
    abilities: str = "[]"
    actions: str = "[]"
    legendary_actions: str = "[]"
    loot_table_id: str = ""
    lore_id: str = ""
    source: str = "SRD"


@dataclass
class SrdItem:
    id: str
    name: str
    category: str = ""
    rarity: str = "common"
    type: str = ""
    description: str = ""
    mechanics: str = "{}"
    base_price_gp: int = 0
    weight: float = 0.0
    is_magical: bool = False
    attunement_required: bool = False
    source: str = "SRD"


@dataclass
class SrdSpell:
    id: str
    name: str
    level: int = 0
    school: str = ""
    casting_time: str = ""
    range: str = ""
    duration: str = ""
    components: str = ""
    description: str = ""
    higher_levels: str = ""
    classes: str = "[]"
    source: str = "SRD"


@dataclass
class Location:
    id: str
    session_id: str
    name: str
    description: str = ""
    type: str = "wilderness"
    parent_location_id: str = ""
    danger_level: int = 1
    discovered_items: str = "[]"
    current_occupants: str = "[]"
    weather_effect: str = ""
    is_discovered: bool = True
    created_at: str = ""


@dataclass
class LocationPath:
    id: str
    from_location_id: str
    to_location_id: str
    travel_hours: int = 1
    danger_encounters: str = "[]"
    is_blocked: bool = False
    block_reason: str = ""
    created_at: str = ""


@dataclass
class WorldNpc:
    id: str
    session_id: str
    name: str
    race: str = ""
    occupation: str = ""
    location_id: str = ""
    personality: str = "{}"
    schedule: str = "{}"
    is_alive: bool = True
    backstory: str = ""
    secrets: str = "[]"
    faction_id: int = 0
    traits: str = "[]"
    known_facts: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class NpcRelation:
    id: int = 0
    session_id: str = ""
    npc_id: str = ""
    character_id: str = ""
    reputation: int = 0
    attitude: str = "neutral"
    known_facts: str = ""
    last_interaction: str = ""
    grudges: str = "[]"
    debts: str = "[]"


@dataclass
class LoreArticle:
    id: str
    session_id: str
    title: str
    category: str = "general"
    content: str = ""
    tags: str = "[]"
    related_article_ids: str = "[]"
    discovered_by_session: str = ""
    created_at: str = ""


@dataclass
class MarketPrice:
    id: int = 0
    session_id: str = ""
    location_id: str = ""
    item_id: str = ""
    base_price_gp: int = 0
    current_price_gp: int = 0
    demand_factor: float = 1.0
    supply_factor: float = 1.0
    last_updated: str = ""


@dataclass
class EconomicEvent:
    id: int = 0
    session_id: str = ""
    name: str = ""
    description: str = ""
    affected_locations: str = "[]"
    price_multiplier: float = 1.0
    duration_days: int = 7
    is_active: bool = True
    created_at: str = ""


@dataclass
class ActiveEffect:
    id: int = 0
    session_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    name: str = ""
    effect_type: str = "curse"
    source: str = ""
    duration_type: str = "permanent"
    remaining: int = 0
    mechanics: str = "{}"
    is_removable: bool = True
    created_at: str = ""


@dataclass
class Timer:
    id: int = 0
    session_id: str = ""
    entity_type: str = ""
    entity_id: str = ""
    event_type: str = ""
    trigger_round: int = 0
    trigger_time: str = ""
    action: str = "{}"
    is_recurring: bool = False
    is_active: bool = True
    created_at: str = ""


@dataclass
class LootTable:
    id: int = 0
    name: str = ""
    min_cr: float = 0.0
    max_cr: float = 0.0
    loot_type: str = "individual"
    entries: str = "[]"
    created_at: str = ""



@dataclass
class DbJournalEntry:
    """Journal of database operations for AI context"""
    id: int = 0
    session_id: str = ""
    operation: str = ""  # INSERT, UPDATE, DELETE
    table_name: str = ""  # characters, hp_log, conditions, etc.
    record_id: str = ""  # ID of affected record
    details: str = ""  # Human-readable description
    created_at: str = ""


class Database:
    """SQLite database for campaign persistence"""

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
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
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
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
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
                    known_facts TEXT DEFAULT '',
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

    # ═══════════════════════════════════════════════════════════
    # DB Journal — NEW METHODS
    # ═══════════════════════════════════════════════════════════

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

    def create_session(self, session: Session) -> Session:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO sessions (id, chat_id, name, creator_id, status, current_scene)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session.id, session.chat_id, session.name, session.creator_id,
                 session.status, session.current_scene)
            )
        self.add_journal_entry(session.id, "INSERT", "sessions", session.id, f"Created session {session.name}")
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def get_session_by_chat(self, chat_id: int) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status = 'active'",
                (chat_id,)
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def get_all_active_sessions(self) -> List[Session]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active'"
            ).fetchall()
            return [self._row_to_session(r) for r in rows]

    def update_session(self, session: Session):
        with self._connect() as conn:
            conn.execute(
                """UPDATE sessions SET
                    name = ?, status = ?, current_scene = ?, combat_active = ?,
                    initiative_order = ?, current_turn_index = ?, round_number = ?,
                    autostart = ?, pvp_active = ?, summary = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (session.name, session.status, session.current_scene,
                 int(session.combat_active), session.initiative_order,
                 session.current_turn_index, session.round_number,
                 int(session.autostart), int(session.pvp_active), session.summary, session.id)
            )
        self.add_journal_entry(session.id, "UPDATE", "sessions", session.id, "Updated session state")

    def end_session(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'ended', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,)
            )
        self.add_journal_entry(session_id, "UPDATE", "sessions", session_id, "Session ended")

    # ═══════════════════════════════════════════════════════════
    # Player Management
    # ═══════════════════════════════════════════════════════════

    def add_player(self, player: Player) -> Player:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (user_id, session_id, username, display_name, is_creator)
                   VALUES (?, ?, ?, ?, ?)""",
                (player.user_id, player.session_id, player.username,
                 player.display_name, int(player.is_creator))
            )
        self.add_journal_entry(player.session_id, "INSERT", "players", str(player.user_id),
                               f"Player {player.display_name} joined")
        return player

    def remove_player(self, user_id: int, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM players WHERE user_id = ? AND session_id = ?",
                (user_id, session_id)
            )
            conn.execute(
                "DELETE FROM characters WHERE player_id = ? AND session_id = ?",
                (user_id, session_id)
            )
        self.add_journal_entry(session_id, "DELETE", "players", str(user_id), "Player left")

    def get_players(self, session_id: str) -> List[Player]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM players WHERE session_id = ? ORDER BY joined_at",
                (session_id,)
            ).fetchall()
            return [self._row_to_player(r) for r in rows]

    def get_player(self, user_id: int, session_id: str) -> Optional[Player]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM players WHERE user_id = ? AND session_id = ?",
                (user_id, session_id)
            ).fetchone()
            if row:
                return self._row_to_player(row)
            return None

    # ═══════════════════════════════════════════════════════════
    # Character Management
    # ═══════════════════════════════════════════════════════════

    def save_character(self, character: Character) -> Character:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO characters
                   (id, session_id, player_id, name, race, class_name, level,
                    hp, max_hp, ac, stats, proficiencies, inventory, spells,
                    features, backstory, death_saves_success, death_saves_failure,
                    is_alive, conditions)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (character.id, character.session_id, character.player_id,
                 character.name, character.race, character.class_name,
                 character.level, character.hp, character.max_hp, character.ac,
                 character.stats, character.proficiencies, character.inventory,
                 character.spells, character.features, character.backstory,
                 character.death_saves_success, character.death_saves_failure,
                 int(character.is_alive), character.conditions)
            )
        self.add_journal_entry(character.session_id, "INSERT", "characters", character.id,
                               f"Character {character.name} saved")
        return character

    def get_character(self, character_id: str) -> Optional[Character]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            if row:
                return self._row_to_character(row)
            return None

    def get_character_by_player(self, player_id: int, session_id: str) -> Optional[Character]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE player_id = ? AND session_id = ?",
                (player_id, session_id)
            ).fetchone()
            if row:
                return self._row_to_character(row)
            return None

    def get_session_characters(self, session_id: str) -> List[Character]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM characters WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [self._row_to_character(r) for r in rows]

    def update_character_hp(self, character_id: str, hp: int):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET hp = ? WHERE id = ?",
                (hp, character_id)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"HP changed to {hp}")

    def update_character_death_saves(self, character_id: str, successes: int, failures: int):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET death_saves_success = ?, death_saves_failure = ? WHERE id = ?",
                (successes, failures, character_id)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"Death saves: {successes}S / {failures}F")

    def kill_character(self, character_id: str):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET is_alive = 0, hp = 0 WHERE id = ?",
                (character_id,)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"Character {char.name} died")

    # ═══════════════════════════════════════════════════════════
    # Character Sheets (full text)
    # ═══════════════════════════════════════════════════════════

    def save_character_sheet(self, session_id: str, player_id: int, sheet_text: str, file_name: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO character_sheets
                   (session_id, player_id, sheet_text, file_name)
                   VALUES (?, ?, ?, ?)""",
                (session_id, player_id, sheet_text, file_name)
            )
        self.add_journal_entry(session_id, "INSERT", "character_sheets", f"{player_id}",
                               f"Sheet uploaded by player {player_id}")

    def get_character_sheet(self, session_id: str, player_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sheet_text FROM character_sheets WHERE session_id = ? AND player_id = ?",
                (session_id, player_id)
            ).fetchone()
            return row["sheet_text"] if row else None

    def get_all_character_sheets(self, session_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sheet_text FROM character_sheets WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [r["sheet_text"] for r in rows if r["sheet_text"]]

    def remove_character_sheet(self, session_id: str, player_id: int):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM character_sheets WHERE session_id = ? AND player_id = ?",
                (session_id, player_id)
            )

    # ═══════════════════════════════════════════════════════════
    # History
    # ═══════════════════════════════════════════════════════════

    def add_history(self, entry: HistoryEntry) -> HistoryEntry:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO history (session_id, author, content, entry_type)
                   VALUES (?, ?, ?, ?)""",
                (entry.session_id, entry.author, entry.content, entry.entry_type)
            )
            entry.id = cursor.lastrowid
        return entry

    def get_history(self, session_id: str, limit: int = 50) -> List[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM history WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit)
            ).fetchall()
            return [self._row_to_history(r) for r in reversed(rows)]

    def get_history_by_type(self, session_id: str, entry_type: str, limit: int = 20) -> List[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM history WHERE session_id = ? AND entry_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, entry_type, limit)
            ).fetchall()
            return [self._row_to_history(r) for r in reversed(rows)]

    def clear_history(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM history WHERE session_id = ?",
                (session_id,)
            )
        self.add_journal_entry(session_id, "DELETE", "history", "", "Cleared history")

    # ═══════════════════════════════════════════════════════════
    # Queue State
    # ═══════════════════════════════════════════════════════════

    def get_queue_state(self, session_id: str) -> Optional[QueueState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_state WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row:
                return QueueState(
                    session_id=row["session_id"],
                    waiting_for=row["waiting_for"],
                    collected_actions=row["collected_actions"],
                    is_resolving=bool(row["is_resolving"]),
                )
            return None

    def set_queue_state(self, state: QueueState):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO queue_state
                   (session_id, waiting_for, collected_actions, is_resolving)
                   VALUES (?, ?, ?, ?)""",
                (state.session_id, state.waiting_for,
                 state.collected_actions, int(state.is_resolving))
            )

    def clear_queue_state(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM queue_state WHERE session_id = ?",
                (session_id,)
            )

    # ═══════════════════════════════════════════════════════════
    # Row Converters
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            chat_id=row["chat_id"],
            name=row["name"],
            creator_id=row["creator_id"],
            status=row["status"],
            current_scene=row["current_scene"] or "",
            combat_active=bool(row["combat_active"]),
            initiative_order=row["initiative_order"] or "[]",
            current_turn_index=row["current_turn_index"],
            round_number=row["round_number"],
            pvp_active=bool(row["pvp_active"]),
            autostart=bool(row["autostart"]),
            summary=row["summary"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @staticmethod
    def _row_to_player(row: sqlite3.Row) -> Player:
        return Player(
            user_id=row["user_id"],
            session_id=row["session_id"],
            username=row["username"] or "",
            display_name=row["display_name"],
            is_creator=bool(row["is_creator"]),
            joined_at=row["joined_at"] or "",
        )

    @staticmethod
    def _row_to_character(row: sqlite3.Row) -> Character:
        return Character(
            id=row["id"],
            session_id=row["session_id"],
            player_id=row["player_id"],
            name=row["name"],
            race=row["race"] or "",
            class_name=row["class_name"] or "",
            level=row["level"],
            hp=row["hp"],
            max_hp=row["max_hp"],
            ac=row["ac"],
            stats=row["stats"] or "{}",
            proficiencies=row["proficiencies"] or "[]",
            inventory=row["inventory"] or "[]",
            spells=row["spells"] or "[]",
            features=row["features"] or "[]",
            backstory=row["backstory"] or "",
            death_saves_success=row["death_saves_success"],
            death_saves_failure=row["death_saves_failure"],
            is_alive=bool(row["is_alive"]),
            conditions=row["conditions"] or "[]",
        )

    @staticmethod
    def _row_to_history(row: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            session_id=row["session_id"],
            author=row["author"],
            content=row["content"],
            entry_type=row["entry_type"],
            id=row["id"],
            created_at=row["created_at"] or "",
        )
    @staticmethod
    def _row_to_world_npc(row: sqlite3.Row) -> WorldNpc:
        return WorldNpc(
            id=row["id"],
            session_id=row["session_id"],
            name=row["name"],
            race=row["race"] or "",
            occupation=row["occupation"] or "",
            location_id=row["location_id"] or "",
            personality=row["personality"] or "{}",
            schedule=row["schedule"] or "{}",
            is_alive=bool(row["is_alive"]),
            backstory=row["backstory"] or "",
            secrets=row["secrets"] or "[]",
            faction_id=row["faction_id"] or 0,
            traits=row["traits"] or "[]",
            known_facts=row["known_facts"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


    # ═══════════════════════════════════════════════════════════
    # HP Tracking (#1)
    # ═══════════════════════════════════════════════════════════

    def add_hp_log(self, session_id: str, character_id: str, character_name: str,
                   old_hp: int, new_hp: int, source: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO hp_log (session_id, character_id, character_name, old_hp, new_hp, change, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, old_hp, new_hp, new_hp - old_hp, source)
            )
        self.add_journal_entry(session_id, "INSERT", "hp_log", character_id,
                               f"{character_name}: {old_hp} -> {new_hp} HP ({source})")

    def get_hp_log(self, session_id: str, character_id: str = None, limit: int = 20) -> List[HpLog]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM hp_log WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, character_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hp_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit)
                ).fetchall()
            return [HpLog(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], old_hp=r["old_hp"], new_hp=r["new_hp"],
                change=r["change"], source=r["source"] or "", created_at=r["created_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Conditions (#3)
    # ═══════════════════════════════════════════════════════════

    def add_condition(self, session_id: str, character_id: str, character_name: str,
                      condition: str, source: str = "", duration: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conditions (session_id, character_id, character_name, condition, source, duration)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, condition.lower(), source, duration)
            )
        self.add_journal_entry(session_id, "INSERT", "conditions", character_id,
                               f"{character_name} gained {condition} ({source})")

    def remove_condition(self, session_id: str, character_id: str, condition: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM conditions WHERE session_id = ? AND character_id = ? AND condition = ?",
                (session_id, character_id, condition.lower())
            )
        self.add_journal_entry(session_id, "DELETE", "conditions", character_id,
                               f"Removed {condition}")

    def get_conditions(self, session_id: str, character_id: str = None) -> List[ConditionEntry]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM conditions WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC",
                    (session_id, character_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conditions WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,)
                ).fetchall()
            return [ConditionEntry(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], condition=r["condition"], source=r["source"] or "",
                duration=r["duration"] or "", created_at=r["created_at"] or "", expires_at=r["expires_at"] or ""
            ) for r in rows]

    def remove_all_conditions(self, session_id: str, character_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM conditions WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            )
        self.add_journal_entry(session_id, "DELETE", "conditions", character_id, "All conditions cleared")

    # ═══════════════════════════════════════════════════════════
    # Rest (#4)
    # ═══════════════════════════════════════════════════════════

    def add_rest(self, session_id: str, character_id: str, character_name: str,
                 rest_type: str, hp_restored: int, hit_dice_used: int, abilities_recovered: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO rest_log (session_id, character_id, character_name, rest_type, hp_restored, hit_dice_used, abilities_recovered)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, rest_type, hp_restored, hit_dice_used, abilities_recovered)
            )
        self.add_journal_entry(session_id, "INSERT", "rest_log", character_id,
                               f"{character_name}: {rest_type} rest, +{hp_restored} HP")

    def get_rest_history(self, session_id: str, character_id: str = None, limit: int = 10) -> List[RestEntry]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM rest_log WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, character_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rest_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit)
                ).fetchall()
            return [RestEntry(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], rest_type=r["rest_type"],
                hp_restored=r["hp_restored"], hit_dice_used=r["hit_dice_used"],
                abilities_recovered=r["abilities_recovered"] or "", created_at=r["created_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Gold & Inventory (#9)
    # ═══════════════════════════════════════════════════════════

    def add_gold_transaction(self, session_id: str, character_id: str, character_name: str,
                             cp: int = 0, sp: int = 0, ep: int = 0, gp: int = 0, pp: int = 0, reason: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO gold_log (session_id, character_id, character_name, delta_cp, delta_sp, delta_ep, delta_gp, delta_pp, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, cp, sp, ep, gp, pp, reason)
            )
        self.add_journal_entry(session_id, "INSERT", "gold_log", character_id,
                               f"{character_name}: {gp:+d}gp ({reason})")

    def get_gold_balance(self, session_id: str, character_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(delta_cp),0) as cp, COALESCE(SUM(delta_sp),0) as sp,
                          COALESCE(SUM(delta_ep),0) as ep, COALESCE(SUM(delta_gp),0) as gp,
                          COALESCE(SUM(delta_pp),0) as pp
                   FROM gold_log WHERE session_id = ? AND character_id = ?""",
                (session_id, character_id)
            ).fetchone()
            return {"cp": row["cp"], "sp": row["sp"], "ep": row["ep"], "gp": row["gp"], "pp": row["pp"]}

    def add_inventory_item(self, session_id: str, character_id: str, character_name: str,
                           item_name: str, quantity: int = 1, description: str = ""):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, quantity FROM inventory WHERE session_id = ? AND character_id = ? AND LOWER(item_name) = LOWER(?)",
                (session_id, character_id, item_name)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (existing["quantity"] + quantity, existing["id"])
                )
            else:
                conn.execute(
                    """INSERT INTO inventory (session_id, character_id, character_name, item_name, quantity, description)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, character_id, character_name, item_name, quantity, description)
                )
        self.add_journal_entry(session_id, "INSERT", "inventory", character_id,
                               f"{character_name} gained {item_name} x{quantity}")

    def remove_inventory_item(self, session_id: str, character_id: str, item_name: str, quantity: int = 1):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, quantity FROM inventory WHERE session_id = ? AND character_id = ? AND LOWER(item_name) = LOWER(?)",
                (session_id, character_id, item_name)
            ).fetchone()
            if existing:
                new_qty = existing["quantity"] - quantity
                if new_qty <= 0:
                    conn.execute("DELETE FROM inventory WHERE id = ?", (existing["id"],))
                else:
                    conn.execute(
                        "UPDATE inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_qty, existing["id"])
                    )
        self.add_journal_entry(session_id, "DELETE", "inventory", character_id,
                               f"Removed {item_name} x{quantity}")

    def get_inventory(self, session_id: str, character_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inventory WHERE session_id = ? AND character_id = ? ORDER BY item_name",
                (session_id, character_id)
            ).fetchall()
            return [{"item": r["item_name"], "qty": r["quantity"], "desc": r["description"] or ""} for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Quests (#10, #17)
    # ═══════════════════════════════════════════════════════════

    def add_quest(self, session_id: str, title: str, description: str = "",
                  assignee_id: str = "", assignee_name: str = "", status: str = "active"):
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO quests (session_id, assignee_id, assignee_name, title, description, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, assignee_id, assignee_name, title, description, status)
            )
            quest_id = cursor.lastrowid
        self.add_journal_entry(session_id, "INSERT", "quests", str(quest_id), f"Quest added: {title}")
        return quest_id

    def update_quest(self, quest_id: int, status: str = None, title: str = None, description: str = None):
        with self._connect() as conn:
            if status:
                conn.execute("UPDATE quests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (status, quest_id))
            if title:
                conn.execute("UPDATE quests SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (title, quest_id))
            if description:
                conn.execute("UPDATE quests SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (description, quest_id))
        self.add_journal_entry("", "UPDATE", "quests", str(quest_id), f"Quest #{quest_id} updated")

    def get_quests(self, session_id: str, status: str = None, assignee_id: str = None) -> List[QuestEntry]:
        with self._connect() as conn:
            query = "SELECT * FROM quests WHERE session_id = ?"
            params = [session_id]
            if status:
                query += " AND status = ?"
                params.append(status)
            if assignee_id:
                query += " AND assignee_id = ?"
                params.append(assignee_id)
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [QuestEntry(
                id=r["id"], session_id=r["session_id"], assignee_id=r["assignee_id"] or "",
                assignee_name=r["assignee_name"] or "", title=r["title"],
                description=r["description"] or "", status=r["status"] or "active",
                created_at=r["created_at"] or "", updated_at=r["updated_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Game Time & Weather (#11)
    # ═══════════════════════════════════════════════════════════

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

    def set_location(self, session_id: str, character_id: str, location_name: str, location_description: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO location_bindings
                   (session_id, character_id, location_name, location_description, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (session_id, character_id, location_name, location_description)
            )
        self.add_journal_entry(session_id, "UPDATE", "location_bindings", character_id,
                               f"Location: {location_name}")

    def get_location(self, session_id: str, character_id: str) -> Optional[LocationBinding]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM location_bindings WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            ).fetchone()
            if row:
                return LocationBinding(
                    session_id=row["session_id"], character_id=row["character_id"],
                    location_name=row["location_name"] or "", location_description=row["location_description"] or "",
                    updated_at=row["updated_at"] or ""
                )
            return None

    def get_all_locations(self, session_id: str) -> List[LocationBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM location_bindings WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [LocationBinding(
                session_id=r["session_id"], character_id=r["character_id"],
                location_name=r["location_name"] or "", location_description=r["location_description"] or "",
                updated_at=r["updated_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Character Resources (#20)
    # ═══════════════════════════════════════════════════════════

    def set_resource(self, session_id: str, character_id: str, resource_name: str,
                     current: int, maximum: int, short_rest: bool = False, long_rest: bool = True):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO character_resources
                   (session_id, character_id, resource_name, current, maximum, short_rest_recover, long_rest_recover, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (session_id, character_id, resource_name.lower(), current, maximum,
                 int(short_rest), int(long_rest))
            )
        self.add_journal_entry(session_id, "INSERT", "character_resources", character_id,
                               f"Resource {resource_name}: {current}/{maximum}")

    def get_resources(self, session_id: str, character_id: str) -> List[CharacterResources]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM character_resources WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            ).fetchall()
            return [CharacterResources(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                resource_name=r["resource_name"], current=r["current"], maximum=r["maximum"],
                short_rest_recover=bool(r["short_rest_recover"]), long_rest_recover=bool(r["long_rest_recover"]),
                updated_at=r["updated_at"] or ""
            ) for r in rows]

    def update_resource(self, session_id: str, character_id: str, resource_name: str, delta: int):
        with self._connect() as conn:
            conn.execute(
                """UPDATE character_resources SET current = MAX(0, MIN(maximum, current + ?)),
                   updated_at = CURRENT_TIMESTAMP
                   WHERE session_id = ? AND character_id = ? AND resource_name = ?""",
                (delta, session_id, character_id, resource_name.lower())
            )
        self.add_journal_entry(session_id, "UPDATE", "character_resources", character_id,
                               f"Resource {resource_name} {delta:+d}")

    def reset_resources(self, session_id: str, character_id: str, rest_type: str):
        with self._connect() as conn:
            if rest_type == "short":
                conn.execute(
                    """UPDATE character_resources SET current = maximum
                       WHERE session_id = ? AND character_id = ? AND short_rest_recover = 1""",
                    (session_id, character_id)
                )
            elif rest_type == "long":
                conn.execute(
                    """UPDATE character_resources SET current = maximum
                       WHERE session_id = ? AND character_id = ? AND long_rest_recover = 1""",
                    (session_id, character_id)
                )
        self.add_journal_entry(session_id, "UPDATE", "character_resources", character_id,
                               f"Resources reset after {rest_type} rest")

    # ═══════════════════════════════════════════════════════════
    # NPC Memory (#21)
    # ═══════════════════════════════════════════════════════════

    def add_npc(self, session_id: str, npc_name: str, personality_pattern: str = "",
                known_facts: str = "", relationships: str = ""):
        npc_id = str(uuid.uuid4())[:8]
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO npcs
                   (id, session_id, name, personality, known_facts, backstory, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (npc_id, session_id, npc_name, json.dumps({"traits": personality_pattern}), known_facts, relationships)
            )
        self.add_journal_entry(session_id, "INSERT", "npcs", npc_id, f"NPC: {npc_name}")

    def get_npc(self, session_id: str, npc_name: str) -> Optional[WorldNpc]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM npcs WHERE session_id = ? AND name = ? COLLATE NOCASE",
                (session_id, npc_name)
            ).fetchone()
            if row:
                return self._row_to_world_npc(row)
            return None

    def get_all_npcs(self, session_id: str) -> List[WorldNpc]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM npcs WHERE session_id = ? ORDER BY name",
                (session_id,)
            ).fetchall()
            return [self._row_to_world_npc(r) for r in rows]

    def update_npc_facts(self, session_id: str, npc_name: str, new_facts: str):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT known_facts FROM npcs WHERE session_id = ? AND name = ? COLLATE NOCASE",
                (session_id, npc_name)
            ).fetchone()
            if existing:
                facts = (existing["known_facts"] or "") + "\n" + new_facts
                conn.execute(
                    "UPDATE npcs SET known_facts = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ? AND name = ?",
                    (facts, session_id, npc_name)
                )
        self.add_journal_entry(session_id, "UPDATE", "npcs", "", f"NPC facts updated: {npc_name}")

    # ═══════════════════════════════════════════════════════════
    # Roll Mode (#6)
    # ═══════════════════════════════════════════════════════════

    def get_roll_mode(self, session_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT mode FROM roll_mode WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            return row["mode"] if row else "mixed"

    def set_roll_mode(self, session_id: str, mode: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO roll_mode (session_id, mode) VALUES (?, ?)",
                (session_id, mode)
            )

    # ═══════════════════════════════════════════════════════════
    # SRD — Static Reference Data
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
    # Locations
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

    def set_npc_relation(self, relation: NpcRelation) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR REPLACE INTO npc_relations
                   (session_id, npc_id, character_id, reputation, attitude, known_facts, last_interaction, grudges, debts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (relation.session_id, relation.npc_id, relation.character_id, relation.reputation, relation.attitude, relation.known_facts, relation.last_interaction, relation.grudges, relation.debts)
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

    # ═══════════════════════════════════════════════════════════
    # Lore
    # ═══════════════════════════════════════════════════════════

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

    def add_effect(self, effect: ActiveEffect) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO active_effects
                   (session_id, entity_type, entity_id, name, effect_type, source, duration_type, remaining, mechanics, is_removable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (effect.session_id, effect.entity_type, effect.entity_id, effect.name, effect.effect_type, effect.source, effect.duration_type, effect.remaining, effect.mechanics, int(effect.is_removable))
            )
            effect_id = cursor.lastrowid
        self.add_journal_entry(effect.session_id, "INSERT", "active_effects", str(effect_id), f"Effect: {effect.name} on {effect.entity_id}")
        return effect_id

    def get_effects(self, session_id: str, entity_type: str = None, entity_id: str = None) -> List[ActiveEffect]:
        with self._connect() as conn:
            query = "SELECT * FROM active_effects WHERE session_id = ?"
            params = [session_id]
            if entity_type:
                query += " AND entity_type = ?"
                params.append(entity_type)
            if entity_id:
                query += " AND entity_id = ?"
                params.append(entity_id)
            rows = conn.execute(query, params).fetchall()
            return [ActiveEffect(id=r["id"], session_id=r["session_id"], entity_type=r["entity_type"], entity_id=r["entity_id"], name=r["name"], effect_type=r["effect_type"], source=r["source"], duration_type=r["duration_type"], remaining=r["remaining"], mechanics=r["mechanics"], is_removable=bool(r["is_removable"]), created_at=r["created_at"] or "") for r in rows]

    def remove_effect(self, effect_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM active_effects WHERE id = ?", (effect_id,))

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
# DatabaseManager — per-session DB factory
# ═══════════════════════════════════════════════════════════

import os


class DatabaseManager:
    """Factory + registry: one Database per session."""

    def __init__(self, base_dir: str = "data/sessions"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Database] = {}
        self._chat_to_session: Dict[int, str] = {}
        self._load_active_sessions()

    def _db_path(self, session_id: str) -> str:
        return str(self.base_dir / f"{session_id}.db")

    def get_db(self, session_id: str) -> Database:
        if session_id not in self._cache:
            self._cache[session_id] = Database(self._db_path(session_id))
        return self._cache[session_id]

    def _load_active_sessions(self):
        for db_file in self.base_dir.glob("*.db"):
            sid = db_file.stem
            try:
                db = Database(str(db_file))
                for session in db.get_all_active_sessions():
                    self._chat_to_session[session.chat_id] = session.id
            except Exception:
                continue

    def get_session_by_chat(self, chat_id: int) -> Optional[Session]:
        sid = self._chat_to_session.get(chat_id)
        if sid:
            return self.get_db(sid).get_session(sid)
        return None

    def create_session(self, session: Session) -> Session:
        db = self.get_db(session.id)
        db.create_session(session)
        self._chat_to_session[session.chat_id] = session.id
        return session

    def get_all_active_sessions(self) -> List[Session]:
        result = []
        for sid in list(self._chat_to_session.values()):
            s = self.get_db(sid).get_session(sid)
            if s and s.status == "active":
                result.append(s)
        return result

    def end_session(self, session_id: str):
        db = self.get_db(session_id)
        session = db.get_session(session_id)
        if session:
            db.end_session(session_id)
            db.clear_queue_state(session_id)
            self._chat_to_session.pop(session.chat_id, None)

    def clear_history(self, session_id: str):
        self.get_db(session_id).clear_history(session_id)
