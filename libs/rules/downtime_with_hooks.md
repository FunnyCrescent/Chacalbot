# Downtime Activities

Rules for downtime activities and their effects on the world state.

## Activity Types

### Crafting
- Duration: Typically 1 day per 5 gp of item value
- Requirements: Tool proficiency, raw materials
- Result: Crafted item

### Training
- Duration: 250 days (or as DM determines)
- Requirements: Trainer NPC, gold (1 gp/day)
- Result: New proficiency or language

### Working
- Duration: Any (typically in week increments)
- Requirements: None
- Result: Lifestyle expenses covered + modest savings

### Carousing
- Duration: 5+ days typically
- Requirements: Social venue (tavern, feast hall)
- Result: Social connections, rumors, possible mishaps

### Research
- Duration: 1+ weeks
- Requirements: Library, archive, or sage
- Result: Information, lore discovery

### Pit Fighting
- Duration: 1+ days
- Requirements: Arena or fighting ring
- Result: Prize money, reputation

### Religious Service
- Duration: 1+ days
- Requirements: Temple, shrine, or religious community
- Result: Temple favor, possible divine insight

### Scribing Scroll
- Duration: 1 day per spell level
- Requirements: Arcane proficiency, fine ink, parchment
- Result: Magic scroll

## World-State Hooks

The following hooks define how downtime activities affect the world.

### Crafting
- hook: item_added_to_market
  - change_type: market_price
  - magnitude: 1.0
  - conditions: item is craftable
  - effect: Creates MarketPrice entry for crafted item. If item exists in local market, reduces price by 10% (supply increased).

- hook: crafting_reputation
  - change_type: npc_relation
  - magnitude: 0.5
  - conditions: character has tool proficiency
  - effect: Local artisans take notice. Crafter gains reputation with local crafter NPCs.

### Training
- hook: trainer_reputation_increase
  - change_type: npc_relation
  - magnitude: 0.5
  - conditions: trainer NPC exists at location
  - effect: Trainer's reputation increases. NpcRelation.reputation += 1. Trainer may offer advanced training next time.

- hook: training_facility_influence
  - change_type: faction_influence
  - magnitude: 0.3
  - conditions: training at a faction facility
  - effect: Training facility (dojo, academy, monastery) gains influence in the local area.

### Working
- hook: local_economy_shift
  - change_type: economic_event
  - magnitude: 1.0
  - conditions: location has economy
  - effect: Local economy adjusts. If many workers, wages decrease. If few, wages increase. Creates EconomicEvent.

- hook: work_reputation
  - change_type: npc_relation
  - magnitude: 0.2
  - conditions: employer NPC exists
  - effect: Employer NPC gains favorable relation with worker. May offer better opportunities.

### Carousing
- hook: rumor_network_expansion
  - change_type: lore
  - magnitude: 1.0
  - conditions: carousing in a social venue
  - effect: Rumor network expands. New MemoryEntry (lore) created. NpcRelation changes with carousing partners.

- hook: social_connections
  - change_type: npc_relation
  - magnitude: 0.5
  - conditions: successful carousing check
  - effect: Character makes social connections. Favorable NpcRelation created with NPCs met during carousing.

### Research
- hook: library_archive_grows
  - change_type: lore
  - magnitude: 1.0
  - conditions: research facility exists
  - effect: Library/archive grows. LoreArticle created or expanded with research findings.

- hook: arcane_discovery
  - change_type: lore
  - magnitude: 0.3
  - conditions: research is arcane in nature
  - effect: Chance of arcane discovery. If research is magical, may find new spell, ritual, or magic item formula.

### Pit Fighting
- hook: arena_reputation
  - change_type: faction_influence
  - magnitude: 0.5
  - conditions: arena exists at location
  - effect: Arena faction influence changes based on fight outcome. Winning increases arena popularity.

### Religious Service
- hook: temple_influence
  - change_type: faction_influence
  - magnitude: 0.3
  - conditions: temple exists at location
  - effect: Temple faction influence increases. Deity's followers in the area become more organized.

### Scribing Scroll
- hook: magic_market_shift
  - change_type: market_price
  - magnitude: 0.5
  - conditions: arcane market exists
  - effect: Magic item market shifts. Scroll supply increases, reducing scroll prices by 5-15%.
