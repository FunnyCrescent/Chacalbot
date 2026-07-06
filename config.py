"""
D&D Dark Fantasy DM Bot — Configuration
Load secrets from .env file. Never commit .env to git!
"""

import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# API Provider (OpenRouter-compatible)
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Model 1 — MASTER (Narrative + Dice)
MASTER_MODEL = os.getenv("MASTER_MODEL", "z-ai/glm-5.2")
MASTER_TEMP = float(os.getenv("MASTER_TEMP", "0.7"))
MASTER_MAX_TOKENS = int(os.getenv("MASTER_MAX_TOKENS", "65536"))

# Model 2 — DB BOT (Database operations)
DB_MODEL = os.getenv("DB_MODEL", "deepseek/deepseek-v4-flash")
DB_TEMP = float(os.getenv("DB_TEMP", "0.1"))
DB_MAX_TOKENS = int(os.getenv("DB_MAX_TOKENS", "16384"))

# Model 3 — RENDERER (Markdown → Telegram HTML)
RENDERER_MODEL = os.getenv("RENDERER_MODEL", "xiaomi/mimo-v2.5")
RENDERER_TEMP = float(os.getenv("RENDERER_TEMP", "0.0"))
RENDERER_MAX_TOKENS = int(os.getenv("RENDERER_MAX_TOKENS", "16384"))

# Model 4 — MEMORY (Summary, SRD lookup)
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "xiaomi/mimo-v2.5")
MEMORY_TEMP = float(os.getenv("MEMORY_TEMP", "0.3"))
MEMORY_MAX_TOKENS = int(os.getenv("MEMORY_MAX_TOKENS", "8192"))

# Game Settings
SETTING = os.getenv("SETTING", "dark_fantasy")
DND_EDITION = os.getenv("DND_EDITION", "5e_2024")
MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "6"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "50"))

# Paths
DB_PATH = os.path.join(BASE_DIR, "data", "campaigns.db")
LOG_PATH = os.path.join(BASE_DIR, "logs", "bot.log")
CHARACTERS_DIR = os.path.join(BASE_DIR, "characters")

# Admin Telemetry
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
