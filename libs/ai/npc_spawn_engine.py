"""NpcSpawnEngine — validates and manages NPC spawning with location-type binding.

Ensures that NPCs are only placed in locations appropriate for their occupation.
Prevents the "merchant in the dungeon" bug by checking occupation vs location type
before any NPC is created.

See libs/rules/npc_spawn.md for the full rule specification.
"""
import json
import logging
import random
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any

from libs.db.models import WorldNpc, Location

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# OCCUPATION → ALLOWED LOCATION TYPES MAP
# ═══════════════════════════════════════════════════════════════════

OCCUPATION_LOCATION_MAP: Dict[str, List[str]] = {
    # Commerce
    "merchant":       ["market", "town_square", "shop", "bazaar", "port", "trading_post"],
    "trader":         ["market", "town_square", "shop", "bazaar", "port", "trading_post"],
    "shopkeeper":     ["shop", "market", "town_square"],
    "wandering_merchant": ["road", "crossroads", "wilderness", "village_edge"],
    "fence":          ["slums", "underground", "alley", "hideout", "black_market"],
    "thief_fence":    ["slums", "underground", "alley", "hideout", "black_market"],

    # Craft
    "blacksmith":     ["forge", "workshop", "market", "town_square"],
    "armorer":        ["forge", "workshop", "market", "town_square"],
    "weaponsmith":    ["forge", "workshop", "market", "town_square"],
    "jeweler":        ["shop", "market", "town_square", "workshop"],
    "leatherworker":  ["workshop", "market", "town_square"],
    "carpenter":      ["workshop", "market", "town_square"],
    "alchemist":      ["workshop", "tower", "market", "apothecary"],

    # Hospitality
    "innkeeper":      ["inn", "tavern", "roadhouse", "waystation"],
    "tavern_keeper":  ["inn", "tavern", "roadhouse", "waystation"],
    "bartender":      ["inn", "tavern", "roadhouse", "waystation"],

    # Military / Law
    "guard":          ["guard_post", "gate", "barracks", "patrol_route", "town_square"],
    "watchman":       ["guard_post", "gate", "barracks", "patrol_route", "town_square"],
    "soldier":        ["barracks", "guard_post", "fortress", "castle", "patrol_route"],
    "captain":        ["barracks", "guard_post", "fortress", "castle", "town_square"],
    "bounty_hunter":  ["tavern", "road", "wilderness", "barracks"],

    # Religion
    "priest":         ["temple", "shrine", "cathedral", "monastery"],
    "cleric_npc":     ["temple", "shrine", "cathedral", "monastery"],
    "acolyte":        ["temple", "shrine", "cathedral", "monastery"],
    "monk_npc":       ["monastery", "temple", "shrine"],

    # Knowledge
    "librarian":      ["library", "academy", "tower"],
    "scholar":        ["library", "academy", "tower", "monastery"],
    "sage":           ["library", "academy", "tower", "monastery"],
    "wizard_npc":     ["tower", "academy", "library"],

    # Healing
    "healer":         ["clinic", "temple", "market", "apothecary", "inn"],
    "apothecary":     ["apothecary", "market", "clinic"],

    # Nobility / Politics
    "noble":          ["manor", "castle", "court", "palace", "town_square"],
    "lord":           ["manor", "castle", "court", "palace"],
    "lady":           ["manor", "castle", "court", "palace"],
    "diplomat":       ["court", "palace", "manor", "town_square"],
    "magistrate":     ["court", "town_square", "castle"],

    # Underworld
    "thief":          ["slums", "underground", "alley", "hideout"],
    "assassin":       ["slums", "underground", "hideout", "alley"],
    "rogue_npc":      ["slums", "underground", "alley", "tavern", "hideout"],

    # Other / Special
    "farmer":         ["farm", "village_edge", "road", "market"],
    "fisherman":      ["port", "dock", "village_edge"],
    "miner":          ["mine", "cave", "forge"],
    "hunter":         ["wilderness", "forest", "village_edge", "road"],
    "herbalist":      ["wilderness", "forest", "market", "apothecary"],
    "entertainer":    ["tavern", "market", "town_square", "inn", "theater"],
    "beggar":         ["town_square", "market", "slums", "alley", "temple"],
    "refugee":        ["road", "village_edge", "camp", "slums"],
    "pilgrim":        ["road", "shrine", "temple", "waystation"],
    "prisoner":       ["dungeon_cell", "cage", "prison", "dungeon"],
}

# Location types where NO settlement NPCs should spawn (except via DM override)
WILDERNESS_LOCATION_TYPES = {
    "dungeon", "wilderness", "cave", "ruins", "lair", "swamp",
    "forest", "desert", "mountain", "ocean", "abyss",
}

# Dungeon-internal location types where specific dungeon NPCs are allowed
DUNGEON_INTERNAL_TYPES = {
    "dungeon_cell", "cage", "prison", "dungeon",
    "dungeon_safe_room", "trading_post", "refugee_camp",
}


@dataclass
class SpawnValidationResult:
    """Result of validating whether an NPC can spawn at a given location."""
    is_valid: bool
    reason: str = ""
    suggested_locations: List[str] = field(default_factory=list)
    npc_type: str = ""         # occupation that was checked
    location_type: str = ""    # location type that was checked
    is_wandering: bool = False # True if wandering merchant chance check passed
    dm_override: bool = False  # True if DM explicitly overrides


@dataclass
class NpcSpawnCandidate:
    """A candidate NPC ready for spawning at a specific location."""
    occupation: str
    name: str
    race: str
    location_type: str
    location_id: str
    personality: str = "{}"
    schedule: str = "{}"
    faction_id: int = 0


class NpcSpawnEngine:
    """Validates NPC spawning against location-type binding rules.

    Usage:
        engine = NpcSpawnEngine(db)
        result = engine.validate_npc_spawn("merchant", "dungeon", "Goblin Caves")
        if not result.is_valid:
            print(f"Cannot spawn: {result.reason}")
            print(f"Suggested: {result.suggested_locations}")
    """

    def __init__(self, db=None):
        """Initialize with optional database reference for lookups."""
        self.db = db

    # ───────────────────────────────────────────────────────────────
    # Core Validation
    # ───────────────────────────────────────────────────────────────

    def validate_npc_spawn(
        self,
        npc_occupation: str,
        current_location_type: str,
        current_location_name: str = "",
        is_travel: bool = False,
        dm_override: bool = False,
    ) -> SpawnValidationResult:
        """Validate whether an NPC of the given occupation can spawn at the given location.

        Args:
            npc_occupation: The NPC's occupation (e.g. "merchant", "guard")
            current_location_type: The location type (e.g. "market", "dungeon")
            current_location_name: Human-readable location name (for error messages)
            is_travel: Whether the party is currently travelling (affects wandering merchants)
            dm_override: Whether the DM has explicitly overridden spawn rules

        Returns:
            SpawnValidationResult with is_valid and details
        """
        occupation = npc_occupation.lower().strip()
        loc_type = current_location_type.lower().strip()

        # DM override always succeeds (but flag it)
        if dm_override:
            return SpawnValidationResult(
                is_valid=True,
                reason=f"DM-override: {occupation} placed at {loc_type} (normally invalid)",
                suggested_locations=[],
                npc_type=occupation,
                location_type=loc_type,
                dm_override=True,
            )

        # Get allowed location types for this occupation
        allowed = OCCUPATION_LOCATION_MAP.get(occupation)

        # Unknown occupation: allow but log a warning
        if allowed is None:
            logger.warning(f"Unknown NPC occupation '{occupation}' — allowing spawn at {loc_type}")
            return SpawnValidationResult(
                is_valid=True,
                reason=f"Unknown occupation '{occupation}' — no location restrictions defined",
                suggested_locations=[],
                npc_type=occupation,
                location_type=loc_type,
            )

        # Special handling for wandering_merchant
        if occupation == "wandering_merchant":
            if not is_travel:
                # Wandering merchants ONLY appear during travel
                return SpawnValidationResult(
                    is_valid=False,
                    reason="Бродячий торговец появляется только во время путешествия (5% шанс за переход)",
                    suggested_locations=["road", "crossroads"],
                    npc_type=occupation,
                    location_type=loc_type,
                )
            # During travel, check chance
            if self.check_wandering_merchant_chance():
                return SpawnValidationResult(
                    is_valid=True,
                    reason="Бродячий торговец встретился на пути!",
                    npc_type=occupation,
                    location_type=loc_type,
                    is_wandering=True,
                )
            else:
                return SpawnValidationResult(
                    is_valid=False,
                    reason="Бродячий торговец не встретился (5% шанс за переход)",
                    suggested_locations=[],
                    npc_type=occupation,
                    location_type=loc_type,
                )

        # Check if the current location type is in the allowed list
        if loc_type in allowed:
            return SpawnValidationResult(
                is_valid=True,
                reason=f"{occupation} может находиться в {loc_type}",
                suggested_locations=[],
                npc_type=occupation,
                location_type=loc_type,
            )

        # Check for dungeon-internal sub-locations that may allow certain NPCs
        if loc_type in DUNGEON_INTERNAL_TYPES:
            # Prisoners are allowed in dungeon cells
            if occupation == "prisoner" and loc_type in ("dungeon_cell", "cage", "prison", "dungeon"):
                return SpawnValidationResult(
                    is_valid=True,
                    reason=f"Пленник может находиться в {loc_type}",
                    npc_type=occupation,
                    location_type=loc_type,
                )
            # Trading posts / refugee camps inside dungeons can have merchants
            if occupation in ("merchant", "trader", "shopkeeper") and loc_type in ("trading_post", "refugee_camp"):
                return SpawnValidationResult(
                    is_valid=True,
                    reason=f"Торговец может находиться в {loc_type} внутри подземелья",
                    npc_type=occupation,
                    location_type=loc_type,
                )

        # NOT valid — build error message with suggestions
        loc_display = current_location_name or loc_type
        reason = (
            f"NPC типа '{occupation}' НЕ может появиться в локации типа '{loc_type}' ({loc_display}). "
            f"Разрешённые типы локаций: {', '.join(allowed)}"
        )

        # Find nearest valid locations from DB if available
        suggested = self._find_suggested_locations(occupation, loc_type)

        return SpawnValidationResult(
            is_valid=False,
            reason=reason,
            suggested_locations=suggested if suggested else allowed,
            npc_type=occupation,
            location_type=loc_type,
        )

    # ───────────────────────────────────────────────────────────────
    # Wandering Merchant Chance
    # ───────────────────────────────────────────────────────────────

    def check_wandering_merchant_chance(self) -> bool:
        """Roll for wandering merchant encounter. ~5% chance per travel segment.

        Returns:
            True if a wandering merchant should appear
        """
        return random.randint(1, 100) <= 5

    # ───────────────────────────────────────────────────────────────
    # Location-based NPC queries
    # ───────────────────────────────────────────────────────────────

    def get_available_npcs_for_location(
        self,
        session_id: str,
        location_type: str,
        location_name: str = "",
    ) -> List[WorldNpc]:
        """Get all NPCs that are allowed to be at this location type.

        Returns NPCs from the world_npcs table whose occupation is compatible
        with the given location type.

        Args:
            session_id: The game session
            location_type: The type of location to check
            location_name: Optional human-readable name

        Returns:
            List of WorldNpc objects that can exist at this location
        """
        if self.db is None:
            logger.warning("No DB reference — cannot query available NPCs")
            return []

        loc_type = location_type.lower().strip()
        all_npcs = self.db.get_npcs(session_id, alive_only=True)

        compatible = []
        for npc in all_npcs:
            occupation = (npc.occupation or "").lower().strip()
            if not occupation:
                # NPCs with no occupation defined are always allowed
                compatible.append(npc)
                continue

            allowed = OCCUPATION_LOCATION_MAP.get(occupation)
            if allowed is None:
                # Unknown occupation — allow
                compatible.append(npc)
                continue

            if loc_type in allowed:
                compatible.append(npc)

        return compatible

    def get_npcs_at_location(
        self,
        session_id: str,
        location_id: str,
    ) -> List[WorldNpc]:
        """Get all NPCs currently at a specific location.

        Args:
            session_id: The game session
            location_id: The exact location ID to filter by

        Returns:
            List of WorldNpc objects at this location
        """
        if self.db is None:
            logger.warning("No DB reference — cannot query NPCs at location")
            return []

        return self.db.get_npcs(session_id, location_id=location_id, alive_only=True)

    # ───────────────────────────────────────────────────────────────
    # Spawning
    # ───────────────────────────────────────────────────────────────

    def spawn_npc_at_location(
        self,
        session_id: str,
        npc_data: dict,
        location_id: str,
        dm_override: bool = False,
    ) -> Optional[WorldNpc]:
        """Spawn an NPC at a specific location after validating location binding.

        Args:
            session_id: The game session
            npc_data: Dict with keys: name, race, occupation, personality, schedule, faction_id
            location_id: The location to spawn at
            dm_override: Whether the DM overrides spawn rules

        Returns:
            The created WorldNpc, or None if validation failed
        """
        if self.db is None:
            logger.error("No DB reference — cannot spawn NPC")
            return None

        # Look up the location to get its type
        location = self.db.get_location_by_id(location_id)
        if not location:
            logger.error(f"Location {location_id} not found")
            return None

        # Validate
        occupation = npc_data.get("occupation", "")
        validation = self.validate_npc_spawn(
            npc_occupation=occupation,
            current_location_type=location.type,
            current_location_name=location.name,
            dm_override=dm_override,
        )

        if not validation.is_valid:
            logger.warning(
                f"NPC spawn REJECTED: {validation.reason}. "
                f"Suggested: {validation.suggested_locations}"
            )
            return None

        # Check for duplicate NPC at this location
        existing = self.db.get_npcs(session_id, location_id=location_id, alive_only=True)
        existing_names = {n.name.lower() for n in existing}
        if npc_data.get("name", "").lower() in existing_names:
            logger.warning(f"NPC '{npc_data['name']}' already exists at location {location_id}")
            return None

        # Check for duplicate NPC name anywhere (Rule 3: no simultaneous duplicates)
        all_npcs = self.db.get_npcs(session_id, alive_only=True)
        all_names = {n.name.lower() for n in all_npcs}
        npc_name = npc_data.get("name", "")
        if npc_name.lower() in all_names:
            # NPC already exists — move them instead of creating duplicate
            existing_npc = next(n for n in all_npcs if n.name.lower() == npc_name.lower())
            logger.info(f"NPC '{npc_name}' already exists at {existing_npc.location_id} — relocating to {location_id}")
            existing_npc.location_id = location_id
            self.db.create_npc(existing_npc)
            return existing_npc

        # Create the NPC
        npc = WorldNpc(
            id=str(uuid.uuid4()),
            session_id=session_id,
            name=npc_name,
            race=npc_data.get("race", ""),
            occupation=occupation,
            location_id=location_id,
            personality=npc_data.get("personality", "{}"),
            schedule=npc_data.get("schedule", "{}"),
            is_alive=True,
            backstory=npc_data.get("backstory", ""),
            secrets=npc_data.get("secrets", "[]"),
            faction_id=npc_data.get("faction_id", 0),
            traits=npc_data.get("traits", "[]"),
            created_at=datetime.utcnow().isoformat(),
            updated_at=datetime.utcnow().isoformat(),
        )

        self.db.create_npc(npc)
        logger.info(f"Spawned NPC '{npc_name}' ({occupation}) at location {location_id} ({location.type})")

        return npc

    # ───────────────────────────────────────────────────────────────
    # Schedule Validation
    # ───────────────────────────────────────────────────────────────

    def validate_npc_schedule(
        self,
        npc: WorldNpc,
        current_hour: int,
    ) -> dict:
        """Check if an NPC should be at their assigned location based on schedule.

        NPCs with schedules (shopkeepers, guards, innkeepers) follow time-based rules:
        - Daytime (8:00-18:00): Shopkeepers at shop, guards on patrol
        - Nighttime (18:00-8:00): Shopkeepers at home/inn, guards at barracks/night patrol

        Args:
            npc: The WorldNpc to check
            current_hour: Current game hour (0-23)

        Returns:
            Dict with 'at_location' (bool), 'reason' (str), 'expected_location_type' (str)
        """
        occupation = (npc.occupation or "").lower().strip()

        # Occupations with daytime-only schedules
        daytime_occupations = {
            "shopkeeper", "merchant", "trader", "blacksmith", "armorer",
            "weaponsmith", "jeweler", "leatherworker", "carpenter",
            "librarian", "scholar", "sage",
        }
        nighttime_occupations = {
            "guard", "watchman", "innkeeper", "tavern_keeper", "bartender",
        }

        schedule = {}
        try:
            schedule = json.loads(npc.schedule) if npc.schedule else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # Custom schedule in the NPC data takes priority
        if schedule:
            opens = schedule.get("opens", 8)
            closes = schedule.get("closes", 18)
            is_open = opens <= current_hour < closes
            return {
                "at_location": is_open,
                "reason": f"По расписанию: работает {opens}:00-{closes}:00, сейчас {current_hour}:00",
                "expected_location_type": "work" if is_open else "home",
            }

        # Default schedule by occupation
        is_daytime = 8 <= current_hour < 18

        if occupation in daytime_occupations:
            if is_daytime:
                return {
                    "at_location": True,
                    "reason": f"{occupation} на рабочем месте (дневное время)",
                    "expected_location_type": "work",
                }
            else:
                return {
                    "at_location": False,
                    "reason": f"{occupation} НЕ на рабочем месте (ночь — дома или в таверне)",
                    "expected_location_type": "home",
                }

        if occupation in nighttime_occupations:
            # Innkeepers/bartenders work at night, guards have night shifts
            return {
                "at_location": True,
                "reason": f"{occupation} на посту (работает в это время)",
                "expected_location_type": "work",
            }

        # No schedule restriction
        return {
            "at_location": True,
            "reason": f"Нет ограничений расписания для {occupation}",
            "expected_location_type": "any",
        }

    # ───────────────────────────────────────────────────────────────
    # Population Density Check
    # ───────────────────────────────────────────────────────────────

    def check_population_density(
        self,
        session_id: str,
        location_type: str,
        location_name: str = "",
    ) -> dict:
        """Check if a location already has enough NPCs based on population density rules.

        Rule 5 from npc_spawn.md:
        - Hamlet (<100 pop): 1-3 named NPCs
BUT- Village (100-1000): 3-8 named NPCs
        - Town (1000-5000): 8-20 named NPCs
        - City (5000+): 20+ named NPCs

        Args:
            session_id: The game session
            location_type: The location type
            location_name: Human-readable name

        Returns:
            Dict with 'can_spawn', 'current_count', 'max_count', 'location_tier'
        """
        # Population density by location type
        DENSITY_MAP = {
            "hamlet":       (1, 3),
            "village_edge": (1, 5),
            "village":      (3, 8),
            "town_square":  (8, 20),
            "town":         (8, 20),
            "city":         (20, 50),
            "market":       (5, 15),
            "port":         (10, 30),
            "bazaar":       (10, 25),
            "inn":          (1, 5),
            "tavern":       (1, 8),
            "temple":       (2, 8),
            "barracks":     (5, 15),
            "castle":       (10, 30),
            "manor":        (3, 10),
            "slums":        (5, 15),
            "dungeon":      (0, 10),   # dungeon NPCs are special
            "wilderness":   (0, 3),
            "road":         (0, 2),
        }

        min_npcs, max_npcs = DENSITY_MAP.get(location_type, (0, 999))

        if self.db is None:
            return {
                "can_spawn": True,
                "current_count": 0,
                "max_count": max_npcs,
                "location_tier": location_type,
            }

        # Count current NPCs at this location type
        all_npcs = self.db.get_npcs(session_id, alive_only=True)
        current_count = len(all_npcs)  # Rough count; exact location filtering needs location_id

        can_spawn = current_count < max_npcs

        return {
            "can_spawn": can_spawn,
            "current_count": current_count,
            "max_count": max_npcs,
            "location_tier": location_type,
        }

    # ───────────────────────────────────────────────────────────────
    # Helpers
    # ───────────────────────────────────────────────────────────────

    def _find_suggested_locations(
        self,
        occupation: str,
        from_location_type: str,
    ) -> List[str]:
        """Find the nearest valid location types for an occupation.

        This is a simple suggestion based on the occupation's allowed list.
        A full implementation would query the DB for actual nearby locations
        of the right type.

        Returns:
            List of suggested location type names
        """
        allowed = OCCUPATION_LOCATION_MAP.get(occupation, [])
        if not allowed:
            return []

        # If the DB is available, try to find actual locations of the right type
        if self.db is not None:
            # We'd need a session_id for this; return type names as fallback
            pass

        return allowed

    def get_occupation_for_location(self, location_type: str) -> List[str]:
        """Get all occupations that can exist at a given location type.

        Useful for populating spawn menus or validation checks.

        Args:
            location_type: The location type to check

        Returns:
            List of occupation strings that are allowed at this location type
        """
        loc_type = location_type.lower().strip()
        compatible = []
        for occupation, allowed_types in OCCUPATION_LOCATION_MAP.items():
            if loc_type in allowed_types:
                compatible.append(occupation)
        return compatible

    def is_merchant_at_valid_location(
        self,
        occupation: str,
        location_type: str,
    ) -> bool:
        """Quick check: is this a merchant-type NPC at a valid commercial location?

        Used by the prompt injection to warn the Master before spawning.
        """
        merchant_types = {"merchant", "trader", "shopkeeper", "wandering_merchant"}
        if occupation.lower() not in merchant_types:
            return True  # Not a merchant, no restriction

        allowed = OCCUPATION_LOCATION_MAP.get(occupation.lower(), [])
        return location_type.lower() in allowed
