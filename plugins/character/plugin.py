"""
character — управление персонажами: импорт листов, библиотека сохранённых, /creu wizard.

Дополнительно разбит на sub-plugins внутри себя:
  - character-import (логика импорта — CharacterParser)
  - character-library (SavedCharsDB picker)
  - character-creu (ConversationHandler wizard)
  - character-smith (in-session /sp)

Пока что все handlers в одном плагине. В дальнейшем можно разбить.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    char_cmd, sheet_cmd, ability_cmd, smith_cmd, _charpick_callback,
)
from libs.creu import get_handler as get_creu_handler
from libs.creu.storage import init_db as init_creu_db, cleanup_stale as creu_cleanup_stale

logger = logging.getLogger(__name__)


class CharacterPlugin(Plugin):
    name = "character"
    version = "0.2.0"
    description = "Character management: import / save-load / sheet view / creu wizard / smith"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        # BUG 4 FIX: раньше исключение из init_creu_db() молча глоталось ДО
        # add_handler() — /creu тихо не регистрировался, и в чате он был «не
        # присоединён». Теперь: init НЕ должен падать (путь к БД исправлен,
        # каталог создаётся автоматически), а если всё же падает — пишем
        # громкую ошибку со стеком и всё равно НЕ вешаем ConversationHandler,
        # чтобы не ловить 500-ки на каждый /creu.
        creu_ok = False
        try:
            init_creu_db()
            creu_cleanup_stale()
            creu_ok = True
        except Exception as e:
            logger.error("[creu] Failed to init creu sandbox DB: %s", e, exc_info=True)

        if creu_ok:
            app.add_handler(get_creu_handler())
            logger.info("[creu] Character-gen wizard loaded (/creu)")
        else:
            logger.error("[creu] Character-gen wizard NOT loaded — /creu is disabled until the DB issue is fixed")

        app.add_handler(CommandHandler("cymeriad", char_cmd))
        app.add_handler(CommandHandler("taflen", sheet_cmd))
        app.add_handler(CommandHandler("gallu", ability_cmd))
        app.add_handler(CommandHandler("sp", smith_cmd))
        app.add_handler(CallbackQueryHandler(_charpick_callback, pattern=r"^charpick:"))
        logger.info("character: 4 commands + 1 callback %s registered", "+ creu wizard" if creu_ok else "(creu FAILED)")
