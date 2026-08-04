# D&D Bot — Plugin Architecture v0.2

Полный рефакторинг монолита в плагинную систему с **глубоким распилом** God Object'ов.

## Что нового в v0.2

В дополнение к 19 плагинам из v0.1, теперь три God Object'а распилены на пакеты:

### `libs/db/` — 11 файлов вместо `db.py` (3512 строк)

```
libs/db/
├── __init__.py          # re-exports всё для обратной совместимости
├── models.py            # 40 dataclasses
├── base.py              # BaseDatabase: __init__ + _connect + _init_db (schema bootstrap)
├── session_repo.py      # SessionRepoMixin: 26 методов (sessions, players, queue, history)
├── character_repo.py    # CharacterRepoMixin: 50 методов (chars, sheets, hp, conditions, gold, inventory)
├── world_repo.py        # WorldRepoMixin: 30 методов (locations, NPCs, lore, factions, time)
├── combat_repo.py       # CombatRepoMixin: 13 методов (encounters, combatants, initiative)
├── memory_repo.py       # MemoryRepoMixin: 5 методов (semantic memory entries)
├── srd_repo.py          # SrdRepoMixin: 9 методов (SRD cache + monsters/items/spells)
├── economy_repo.py      # EconomyRepoMixin: 12 методов (journal, market, timers, loot)
├── settings_repo.py     # SettingsRepoMixin: 8 методов (player_languages, settings)
├── database.py          # class Database(*all Mixins, BaseDatabase): pass
├── manager.py           # DatabaseManager (multi-session)
└── saved_chars.py       # SavedCharsDB (cross-session library)
```

### `libs/ai/` — 13 файлов вместо `ai_client.py` (2460 строк)

```
libs/ai/
├── __init__.py          # re-exports
├── tools.py             # ROLL_TYPE_ENUM + DICE_TOOLS + GAME_TOOLS + COMBAT_TOOLS
│                        # + GET_TOOLS + ALL_TOOLS + MASTER_TOOLS + DB_TOOLS
│                        # + DB_WRITE_TOOL_NAMES (moved from class attr)
├── prompts.py           # 5 system prompts (MASTER/ASK/RENDERER/NPC_AI/DB_BOT)
├── utils.py             # strip_stray_tags
├── client.py            # OpenAIClient (HTTP wrapper)
├── base.py              # BaseEngine: __init__ (6 LLM clients) + _execute_roll
├── master_engine.py     # MasterEngineMixin: process_master_turn, answer_question,
│                        # generate_encounter, generate_world_seed
├── db_bot_engine.py     # DBBotEngineMixin: process_db_bot, process_dbask,
│                        # process_with_db_bot, _read_db_get_tool, parse_character_sheet
├── renderer_engine.py   # RendererEngineMixin: process_renderer, _fallback_render
├── memory_engine.py     # MemoryEngineMixin: summarize, srd_lookup, generate_weather,
│                        # generate_world_event, extract_currency_from_world,
│                        # extract_quest_hooks, extract_all_npcs_from_lore,
│                        # extract_backstory_relations
├── npc_ai_engine.py     # NpcAIEngineMixin: npc_ai_decision
├── validators.py        # ValidatorsMixin: validate_character_sheet
├── manual_rolls.py      # ManualRollsMixin: resolve_manual_roll
└── engine.py            # class DMEngine(*all Mixins, BaseEngine): pass
```

### `libs/session/` — 11 файлов вместо `session_manager.py` (2715 строк)

```
libs/session/
├── __init__.py          # re-exports
├── base.py              # BaseSessionMixin: __init__ + locks + db_busy + pending_resolve
├── session_lifecycle.py # SessionLifecycleMixin: 13 методов (create/end/players/kick/pvp)
├── round_coordinator.py # RoundCoordinatorMixin: 13 методов (action collection, non-combat queue)
├── combat_coordinator.py # CombatCoordinatorMixin: 15 методов (initiative, encounters, action groups)
├── resolution.py        # ResolutionMixin: 6 методов (resolve_round, _apply_game_actions, summarize)
├── character_service.py # CharacterServiceMixin: 25 методов (HP/rests/gold/inventory/quests)
├── world_service.py     # WorldServiceMixin: 19 методов (NPCs/time/factions/locations)
├── generators.py        # GeneratorsMixin: 6 методов (AI-coupled world/encounter/weather)
├── manual_rolls.py      # ManualRollsMixin: 3 метода (/roll + pending registry)
└── manager.py           # class SessionManager(*all Mixins, BaseSessionMixin): pass
```

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                      main.py (entry point)                   │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
     ┌────────────────┐              ┌────────────────┐
     │  core/ (ядро)   │              │ plugins/ (19)  │
     │  - пустышка     │ ◄─────────── │                │
     │  - bootstrap    │   setup()    │                │
     │  - manager      │              │                │
     │  - hooks        │              │                │
     │  - config       │              │                │
     └────────────────┘              └────────────────┘
                                              │
                                              ▼
                                    ┌────────────────┐
                                    │  libs/ (split)  │
                                    │  - db/ (11)     │
                                    │  - ai/ (13)     │
                                    │  - session/ (11)│
                                    │  - bot_handlers │
                                    │  - creu/        │
                                    │  - config_legacy│
                                    └────────────────┘
```

## Mixin-паттерн (Python partial classes)

Каждый God Object разбит через multiple inheritance:

```python
# libs/db/database.py
class Database(
    SessionRepoMixin,       # 26 методов
    CharacterRepoMixin,     # 50 методов
    WorldRepoMixin,         # 30 методов
    CombatRepoMixin,        # 13 методов
    MemoryRepoMixin,        # 5 методов
    SrdRepoMixin,           # 9 методов
    EconomyRepoMixin,       # 12 методов
    SettingsRepoMixin,      # 8 методов
    BaseDatabase,           # __init__ + _init_db
):
    """Per-session SQLite database. Mixin composition."""
    pass
```

Каждый mixin-файл содержит `class XxxMixin:` с методами, использующими `self._connect()`, `self._row_to_*` из base. Все методы имеют тот же API, что и в монолите.

## Запуск

```bash
# 1. Установить зависимости
pip install -r requirements.txt

# 2. Создать .env
cp .env.example .env
# TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ADMIN_CHAT_ID

# 3. Запустить
python main.py
```

## Структура проекта

```
dnd-bot-plugin/
├── core/                    # Ядро (6 файлов, ~600 строк)
├── libs/                    # Разбитые пакеты + совместимые shims
│   ├── db/                  # 11 файлов (бывший db.py 3512 строк)
│   ├── ai/                  # 13 файлов (бывший ai_client.py 2460 строк)
│   ├── session/             # 11 файлов (бывший session_manager.py 2715 строк)
│   ├── ai_client.py         # shim → libs.ai
│   ├── session_manager.py   # shim → libs.session
│   ├── bot_handlers.py      # оригинальный bot.py (handlers для плагинов)
│   ├── character_parser.py
│   ├── dice_parser.py
│   ├── memory_store.py
│   ├── config_legacy.py     # бывший config.py
│   └── creu/                # character-gen wizard sub-package
├── plugins/                 # 19 плагинов
│   ├── persistence/         # сервис: db_manager + SavedCharsDB
│   ├── ai-engine/           # сервис: DMEngine
│   ├── session-core/        # сервис: SessionManager + crash recovery
│   ├── logging/             # сервис: MarkdownLogger
│   ├── memory/              # сервис: MemoryStore
│   ├── anti-spam/           # cross-cutting: Clean Book Mode
│   ├── lobby-session/       # 13 команд
│   ├── character/           # 4 команды + creu wizard + charpick callback
│   ├── character-state/     # 9 read-only команд
│   ├── world-state/         # 9 команд
│   ├── dice/                # 3 команды + 2 callbacks
│   ├── settings-catalog/    # 5 команд + 1 callback
│   ├── srd-reference/       # 1 команда
│   ├── translator/          # 1 команда + 1 callback
│   ├── admin/               # 10 команд + error_handler
│   ├── narrator/            # ★ главный ДМ-наративщик + extension points
│   ├── combat-driver/       # ★ mixin: initiative combat loop
│   ├── db-bot-phase/        # ★ mixin: DB mutations from narrative
│   └── renderer/            # ★ mixin: MD→HTML + chunked send
├── data/                    # runtime: sessions/*.db
├── characters/              # runtime: uploaded sheets
├── settings/                # 8 world settings + registry.json
├── logs/                    # bot.log + markdown transcripts
├── plugins.toml             # включение/выключение плагинов
├── requirements.txt
├── .env.example
├── main.py
└── README.md
```

## Включение/выключение плагинов

В `plugins.toml`:

```toml
[plugins.combat-driver]
enabled = false  # бои не запустятся, остальное работает

[plugins.renderer]
enabled = false  # нарратив будет plain text
```

Изменения требуют рестарта бота.

## Backward Compatibility

Старый код, который импортирует `from libs.db import Database` или `from libs.ai_client import DMEngine`, **продолжает работать** — пакеты экспортируют те же символы. shims `libs/ai_client.py` и `libs/session_manager.py` переадресуют на новые пакеты.

## Что НЕ менялось

Бизнес-логика handlers в `libs/bot_handlers.py` — **без правок**. Только:
- Импорты переписаны с `from ai_client` → `from libs.ai_client`
- Старые monolith'ы `db.py`, `ai_client.py`, `session_manager.py` разбиты на пакеты с тем же API

Когда захотите тронуть конкретный метод — теперь его легко найти:
- Хотите поменять логику HP? → `libs/db/character_repo.py` → `update_character_hp`
- Хотите поменять промпт мастера? → `libs/ai/prompts.py` → `MASTER_PROMPT`
- Хотите поменять combat initiative? → `libs/session/combat_coordinator.py` → `start_initiative_combat`
- Хотите поменять правило броска кубика? → `libs/ai/base.py` → `_execute_roll`

## Smoke test (пройден)

- 19 плагинов discover + resolve + setup_all — без ошибок
- 62 Telegram handler'а зарегистрированы (как в оригинале)
- 10 shared сервисов в ctx.services
- 5 extension points с правильными mixin'ами:
  - `narrator.combat_turn_started ← [combat-driver]`
  - `narrator.on_master_output ← [db-bot-phase, renderer]`
- Toggle работает: выключение combat-driver/renderer отсоединяет их hooks
- Deps cascade: выключение persistence skip'ает 16 зависимых плагинов
- Backward-compat: `from libs.db import Database` работает через пакет
