"""
D&D Dark Fantasy DM Bot — Configuration (legacy compatibility layer).

Tokens are read from .env at the project root. No hardcoded secrets.
Proxy is configured via .env variables (GLOBAL_PROXY / TELEGRAM_PROXY / OPENAI_PROXY).
See libs/proxy_helper.py for proxy application logic.
"""

import os
import sys
from typing import Optional
from dotenv import load_dotenv

# BASE_DIR — корень проекта (родитель libs/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path, override=False)

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ═══════════════════════════════════════════════════════════════
# API PROVIDER — any OpenAI-compatible endpoint
# (OpenAI, OpenRouter, Together, Groq, DeepSeek, LM Studio, Ollama, vLLM, ...)
# ═══════════════════════════════════════════════════════════════
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not TELEGRAM_BOT_TOKEN:
    print("⚠️  TELEGRAM_BOT_TOKEN не задан в .env — бот не подключится к Telegram", file=sys.stderr)
if not OPENAI_API_KEY:
    print("⚠️  OPENAI_API_KEY не задан в .env — LLM-клиенты не будут работать", file=sys.stderr)

# ═══════════════════════════════════════════════════════════════
# MODEL 1 — MASTER — Narrative + Dice
# ═══════════════════════════════════════════════════════════════
MASTER_MODEL = "google/gemma-4-31b-it"
MASTER_TEMP = 0.7
MASTER_MAX_TOKENS = 65536

# ═══════════════════════════════════════════════════════════════
# MODEL 2 — DB BOT — Database operations
# ═══════════════════════════════════════════════════════════════
DB_MODEL = "google/gemma-4-31b-it"
DB_TEMP = 0.1
DB_MAX_TOKENS = 16384

# ═══════════════════════════════════════════════════════════════
# MODEL 3 — RENDERER — Telegram HTML
# ═══════════════════════════════════════════════════════════════
RENDERER_MODEL = "google/gemma-4-31b-it"
RENDERER_TEMP = 0.0
RENDERER_MAX_TOKENS = 16384

# ═══════════════════════════════════════════════════════════════
# MODEL 4 — MEMORY — Summary, SRD
# ═══════════════════════════════════════════════════════════════
MEMORY_MODEL = "google/gemma-4-31b-it"
MEMORY_TEMP = 0.3
MEMORY_MAX_TOKENS = 8192

# ═══════════════════════════════════════════════════════════════
# MODEL 5 — EMBEDDINGS (world memory / semantic diary)
# ═══════════════════════════════════════════════════════════════
# Any OpenAI-compatible endpoint — exposes /embeddings alongside /chat/completions.
# Works with OpenAI, OpenRouter (when enabled), Together, vLLM, LocalAI, LM Studio, Ollama, etc.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_ENDPOINT = "/embeddings"
MEMORY_TOP_K = 8                    # сколько записей памяти подмешивать в контекст хода
MEMORY_MIN_RAW_FOR_SLEEP = 40       # порог "сырых" записей, чтобы предложить консолидацию
MEMORY_SLEEP_CLUSTER_SIZE = 12      # сколько записей за раз отдаём на консолидацию

# ═══════════════════════════════════════════════════════════════
# PLAYER ROLL BUTTONS
# ═══════════════════════════════════════════════════════════════
# BUG #8: Combat turn timeout is configurable via env. The user-facing bug
# report said the combat turn was limited to 1 minute; the actual code had
# COMBAT_PC_TURN_TIMEOUT_SECONDS = 300 (5 minutes) all along, but the value
# was hardcoded and couldn't be verified or overridden at runtime. Now both
# timeouts are read from .env first, defaulting to 5 min (combat) / 10 min
# (button press) — both well above the perceived "1 minute" issue. If the
# user wants longer (say 10 min for combat), they can set
# COMBAT_PC_TURN_TIMEOUT_SECONDS=600 in .env without touching code.
try:
    PLAYER_ROLL_TIMEOUT_SECONDS = int(os.environ.get(
        "PLAYER_ROLL_TIMEOUT_SECONDS",
        600,  # default: 10 minutes for player to press the dice button
    ))
except (TypeError, ValueError):
    PLAYER_ROLL_TIMEOUT_SECONDS = 600

try:
    # BUG #8: explicitly 5 minutes (300s). Override via env if needed.
    COMBAT_PC_TURN_TIMEOUT_SECONDS = int(os.environ.get(
        "COMBAT_PC_TURN_TIMEOUT_SECONDS",
        300,  # default: 5 minutes for the player to write Дн. during their combat turn
    ))
except (TypeError, ValueError):
    COMBAT_PC_TURN_TIMEOUT_SECONDS = 300

MAX_MANUAL_ROLLS_PER_ROUND = 3       # макс. бросков /rholio на игрока за раунд (каждый — на разную характеристику)

# ═══════════════════════════════════════════════════════════════
# GAME SETTINGS
# ═══════════════════════════════════════════════════════════════
SETTING = "dark_fantasy"
DND_EDITION = "5e_2014"
MAX_PLAYERS = 6
MAX_HISTORY = 50
MASTER_CONTEXT_HISTORY = 30  # Number of history entries to load into Master's context

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
DB_PATH = os.path.join(BASE_DIR, "data", "campaigns.db")
LOG_PATH = os.path.join(BASE_DIR, "logs", "bot.log")
# MD-консолидация (см. ТЗ): весь редактируемый контент живёт в MD/.
# Во время игры движок читает НЕ эти папки, а per-session копии
# data/sessions/<sid>/MD/.../*_copy.md (см. libs/md_store.py).
MD_DIR = os.path.join(BASE_DIR, "MD")
PROMPTS_DIR = os.path.join(MD_DIR, "prompts")
CHARACTERS_DIR = os.path.join(MD_DIR, "characters")
SETTINGS_DIR = os.path.join(MD_DIR, "settings")
SAVED_CHARS_DB = os.path.join(BASE_DIR, "data", "saved_chars.db")

# ═══════════════════════════════════════════════════════════════
# MODEL 6 — TRANSLATOR (optional, async)
# ═══════════════════════════════════════════════════════════════
TRANSLATOR_ENABLED = True
TRANSLATOR_MODEL = "google/gemma-4-31b-it"
TRANSLATOR_TEMP = 0.3
TRANSLATOR_MAX_TOKENS = 8192
TRANSLATOR_BATCH_DELAY = 0.5  # seconds between batched translations

# ═══════════════════════════════════════════════════════════════
# MODEL 7 — NPC AI (optional, emergent world)
# ═══════════════════════════════════════════════════════════════
NPC_AI_ENABLED = True  # master toggle
NPC_AI_MODE = "full"  # "off" | "fight" | "full"
NPC_AI_MODEL = "google/gemma-4-31b-it"
NPC_AI_TEMP = 0.7
NPC_AI_MAX_TOKENS = 2048
# fight mode: batch all NPCs in same initiative slot into one request
NPC_AI_BATCH_SAME_SLOT = True

# ═══════════════════════════════════════════════════════════════
# MODEL 8 — MODER AI (ask filter for /gofyn + dice dispatch)
# ═══════════════════════════════════════════════════════════════
MODER_AI_ENABLED = True
MODER_AI_MODEL = "google/gemma-4-31b-it"
MODER_AI_TEMP = 0.0
MODER_AI_MAX_TOKENS = 256

# ModerAI dispatch — extended role: dispatch dice rolls by @username.
# When True, Master can call `request_roll_intent(@username, ...)` tool,
# which routes through ModerAI. ModerAI fetches the character sheet from DB
# via `get_character_sheet(@username)`, then dispatches either
# `request_roll_dice` (inline button to player) or `roll_npc_dice`
# (server-side, for NPC/monster rolls — cheatproof).
# Result is returned to Master, who writes narrative AFTER knowing the outcome.
# When False, Master falls back to direct roll_dice / request_player_roll calls.
MODER_AI_DISPATCH_ENABLED = True
# Separated config for dispatch calls (uses the same model but allows overrides)
MODER_AI_DISPATCH_MODEL = MODER_AI_MODEL
MODER_AI_DISPATCH_TEMP = 0.1  # slightly higher — needs to reason about modifiers
MODER_AI_DISPATCH_MAX_TOKENS = 1024  # needs room for tool calls + final text

# ═══════════════════════════════════════════════════════════════
# ANTI-SPAM (Clean Book Mode)
# ═══════════════════════════════════════════════════════════════
ANTISPAM_ENABLED = True  # delete Дн., /ask, bot confirmations after narrative
ANTISPAM_DELETE_DELAY = 2.0  # seconds after narrative before deleting
ANTISPAM_DELETE_BATCH_SIZE = 5  # messages per batch to avoid Telegram rate limits
ANTISPAM_DELETE_BATCH_DELAY = 0.3  # seconds between batches
# What to delete: "dn" = Дн. messages, "ask" = /ask messages, "bot_confirm" = bot queue confirmations
ANTISPAM_DELETE_TYPES = {"dn", "ask", "bot_confirm", "bot_error"}
# What NOT to delete: normal chat messages, master narrative, DM commands (/skip etc.)
# Tech commands respond in DM only — see ANTISPAM_DM_ONLY_COMMANDS
ANTISPAM_DM_ONLY_COMMANDS = {"iechyd", "arbedmarwolaeth", "cyflwr", "gorffwys", "aur", "eiddo", "cwest", "amser", "tywydd", "ffactiynau", "rholio", "cyfeirlyfr", "lleoliad", "adnoddau", "modd", "canolbwyntio", "cymeriadnc", "gallu", "taflen"}

# ═══════════════════════════════════════════════════════════════
# ADMIN RESTRICTIONS
# ═══════════════════════════════════════════════════════════════
# Admin is TECHNICIAN ONLY: kick, skip, force resolve, end combat, clear history.
# Admin CANNOT influence narrative, HP, gold, inventory, quests, conditions, etc.
# Only the ИИ-Мастер (neural network) can change game state through Дн. actions.
ADMIN_IS_GAME_MASTER = False
# All game-state commands are read-only for EVERYONE (including admin).
READONLY_COMMANDS = {
    "iechyd", "aur", "eiddo", "gallu", "cyflwr", "cwest", "lleoliad",
    "adnoddau", "amser", "tywydd", "ffactiynau", "perthynasau", "nodau",
    "cymeriadnc", "dinas", "byd", "digwyddiad", "arbedmarwolaeth",
    "gorffwys", "modd", "canolbwyntio", "rholio", "cccg",
}

# ═══════════════════════════════════════════════════════════════
# COMBAT — INITIATIVE SYSTEM
# ═══════════════════════════════════════════════════════════════
COMBAT_INITIATIVE_ENABLED = True  # per-turn collection instead of all-at-once
# Tool name: dechrauymladd (Welsh for "begin combat")
COMBAT_TOOL_NAME = "dechrauymladd"
COMBAT_END_TOOL_NAME = "diweddymladd"

# ═══════════════════════════════════════════════════════════════
# DUAL NARRATIVE (combat + non-combat split)
# ═══════════════════════════════════════════════════════════════
DUAL_NARRATIVE_ENABLED = True  # split players into combat/non-combat groups

# ═══════════════════════════════════════════════════════════════
# SETTINGS / GENRES SYSTEM
# ═══════════════════════════════════════════════════════════════
SETTINGS_DEFAULT_GENRE = "medieval_fantasy"  # SETTINGS_DIR объявлен выше, в блоке PATHS (→ MD/settings)
# /categori command
CATEGORIES_INDEX_FILE = os.path.join(SETTINGS_DIR, "categories.json")
# Minimum complexity score (0-1) for user-submitted settings
SETTINGS_MIN_COMPLEXITY = 0.3

# ═══════════════════════════════════════════════════════════════
# ADMIN TELEMETRY
# ═══════════════════════════════════════════════════════════════
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "-1003311333760")  # Admin channel for telemetry

# ═══════════════════════════════════════════════════════════════
# COMMERCIAL MODE / BILLING (ИТЕРАЦИЯ 10, Разделы 6-7)
# ═══════════════════════════════════════════════════════════════
# COMMERCIAL_MODE=false (или не задана) — играть можно БЕСПЛАТНО везде:
# общий чат, ЛС, любые другие чаты. Токен-гейт отключён, MAIN_CHAT_ID
# не имеет значения. Вся логика живёт в плагине plugins/billing/ — удаление
# папки плагина или выключение его в plugins.toml тоже полностью отключает
# биллинг (безопасный паттерн get_plugin() в хендлерах).
#
# COMMERCIAL_MODE=true — включается вся схема Раздела 6:
#   • общий чат (MAIN_CHAT_ID) — бесплатен;
#   • ЛС закрыта для игры для всех, КРОМЕ TESTERS (у не-тестеров в ЛС
#     работают только /creu+диалог, /cyfieithu, /gwneud, заглушка оплаты);
#   • любой другой чат — реальное списание токенов по игроку (грант
#     30 000 000 при первом входе), меню способа оплаты в /newydd,
#     подтверждение /ymuno.
COMMERCIAL_MODE = os.environ.get("COMMERCIAL_MODE", "false").strip().lower() == "true"

# TESTERS — де-факто модераторы. Список Telegram user_id через запятую.
# У тестеров БЕЗЛИМИТНЫЙ баланс: токен-гейт на них не действует НИ В КАКОМ
# чате (проверка баланса первым делом смотрит на TESTERS и пропускает).
# Им же доступно ручное пополнение игроков: /ychwanegu <user_id> <сумма>.
TESTERS = os.environ.get("TESTERS", "")  # список ID через запятую


def get_testers() -> set:
    """Parse TESTERS env ("id1,id2,...") into a set of ints. Empty → set()."""
    result = set()
    for raw in (TESTERS or "").replace(";", ",").split(","):
        raw = raw.strip()
        if raw and raw.lstrip("-").isdigit():
            result.add(int(raw))
    return result


# MAIN_CHAT_ID — «общий/главный чат бота» (бесплатная игра в commercial-режиме).
# 0 = не задан: тогда бесплатным считается ТОЛЬКО ЛС-поведение по правилам выше,
# а все групповые чаты считаются платными (осторожно — задай явно).
MAIN_CHAT_ID = int(os.environ.get("MAIN_CHAT_ID", "0") or 0)

# Стартовый грант токенов новому игроку (выдаётся РОВНО ОДИН РАЗ за жизнь
# аккаунта, глобально по user_id, не пересоздаётся при выходе/входе в сессию).
TOKEN_GRANT_AMOUNT = int(os.environ.get("TOKEN_GRANT_AMOUNT", "30000000"))

# Глобальная БД биллинга (балансы + журнал операций). НЕ per-session:
# баланс игрока един по user_id для всех его сессий и чатов.
BILLING_DB_PATH = os.path.join(BASE_DIR, "data", "billing.db")

# ═══════════════════════════════════════════════════════════════
# LLM PROVIDERS — трёхуровневый перебор (ИТЕРАЦИЯ 10, Раздел 8)
# ═══════════════════════════════════════════════════════════════
# Формат: Host+API = один «провайдер», под ним НЕСКОЛЬКО моделей. Пока на
# текущем провайдере не перепробованы ВСЕ его модели — к следующему
# провайдеру перехода нет. Каждый НОВЫЙ вызов chat() начинает перебор
# ЗАНОВО с провайдера №1 / модели №1 — никакой памяти между запросами
# (провайдер_state.json не создаётся сознательно).
#
# LLM_PROVIDERS — общий список для всех ролей; <ROLE>_PROVIDERS —
# переопределение под роль (MASTER_PROVIDERS, DB_PROVIDERS, RENDERER_PROVIDERS,
# MEMORY_PROVIDERS, EMBEDDING_PROVIDERS, TRANSLATOR_PROVIDERS, NPC_AI_PROVIDERS,
# MODER_AI_PROVIDERS, MODER_AI_DISPATCH_PROVIDERS, AUDITOR_PROVIDERS).
#
# Если ни LLM_PROVIDERS, ни <ROLE>_PROVIDERS не заданы — используется
# одиночный провайдер из OPENAI_BASE_URL/OPENAI_API_KEY и модели роли
# (старое поведение, полный back-compat).
#
# Пример:
# LLM_PROVIDERS='[
#   {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
#    "api_key": "sk-or-...",
#    "models": ["google/gemma-3-4b-it:free", "google/gemma-3-4b-it"]},
#   {"name": "polza", "base_url": "https://api.polza.ai/v1",
#    "api_key": "sk-polza-...", "models": ["google/gemma-3-4b-it"]}
# ]'
LLM_PROVIDERS = os.environ.get("LLM_PROVIDERS", "")

# Переключение провайдера/модели при этих HTTP-статусах (auth/платёж/доступ).
# Остальные 4xx (400/404/422 — ошибка ЗАПРОСА, а не провайдера) перебор
# не запускают — ошибка сразу уходит наружу.
PROVIDER_SWITCH_STATUSES = {401, 402, 403}

# ИТЕРАЦИЯ 11: «средовые» ошибки, приходящие с 400/403/404. По умолчанию 400/404
# считаются ошибкой ЗАПРОСА и уходят наружу НЕ переключаясь, но если в теле
# ответа есть одна из этих подстрок — провайдер не обслужит НИ эту, НИ любую
# другую модель (гео-блок региона, невалидный ключ, отключённый биллинг,
# заблокированный аккаунт). Такой ответ обязан увести перебор к следующему
# провайдеру. Сравнение case-insensitive по сырому телу ответа.
# Реальный кейс: Google Gemini → «400 FAILED_PRECONDITION: User location is
# not supported for the API use.» — раньше падал наружу и валил /cymeriad,
# хотя остальные API из LLM_PROVIDERS рабочие.
PROVIDER_LEVEL_ERROR_PATTERNS = (
    "failed_precondition",        # Google: гео-блок / окружение
    "user location",              # "User location is not supported for the API use."
    "location is not supported",  # вариант той же гео-ошибки
    "country not supported",      # вариант гео-ошибки
    "api key not valid",          # Google: ключ невалиден
    "api_key_not_valid",
    "incorrect api key",          # OpenAI-стиль
    "invalid_api_key",
    "permission denied",          # Google PERMISSION_DENIED: ключ без доступа к API
    "permission_denied",
    "unauthenticated",            # ключ вообще не передался/не принят
    "billing",                    # "Billing has not been enabled...", лимиты ключа
    "insufficient_quota",         # квота ключа исчерпана (не оживёт от ретрая)
    "quota exceeded",
    "account deactivated",
    "account suspended",
    "has been suspended",
)


def _parse_providers_json(raw: str) -> list:
    """Parse a providers JSON string into a list of validated provider dicts.
    Invalid entries are skipped with a warning (never raises)."""
    import json as _json
    if not raw or not raw.strip():
        return []
    try:
        data = _json.loads(raw)
    except (ValueError, TypeError) as e:
        print(f"⚠️  LLM_PROVIDERS: невалидный JSON ({e}) — используется одиночный "
              f"провайдер из OPENAI_BASE_URL", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print("⚠️  LLM_PROVIDERS: корень должен быть массивом — используется "
              "одиночный провайдер", file=sys.stderr)
        return []
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        base = str(entry.get("base_url") or "").strip().rstrip("/")
        models = entry.get("models")
        if not base or not isinstance(models, list) or not models:
            print(f"⚠️  LLM_PROVIDERS: провайдер без base_url/models пропущен "
                  f"({entry.get('name', '?')})", file=sys.stderr)
            continue
        result.append({
            "name": str(entry.get("name") or base),
            "base_url": base,
            "api_key": str(entry.get("api_key") or ""),
            "models": [str(m) for m in models if str(m).strip()],
        })
    return result


# config.json support: api.llm_providers (тот же формат) перекрывает env.
def _config_llm_providers() -> list:
    raw = _CONFIG_JSON.get("api", {}).get("llm_providers")
    if isinstance(raw, str):
        return _parse_providers_json(raw)
    if isinstance(raw, list):
        import json as _json
        try:
            return _parse_providers_json(_json.dumps(raw, ensure_ascii=False))
        except Exception:
            return []
    return []


def get_providers_for_role(role: str, default_model: str = "",
                           default_base_url: str = "", default_api_key: str = "") -> list:
    """Список провайдеров для роли. Статическая конфигурация БЕЗ состояния:
    каждый вызов читает <ROLE>_PROVIDERS → LLM_PROVIDERS → одиночный
    дефолтный провайдер (default_* или глобальные OPENAI_*)."""
    role_env = os.environ.get(f"{role.upper()}_PROVIDERS", "")
    if role_env.strip():
        providers = _parse_providers_json(role_env)
        if providers:
            return providers
    if LLM_PROVIDERS.strip():
        providers = _parse_providers_json(LLM_PROVIDERS)
        if providers:
            return providers
    cfg_providers = _config_llm_providers()
    if cfg_providers:
        return cfg_providers
    # Fallback: одиночный провайдер из дефолтов (старое поведение).
    return [{
        "name": "default",
        "base_url": (default_base_url or OPENAI_BASE_URL).rstrip("/"),
        "api_key": default_api_key or OPENAI_API_KEY,
        "models": [default_model] if default_model else [],
    }]

# ═══════════════════════════════════════════════════════════════
# ПРОКСИ — чтобы не поднимать туннели
# ═══════════════════════════════════════════════════════════════
# Форматы: http://host:port | http://user:pass@host:port
#          socks5://host:port | socks5h://host:port | socks5://user:pass@host:port
# Пусто = прямое подключение.
#
# GLOBAL_PROXY применяется и к Telegram, и к LLM.
# TELEGRAM_PROXY / OPENAI_PROXY точечно перекрывают GLOBAL_PROXY.
#
# См. libs/proxy_helper.py — там логика применения к httpx (Telegram)
# и aiohttp (LLM-клиенты).
GLOBAL_PROXY = os.environ.get("GLOBAL_PROXY", "").strip() or None
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip() or None
OPENAI_PROXY = os.environ.get("OPENAI_PROXY", "").strip() or None

# Эффективные значения (specific перекрывает global)
TELEGRAM_PROXY_EFFECTIVE = TELEGRAM_PROXY or GLOBAL_PROXY
OPENAI_PROXY_EFFECTIVE = OPENAI_PROXY or GLOBAL_PROXY

# ═══════════════════════════════════════════════════════════════
# EDITION-SPECIFIC RULES
# ═══════════════════════════════════════════════════════════════
# Rules that change between 5e 2014 and 5e 2024.
# The EditionDiff class in libs/srd/edition_diff.py uses DND_EDITION above
# to determine the active ruleset.  This dict is for any additional
# overrides or per-campaign deviations.
EDITION_SPECIFIC_RULES = {
    # Set to True to allow homebrew overrides for specific rules
    "allow_homebrew_overrides": False,
    # If True, the bot enforces 2014-style race-based ability bonuses
    # even when DND_EDITION is "5e_2024" (for backwards compat)
    "force_race_ability_bonuses": DND_EDITION == "5e_2014",
    # If True, Weapon Mastery is available (2024 only, unless overridden)
    "weapon_mastery_enabled": DND_EDITION == "5e_2024",
}



# ═══════════════════════════════════════════════════════════════
# SESSION AUDITOR
# ═══════════════════════════════════════════════════════════════
# The auditor periodically analyzes game sessions for problems:
# character validity, rule compliance, dice roll anomalies, world inconsistencies.
AUDITOR_ENABLED = True
AUDITOR_MODEL = "google/gemma-4-31b-it"
AUDITOR_TEMP = 0.2
AUDITOR_MAX_TOKENS = 4096
AUDITOR_SCHEDULE_HOURS = 24       # How often the daily audit runs
AUDITOR_DICE_ANOMALY_THRESHOLD = 0.01  # Chi-squared p-value threshold
AUDITOR_RULE_COMPLIANCE_TURNS = 20     # How many recent turns to check
AUDITOR_DICE_TURNS = 50               # How many recent dice rolls to check
AUDITOR_REPORTS_DIR = os.path.join(BASE_DIR, "data", "audits")

# ═══════════════════════════════════════════════════════════════
# CONFIG.JSON — user-friendly config layer (Фича 4)
# ═══════════════════════════════════════════════════════════════
# Все настройки, которые раньше приходилось править в этом .py-файле,
# теперь можно менять в config.json в корне проекта:
#   - выбор моделей для всех ролей (+ temperature + max_tokens)
#   - thinking mode по ролям (у Мастера-нарративщика ВСЕГДА включён,
#     что бы ни было написано в config.json — см. get_thinking_payload)
#   - какие функции включены (переводчик, NPC AI, антиспам и т.д.)
#   - игровые параметры (таймауты, лимиты бросков и т.п.)
#
# В config.json НЕТ токенов и ID — они остаются в .env:
#   TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ADMIN_CHAT_ID, прокси.
#
# config.json опционален: если файла нет — работают значения ниже.

CONFIG_JSON_PATH = os.path.join(BASE_DIR, "config.json")


def _load_config_json() -> dict:
    """Read config.json if present. Returns {} when missing/broken."""
    try:
        import json as _json
        if os.path.exists(CONFIG_JSON_PATH):
            with open(CONFIG_JSON_PATH, "r", encoding="utf-8") as _f:
                data = _json.load(_f)
            if isinstance(data, dict):
                return data
            print(f"⚠️  config.json: корень должен быть объектом, получено {type(data).__name__}", file=sys.stderr)
    except Exception as _e:
        print(f"⚠️  config.json не прочитан ({_e}) — используются значения по умолчанию", file=sys.stderr)
    return {}


_CONFIG_JSON = _load_config_json()


def _cfg(section: str, key: str, current, caster=None):
    """Pull `<section>.<key>` from config.json; fall back to the default above."""
    try:
        value = _CONFIG_JSON.get(section, {}).get(key)
        if value is None:
            return current
        return caster(value) if caster else value
    except Exception:
        return current


def _apply_config_json() -> None:
    """Override this module's globals with config.json values (import-time)."""
    _g = globals()

    # ── models ──
    # ИТЕРАЦИЯ 8: локальный запуск — api.base_url из config.json имеет приоритет
    # над OPENAI_BASE_URL из .env. Пустая строка → вернуть env-значение.
    _api_base = _CONFIG_JSON.get("api", {}).get("base_url")
    if isinstance(_api_base, str):
        _api_base = _api_base.strip()
        if _api_base:
            _g["OPENAI_BASE_URL"] = _api_base.rstrip("/")
            print(f"🌐 config.json api.base_url → {_api_base}", file=sys.stderr)
        elif os.environ.get("OPENAI_BASE_URL"):
            _g["OPENAI_BASE_URL"] = os.environ["OPENAI_BASE_URL"].rstrip("/")

    _g["MASTER_MODEL"] = _cfg("models", "master", MASTER_MODEL, str)
    _g["MASTER_TEMP"] = _cfg("models", "master_temperature", MASTER_TEMP, float)
    _g["MASTER_MAX_TOKENS"] = _cfg("models", "master_max_tokens", MASTER_MAX_TOKENS, int)
    _g["DB_MODEL"] = _cfg("models", "db", DB_MODEL, str)
    _g["DB_TEMP"] = _cfg("models", "db_temperature", DB_TEMP, float)
    _g["DB_MAX_TOKENS"] = _cfg("models", "db_max_tokens", DB_MAX_TOKENS, int)
    _g["RENDERER_MODEL"] = _cfg("models", "renderer", RENDERER_MODEL, str)
    _g["RENDERER_TEMP"] = _cfg("models", "renderer_temperature", RENDERER_TEMP, float)
    _g["RENDERER_MAX_TOKENS"] = _cfg("models", "renderer_max_tokens", RENDERER_MAX_TOKENS, int)
    _g["MEMORY_MODEL"] = _cfg("models", "memory", MEMORY_MODEL, str)
    _g["MEMORY_TEMP"] = _cfg("models", "memory_temperature", MEMORY_TEMP, float)
    _g["MEMORY_MAX_TOKENS"] = _cfg("models", "memory_max_tokens", MEMORY_MAX_TOKENS, int)
    _g["EMBEDDING_MODEL"] = _cfg("models", "embedding", EMBEDDING_MODEL, str)
    _g["EMBEDDING_ENDPOINT"] = _cfg("models", "embedding_endpoint", EMBEDDING_ENDPOINT, str)
    _g["TRANSLATOR_MODEL"] = _cfg("models", "translator", TRANSLATOR_MODEL, str)
    _g["TRANSLATOR_TEMP"] = _cfg("models", "translator_temperature", TRANSLATOR_TEMP, float)
    _g["TRANSLATOR_MAX_TOKENS"] = _cfg("models", "translator_max_tokens", TRANSLATOR_MAX_TOKENS, int)
    _g["NPC_AI_MODEL"] = _cfg("models", "npc_ai", NPC_AI_MODEL, str)
    _g["NPC_AI_TEMP"] = _cfg("models", "npc_ai_temperature", NPC_AI_TEMP, float)
    _g["NPC_AI_MAX_TOKENS"] = _cfg("models", "npc_ai_max_tokens", NPC_AI_MAX_TOKENS, int)
    _g["MODER_AI_MODEL"] = _cfg("models", "moder_ai", MODER_AI_MODEL, str)
    _g["MODER_AI_TEMP"] = _cfg("models", "moder_ai_temperature", MODER_AI_TEMP, float)
    _g["MODER_AI_MAX_TOKENS"] = _cfg("models", "moder_ai_max_tokens", MODER_AI_MAX_TOKENS, int)
    _g["MODER_AI_DISPATCH_MODEL"] = _cfg("models", "moder_ai_dispatch", MODER_AI_DISPATCH_MODEL, str)
    _g["MODER_AI_DISPATCH_TEMP"] = _cfg("models", "moder_ai_dispatch_temperature", MODER_AI_DISPATCH_TEMP, float)
    _g["MODER_AI_DISPATCH_MAX_TOKENS"] = _cfg("models", "moder_ai_dispatch_max_tokens", MODER_AI_DISPATCH_MAX_TOKENS, int)
    _g["AUDITOR_MODEL"] = _cfg("models", "auditor", AUDITOR_MODEL, str)
    _g["AUDITOR_TEMP"] = _cfg("models", "auditor_temperature", AUDITOR_TEMP, float)
    _g["AUDITOR_MAX_TOKENS"] = _cfg("models", "auditor_max_tokens", AUDITOR_MAX_TOKENS, int)

    # ── thinking mode ──
    # style: "reasoning" → payload["reasoning"]={"enabled":true}     (OpenRouter-совместимые агрегаторы)
    #        "chat_template_kwargs" → payload["chat_template_kwargs"]={"enable_thinking":true}
    #        "raw" → payload["thinking"]={"type":"enabled"}          (OpenAI o-серии / некоторые прокси)
    #        "off" → thinking вообще не передаётся
    # master ВСЕГДА true — у нарративщика thinking не выключается.
    _g["THINKING_STYLE"] = _cfg("thinking", "style", "reasoning", str)
    _g["THINKING_ROLES"] = {
        "master": True,  # нарративщик — всегда включён (не настраивается)
        "db": bool(_cfg("thinking", "db", False)),
        "renderer": bool(_cfg("thinking", "renderer", False)),
        "memory": bool(_cfg("thinking", "memory", False)),
        "translator": bool(_cfg("thinking", "translator", False)),
        "npc_ai": bool(_cfg("thinking", "npc_ai", False)),
        "moder_ai": bool(_cfg("thinking", "moder_ai", False)),
        "moder_ai_dispatch": bool(_cfg("thinking", "moder_ai_dispatch", False)),
        "auditor": bool(_cfg("thinking", "auditor", False)),
    }

    # ── features (какие функции включены) ──
    _g["TRANSLATOR_ENABLED"] = _cfg("features", "translator_enabled", TRANSLATOR_ENABLED, bool)
    _g["NPC_AI_ENABLED"] = _cfg("features", "npc_ai_enabled", NPC_AI_ENABLED, bool)
    _g["NPC_AI_MODE"] = _cfg("features", "npc_ai_mode", NPC_AI_MODE, str)
    _g["NPC_AI_BATCH_SAME_SLOT"] = _cfg("features", "npc_ai_batch_same_slot", NPC_AI_BATCH_SAME_SLOT, bool)
    _g["MODER_AI_ENABLED"] = _cfg("features", "moder_ai_enabled", MODER_AI_ENABLED, bool)
    _g["MODER_AI_DISPATCH_ENABLED"] = _cfg("features", "moder_ai_dispatch_enabled", MODER_AI_DISPATCH_ENABLED, bool)
    _g["ANTISPAM_ENABLED"] = _cfg("features", "antispam_enabled", ANTISPAM_ENABLED, bool)
    _g["COMBAT_INITIATIVE_ENABLED"] = _cfg("features", "combat_initiative_enabled", COMBAT_INITIATIVE_ENABLED, bool)
    _g["DUAL_NARRATIVE_ENABLED"] = _cfg("features", "dual_narrative_enabled", DUAL_NARRATIVE_ENABLED, bool)
    _g["AUDITOR_ENABLED"] = _cfg("features", "auditor_enabled", AUDITOR_ENABLED, bool)
    _g["ADMIN_IS_GAME_MASTER"] = _cfg("features", "admin_is_game_master", ADMIN_IS_GAME_MASTER, bool)

    # ── game settings ──
    _g["SETTING"] = _cfg("game", "setting", SETTING, str)
    _g["DND_EDITION"] = _cfg("game", "dnd_edition", DND_EDITION, str)
    _g["MAX_PLAYERS"] = _cfg("game", "max_players", MAX_PLAYERS, int)
    _g["MAX_HISTORY"] = _cfg("game", "max_history", MAX_HISTORY, int)
    _g["MASTER_CONTEXT_HISTORY"] = _cfg("game", "master_context_history", MASTER_CONTEXT_HISTORY, int)
    _g["MEMORY_TOP_K"] = _cfg("game", "memory_top_k", MEMORY_TOP_K, int)
    _g["MAX_MANUAL_ROLLS_PER_ROUND"] = _cfg("game", "max_manual_rolls_per_round", MAX_MANUAL_ROLLS_PER_ROUND, int)
    _g["PLAYER_ROLL_TIMEOUT_SECONDS"] = _cfg("game", "player_roll_timeout_seconds", PLAYER_ROLL_TIMEOUT_SECONDS, int)
    _g["COMBAT_PC_TURN_TIMEOUT_SECONDS"] = _cfg("game", "combat_pc_turn_timeout_seconds", COMBAT_PC_TURN_TIMEOUT_SECONDS, int)
    _g["ANTISPAM_DELETE_DELAY"] = _cfg("game", "antispam_delete_delay", ANTISPAM_DELETE_DELAY, float)
    _g["SETTINGS_DEFAULT_GENRE"] = _cfg("game", "settings_default_genre", SETTINGS_DEFAULT_GENRE, str)
    _g["SETTINGS_MIN_COMPLEXITY"] = _cfg("game", "settings_min_complexity", SETTINGS_MIN_COMPLEXITY, float)
    # ── edition-specific rules ──
    # config.json может перекрыть правила редакции; фолбэк — производные
    # от активной редакции DND_EDITION (старое поведение).
    _esr = _CONFIG_JSON.get("edition_specific_rules") or {}
    _g["EDITION_SPECIFIC_RULES"]["allow_homebrew_overrides"] = bool(
        _esr.get("allow_homebrew_overrides", False))
    _g["EDITION_SPECIFIC_RULES"]["force_race_ability_bonuses"] = bool(
        _esr.get("force_race_ability_bonuses", DND_EDITION == "5e_2014"))
    _g["EDITION_SPECIFIC_RULES"]["weapon_mastery_enabled"] = bool(
        _esr.get("weapon_mastery_enabled", DND_EDITION == "5e_2024"))

    # ── anti-spam details ──
    # delete_types / dm_only_commands в config.json — списки строк, в коде — сеты.
    _antispam = _CONFIG_JSON.get("antispam") or {}
    if isinstance(_antispam.get("delete_batch_size"), int):
        _g["ANTISPAM_DELETE_BATCH_SIZE"] = _antispam["delete_batch_size"]
    if isinstance(_antispam.get("delete_batch_delay"), (int, float)):
        _g["ANTISPAM_DELETE_BATCH_DELAY"] = float(_antispam["delete_batch_delay"])
    if isinstance(_antispam.get("delete_types"), list):
        _g["ANTISPAM_DELETE_TYPES"] = {str(t) for t in _antispam["delete_types"]}
    if isinstance(_antispam.get("dm_only_commands"), list):
        _g["ANTISPAM_DM_ONLY_COMMANDS"] = {str(c) for c in _antispam["dm_only_commands"]}

    # ── readonly commands (top-level list) ──
    _ro = _CONFIG_JSON.get("readonly_commands")
    if isinstance(_ro, list):
        _g["READONLY_COMMANDS"] = {str(c) for c in _ro}

    # ── auditor tuning ──
    _audit = _CONFIG_JSON.get("auditor") or {}
    if isinstance(_audit.get("schedule_hours"), (int, float)):
        _g["AUDITOR_SCHEDULE_HOURS"] = int(_audit["schedule_hours"])
    if isinstance(_audit.get("dice_anomaly_threshold"), (int, float)):
        _g["AUDITOR_DICE_ANOMALY_THRESHOLD"] = float(_audit["dice_anomaly_threshold"])
    if isinstance(_audit.get("rule_compliance_turns"), int):
        _g["AUDITOR_RULE_COMPLIANCE_TURNS"] = _audit["rule_compliance_turns"]
    if isinstance(_audit.get("dice_turns"), int):
        _g["AUDITOR_DICE_TURNS"] = _audit["dice_turns"]


_apply_config_json()


def get_thinking_payload(role: str) -> Optional[dict]:
    """Return the JSON payload fragment that enables thinking for a role, or None.

    The narrator role ("master") ALWAYS has thinking enabled — it cannot be
    turned off even via config.json (product decision: narrative quality).
    """
    style = globals().get("THINKING_STYLE", "reasoning")
    enabled_map = globals().get("THINKING_ROLES", {})
    enabled = True if role == "master" else bool(enabled_map.get(role, False))
    if not enabled or style == "off":
        return None
    if style == "chat_template_kwargs":
        return {"chat_template_kwargs": {"enable_thinking": True}}
    if style == "raw":
        return {"thinking": {"type": "enabled"}}
    return {"reasoning": {"enabled": True}}
