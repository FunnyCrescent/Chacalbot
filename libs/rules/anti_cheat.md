# Anti-Cheat Rules & Homebrew Policy — D&D 5e Bot

This document defines what is forbidden, what requires DM approval, and the homebrew policy for the Chacalbot D&D bot. These rules are enforced by the rule engine and the anti-cheat validator.

## Prohibited Actions

### Player Prohibitions (Zero Tolerance)

1. **Faking dice rolls**: Players cannot claim a roll result they did not make through the bot's dice system. All rolls must go through `roll_dice` (NPC) or `request_player_roll` (player). Manual roll claims are ignored unless verified by `/rholio`.

2. **Modifying character stats without DM action**: Players cannot change their HP, AC, ability scores, gold, inventory, or conditions directly. All game state changes must flow through the DB-Bot based on the Master's narrative.

3. **Controlling other characters**: A player cannot declare actions for another player's character, move another character, or spend another character's resources.

4. **Metagaming**: Using out-of-character knowledge to make in-character decisions. Examples:
   - Acting on monster stats you read in the Monster Manual.
   - Preparing for an encounter you only know about from the DM's notes.
   - Using knowledge from a different playthrough of the same adventure.

5. **Godmoding**: Declaring outcomes without rolling or DM adjudication. Examples:
   - "I kill the guard" (without rolling attack and damage).
   - "I convince the king to give me his crown" (without a skill check).
   - "I find the secret door" (without a Perception check or search action).

6. **Retroactive actions**: Changing a declared action after seeing the result of a roll. Once you declare an action and the roll is made, the action stands.

7. **Exploiting rule ambiguities**: Intentionally misinterpreting unclear rules to gain advantage. When a rule is ambiguous, the DM's ruling stands. The DM may consult the SRD for clarification.

### Character Sheet Prohibitions

1. **Impossible ability scores**: No ability score may exceed 20 at character creation without explicit magical justification (e.g., a magic item, class feature at high level). No score below 1.

2. **Impossible HP**: HP must be calculable from hit dice + CON modifier. A 1st-level Wizard with 20 HP is mathematically impossible (max d6 + max CON mod = 6 + 5 = 11).

3. **Impossible AC**: AC must be derivable from a valid armor/shield/class feature combination. An unarmored character with AC 25 is suspicious.

4. **Incompatible multiclass**: Multiclass combinations that don't meet prerequisites are flagged. A Wizard with STR 8 cannot multiclass into Fighter (requires STR 13 or DEX 13).

5. **Invalid spell lists**: A character cannot have spells from a class they are not a member of, unless they have a feature that grants those spells (e.g., Magic Initiate, racial spellcasting, Divine Soul Sorcerer).

6. **Duplicate magic items**: Attunement slots are limited to 3. A character cannot attune to 4+ items simultaneously.

## DM Approval Required

The following actions require explicit DM (AI-Master) approval before they are allowed:

1. **Homebrew races**: Any race not in the SRD 5e (2014) list. The DM evaluates balance and world-fit.
2. **Homebrew classes**: Any class not in the SRD. **Hard to approve** — classes are deeply mechanical.
3. **Homebrew spells**: Any spell not in the SRD. The DM checks level, school, casting time, and balance.
4. **Homebrew magic items**: Any magic item not in the SRD. The DM checks rarity and attunement.
5. **Unusual backgrounds**: Backgrounds with mechanical benefits beyond SRD backgrounds.
6. **Faction creation**: Creating a new faction in the world (may conflict with existing factions).
7. **Location creation by players**: Players cannot create new locations — only the DM can.
8. **Mass NPC creation**: Spawning more than 3 NPCs at once requires DM oversight.
9. **PvP actions**: Any attack or hostile action against another player character.
10. **Wish and similar spells**: Any spell that can alter reality (Wish, True Polymorph on creatures, etc.) requires careful DM adjudication.

## Homebrew Policy

### Allowed Homebrew (No Approval Needed)

The following homebrew is **implicitly allowed** because it doesn't break game balance:
1. **Flavor reskinning**: Describing a longsword as a katana mechanically (same stats, different description).
2. **Custom appearance**: Any physical description for your character, regardless of race.
3. **Custom backstory**: Any backstory that doesn't claim mechanical benefits you don't have.
4. **Common magic items**: Items from the common magic item table (e.g., cloak of many fashions, hat of wizardry) — generally harmless.

### Conditionally Allowed Homebrew (DM Approval)

1. **Uncommon/Rare homebrew items**: DM evaluates against SRD items of similar rarity.
2. **Modified SRD spells**: Changing the damage type of a spell (e.g., fireball but cold) — usually allowed if it doesn't bypass common resistances.
3. **Variant rules**: Flanking, facing, critical hit modifications — DM decides for the campaign.
4. **Feats not in SRD**: Homebrew feats are evaluated based on power level relative to SRD feats.

### Prohibited Homebrew

1. **Epic-level content at low levels**: No 9th-level spells for a 5th-level character.
2. **Infinite loops**: Any combination of features that creates an infinite loop (e.g., infinite damage, infinite healing, infinite spell slots).
3. **Action economy violations**: Features that grant more actions than the rules allow (e.g., "you may take 3 actions on your turn").
4. **Flat immunities**: "Immune to all damage" or "immune to all spells" without extreme cost (e.g., a 1st-level feature).
5. **Stat inflation**: Features that grant +10 or more to any ability score or AC.

## Validation Tiers

The anti-cheat system uses three validation tiers:

### Tier 1: Automatic (Always Runs)
- Math validation (HP, AC, stats, gold calculations).
- SRD reference check (race, class, background, spells).
- Syntax validation (valid JSON, valid dice notation).
- Range validation (stats 1-30, HP ≥ 0, AC 1-30).

### Tier 2: Advisory (Flags but Doesn't Block)
- Balance concerns (homebrew race with too many features, etc.).
- Backstory claims (claims immunity without justification).
- Unusual combinations (e.g., Barbarian/Wizard without explaining how rage works with spellcasting).

### Tier 3: DM Override (Always Available)
- The DM can override any validation result with a narrative justification.
- Overrides are logged and visible to all players.
- Overrides are specific to the character/situation — they don't create global rules.

## Enforcement

- **Tier 1 violations**: Action is blocked. The player must fix the issue before proceeding.
- **Tier 2 warnings**: Action proceeds, but the DM is notified. The DM may intervene retroactively.
- **Tier 3 overrides**: DM's word is final. No appeal beyond the DM.

The anti-cheat system is a **safety net, not a prison**. Its purpose is to catch genuine errors (math mistakes, rule misunderstandings) and flag potential abuse (impossible stats, homebrew exploits). It is NOT meant to limit creativity or punish players for trying interesting builds.
