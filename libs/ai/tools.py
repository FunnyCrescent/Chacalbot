"""Tool schemas для LLM function-calling."""

# ═══════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════

ROLL_TYPE_ENUM = ["normal", "advantage", "disadvantage"]

DICE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": (
                "Roll dice for the GM's side of the table ONLY: NPC/monster attacks, NPC saving "
                "throws, environmental/trap checks, NPC initiative. "
                "⚠️ NEVER use this for ANY player character roll — not attack, not damage, not saving throw, "
                "not ability check, not skill check, not initiative, not death save, not advantage/disadvantage, "
                "NOTHING belonging to a player. ALL player rolls MUST go through request_player_roll. "
                "Even damage dealt BY a player character is rolled BY that player via request_player_roll. "
                "If you use roll_dice for a player roll, this is a CRITICAL error — the player will NOT "
                "see their dice and will NOT be able to roll. "
                "If multiple players need to roll, call request_player_roll ONCE PER PLAYER — never batch "
                "multiple players into a single roll_dice call. "
                "For a single d20 check with advantage/disadvantage, ALWAYS use count=1 and set "
                "roll_type accordingly — do NOT set count=2 to fake advantage, that just sums two "
                "independent rolls instead of taking the better/worse one, which is wrong. "
                "count>1 is only for multi-die damage rolls (e.g. 3d6 fireball) or when a mechanic "
                "genuinely calls for multiple simultaneous dice of the same kind for DIFFERENT targets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "description": "Number of dice. For d20 checks with advantage/disadvantage, always 1 — use roll_type, not count=2."},
                    "sides": {"type": "integer", "enum": [4, 6, 8, 10, 12, 20, 30, 100], "description": "Dice sides. d30 is used for complexity/difficulty checks in some mechanics."},
                    "modifier": {"type": "integer", "default": 0, "description": "Bonus/penalty"},
                    "roll_type": {"type": "string", "enum": ROLL_TYPE_ENUM, "default": "normal", "description": "Only meaningful for a single d20 check. 'advantage'/'disadvantage' roll TWO d20 internally and keep the better/worse — do not also raise count."},
                    "label": {"type": "string", "description": "What this roll is for. Example: 'Гоблин 1 скимитар' or 'Урон от файрбола'"},
                    "visible": {"type": "boolean", "default": False, "description": "MUST be False for ALL NPC/monster/trap/damage rolls — players never see these dice. Set True ONLY for rare GM-managed visible rolls (e.g., public initiative)."},
                },
                "required": ["count", "sides", "visible", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_player_roll",
            "description": (
                "⚠️ MANDATORY for EVERY player roll — attack, damage, saving throw, ability check, "
                "skill check, initiative, death save, advantage, disadvantage, d100, d30 — literally ANY "
                "die that belongs to a player character. If you use roll_dice for a player roll, this is "
                "a CRITICAL error — the player will NOT see their dice and will NOT be able to roll. "
                "You MUST call this SEPARATELY for EACH player's roll: if 3 players need to roll, you "
                "must call request_player_roll 3 times — once per character. Example: if Кейн attacks, "
                "Храфна attacks, and Рин rolls Persuasion, you call request_player_roll 3 times with "
                "character_name='Кейн', character_name='Храфна', and character_name='Рин' respectively. "
                "NEVER use roll_dice for ANY player roll — even if the player is not the one who triggered "
                "the action. If a player is affected by a spell and needs a saving throw, THEY roll it, "
                "not the GM. You will NOT get the result immediately — the bot pauses this tool call until "
                "the named player presses their button, then the result comes back to you as the tool result, "
                "exactly like roll_dice. "
                "If the player already provided a verified roll this round via the /roll command (you will "
                "see it wrapped in a [ROLL] tag, separate from their [PLAYER] action text), do NOT call this "
                "again for the same check — reuse that already-verified number instead of re-rolling it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string", "description": "Exact character name this roll belongs to — used to find the right player and send them the button."},
                    "count": {"type": "integer", "minimum": 1, "default": 1, "description": "Almost always 1 for a player check."},
                    "sides": {"type": "integer", "enum": [4, 6, 8, 10, 12, 20, 30, 100], "description": "Dice sides. d30 is used for complexity/difficulty checks in some mechanics."},
                    "modifier": {"type": "integer", "default": 0, "description": "Bonus/penalty to show pre-filled on the button"},
                    "roll_type": {"type": "string", "enum": ROLL_TYPE_ENUM, "default": "normal", "description": "advantage/disadvantage rolls two d20 and keeps better/worse — count stays 1."},
                    "label": {"type": "string", "description": "What this roll is for, shown to the player. Example: 'Эйра — атака рапирой' or 'Эйра — спасбросок Ловкости'"},
                    "visible": {"type": "boolean", "default": True, "description": "Almost always true — the player is rolling it themselves, they see it. Set false only for a genuinely secret player roll (e.g. blind Insight check)."},
                },
                "required": ["character_name", "sides", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Выполни математическое вычисление. Используй ВСЕГДА для любых расчётов: модификаторы характеристик, бонус мастерства, итоговые суммы бросков, proficiency bonus по уровню. НЕ считай в уме — вызови этот инструмент. Примеры: calculate(expression='(14-10)//2') для модификатора характеристики, calculate(expression='2+3') для проверки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Математическое выражение. Поддерживаются: +, -, *, /, //, %, **, (). Пример: '(18-10)//2 + 3'"},
                },
                "required": ["expression"],
            },
        },
    },
]

GAME_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "change_hp",
            "description": "Change a character's HP. Use EVERY time HP changes: damage, healing, regeneration, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "delta": {"type": "integer", "description": "Negative for damage, positive for healing"},
                    "source": {"type": "string", "description": "Why HP changed"},
                },
                "required": ["character_name", "delta", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_condition",
            "description": "Add a condition to a character. Use when someone gets poisoned, stunned, prone, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "condition": {"type": "string", "description": "poisoned, prone, stunned, paralyzed, unconscious, bleeding, charmed, frightened, restrained, grappled, invisible, concentrating, exhaustion"},
                    "source": {"type": "string"},
                    "duration": {"type": "string", "default": ""},
                },
                "required": ["character_name", "condition", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_condition",
            "description": "Remove a condition from a character when it ends or is cured.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "condition": {"type": "string"},
                },
                "required": ["character_name", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_item",
            "description": "Add an item to inventory. Use when they find loot, buy items, receive rewards.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "item_name": {"type": "string"},
                    "quantity": {"type": "integer", "default": 1},
                    "description": {"type": "string", "default": ""},
                },
                "required": ["character_name", "item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "Remove an item from inventory. Use when consumed, lost, sold, or broken.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "item_name": {"type": "string"},
                    "quantity": {"type": "integer", "default": 1},
                },
                "required": ["character_name", "item_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_gold",
            "description": "Change a character's gold/currency. 1 gp = 10 sp = 100 cp = 0.5 ep = 0.1 pp. Use the currency actually mentioned in the narrative (медь/серебро/электрум/золото/платина) — do not silently convert everything to gp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "amount": {"type": "integer", "description": "Positive = gain, negative = spend"},
                    "currency": {"type": "string", "enum": ["cp", "sp", "ep", "gp", "pp"], "default": "gp", "description": "cp=медь, sp=серебро, ep=электрум, gp=золото, pp=платина"},
                    "reason": {"type": "string"},
                },
                "required": ["character_name", "amount", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_time",
            "description": "Advance in-game time. Use when players travel, rest, explore, or time passes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "default": 0},
                    "hours": {"type": "integer", "default": 0},
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_quest",
            "description": "Update a quest status. Use when quest is completed, failed, or advanced.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "completed", "failed"]},
                    "note": {"type": "string", "default": ""},
                },
                "required": ["title", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_location",
            "description": "Set current location for a character. Use when they move to a new place.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "location_name": {"type": "string"},
                    "location_description": {"type": "string", "default": ""},
                },
                "required": ["character_name", "location_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_reputation",
            "description": "Change faction reputation. Use when players help/harm a faction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "faction_name": {"type": "string"},
                    "delta": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["faction_name", "delta", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_resource",
            "description": "Use a class/resource ability (Rage, Ki, Spell Slot, Bardic Inspiration, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "resource_name": {"type": "string"},
                    "amount": {"type": "integer", "default": 1},
                },
                "required": ["character_name", "resource_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_world_event",
            "description": "Record a world event that happened. Use for significant narrative events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["event_type", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_srd_monster",
            "description": "Look up a monster from the SRD bestiary by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_srd_item",
            "description": "Look up an item from the SRD by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_srd_spell",
            "description": "Look up a spell from the SRD by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_location",
            "description": "Create a new location in the world.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "type": {"type": "string", "enum": ["wilderness", "city", "dungeon", "tavern", "shop", "temple", "castle", "cave", "forest", "ruins"]},
                    "parent_location": {"type": "string", "default": ""},
                    "danger_level": {"type": "integer", "default": 1},
                },
                "required": ["name", "description", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_location",
            "description": "Retrieve a location from the world database by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_npc",
            "description": "Create a persistent NPC in the world.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "race": {"type": "string", "default": ""},
                    "occupation": {"type": "string", "default": ""},
                    "location_name": {"type": "string", "default": ""},
                    "personality": {"type": "string", "default": ""},
                    "backstory": {"type": "string", "default": ""},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_npc",
            "description": "Retrieve an NPC from the world database by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_npc_relation",
            "description": "Update what a SPECIFIC named NPC feels/knows about a SPECIFIC character — reputation delta, attitude, any new fact, and any new grudge or debt between them. Use whenever a meaningful NPC interaction happens, not only combat/quests. Applies just as well to unnamed-but-significant NPCs (a guard captain, a jailer) as long as they were created with create_npc first using a stable descriptive title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "npc_name": {"type": "string"},
                    "character_name": {"type": "string"},
                    "delta": {"type": "integer", "default": 0},
                    "attitude": {"type": "string", "enum": ["friendly", "helpful", "neutral", "unfriendly", "hostile"]},
                    "known_fact": {"type": "string", "description": "A new fact this NPC now knows/believes about the character, e.g. 'знает, что Эйра — беглая преступница'."},
                    "grudge": {"type": "string", "description": "Новая конкретная обида этого NPC к персонажу, если она возникла в этой сцене. Оставь пустым, если обиды не появилось."},
                    "debt": {"type": "string", "description": "Новый конкретный долг между NPC и персонажем в любую сторону, если он возник в этой сцене. Оставь пустым, если долга не возникло."},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["npc_name", "character_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_lore",
            "description": "Record a new lore article about the world.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": ["history", "gods", "factions", "places", "legends", "items", "general"]},
                    "content": {"type": "string"},
                    "tags": {"type": "string", "default": ""},
                },
                "required": ["title", "category", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lore",
            "description": "Retrieve a lore article by title.",
            "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_market_price",
            "description": "Set or update the price of an item at a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string"},
                    "location_name": {"type": "string"},
                    "base_price_gp": {"type": "integer"},
                    "current_price_gp": {"type": "integer"},
                },
                "required": ["item_name", "location_name", "base_price_gp", "current_price_gp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_economic_event",
            "description": "Record an economic event affecting prices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "affected_locations": {"type": "string", "default": ""},
                    "price_multiplier": {"type": "number", "default": 1.0},
                    "duration_days": {"type": "integer", "default": 7},
                },
                "required": ["name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_effect",
            "description": "Apply a persistent effect (curse, blessing, poison, magical aura) to a character, NPC, or location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string"},
                    "entity_type": {"type": "string", "enum": ["character", "npc", "location"]},
                    "name": {"type": "string"},
                    "effect_type": {"type": "string", "enum": ["curse", "blessing", "poison", "disease", "magical"]},
                    "source": {"type": "string", "default": ""},
                    "duration": {"type": "string", "default": "permanent"},
                    "mechanics": {"type": "string", "default": ""},
                },
                "required": ["entity_name", "entity_type", "name", "effect_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_timer",
            "description": "Set a delayed event. Use for poison ticks, spell durations, timed traps, recurring world events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_name": {"type": "string"},
                    "entity_type": {"type": "string", "enum": ["character", "npc", "location"]},
                    "event_type": {"type": "string"},
                    "trigger_in_rounds": {"type": "integer"},
                    "action": {"type": "string", "default": ""},
                    "is_recurring": {"type": "boolean", "default": False},
                },
                "required": ["entity_name", "entity_type", "event_type", "trigger_in_rounds"],
            },
        },
    },
    # ─── Character progression (B1) — the DB is the live source of truth for
    # character state; the uploaded sheet file is only a starting snapshot. ───
    {
        "type": "function",
        "function": {
            "name": "level_up_character",
            "description": "Raise a character's level (and optionally max HP) when the narrative reflects a level-up (quest reward, milestone, montage of downtime). Only for real, narratively-earned progression — not something a player merely claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "new_level": {"type": "integer", "minimum": 1, "maximum": 20},
                    "new_max_hp": {"type": "integer", "description": "New max HP if it changed (optional)."},
                    "reason": {"type": "string"},
                },
                "required": ["character_name", "new_level", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_ability_score",
            "description": "Change one of a character's six ability scores (STR/DEX/CON/INT/WIS/CHA). Use for Ability Score Improvements, magical boons/curses, cursed items, tomes of power, etc. — not for temporary combat buffs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "ability": {"type": "string", "enum": ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]},
                    "new_value": {"type": "integer", "minimum": 1, "maximum": 30},
                    "reason": {"type": "string"},
                },
                "required": ["character_name", "ability", "new_value", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_feature",
            "description": "Grant a character a new class/racial feature or feat they narratively earned (level-up choice, boon, training montage).",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "feature_name": {"type": "string"},
                },
                "required": ["character_name", "feature_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_proficiency",
            "description": "Grant a character a new proficiency (skill, tool, weapon, armor, language) they narratively earned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "proficiency_name": {"type": "string"},
                },
                "required": ["character_name", "proficiency_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_spell_known",
            "description": "Grant a character a new known/prepared spell they narratively learned (level-up, spellbook, scroll study).",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "spell_name": {"type": "string"},
                },
                "required": ["character_name", "spell_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_character_goal",
            "description": (
                "Record or update a PERSONAL GOAL/motivation the character holds — distinct from update_quest. "
                "A quest requires a narratively confirmed task, usually from an NPC. A goal is what the "
                "character ALREADY wants: from their backstory (revenge, finding family, destroying a faction), "
                "or a clearly stated intention/thought during play (even an inner thought via 'Дн. Думаю...'), "
                "with NO external confirmation required. Use this whenever the narrative or a character's own "
                "declared intention makes a personal goal explicit — do not wait for an NPC to assign it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "title": {"type": "string", "description": "Short goal phrase, e.g. 'Найти настоящих родителей' or 'Уничтожить Аэлинниэль Серебряную Звезду'."},
                    "status": {"type": "string", "enum": ["active", "achieved", "abandoned"], "default": "active"},
                    "source": {"type": "string", "enum": ["backstory", "session"], "default": "session"},
                },
                "required": ["character_name", "title"],
            },
        },
    },
    # ─── H8: what a LOCATION knows/feels about a character (npc side already covered by set_npc_relation above) ───
    {
        "type": "function",
        "function": {
            "name": "update_location_relation",
            "description": "Update what a LOCATION (city/town/region) knows about a character — fame, infamy, wanted status (e.g. 'плакаты о розыске на каждой стене'). Direction is location -> character: this is what the PLACE knows/thinks of the PC, not the reverse. Use whenever the party does something publicly significant, gets caught, becomes known, or a rumor spreads.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location_name": {"type": "string"},
                    "character_name": {"type": "string"},
                    "fame_delta": {"type": "integer", "default": 0, "description": "Positive = more famous/well-regarded, negative = more infamous."},
                    "reputation_delta": {"type": "integer", "default": 0},
                    "is_wanted": {"type": "boolean", "description": "Set true if the character becomes wanted by law here."},
                    "notoriety_note": {"type": "string", "description": "Free text, e.g. 'разыскивается за убийство стражника, плакаты по всему городу'."},
                },
                "required": ["location_name", "character_name"],
            },
        },
    },
]

ALL_TOOLS = DICE_TOOLS + GAME_TOOLS
COMBAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "dechrauymladd",
            "description": (
                "Начать бой (initiative combat). Вызывай ТОЛЬКО когда по нарративу это оправдано — "
                "игрок инициировал конфликт через Дн. (напал, спровоцировал, попал в засаду и т.д.), "
                "НЕ по команде живого человека-админа: он не Мастер и не решает, когда начинается бой. "
                "После вызова КАЖДЫЙ игрок-участник получит инлайн-кнопку для броска инициативы. "
                "NPC бросают инициативу автоматически (скрыто). "
                "Пока все игроки не кинут — бой не начнётся."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Имена персонажей и NPC/монстров, вступающих в бой прямо сейчас.",
                    },
                    "reason": {"type": "string", "description": "Краткая причина начала боя, для лога."},
                },
                "required": ["participants", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diweddymladd",
            "description": "Завершить бой. Вызывай когда все враги мертвы, сбежали, или бой окончен по сюжету.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Краткая причина завершения боя."},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "npc_ai_action",
            "description": (
                "Запросить AI-решение для группы NPC в бою. Передай текущее состояние каждого NPC "
                "(имя, черты, HP, ситуация) и контекст боя. AI вернёт массив решений (атака, "
                "перемещение, отступление, способность и т.д.). Используй для принятия тактических "
                "решений NPC вместо самостоятельного придумывания."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "npc_group": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "traits": {"type": "string", "description": "Черты из листа: cowardly, brave, tactical и т.д."},
                                "hp_current": {"type": "integer"},
                                "hp_max": {"type": "integer"},
                                "situation": {"type": "string", "description": "Краткое описание текущей ситуации NPC."},
                            },
                            "required": ["name", "traits", "hp_current", "hp_max", "situation"],
                        },
                        "description": "Массив NPC с их текущим состоянием.",
                    },
                    "context": {"type": "string", "description": "Описание текущего состояния боя: кто жив, позиции, последние действия."},
                },
                "required": ["npc_group", "context"],
            },
        },
    },
]

MASTER_TOOLS = DICE_TOOLS + COMBAT_TOOLS  # Master rolls dice AND decides when combat starts

# ═══════════════════════════════════════════════════════════════
# MODERAI DISPATCH TOOLS (Idea 1: Master → ModerAI → dice)
# ═══════════════════════════════════════════════════════════════

# Tool exposed to the Master: signal roll intent with @username.
# When Master calls this, the engine intercepts and routes to ModerAI,
# which fetches the character sheet from DB and dispatches the actual roll
# (inline button for player / server-side for NPC).
REQUEST_ROLL_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "request_roll_intent",
        "description": (
            "Request a dice roll via ModerAI dispatcher. PREFERRED way to roll dice. "
            "Pass the player's @username (or 'npc' for NPC/monster rolls) and describe "
            "what the roll is for. ModerAI will: (1) fetch the character sheet from DB "
            "via get_character_sheet(@username), (2) determine the modifier, (3) dispatch "
            "the roll — inline button for player rolls OR server-side for NPC, "
            "(4) return the result to you as text. "
            "Use this INSTEAD of roll_dice / request_player_roll when possible. "
            "Rolls happen BEFORE narrative — call this first, then write narrative with the result. "
            "Examples: "
            "request_roll_intent(username='@kane', roll_type='skill', description='Скрытность vs стража DC 13'); "
            "request_roll_intent(username='npc', roll_type='npc_action', description='Атака гоблина-лучника по Кейну, AC 14'); "
            "request_roll_intent(username='@kane', roll_type='save', description='Спасбросок Тела против яда паука, DC 13')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Telegram @username of the player (e.g. '@kane'). Use 'npc' for NPC/monster/trap rolls.",
                },
                "roll_type": {
                    "type": "string",
                    "enum": ["attack", "damage", "save", "skill", "initiative", "death_save", "npc_action", "npc_damage"],
                    "description": "Type of roll. attack/damage/save/skill/initiative/death_save = player rolls (inline button). npc_action/npc_damage = server-side NPC rolls (hidden from players).",
                },
                "description": {
                    "type": "string",
                    "description": "What the roll is for, including DC/AC if known. Example: 'Скрытность vs passive Perception 12' or 'Атака рапирой по гоблину, AC 14'.",
                },
            },
            "required": ["username", "roll_type", "description"],
        },
    },
}

# Tools available to ModerAI when dispatching a roll.
# - get_character_sheet: fetches character by @username from DB
# - request_roll_dice: dispatches inline-button roll to player (waits for button press)
# - roll_npc_dice: server-side roll for NPC/monster/trap (cheatproof, hidden from players)
# - calculate: math helper for modifier computation
MODER_AI_DISPATCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_character_sheet",
            "description": (
                "Fetch a character's sheet from DB by Telegram @username. "
                "Returns: name, level, stats (STR/DEX/CON/INT/WIS/CHA), AC, HP, "
                "proficiencies (skills the character is proficient in), saves proficiencies, "
                "and other relevant modifiers. "
                "Use this to determine the correct modifier for a roll."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Telegram @username of the player (with or without leading @).",
                    },
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_roll_dice",
            "description": (
                "Dispatch an inline-button dice roll to a PLAYER. The player receives a button "
                "in the group chat and rolls themselves. Wait for the result — it comes back "
                "as the tool result. Use for ALL player rolls: attack, damage, save, skill, "
                "initiative, death_save, advantage/disadvantage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string", "description": "Exact character name (from get_character_sheet)."},
                    "count": {"type": "integer", "minimum": 1, "default": 1, "description": "Number of dice. Almost always 1 for a check; >1 for damage rolls."},
                    "sides": {"type": "integer", "enum": [4, 6, 8, 10, 12, 20, 30, 100], "description": "Dice sides. d20 for attacks/saves/checks; d4/d6/d8/d10/d12 for damage."},
                    "modifier": {"type": "integer", "default": 0, "description": "Bonus/penalty pre-filled on the button (e.g. DEX mod + proficiency for Stealth)."},
                    "roll_type": {"type": "string", "enum": ROLL_TYPE_ENUM, "default": "normal", "description": "advantage/disadvantage rolls two d20 and keeps better/worse — count stays 1."},
                    "label": {"type": "string", "description": "What this roll is for, shown to the player. Example: 'Скрытность (Кейн) vs стража DC 13'."},
                    "visible": {"type": "boolean", "default": True, "description": "Almost always true for player rolls."},
                },
                "required": ["character_name", "sides", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roll_npc_dice",
            "description": (
                "Roll dice server-side for an NPC/monster/trap. Result is hidden from players "
                "(visible=false ALWAYS) — cheatproof. Use for NPC attacks, NPC saves, trap damage, "
                "environmental checks, NPC initiative. "
                "The Master will describe the result narratively in the narrative."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "description": "Number of dice. For d20 checks with advantage/disadvantage, always 1."},
                    "sides": {"type": "integer", "enum": [4, 6, 8, 10, 12, 20, 30, 100], "description": "Dice sides."},
                    "modifier": {"type": "integer", "default": 0, "description": "Bonus/penalty."},
                    "roll_type": {"type": "string", "enum": ROLL_TYPE_ENUM, "default": "normal", "description": "advantage/disadvantage only for single d20."},
                    "label": {"type": "string", "description": "What this roll is for. Example: 'Гоблин-лучник атака по Кейну' or 'Урон от копья гоблина'."},
                    "visible": {"type": "boolean", "default": False, "description": "MUST be False for NPC rolls — players never see these."},
                },
                "required": ["count", "sides", "label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Выполни математическое вычисление. Используй ВСЕГДА для любых расчётов: модификаторы характеристик ((значение-10)//2), бонус мастерства, итоговые суммы. Пример: calculate(expression='(18-10)//2 + 2 + 3').",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Математическое выражение. Пример: '(16-10)//2 + 2'"},
                },
                "required": ["expression"],
            },
        },
    },
]

# ═══════════════════════════════════════════════════════════════
# DB-BOT RECONCILIATION TOOLS (Idea 2: DB-bot → Master / ModerAI)
# ═══════════════════════════════════════════════════════════════

# Tool for DB-Bot to ask Master a clarifying question about the narrative.
ASK_MASTER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_master",
        "description": (
            "Ask the Master (Dungeon Master LLM) a clarifying question about the narrative, "
            "when the narrative is missing information needed for DB updates. "
            "Examples: missing damage amount, unclear source of a condition, unnamed NPC. "
            "Master will respond with text — use the answer to call the appropriate DB-write tool. "
            "Use SPARINGLY — only when a reasonable assumption cannot be made from context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Concise question for the Master. Example: 'Сколько урона нанёс гоблин копьём по Кейну?' or 'Какой источник отравления у Кейна?'",
                },
            },
            "required": ["question"],
        },
    },
}

# Tool for DB-Bot to dispatch a missing roll via ModerAI.
# Same shape as request_roll_intent — DB-Bot signals intent, ModerAI dispatches.
DISPATCH_ROLL_TOOL = {
    "type": "function",
    "function": {
        "name": "dispatch_roll",
        "description": (
            "Dispatch a dice roll via ModerAI when the narrative is missing a roll that should "
            "have happened (e.g., Master forgot to roll damage for a hit, or a saving throw was "
            "implied but not rolled). ModerAI fetches the sheet, dispatches the roll (player button "
            "or server-side for NPC), and returns the result. Use the result to call change_hp / "
            "add_condition / etc. as appropriate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Telegram @username of the player (e.g. '@kane'). Use 'npc' for NPC rolls.",
                },
                "roll_type": {
                    "type": "string",
                    "enum": ["attack", "damage", "save", "skill", "initiative", "death_save", "npc_action", "npc_damage"],
                    "description": "Type of roll. attack/damage/save/skill/initiative/death_save = player. npc_action/npc_damage = NPC.",
                },
                "description": {
                    "type": "string",
                    "description": "What the roll is for, including DC/AC if known. Example: 'Урон от копья гоблина по Кейну, 1d6+2'.",
                },
            },
            "required": ["username", "roll_type", "description"],
        },
    },
}

# Augmentation of MASTER_TOOLS and DB_TOOLS happens AFTER GET_TOOLS and DB_TOOLS
# are defined (see bottom of file). The new tools are added there to keep the
# original tool-group definitions intact.

GET_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_character_state",
            "description": "Read current character state from DB: HP, max HP, AC, gold, location, conditions. Use BEFORE any change to verify current values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                },
                "required": ["character_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory",
            "description": "Read character inventory from DB. Use BEFORE adding/removing items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                },
                "required": ["character_name"],
            },
        },
    },
]

DB_TOOLS = GAME_TOOLS + GET_TOOLS  # DB bot handles all database operations

# ── Augment MASTER_TOOLS and DB_TOOLS with new dispatch/reconciliation tools ──
# Done at module level — Master and DB-Bot always have them available; the prompt
# governs when they're actually used (see MASTER_PROMPT → "БРОСКИ ЧЕРЕЗ MODERAI"
# and DB_BOT_PROMPT → "RECONCILIATION С МАСТЕРОМ" sections).
MASTER_TOOLS = MASTER_TOOLS + [REQUEST_ROLL_INTENT_TOOL]
DB_TOOLS = DB_TOOLS + [ASK_MASTER_TOOL, DISPATCH_ROLL_TOOL]



# Extracted from DMEngine class attribute (was a class attr in monolith)
DB_WRITE_TOOL_NAMES = (
"change_hp", "add_condition", "remove_condition",
        "add_item", "remove_item", "change_gold",
        "advance_time", "update_quest", "set_location",
        "change_reputation", "use_resource", "add_world_event",
        "create_location", "get_location", "create_npc", "get_npc",
        "set_npc_relation", "create_lore", "get_lore",
        "set_market_price", "add_economic_event", "add_effect", "create_timer",
        "get_srd_monster", "get_srd_item", "get_srd_spell",
        "level_up_character", "set_ability_score", "add_feature",
        "add_proficiency", "add_spell_known", "update_location_relation",
        "set_character_goal",
)
