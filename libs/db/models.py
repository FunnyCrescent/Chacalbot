"""Все dataclasses для D&D бота (40 классов)."""
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any

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
    summary_at_count: int = 0   # total history row count at last auto-summary — prevents re-firing every turn
    genre: str = ""            # selected setting/genre ID (e.g. "medieval_fantasy", "witcher_world")
    message_thread_id: int = 0  # BUG #2: Telegram forum/topic thread id — replies must go back to the same topic
    currency_name: str = ""    # Primary currency name (e.g. "Серебро", "Золото")
    currency_plural: str = ""  # Plural form
    currency_symbol: str = ""  # Short symbol (e.g. "зм", "см")
    currency_sub_name: str = ""  # Sub-currency name (e.g. "Медь")
    currency_sub_plural: str = ""
    currency_sub_symbol: str = ""
    currency_sub_value: float = 0.1  # How many primary per 1 sub (e.g. 10 copper = 1 silver)
    currency_super_name: str = ""  # Super-currency (e.g. "Платина")
    currency_super_plural: str = ""
    currency_super_symbol: str = ""
    currency_super_value: float = 10.0  # How many primary per 1 super
    # ИТЕРАЦИЯ 10 (Разделы 6-7): способ оплаты сессии. Выбирается ОДИН раз в
    # меню /newydd и фиксируется на всю жизнь сессии — менять нельзя, кто бы
    # ни заходил/выходил позже. "split" = стоимость раунда делится поровну
    # между активными игроками; "creator_pays" = полностью платит создатель.
    billing_mode: str = "split"
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
    languages: str = "[]"
    # ИТЕРАЦИЯ 15: скрытый опыт (XP). Игроки его НЕ видят — только
    # Мастер-нейросеть (progression summary / контекст) и DB-бот
    # (db_state / get_character_state). Авто-уровневание — libs.xp_system.
    xp: int = 0

    # ── Convenience properties: read stats from the JSON `stats` field ──
    # These prevent AttributeError when code accidentally accesses char.strength
    # instead of json.loads(char.stats).get('strength', 10).

    def _stat(self, key: str, default: int = 10) -> int:
        """Read a single stat from the JSON `stats` field."""
        try:
            return json.loads(self.stats).get(key, default)
        except (json.JSONDecodeError, TypeError):
            return default

    @property
    def strength(self) -> int:
        return self._stat("strength")

    @property
    def dexterity(self) -> int:
        return self._stat("dexterity")

    @property
    def constitution(self) -> int:
        return self._stat("constitution")

    @property
    def intelligence(self) -> int:
        return self._stat("intelligence")

    @property
    def wisdom(self) -> int:
        return self._stat("wisdom")

    @property
    def charisma(self) -> int:
        return self._stat("charisma")


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
class NpcMemory:
    id: int
    session_id: str
    npc_name: str
    personality_pattern: str
    known_facts: str
    relationships: str
    created_at: str
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


@dataclass
class MemoryEntry:
    """One entry in the session's semantic 'world diary' (embedding-based memory)."""
    id: int = 0
    session_id: str = ""
    content: str = ""
    source_type: str = "narrative"  # narrative, npc_fact, lore, event, consolidated
    embedding: str = "[]"           # JSON list[float]
    confidence: float = 0.0         # -1 known-false, 0 default/theory, 1 ground-truth (immutable)
    score: float = 0.0              # importance weight, boosted on consolidation
    last_used: str = ""
    usage_count: int = 0
    created_at: str = ""


@dataclass
class LocationRelation:
    """What a LOCATION knows/thinks about a CHARACTER — fame, notoriety, wanted status.
    Direction is location -> character, not the reverse."""
    id: int = 0
    session_id: str = ""
    location_id: str = ""
    location_name: str = ""
    character_id: str = ""
    character_name: str = ""
    fame: int = 0            # -100..100, how well-known (negative = infamous)
    reputation: int = 0      # general standing, like faction reputation
    is_wanted: bool = False
    notoriety: str = ""      # free text: "плакаты о розыске за убийство", etc.
    last_interaction: str = ""
    created_at: str = ""


@dataclass
class CombatEncounter:
    """Combat encounter tracking"""
    id: str
    session_id: str
    reason: str = ""
    location_name: str = ""
    round_number: int = 0
    is_active: bool = True
    created_at: str = ""

@dataclass
class Combatant:
    """Participant in a combat encounter"""
    id: str
    encounter_id: str
    session_id: str
    name: str
    entity_type: str = "pc"  # "pc" | "npc" | "monster"
    player_id: int = 0  # 0 for NPCs/monsters
    initiative: int = 0
    natural_roll: int = 0
    dex_mod: int = 0
    hp: int = 0
    max_hp: int = 0
    ac: int = 10
    current_conditions: str = "[]"  # JSON list
    is_alive: bool = True
    traits: str = ""  # JSON: personality traits for AI NPC
    brief_context: str = ""  # short context for AI NPC decisions
    sort_order: int = 0  # position in initiative order
    # ИТЕРАЦИЯ 10 (Раздел 4): тай-брейк инициативы при РАВНЫХ значениях.
    # МЕНЬШЕ = ходит РАНЬШЕ. Виден только системе (в нарративе игроки видят
    # сырую инициативу без учёта priority — «оба 15», но Мастер сказал, кто
    # первый — это фиксируется явным полем, а не трюком +1/+2 к числу).
    priority: int = 0

@dataclass
class PlayerLanguage:
    """Player translation preferences"""
    session_id: str
    player_id: int
    language: str = ""  # target language code
    enabled: bool = False

@dataclass
class SettingEntry:
    """User-submitted or built-in game setting"""
    id: str
    name: str
    category: str = ""
    complexity_score: float = 0.0
    summary: str = ""  # DB-bot generated summary
    full_description: str = ""
    lore: str = ""  # optional lore content
    is_builtin: bool = True
    is_verified: bool = True
    created_by: int = 0  # player_id who submitted, 0 for built-in
    file_path: str = ""
    tags: str = "[]"  # JSON list

@dataclass
class RoundMessageTracker:
    """Tracks message IDs for anti-spam deletion"""
    session_id: str
    message_id: int
    chat_id: int
    player_id: int  # 0 for bot messages
    message_type: str = "dn"  # "dn", "ask", "bot_confirm", "bot_error", "narrative", "command"
    created_at: str = ""


@dataclass
class ValidationLog:
    """Log of anti-cheat validation results for a character."""
    id: int = 0
    session_id: str = ""
    character_id: str = ""
    character_name: str = ""
    validation_type: str = ""       # "full", "race", "class", "background", "backstory", "math"
    is_valid: bool = True
    severity: str = "info"          # "error", "warning", "info"
    message: str = ""
    suggestion: str = ""
    details_json: str = "{}"        # Full CharacterValidationResult as JSON
    dm_override: bool = False       # DM can override any validation result
    override_reason: str = ""
    created_at: str = ""
