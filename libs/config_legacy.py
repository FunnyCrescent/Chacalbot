"""
D&D Dark Fantasy DM Bot — Configuration (legacy compatibility layer).

Tokens are read from .env at the project root. No hardcoded secrets.
Proxy is configured via .env variables (GLOBAL_PROXY / TELEGRAM_PROXY / OPENAI_PROXY).
See libs/proxy_helper.py for proxy application logic.
"""

import os
import sys
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
MASTER_MODEL = "xiaomi/mimo-v2.5"
MASTER_TEMP = 0.7
MASTER_MAX_TOKENS = 65536

# ═══════════════════════════════════════════════════════════════
# MODEL 2 — DB BOT — Database operations
# ═══════════════════════════════════════════════════════════════
DB_MODEL = "xiaomi/mimo-v2.5"
DB_TEMP = 0.1
DB_MAX_TOKENS = 16384

# ═══════════════════════════════════════════════════════════════
# MODEL 3 — RENDERER — Telegram HTML
# ═══════════════════════════════════════════════════════════════
RENDERER_MODEL = "google/gemma-3-4b-it"
RENDERER_TEMP = 0.0
RENDERER_MAX_TOKENS = 16384

# ═══════════════════════════════════════════════════════════════
# MODEL 4 — MEMORY — Summary, SRD
# ═══════════════════════════════════════════════════════════════
MEMORY_MODEL = "deepseek/deepseek-v4-flash"
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
PLAYER_ROLL_TIMEOUT_SECONDS = 600   # сколько ждать нажатия кнопки, прежде чем считать бросок пропущенным
COMBAT_PC_TURN_TIMEOUT_SECONDS = 300  # сколько ждать Дн. от игрока в бою, прежде чем автопропустить ход (5 мин)
MAX_MANUAL_ROLLS_PER_ROUND = 3       # макс. бросков /rholio на игрока за раунд (каждый — на разную характеристику)

# ═══════════════════════════════════════════════════════════════
# GAME SETTINGS
# ═══════════════════════════════════════════════════════════════
SETTING = "dark_fantasy"
DND_EDITION = "5e_2024"
MAX_PLAYERS = 6
MAX_HISTORY = 50
MASTER_CONTEXT_HISTORY = 30  # Number of history entries to load into Master's context

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
DB_PATH = os.path.join(BASE_DIR, "data", "campaigns.db")
LOG_PATH = os.path.join(BASE_DIR, "logs", "bot.log")
CHARACTERS_DIR = os.path.join(BASE_DIR, "characters")
SAVED_CHARS_DB = os.path.join(BASE_DIR, "data", "saved_chars.db")

# ═══════════════════════════════════════════════════════════════
# MODEL 6 — TRANSLATOR (optional, async)
# ═══════════════════════════════════════════════════════════════
TRANSLATOR_ENABLED = True
TRANSLATOR_MODEL = "ibm-granite/granite-4.1-8b"
TRANSLATOR_TEMP = 0.3
TRANSLATOR_MAX_TOKENS = 8192
TRANSLATOR_BATCH_DELAY = 0.5  # seconds between batched translations

# ═══════════════════════════════════════════════════════════════
# MODEL 7 — NPC AI (optional, emergent world)
# ═══════════════════════════════════════════════════════════════
NPC_AI_ENABLED = True  # master toggle
NPC_AI_MODE = "full"  # "off" | "fight" | "full"
NPC_AI_MODEL = "mistralai/mistral-small-3.1-24b-instruct"
NPC_AI_TEMP = 0.7
NPC_AI_MAX_TOKENS = 2048
# fight mode: batch all NPCs in same initiative slot into one request
NPC_AI_BATCH_SAME_SLOT = True

# ═══════════════════════════════════════════════════════════════
# MODEL 8 — MODER AI (ask filter for /gofyn + dice dispatch)
# ═══════════════════════════════════════════════════════════════
MODER_AI_ENABLED = True
MODER_AI_MODEL = "google/gemma-3-4b-it"
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
SETTINGS_DIR = os.path.join(BASE_DIR, "settings")
SETTINGS_DEFAULT_GENRE = "medieval_fantasy"
# /categori command
CATEGORIES_INDEX_FILE = os.path.join(SETTINGS_DIR, "categories.json")
# Minimum complexity score (0-1) for user-submitted settings
SETTINGS_MIN_COMPLEXITY = 0.3

# ═══════════════════════════════════════════════════════════════
# ADMIN TELEMETRY
# ═══════════════════════════════════════════════════════════════
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "-1003311333760")  # Admin channel for telemetry

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
