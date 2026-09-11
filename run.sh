#!/usr/bin/env bash
# run.sh — быстрый запуск D&D бота.
# Что делает:
#   1. Самонаходит свою директорию (работает из любого cwd)
#   2. Проверяет, что .venv создан (иначе подсказывает запустить setup.sh)
#   3. Активирует venv и запускает main.py
#
# Запуск:
#   ./run.sh
#   bash run.sh
#   ~/Загрузки/dnd-bot-plugin/run.sh

set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Самонаходимся (работает из любой директории, через symlink или нет)
# ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Цвета
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

echo -e "${CYAN}=== D&D Bot — запуск ===${NC}"
echo "Папка проекта: $SCRIPT_DIR"
echo

# ─────────────────────────────────────────────────────────────────
# Проверка venv
# ─────────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    err ".venv не найден. Сначала запусти:"
    echo
    echo -e "    ${CYAN}./setup.sh${NC}"
    echo
    exit 1
fi

if [ ! -f "main.py" ]; then
    err "main.py не найден в $SCRIPT_DIR"
    err "Похоже скрипт лежит не в корне проекта."
    exit 1
fi

if [ ! -f ".env" ]; then
    warn ".env не найден — копирую из .env.example"
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn "Открой .env и впиши TELEGRAM_BOT_TOKEN и OPENAI_API_KEY!"
    else
        err ".env и .env.example оба отсутствуют — не могу запустить."
        exit 1
    fi
fi

# ─────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────
log "Активирую venv..."
# shellcheck disable=SC1091
source .venv/bin/activate

log "Запускаю бота..."
echo
exec python main.py
