"""
AI Client — 4-Model Architecture
1. Master — narrative + dice rolling
2. DB-Bot — database operations, journal, sheet validation
3. Renderer — text → Telegram HTML
4. Memory — summary, SRD, rules lookup
"""

import json
import logging
import asyncio
import random
import re
from typing import Dict, List, Optional

import aiohttp

from character_parser import ParsedCharacter
from config import (
    OPENAI_API_KEY, OPENAI_BASE_URL,
    MASTER_MODEL, MASTER_TEMP, MASTER_MAX_TOKENS,
    DB_MODEL, DB_TEMP, DB_MAX_TOKENS,
    RENDERER_MODEL, RENDERER_TEMP, RENDERER_MAX_TOKENS,
    MEMORY_MODEL, MEMORY_TEMP, MEMORY_MAX_TOKENS,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════

DICE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": (
                "Roll D&D dice. This is the ONLY way to roll dice. "
                "You MUST call this function every time any dice roll is needed: "
                "player attacks, player saving throws, player skill checks, "
                "NPC attacks, NPC saving throws, damage rolls, death saves, "
                "initiative, ANYTHING that requires randomness. "
                "Do NOT make up numbers. Always call this function."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "description": "Number of dice"},
                    "sides": {"type": "integer", "enum": [4, 6, 8, 10, 12, 20, 100], "description": "Dice sides"},
                    "modifier": {"type": "integer", "default": 0, "description": "Bonus/penalty"},
                    "label": {"type": "string", "description": "What this roll is for. Example: 'Эйра рапира атака' or 'Гоблин 1 скимитар'"},
                    "visible": {"type": "boolean", "description": "true = players see. false = hidden from players (NPC rolls, traps, etc.)"},
                },
                "required": ["count", "sides", "visible", "label"],
            },
        },
    }
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
            "description": "Change a character's gold/currency. 1 gp = 10 sp = 100 cp. Use gp as default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "character_name": {"type": "string"},
                    "amount": {"type": "integer", "description": "Positive = gain, negative = spend"},
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
            "description": "Update how an NPC feels about a specific player character.",
            "parameters": {
                "type": "object",
                "properties": {
                    "npc_name": {"type": "string"},
                    "character_name": {"type": "string"},
                    "delta": {"type": "integer"},
                    "reason": {"type": "string", "default": ""},
                },
                "required": ["npc_name", "character_name", "delta"],
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
]

ALL_TOOLS = DICE_TOOLS + GAME_TOOLS
MASTER_TOOLS = DICE_TOOLS  # Master only rolls dice

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


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════

MASTER_PROMPT = """Ты - Дунгеон Мастер для D&D 5e (2024). Тёмное фэнтези. Отвечай на русском.

## КАК БРОСАТЬ КУБИКИ - ТОЛЬКО ЧЕРЕЗ ИНСТРУМЕНТ roll_dice

У тебя есть инструмент `roll_dice`. Ты ОБЯЗАН вызывать его КАЖДЫЙ раз, когда нужен бросок.

### ФОРМАТ БРОСКОВ В ТЕКСТЕ (после получения результатов):

Используй СТРОГО такой формат для каждого броска:

**Обычная атака против AC:**
🎲 [Эйра против Гоблина]: Атака рапирой 21[15+6] vs Класс брони 14 ✅ Попадание!

**С преимуществом:**
🎲 [Эйра против Гоблина]: Атака рапирой (преимущество) 21[15+6;8+6] vs Класс брони 14 ✅ Попадание!

**С помехой:**
🎲 [Эйра в тумане]: Атака рапирой (помеха) 10[4+6;14+6] vs Класс брони 14 ❌ Промах!

**Проверка навыка против СЛ:**
🎲 [Эйра]: Проверка Ловкости рук 22[18+4] vs СЛ 15 ✅ Успех!
🎲 [Эйра]: Проверка Скрытности 9[5+4] vs СЛ 12 ❌ Провал!

**Без DC (инициатива, урон, смертельные спасброски):**
🎲 [Эйра]: Инициатива 18[14+4]
🎲 [Эйра]: Урон рапирой 9[5+4] колющий
🎲 [Эйра]: Смертельный спасбросок 12[12+0] - 1 успех ✓

**Спасбросок от заклинания:**
🎲 [Эйра против Мага]: Спасбросок Ловкости 8[2+6] vs СЛ заклинания 15 ❌ Провал! Урон: 24[8d6: 3+6+5+4+2+4]

**Контест (навык vs навык):**
🎲 [Эйра против Стражника]: Обман 20[16+4;2+4] vs Проницательность 10[5+5] ✅ Успех!

**Криты (только чистая 20 или 1):**
🎲 [Эйра]: Атака рапирой 💥 26[20+6] vs Класс брони 14 ✅ КРИТИЧЕСКИЙ УСПЕХ!
🎲 [Эйра]: Атака рапирой 💀 7[1+6] vs Класс брони 14 ❌ КРИТИЧЕСКИЙ ПРОВАЛ!

**Скрытый бросок NPC (visible=false):**
🎲 [Гоблин против Эйры]: Атака скимитаром 18[14+4] vs Класс брони 14 ✅ Попадание!

**Урон с несколькими кубиками:**
🎲 [Эйра]: Урон Sneak Attack 14[3d6: 4+5+5] + 4 = 18 колющий

### Параметры roll_dice:
- count: сколько кубиков (1 для d20, 2 для 2d6)
- sides: граней (4, 6, 8, 10, 12, 20, 100)
- modifier: модификатор (+5, -2)
- label: что бросаешь. Примеры: "Эйра рапира атака", "Гоблин 1 скимитар", "Огненный шар урон СЛ 15 ЛОВ спас"
- visible: true = игроки видят (их атаки, их спасброски). false = скрыто (NPC, монстры, ловушки)

### Важно:
- Вызывай roll_dice для КАЖДОГО броска ОТДЕЛЬНО
- Не пиши результат сам - система бросит и вернёт
- Критический успех - только при чистой 20 на d20
- Критический провал - только при чистой 1 на d20

## ЧЕРТЫ ЛИЧНОСТИ NPC (RimWorld-style)

Каждый NPC и монстр имеет черты:
- cowardly: убегает при 75% HP, сдаётся при 50%
- brave: держится до 25% HP, +2 к спасброскам страха
- bloodthirsty: не проверяет мораль, добивает раненых
- tactical: использует укрытия, фокусит заклинателей
- honorable: не атакует безоружных, принимает поединки
- pain_averse: избегает драки, предлагает взятку
- sadistic: мучит пленных, не убивает сразу
- greedy: подчиняется за золото, крадёт лут

## ПРАВИЛА ИГРЫ

1. НИКОГДА не говори за игроков. НИКОГДА не предлагай варианты действий.
2. НАТ 20 = лучший исход в рамках реальности, НЕ чит-код.
3. Смерть реальна. 0 ХП → спасброски от смерти (СЛ 10). 3 провала = смерть.
4. Мир жесток, магия опасна, надежды мало, насилие имеет последствия.
5. D&D 5e 2024: преимущество/помеха, укрытие, концентрация, атаки по возможности, экономия действий.
6. Проверки навыков: СЛ 5/10/15/20/25/30. Бросок только когда исход неопределён.
7. Если игроки совершают что-то значимое - предложи обновить квест или фракцию.

## ЗАЩИТА ОТ НАРУШЕНИЙ (Godmoding / Metagaming / Powergaming)

**Godmoding** — игрок придумывает мир, NPC или действия чужого персонажа.
- Если игрок написал "34 гнома в комнате" → Мастер ИГНОРИРУЕТ это и описывает реальность: "Ты вваливаешься в комнату. Там пусто, лишь пыль и запах ладана."
- Если игрок написал "Эйра отошла поссать" → Мастер пишет: "Эйра стоит у двери, рапира в руке. Она никуда не уходила."
- Если игрок описывает чувства/мысли/действия NPC → Мастер переписывает со своей точки зрения.

**Metagaming** — игрок использует знания, которые у него есть, но у персонажа нет.
- Игрок знает из чата, что в соседней комнате засада → его персонаж НЕ знает, пока не проверит/не увидит.
- Игрок знает, что у Эйры 30 зм → его персонаж НЕ знает, сколько у неё монет, пока она не сказала.
- Мастер НЕ позволяет действиям основываться на информации, которую персонаж не мог получить.

**Powergaming** — игрок игнорирует механику, предполагает автоматический успех.
- "Я скрытно подкрадываюсь и убиваю его" → НЕТ. Сначала бросок Скрытности vs Пассивная Внимательность, потом атака с преимуществом, потом урон. Нет автоматического убийства.
- "Я убеждаю короля отдать мне корону" → НЕТ. Бросок Убеждения vs Проницательность короля. Нат 20 ≠ разумная реакция.
- "Я перепрыгиваю пропасть в 30 футов" → НЕТ. Бросок Атлетики СЛ 25. Провал = падение.

Мастер — единственный источник правды о мире. Игроки управляют ТОЛЬКО своим персонажем.

## ЖИВОЙ МИР

Мир живёт даже без игроков: NPC имеют цели, фракции ведут скрытые игры, события происходят независимо.

## ФОРМАТ ОТВЕТА
- От второго лица. Жирным - важные термины и числа.
- После получения результатов roll_dice - опиши что произошло.
- НЕ более 400 слов.
- НЕ вызывай инструменты БД (change_hp, add_item и т.д.) - это делает другая система. Ты только пишешь текст и бросаешь кости."""

ASK_PROMPT = """Ты - Дунгеон Мастер для D&D 5e (2024). Это УТОЧНЯЮЩИЙ ВОПРОС игрока - НЕ игровой ход.

ВАЖНО:
- Это НЕ игровое действие. НЕ продолжай игру, НЕ развивай сюжет.
- Ответь кратко и по существу.
- Можешь напомнить правило, объяснить механику, дать контекст мира.
- НЕ раскрывай тайны, скрытые NPC, будущие повороты.
- Отвечай на русском."""

RENDERER_PROMPT = """Ты - конвертер Markdown → Telegram HTML.

Преобразуй текст в корректный Telegram HTML. Правила:
- <b>жирный</b> вместо **жирный**
- <i>курсив</i> вместо *курсив*
- <code>код</code> вместо `код`
- <blockquote>цитата</blockquote> для строк с 🎲
- Сохраняй эмодзи
- НЕ используй markdown синтаксис в выходе
- НЕ оборачивай в ```html
- Выводи только готовый HTML без лишних пояснений
- Если текст уже содержит HTML-теги - сохрани их"""


DB_BOT_PROMPT = """Ты - Бухгалтер и Администратор базы данных D&D 5e (2024).

Твоя задача: прочитать текст Дунгеон Мастера и вызвать инструменты для обновления игровой базы данных.

## ЧТО ТЫ ДЕЛАЕШЬ

Ты НЕ пишешь нарратив. Ты НЕ придумываешь историю. Ты только вызываешь инструменты БД.

## КАКИЕ ИНСТРУМЕНТЫ ИСПОЛЬЗОВАТЬ

Ты имеешь доступ ко ВСЕМ инструментам базы данных:
- change_hp - КАЖДЫЙ раз, когда персонаж получает урон или лечение
- add_condition / remove_condition - состояния (poisoned, prone, stunned, и т.д.)
- add_item / remove_item - предметы в инвентаре
- change_gold - золото, валюта
- advance_time - время в игре
- update_quest - квесты
- set_location - локации персонажей
- change_reputation - репутация фракций
- use_resource - ресурсы класса (Rage, Ki, Spell Slots)
- add_world_event - мировые события
- create_location / get_location - создание/поиск локаций
- create_npc / get_npc / set_npc_relation - NPC
- create_lore / get_lore - лор
- set_market_price / add_economic_event - экономика
- add_effect - эффекты (проклятия, благословения, яды)
- create_timer - таймеры (яд, длительность заклинаний)
- get_srd_monster / get_srd_item / get_srd_spell - SRD справочник

## ПРАВИЛА ВЫЗОВОВ

1. Вызывай инструменты для КАЖДОГО изменения в тексте Мастера.
2. Если Мастер написал "Лира получает 2 урона" → change_hp(delta=-2).
3. Если Мастер написал "Эйра находит 15 зм" → change_gold(amount=15).
4. Если Мастер написал "Гоблин отравлен" → add_condition(condition="poisoned").
5. Если Мастер написал "прошёл час" → advance_time(hours=1).
6. Если Мастер написал "квест выполнен" → update_quest(status="completed").
7. Если персонажи переместились в новую локацию → set_location.
8. Если появился новый NPC → create_npc.
9. Если произошло экономическое событие → add_economic_event.
10. НЕ пропускай изменения. Если не уверен - вызови инструмент.

## СПЕЦИАЛЬНЫЕ ПРАВИЛА ДЛЯ ГЕНЕРАЦИИ МИРА

Если текст содержит описание мира (не игровой раунд):
- Для КАЖДОЙ упомянутой локации вызови create_location с name, description, type.
- Для КАЖДОГО упомянутого NPC вызови create_npc с name, race, occupation, personality.
- Для КАЖДОГO упомянутого лор-факта вызови create_lore с title, category, content.
- Для КАЖДОЙ упомянутой фракции - add_world_event или create_lore (factions).
- Если указана стартовая локация - set_location для ВСЕХ персонажей.
- НЕ создавай дубли. Если локация/NPC уже есть - пропусти.

## ВАЖНО

- Ты можешь вызывать НЕСКОЛЬКО инструментов подряд. Нет лимита.
- После каждого вызова тебе вернут результат "OK". Продолжай вызывать следующие.
- Если в тексте нет изменений состояния - не вызывай ничего.
- НЕ вызывай roll_dice - кости уже брошены Мастером.
- НЕ придумывай изменения, которых нет в тексте."""

class OpenAIClient:
    """Universal OpenAI-compatible client"""

    def __init__(self, model: str, temperature: float = 0.8, max_tokens: int = 4096,
                 base_url: str = None, api_key: str = None):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or OPENAI_API_KEY
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://dnd-bot.local",
            "X-Title": "D&D Dark Fantasy Bot",
        }

    async def chat(self, messages: List[Dict], system_prompt: Optional[str] = None,
                   tools: Optional[List[Dict]] = None, tool_choice: Optional[str] = "auto",
                   max_tokens: Optional[int] = None, retries: int = 3) -> Dict:
        # Trim messages to avoid payload bloat (keep last 25 + system)
        trimmed_messages = messages[-25:] if len(messages) > 25 else messages

        payload = {
            "model": self.model,
            "messages": [],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})
        payload["messages"].extend(trimmed_messages)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        # Log payload size for debugging
        raw_payload = json.dumps(payload, ensure_ascii=False)
        payload_size = len(raw_payload.encode('utf-8'))
        logger.info(f"[chat] Payload size: {payload_size} bytes, model: {self.model}, messages: {len(payload['messages'])}")
        logger.info(f"[PAYLOAD_RAW] {raw_payload[:4000]}")

        last_error = None
        for attempt in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self.base_url}/chat/completions",
                        headers=self.headers,
                        json=payload,
                    ) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            raise Exception(f"API {response.status}: {error_text}")
                        # Read response fully with explicit encoding
                        text = await response.text(encoding='utf-8')
                        return json.loads(text)
            except (aiohttp.ClientError, aiohttp.http_exceptions.TransferEncodingError, 
                    aiohttp.ClientPayloadError, ConnectionResetError) as e:
                last_error = e
                wait = min(2 ** attempt, 30)  # cap at 30s
                logger.warning(f"[chat] Attempt {attempt+1}/{retries} failed: {type(e).__name__}: {e}. Retrying in {wait}s...")
                await asyncio.sleep(wait)
            except Exception as e:
                # Non-retryable error
                raise

        raise Exception(f"API failed after {retries} attempts: {last_error}")

    async def stream_chat(self, messages: List[Dict], system_prompt: Optional[str] = None):
        trimmed_messages = messages[-25:] if len(messages) > 25 else messages
        payload = {
            "model": self.model,
            "messages": [],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})
        payload["messages"].extend(trimmed_messages)

        timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API {response.status}: {error_text}")
                async for line in response.content:
                    line = line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                            if chunk["choices"] and chunk["choices"][0]["delta"].get("content"):
                                yield chunk["choices"][0]["delta"]["content"]
                        except (json.JSONDecodeError, KeyError):
                            continue


class DMEngine:
    """4-Model Engine: Master, DB-Bot, Renderer, Memory"""

    def __init__(self, base_url: str = None, api_key: str = None, db_manager=None):
        self.master = OpenAIClient(MASTER_MODEL, MASTER_TEMP, MASTER_MAX_TOKENS, base_url, api_key)
        self.db_bot = OpenAIClient(DB_MODEL, DB_TEMP, DB_MAX_TOKENS, base_url, api_key)
        self.renderer = OpenAIClient(RENDERER_MODEL, RENDERER_TEMP, RENDERER_MAX_TOKENS, base_url, api_key)
        self.memory = OpenAIClient(MEMORY_MODEL, MEMORY_TEMP, MEMORY_MAX_TOKENS, base_url, api_key)
        self.db_manager = db_manager

    def _execute_roll(self, args: Dict) -> Dict:
        count = args.get("count", 1)
        sides = args.get("sides", 20)
        modifier = args.get("modifier", 0)
        label = args.get("label", "roll")
        visible = args.get("visible", True)

        rolls = [random.randint(1, sides) for _ in range(count)]
        natural = rolls[0] if rolls else 0
        total = sum(rolls) + modifier

        roll_str = " + ".join(str(r) for r in rolls)
        mod_str = f"{modifier:+d}" if modifier else ""

        crit = ""
        if sides == 20 and natural == 20:
            crit = " 💥 КРИТИЧЕСКИЙ УСПЕХ!"
        elif sides == 20 and natural == 1:
            crit = " 💀 КРИТИЧЕСКИЙ ПРОВАЛ!"

        display = f"🎲 {label}: {count}d{sides}{mod_str} → [{roll_str}]{mod_str} = **{total}**{crit}"

        return {
            "visible": visible,
            "display": display,
            "result": f"nat {natural}, total {total}, rolls {rolls}",
        }

    async def process_master_turn(
        self,
        session_history: List[Dict],
        player_actions: Dict[str, str],
        character_sheets: Optional[List[str]] = None,
        context: str = "",
        roll_mode: str = "mixed",
        summary: str = "",
        db_journal: str = "",
    ) -> Dict[str, str]:
        """
        Step 1: Master (Kimi) writes narrative + rolls dice.
        Returns: {"player_text": str, "gm_log": str, "raw_narrative": str}
        """
        MAX_ITERATIONS = 15
        iteration = 0

        messages = []
        for msg in session_history[-30:]:
            messages.append(msg)

        actions_text = "\n".join(f"{nick}: {action}" for nick, action in player_actions.items())
        sheets_text = ""
        if character_sheets:
            sheets_text = "\n\nЛИСТЫ ПЕРСОНАЖЕЙ:\n" + "\n---\n".join(character_sheets)

        mode_instructions = {
            "gmroll": "ALL rolls use visible=false (hidden from players). Only narrative descriptions.",
            "playerroll": "ALL rolls use visible=true (players see everything). Full transparency.",
            "mixed": "visible=true for PLAYER rolls, visible=false for NPC/monster rolls.",
        }
        mode_text = mode_instructions.get(roll_mode, mode_instructions["mixed"])
        summary_block = f"\n\n📜 СВОДКА КАМПАНИИ:\n{summary}" if summary else ""
        journal_block = f"\n\n📊 ЖУРНАЛ БД (последние операции):\n{db_journal}" if db_journal else ""

        turn_prompt = f"""Игроки объявили действия:

{actions_text}{sheets_text}
{context}{summary_block}{journal_block}

Разреши по правилам D&D 5e 2024.

ВАЖНО: Для КАЖДОГО броска вызывай инструмент roll_dice. Не придумывай числа.
НЕ вызывай change_hp, add_item и другие инструменты БД - это делает другая система.
{mode_text}
- НИКОГДА не говори за игроков. Персонажи МОГУТ умирать.
- Если HP падает до 0 - опиши падение и начни спасброски от смерти.
- Учитывай текущую погоду и время суток.

Отвечай на русском."""

        messages.append({"role": "user", "content": turn_prompt})

        try:
            response = await self.master.chat(
                messages, system_prompt=MASTER_PROMPT, tools=MASTER_TOOLS,
            )

            visible_rolls: List[str] = []
            hidden_rolls: List[str] = []
            raw_narrative = ""

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                finish_reason = response["choices"][0].get("finish_reason", "unknown")
                logger.info(f"[master] Iteration {iteration}, finish_reason={finish_reason}")

                tool_calls = choice.get("tool_calls")
                if tool_calls:
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"])
                        if tool_name == "roll_dice":
                            result = self._execute_roll(args)
                            result["call_id"] = tc["id"]
                            tool_results.append(result)
                            if result.get("visible"):
                                visible_rolls.append(result["display"])
                            else:
                                hidden_rolls.append(result["display"])
                        else:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: Master should not call {tool_name}",
                            })

                    messages.append({
                        "role": "assistant",
                        "content": choice.get("content") or "",
                        "tool_calls": tool_calls,
                    })
                    for tr in tool_results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tr["call_id"],
                            "content": tr["result"],
                        })

                    response = await self.master.chat(
                        messages, system_prompt=MASTER_PROMPT, tools=MASTER_TOOLS,
                    )
                    continue

                # Final narrative
                raw_narrative = choice.get("content") or ""
                if visible_rolls:
                    dice_block = "📑 **Броски:**\n" + "\n".join(visible_rolls) + "\n\n"
                else:
                    dice_block = ""

                player_text = dice_block + raw_narrative
                gm_log = "\n".join(hidden_rolls) if hidden_rolls else ""

                return {
                    "player_text": player_text,
                    "gm_log": gm_log,
                    "raw_narrative": raw_narrative,
                }

            logger.error(f"[master] Max iterations reached")
            return {
                "player_text": "[Ошибка: Мастер зациклился на костях]",
                "gm_log": "",
                "raw_narrative": "",
            }

        except Exception as e:
            logger.error(f"Master error: {e}")
            return {
                "player_text": f"*[Ошибка Мастера: {str(e)}]*",
                "gm_log": f"ERROR: {e}",
                "raw_narrative": "",
            }

    async def process_db_bot(
        self,
        raw_narrative: str,
        context: str = "",
        character_sheets: Optional[List[str]] = None,
        session_id: str = "",
    ) -> List[Dict]:
        """
        Step 2: DB-Bot reads narrative + queries DB via GET tools, then returns DB actions.
        Returns: list of {"tool_name": str, "arguments": dict}
        """
        MAX_ITERATIONS = 15
        iteration = 0
        game_actions: List[Dict] = []

        sheets_text = ""
        if character_sheets:
            sheets_text = chr(10) + chr(10) + "ЛИСТЫ ПЕРСОНАЖЕЙ:" + chr(10) + chr(10).join(["---" + chr(10) + s for s in character_sheets])

        prompt = f"""Текст Дунгеон Мастера:
---
{raw_narrative}
---
{context}{sheets_text}

Проанализируй текст и вызови ВСЕ необходимые инструменты базы данных.
Если нужно изменить золото, HP, предметы — СНАЧАЛА вызови get_character_state чтобы узнать текущее значение.
Если в тексте нет изменений состояния - не вызывай ничего, просто ответь "Нет изменений".
"""

        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.db_bot.chat(
                messages, system_prompt=DB_BOT_PROMPT, tools=DB_TOOLS,
            )

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                tool_calls = choice.get("tool_calls")

                if tool_calls:
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"])

                        # --- GET TOOLS (read from DB) ---
                        db = self.db_manager.get_db(session_id) if self.db_manager and session_id else None
                        if tool_name == "get_character_state" and db and session_id:
                            char_name = args.get("character_name", "")
                            chars = db.get_session_characters(session_id)
                            found = None
                            for c in chars:
                                if char_name.lower() in c.name.lower() or c.name.lower() in char_name.lower():
                                    found = c
                                    break
                            if found:
                                gold = db.get_gold_balance(session_id, found.id)
                                loc = db.get_location(session_id, found.id)
                                conds = db.get_conditions(session_id, found.id)
                                result_text = json.dumps({
                                    "name": found.name,
                                    "hp": found.hp,
                                    "max_hp": found.max_hp,
                                    "ac": found.ac,
                                    "gold_gp": gold.get("gp", 0),
                                    "location": loc.location_name if loc else "",
                                    "conditions": [cc.condition for cc in conds],
                                }, ensure_ascii=False)
                            else:
                                result_text = f'{{"error": "Character {char_name} not found"}}'
                            tool_results.append({"call_id": tc["id"], "result": result_text})

                        elif tool_name == "get_inventory" and db and session_id:
                            char_name = args.get("character_name", "")
                            chars = db.get_session_characters(session_id)
                            found = None
                            for c in chars:
                                if char_name.lower() in c.name.lower() or c.name.lower() in char_name.lower():
                                    found = c
                                    break
                            if found:
                                inv = db.get_inventory(session_id, found.id)
                                result_text = json.dumps({
                                    "items": [{"name": i["item"], "qty": i["qty"]} for i in inv]
                                }, ensure_ascii=False)
                            else:
                                result_text = f'{{"error": "Character {char_name} not found"}}'
                            tool_results.append({"call_id": tc["id"], "result": result_text})

                        # --- WRITE TOOLS (record for later) ---
                        elif tool_name in (
                            "change_hp", "add_condition", "remove_condition",
                            "add_item", "remove_item", "change_gold",
                            "advance_time", "update_quest", "set_location",
                            "change_reputation", "use_resource", "add_world_event",
                            "create_location", "get_location", "create_npc", "get_npc",
                            "set_npc_relation", "create_lore", "get_lore",
                            "set_market_price", "add_economic_event", "add_effect", "create_timer",
                            "get_srd_monster", "get_srd_item", "get_srd_spell",
                        ):
                            game_actions.append({"tool_name": tool_name, "arguments": args})
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"OK: {tool_name} recorded.",
                            })
                        else:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: unknown tool {tool_name}",
                            })

                    messages.append({
                        "role": "assistant",
                        "content": choice.get("content") or "",
                        "tool_calls": tool_calls,
                    })
                    for tr in tool_results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tr["call_id"],
                            "content": tr["result"],
                        })

                    response = await self.db_bot.chat(
                        messages, system_prompt=DB_BOT_PROMPT, tools=DB_TOOLS,
                    )
                    continue

                # No more tool calls
                return game_actions

            logger.error(f"[db_bot] Max iterations reached")
            return game_actions

        except Exception as e:
            logger.error(f"DB-Bot error: {e}")
            return game_actions

    async def process_renderer(self, text: str) -> str:
        """
        Step 3: Renderer (Llama 3.3 70B) converts text to Telegram HTML.
        Returns: HTML string.
        """
        try:
            messages = [{"role": "user", "content": text}]
            response = await self.renderer.chat(messages, system_prompt=RENDERER_PROMPT)
            html = response["choices"][0]["message"].get("content", text)
            # Strip any markdown code blocks if the model wrapped HTML
            html = html.strip()
            if html.startswith("```html"):
                html = html[7:]
            if html.startswith("```"):
                html = html[3:]
            if html.endswith("```"):
                html = html[:-3]
            return html.strip()
        except Exception as e:
            logger.error(f"Renderer error: {e}")
            # Fallback: basic md_to_html
            return self._fallback_render(text)

    @staticmethod
    def _fallback_render(text: str) -> str:
        import re as _re
        text = _re.sub(r'\*\*(.+?)\*\*', r'<b></b>', text)
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i></i>', text)
        text = _re.sub(r'`(.+?)`', r'<code></code>', text)
        # Whole message is wrapped in <blockquote expandable> externally
        return text

    async def process_with_db_bot(self, raw_text: str, context: str = "") -> List[Dict]:
        """
        Universal wrapper: after ANY AI text, run DB-Bot to extract and apply DB actions.
        Returns list of game_actions.
        """
        if not raw_text or not raw_text.strip():
            return []
        return await self.process_db_bot(raw_text, context=context)

    async def answer_question(
        self,
        session_history: List[Dict],
        question: str,
        player_name: str,
    ) -> str:
        messages = []
        for msg in session_history[-30:]:
            messages.append(msg)
        messages.append({
            "role": "user",
            "content": f"[УТОЧНЕНИЕ - НЕ игровой ход]\n\nИгрок **{player_name}** спрашивает: {question}\n\nОтветь кратко.",
        })
        try:
            response = await self.master.chat(messages, system_prompt=ASK_PROMPT, max_tokens=1024)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Ask error: {e}")
            return f"*[Ошибка: {str(e)}]*"

    async def summarize(self, text: str) -> str:
        # Trim to avoid OpenRouter 400 on oversized payloads
        MAX_CHARS = 15000
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n...[truncated]"
        messages = [{
            "role": "user",
            "content": f"Создай детальную сводку кампании D&D для Dungeon Master. Сохрани ВСЕ важные детали.\n\nИстория:\n{text}",
        }]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Summarize error: {e}")
            return ""

    async def validate_character_sheet(self, sheet_text: str) -> Dict[str, str]:
        """Validate character sheet using DB-Bot — rules expert"""
        prompt = f"""Ты - эксперт по правилам D&D 5e (2024). Проверь лист персонажа.

ВАЖНЫЕ ПРАВИЛА ПРОВЕРКИ:
1. Будь ЛОЯЛЬНЫМ и НЕ ПРИДИРЧИВЫМ. Лист должен быть ОЧЕВИДНО сломанным, чтобы получить NEEDS_FIX.
2. НЕ ПРИДУМЫВАЙ правила. Если не уверен - молчи.
3. HP 1-го уровня = МАКСИМУМ хит-куба + мод CON. Запись "1d8 + 0" или просто "8" для плут с CON 10 - КОРРЕКТНА.
4. Expertise (Экспертиза) у плутов - на 1-м уровне. Бард тоже получает на 3-м. Не пиши, что это ошибка.
5. Навыки зависят от ПРЕДЫСТОРИИ, не только класса. Любой класс может взять Выживание, Акробатику и т.д. через предысторию.
6. Бонус мастерства +2 на 1-м уровне - НОРМАЛЬНО для ВСЕХ классов.
7. AC 10 + DEX для безбронного - нормально. AC 14 с DEX 19 - математически верно.
8. Стартовое снаряжение: если указаны предметы из стартового набора класса + предыстории - это НЕ ошибка.
9. Заклинания: проверяй только если класс точно не может их колдовать. Плут-чародей (Arcane Trickster) и воин-чародей (Eldritch Knight) - полукастеры.
10. Значения характеристик 8-20 на старте - допустимы. Point Buy допускает 8-15 до расовых бонусов. После расовых - до 17. Если есть фича дающая +2/+1 - до 18.
11. Пассивная Проницательность = 10 + бонус навыка. Это нормально.
12. НЕ придирайся к формулировкам. "1d8 + CON 0" = "8 HP" - это ОДНО И ТО ЖЕ.
13. НЕ отмечай как ошибку то, что может быть вариантом правил или домашним правилом Мастера.
14. Если лист выглядит разумно и математически корректен - ставь VALID.

Что считать ошибкой:
- HP больше максимума хит-куба + CON × уровень
- AC явно невозможное (например, 30 без магических предметов)
- Заклинания, которые класс точно не может знать
- Характеристики > 20 на 1-м уровне (без явных причин)
- Отрицательные HP
- Очевидные математические ошибки

Лист персонажа:
---
{sheet_text}
---

Ответь СТРОГО в формате:
**Вердикт**: [VALID / NEEDS_FIX / REJECT]
**Ошибки**: список найденных проблем (только реальные ошибки, не предпочтения)
**Рекомендации**: что можно улучшить (опционально, кратко)
**Исправления**: краткое описание необходимых исправлений (только если NEEDS_FIX или REJECT)

Если вердикт VALID - поля "Ошибки" и "Исправления" должны быть пустыми или содержать "Нет"."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages)
            content = response["choices"][0]["message"].get("content", "")
            verdict = "UNKNOWN"
            if "VALID" in content.upper():
                verdict = "VALID"
            elif "NEEDS_FIX" in content.upper():
                verdict = "NEEDS_FIX"
            elif "REJECT" in content.upper():
                verdict = "REJECT"
            return {"verdict": verdict, "details": content}
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {"verdict": "ERROR", "details": str(e)}

    async def srd_lookup(self, query: str) -> str:
        srd_prompt = f"""Ты - справочник по правилам D&D 5e (2024). Ответь кратко и точно.
Если вопрос про конкретное заклинание, класс, способность - приведи основные цифры.
Вопрос: {query}"""
        messages = [{"role": "user", "content": srd_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "[Нет данных]")
        except Exception as e:
            logger.error(f"SRD error: {e}")
            return f"*[Ошибка SRD: {e}]*"

    async def generate_encounter(self, context: str, party_level: int, party_size: int, terrain: str = "") -> str:
        enc_prompt = f"""Придумай случайное столкновение для D&D 5e (2024). Тёмное фэнтези.
Контекст: {context} | Уровень партии: {party_level} | Размер: {party_size} | Местность: {terrain or "любая"}
Ответь: **Столкновение**, **Описание**, **Существа**, **Сложность**, **Возможности**."""
        messages = [{"role": "user", "content": enc_prompt}]
        try:
            response = await self.master.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Encounter error: {e}")
            return f"*[Ошибка: {e}]*"

    async def generate_weather(self, season: str, terrain: str = "", current_weather: str = "") -> str:
        w_prompt = f"""Опиши погоду для D&D в 2-3 предложения.
Сезон: {season} | Местность: {terrain or "открытая"} | Предыдущая: {current_weather or "ясно"}
Укажи: погоду, температуру, видимость, эффект на игру. Отвечай на русском."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "Ясная погода, умеренная температура.")
        except Exception as e:
            logger.error(f"Weather error: {e}")
            return "Ясная погода, умеренная температура."

    async def generate_world_event(self, context: str) -> str:
        w_prompt = f"""Придумай событие в мире D&D, которое происходит НЕЗАВИСИМО от игроков.
Контекст: {context}
Формат: **Событие**, **Описание**, **Влияние**. Не более 100 слов."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"World event error: {e}")
            return ""

    async def generate_world_seed(self, theme: str = "dark fantasy") -> str:
        w_prompt = f"""Создай начальный мир для кампании D&D 5e. Тема: {theme}.

Опиши мир текстом: ключевые локации, важные NPC, лор, фракции, экономику.
НЕ вызывай инструменты — просто опиши мир текстом. Закончи описание полностью, не обрывай посреди предложения. На русском."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.master.chat(messages, max_tokens=8000)
            text = response["choices"][0]["message"].get("content", "")
            # If model returned tool calls instead of text, return empty to trigger fallback
            if not text or text.strip().startswith("{"):
                return ""
            return text
        except Exception as e:
            logger.error(f"World gen error: {e}")
            return "*[Ошибка генерации мира]*"    # ═══════════════════════════════════════════════════════════
    # CHARACTER SHEET PARSING - Llama 4 (DB-Bot)
    # ═══════════════════════════════════════════════════════════

    async def parse_character_sheet(self, sheet_text: str) -> Optional[ParsedCharacter]:
        """Parse raw character sheet text using LLM. Returns dict or None."""
        from character_parser import ParsedCharacter
        prompt = f"""Ты - парсер листов персонажей D&D 5e (2024).

Прочитай текст листа персонажа и верни СТРОГО JSON в таком формате:
{{
  "name": "Имя персонажа",
  "race": "Раса",
  "class_name": "Класс",
  "level": 1,
  "background": "Предыстория",
  "alignment": "Мировоззрение",
  "strength": 10,
  "dexterity": 10,
  "constitution": 10,
  "intelligence": 10,
  "wisdom": 10,
  "charisma": 10,
  "hp": 8,
  "max_hp": 8,
  "ac": 14,
  "speed": 30,
  "hit_dice": 8,
  "gold": 15,
  "proficiencies": ["Воровские инструменты", "Игральные карты"],
  "skills": ["Проницательность", "Расследование", "Запугивание"],
  "languages": ["Общий", "Эльфийский"],
  "inventory": ["Рапира", "Кинжал (2 шт.)", "Воровские инструменты"],
  "spells": [],
  "features": ["Expertise", "Sneak Attack"],
  "backstory": "Краткая предыстория"
}}

ПРАВИЛА:
1. Имя может быть в рамке типа ║ ЭЙРА «ЧЕРТИЛА» ВАЛЬЕНТЕ ║ - вытащи его.
2. Характеристики ищи в таблицах, key-value, любом формате.
3. HP = максимум хит-куба + мод CON (если явно не указано).
4. AC бери как указано, если не указано - 10 + мод DEX.
5. Золото ищи "15 зм", "15 gp", "стартового капитала" и т.д.
6. Навыки, языки, снаряжение - списком.
7. Если чего-то нет - используй значения по умолчанию (10 для статов, 0 для золота).
8. НЕ придумывай. Только то, что есть в тексте.
9. Ответь ТОЛЬКО JSON. Без markdown, без объяснений.

Лист персонажа:
---
{sheet_text}
---"""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages, system_prompt=None, tools=None, tool_choice=None)
            raw = response["choices"][0]["message"].get("content", "")
            # Strip markdown code blocks if present
            logger.info(f"[parse_character_sheet] AI raw response: {raw[:500]}...")
            raw = raw.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            data = json.loads(raw)
            logger.info(f"[parse_character_sheet] Parsed JSON keys: {list(data.keys())}")
            # Build ParsedCharacter from dict
            char = ParsedCharacter()
            for key, value in data.items():
                if hasattr(char, key):
                    setattr(char, key, value)
            # Ensure hp/max_hp consistency
            if char.hp and not char.max_hp:
                char.max_hp = char.hp
            if char.max_hp and not char.hp:
                char.hp = char.max_hp
            # Calculate AC if missing
            if char.ac == 10 and char.dexterity > 10:
                char.ac = 10 + char.get_modifier("dexterity")
            return char
        except Exception as e:
            logger.error(f"Sheet parsing error: {e}")
            return None


