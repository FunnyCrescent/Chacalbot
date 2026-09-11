# NPC Spawning Rules — D&D 5e Bot Policy

This document defines the rules for spawning and placing NPCs in the game world. These rules are **critical** for maintaining world consistency and preventing the NPC location-binding bug where merchants appear in inappropriate locations.

## NPC Placement by Type

### Settlement NPCs (Fixed Location)

Settlement NPCs are **bound to a specific location type** and cannot appear elsewhere without explicit DM narrative justification.

| NPC Type            | Allowed Location Types                          | Forbidden Location Types               |
|---------------------|-------------------------------------------------|----------------------------------------|
| **Merchant/Trader** | Market, Town Square, Bazaar, Shop, Trading Post | Dungeon, Wilderness, Road, Cave, Ruins |
| **Shopkeeper**      | Shop, Market, Town Square                       | Any non-settlement location            |
| **Innkeeper**       | Inn, Tavern, Waystation                        | Wilderness, Dungeon, Road              |
| **Blacksmith**      | Forge, Workshop, Market, Town Square            | Wilderness, Dungeon                    |
| **Guard/Soldier**   | Gate, Barracks, Patrol Route, Town Square       | Wilderness (except patrol)             |
| **Noble/Official**  | Manor, Palace, Courthouse, Town Square          | Wilderness, Dungeon, Road              |
| **Priest/Cleric**   | Temple, Shrine, Cathedral                       | Wilderness (except shrine)             |
| **Sage/Scholar**    | Library, Academy, Tower                         | Wilderness, Dungeon                    |

### Wandering NPCs (Variable Location)

Wandering NPCs may appear in multiple location types but follow strict spawn rules:

| NPC Type               | Spawn Probability | Allowed Locations                        | Restrictions                                          |
|------------------------|-------------------|------------------------------------------|-------------------------------------------------------|
| **Wandering Merchant** | 10% per travel    | Road, Crossroads, Village Edge           | Must have a named origin and destination. Never spawns in dungeons or deep wilderness. |
| **Patrol Guard**       | 25% per travel    | Road, Gate, Town boundary                | Follows patrol route. Returns to barracks.            |
| **Bounty Hunter**      | 5% per rest       | Tavern, Road, Wilderness                 | Spawned by DM narrative when bounties exist.          |
| **Pilgrim**            | 8% per travel     | Road, Shrine, Temple, Waystation         | Moves between holy sites on a route.                  |
| **Refugee**            | 5% per event      | Road, Village Edge, Camp                 | Only spawns after a narrative event (war, disaster).  |

### Dungeon NPCs

Dungeon NPCs are **static** — they are placed when the dungeon is generated and do not leave.

| NPC Type            | Allowed Locations          | Restrictions                                    |
|---------------------|----------------------------|-------------------------------------------------|
| **Dungeon Boss**    | Boss Room, Final Chamber   | One per dungeon. Does not respawn.              |
| **Dungeon Minion**  | Any room in dungeon        | Spawned with dungeon. May be cleared.           |
| **Prisoner**        | Cell, Cage, Prison Room    | Cannot leave without being freed by party.      |
| **Dungeon Merchant**| Rare: Safe room only       | Must be explicitly placed by DM. Max 1 per dungeon. Must have a reason to be there (e.g., trapped, magical, undercover). |

## Spawn Rules

### Rule 1: Location-Type Binding
**A merchant/trader NPC MUST be spawned in a location of type: market, town_square, bazaar, shop, or trading_post.**
If the world generator attempts to place a merchant in a non-commercial location, the spawn MUST be rejected and the NPC placed in the nearest valid commercial location instead.

### Rule 2: Wandering Merchant Rarity
Wandering merchants are **rare**. The probability of encountering one on a road is at most 10% per travel segment. Wandering merchants must:
- Have a **named origin** (where they came from) and **named destination** (where they're heading).
- Have a **limited inventory** (fewer items than a fixed merchant, typically 1-5 items).
- **Disappear** after the party interacts with them or after 1-2 game days, whichever comes first.
- NOT replace a fixed merchant. If a fixed merchant already exists at a location, the wandering merchant passes through but does not set up shop.

### Rule 3: NPC Duplication Prevention
The same NPC (by name) **cannot exist in two locations simultaneously**. If an NPC is moved (by narrative or schedule), they are removed from the old location before being placed in the new one.

### Rule 4: NPC Schedule Consistency
NPCs with schedules (shopkeepers, guards, innkeepers) must follow their schedule:
- **Daytime**: Shopkeepers are at their shop (8 AM - 6 PM by default). Guards patrol their route.
- **Nighttime**: Shopkeepers are at their home or the inn. Guards are at the barracks or on night patrol.
- The NPC's current location must match their schedule. If the party visits a shop at midnight, the shopkeeper is NOT there — they're at home.

### Rule 5: Population Density
The number of NPCs in a location is proportional to the location's size/type:
- **Hamlet** (population < 100): 1-3 named NPCs.
- **Village** (100-1000): 3-8 named NPCs.
- **Town** (1000-5000): 8-20 named NPCs.
- **City** (5000+): 20+ named NPCs.
- **Dungeon**: Determined by dungeon level and size, not population.

### Rule 6: No Spontaneous Merchants in Dungeons
Merchants do NOT spontaneously appear in dungeons unless:
1. The DM explicitly places them as part of the dungeon design (e.g., a trapped merchant, a magical vendor, an undercover agent).
2. The party rescues a merchant NPC who was captured — this merchant then **leaves** the dungeon at the first opportunity.
3. A **planar merchant** exists as a rare, planned encounter (e.g., a fiendish bargain-maker in a specific room).

## Anti-Pattern: The "Merchant in the Dungeon" Bug

**PROBLEM**: Without these rules, the NPC generator may place a generic merchant in a dungeon room, creating a nonsensical scenario where a shopkeeper is calmly selling goods next to a dragon's lair.

**SOLUTION**: The spawn engine must check the NPC type against the location type BEFORE creating the NPC. If the location type is not in the NPC's allowed location types, the spawn is rejected. This check MUST happen at the database level (in `create_npc`) — not just at the UI level.

## Integration with Rule Engine

When the game engine attempts to spawn an NPC, it should:
1. Call `rule_engine.validate_action(f"spawn {npc_type} at {location_type}")` to check against these rules.
2. If `is_valid` is False, check `suggestions` for alternative placement (e.g., nearest valid location).
3. Log the spawn attempt and result for debugging.
4. If a DM override exists, allow the spawn but flag it in the narrative as unusual.
