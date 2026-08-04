#!/usr/bin/env bash
# setup.sh — полная установка и запуск D&D бота.
# Что делает:
#   1. Проверяет Python, при необходимости ставит через pacman
#   2. Создаёт виртуальное окружение .venv
#   3. Устанавливает все зависимости из requirements.txt
#   4. Автоматически ищет локальный прокси (Happ/Hiddify/v2rayN/Clash)
#      и прописывает его в .env
#   5. Запускает бота
#
# Запуск:
#   ./setup.sh
#
# Можно запускать многократно — пропустит уже установленное.

set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Самонаходимся (работает из любой директории, через symlink или нет)
# ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Цвета для лога
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
hdr()  { echo -e "\n${CYAN}=== $* ===${NC}"; }

hdr "D&D Bot — установка (Arch-based)"
echo "Папка проекта: $SCRIPT_DIR"
echo

# ─────────────────────────────────────────────────────────────────
# 1. Python
# ─────────────────────────────────────────────────────────────────
hdr "Шаг 1/5 — Проверка Python"

if ! command -v python3 &>/dev/null; then
    warn "Python 3 не найден. Ставлю через pacman..."
    sudo pacman -S --needed --noconfirm python python-pip
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Python $PY_VER OK: $(which python3)"

# ─────────────────────────────────────────────────────────────────
# 2. Виртуальное окружение
# ─────────────────────────────────────────────────────────────────
hdr "Шаг 2/5 — Виртуальное окружение"

if [ ! -d ".venv" ]; then
    log "Создаю .venv ..."
    python3 -m venv .venv
else
    log ".venv уже существует — пропускаю"
fi

# Активируем
# shellcheck disable=SC1091
source .venv/bin/activate
log "Активирован venv: $(which python)"

# ─────────────────────────────────────────────────────────────────
# 3. Зависимости
# ─────────────────────────────────────────────────────────────────
hdr "Шаг 3/5 — Установка зависимостей"

log "Обновляю pip..."
pip3 install --upgrade pip --quiet

log "Ставлю пакеты из requirements.txt..."
pip3 install -r requirements.txt
log "Зависимости установлены"

# ─────────────────────────────────────────────────────────────────
# 4. Автодетект локального прокси (Happ / Hiddify / v2rayN / Clash)
# ─────────────────────────────────────────────────────────────────
hdr "Шаг 4/5 — Поиск локального прокси"

# Убедимся что .env существует
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        warn ".env не найден — копирую из .env.example"
        cp .env.example .env
        warn "ВНИМАНИЕ: открой .env и впиши TELEGRAM_BOT_TOKEN и OPENAI_API_KEY!"
    else
        err ".env не найден и .env.example тоже. Создаю пустой .env"
        touch .env
    fi
fi

# Функция проверки — слушает ли что-то порт
port_open() {
    if command -v ss &>/dev/null; then
        ss -tln 2>/dev/null | grep -q ":$1 "
    elif command -v netstat &>/dev/null; then
        netstat -tln 2>/dev/null | grep -q ":$1 "
    else
        return 1
    fi
}

# Перебираем популярные порты локальных прокси-клиентов
# (тип порта определяем по конвенции: 10808/1080/12334/7890 → SOCKS5, 10809/1081 → HTTP)
FOUND_PROXY=""
for p in 10808 10809 1080 1081 7890 12334; do
    if port_open "$p"; then
        case "$p" in
            10808|1080|12334|7890)
                FOUND_PROXY="socks5h://127.0.0.1:$p"
                ;;
            10809|1081)
                FOUND_PROXY="http://127.0.0.1:$p"
                ;;
        esac
        log "Найден прокси на порту $p → $FOUND_PROXY"
        break
    fi
done

if [ -n "$FOUND_PROXY" ]; then
    # Прописываем GLOBAL_PROXY в .env
    if grep -q "^GLOBAL_PROXY=" .env; then
        sed -i "s|^GLOBAL_PROXY=.*|GLOBAL_PROXY=$FOUND_PROXY|" .env
    else
        echo "GLOBAL_PROXY=$FOUND_PROXY" >> .env
    fi
    log "GLOBAL_PROXY прописан в .env"
elif [ -n "${GLOBAL_PROXY:-}" ]; then
    # Системная env-переменная (Hiddify/Happ могут выставить)
    warn "Локальный порт не найден, но в окружении есть GLOBAL_PROXY=$GLOBAL_PROXY"
    warn "Бот подхватит его автоматически (trust_env=True)"
else
    warn "Локальный прокси НЕ найден."
    warn "Если у тебя Happ/Hiddify/v2rayN — открой его и подключись к серверу,"
    warn "потом перезапусти этот скрипт."
    warn "Без прокси (в РФ) Telegram API недоступен → будет ConnectTimeout."
    warn "Продолжаю с пустым GLOBAL_PROXY..."
fi

# ─────────────────────────────────────────────────────────────────
# 5. Запуск
# ─────────────────────────────────────────────────────────────────
hdr "Шаг 5/5 — Запуск бота"

log "Поехали!"
echo
exec python main.py
