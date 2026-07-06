# 🐺 Chacal — D&D Dark Fantasy DM Bot

> **Мастера зовут Шакал.** Он не прощает ошибок, не подсказывает решений и не ведёт за руку.  
> Тёмное фэнтези, жёсткие правила D&D 5e (2024), реальные последствия.

Telegram-бот для асинхронных кампаний в жанре тёмного фэнтези.  
Игроки ходят по очереди через `Дн.`, мастер ждёт всех — и разрешает раунд разом.

## 🎭 Архитектура

Бот использует 4-модульную систему:

1. **Мастер (Шакал)** — пишет наратив и бросает кости через tool calling
2. **DB-Бот** — анализирует текст мастера и обновляет базу данных
3. **Рендерер** — конвертирует Markdown в Telegram HTML
4. **Память** — сводки кампании и справочник по правилам

## ⚡ Особенности

- **Queue-based ходы** — мастер ждёт, пока все игроки сходят, и разрешает раунд разом
- **Tool calling для костей** — LLM вызывает `roll_dice`, не придумывает числа
- **Живая база данных** — HP, инвентарь, квесты, фракции, погода, NPC — всё в SQLite
- **Защита от нарушений** — godmoding, metagaming, powergaming
- **Приватные действия** — `/do` в ЛС бота для скрытых ходов
- **Мир живёт без игроков** — случайные события, фракции, экономика

## 🚀 Быстрый старт

```bash
git clone https://github.com/FunnyCrescent/Chacalbot.git
cd Chacalbot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Отредактируй .env, добавь токены
python bot.py
```

### Первые шаги в Telegram

1. `/new Название кампании` — создать сессию
2. `/join` — присоединиться
3. Отправить `.txt` / `.md` лист персонажа и ответить `/character`
4. `Дн. я атакую гоблина мечом!` — сходить

## 📋 Команды

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
| `/dbask` | Апелляция к DB-Боту |
| `/do` | Приватное действие (в ЛС) |

## 📁 Структура

```
Chacalbot/
├── bot.py              # Telegram-бот, команды, хендлеры
├── ai_client.py        # 4-модульная AI-архитектура
├── db.py               # SQLite-схема и операции
├── session_manager.py  # Логика сессий, очереди, боя
├── character_parser.py # Парсер листов персонажей
├── dice_parser.py      # Броски костей, криты, преимущество
├── config.py           # Конфигурация (читает .env)
├── prompts/            # Промпты для AI (подготовка к мультиязычности)
├── data/               # SQLite-базы сессий
├── logs/               # Логи кампаний
├── characters/         # Загруженные листы персонажей
└── .env.example        # Шаблон переменных окружения
```

## ⚙️ Требования

- Python 3.11+
- Telegram Bot Token (получить у [@BotFather](https://t.me/BotFather))
- OpenRouter API Key (или любой OpenAI-compatible провайдер)

## 📝 Лицензия

MIT License — см. [LICENSE](LICENSE).

## 🐺 Автор

**Eira** — [@crescentfunny](https://t.me/crescentfunny)  
GitHub: [FunnyCrescent/Chacalbot](https://github.com/FunnyCrescent/Chacalbot)
