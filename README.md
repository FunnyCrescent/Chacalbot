# D&D Dark Fantasy DM Bot

Telegram-бот для асинхронных D&D 5e (2024) кампаний в жанре тёмного фэнтези.

## Архитектура

Бот использует 4-модульную архитектуру:

1. **Master** — пишет наратив и бросает кости через tool calling
2. **DB-Bot** — анализирует текст мастера и обновляет базу данных
3. **Renderer** — конвертирует Markdown в Telegram HTML
4. **Memory** — сводки кампании и справочник по правилам

## Особенности

- **Queue-based ходы**: мастер ждёт, пока все игроки сходят, и разрешает раунд разом
- **Tool calling для костей**: LLM вызывает `roll_dice` вместо придумывания чисел
- **Живая база данных**: HP, инвентарь, квесты, фракции, погода, NPC — всё в SQLite
- **Защита от нарушений**: godmoding, metagaming, powergaming
- **Приватные действия**: `/do` в ЛС бота для скрытых ходов

## Установка

```bash
git clone https://github.com/yourname/dnd-dark-fantasy-bot.git
cd dnd-dark-fantasy-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Отредактируй .env, добавь токены
python bot.py
```

## Быстрый старт

1. `/new Название кампании` — создать сессию
2. `/join` — присоединиться
3. Отправить `.txt` / `.md` лист персонажа и ответить `/character`
4. `Дн. я атакую гоблина мечом!` — сходить

## Команды

| Команда | Описание |
|---------|----------|
| `/new` | Создать сессию |
| `/join` | Войти в сессию |
| `/character` | Загрузить лист персонажа |
| `/combat` | Начать бой |
| `/ask` | Вопрос мастеру вне очереди |
| `/hp` | Показать / изменить HP |
| `/inventory` | Инвентарь |
| `/quest` | Журнал квестов |
| `/world` | Генерация мира |
| `/npc` | Управление NPC |
| `/dbask` | Апелляция к DB-Bot |
| `/do` | Приватное действие (в ЛС) |

## Требования

- Python 3.11+
- Telegram Bot Token
- OpenRouter API Key (или совместимый провайдер)

## Лицензия

MIT License
