"""
persistence — сервисный плагин: SQLite data layer.

Регистрирует в ctx.services:
    - 'db_manager'    : DatabaseManager (multi-session: data/sessions/<id>.db)
    - 'saved_chars_db': SavedCharsDB   (cross-session library: data/saved_chars.db)
"""
from __future__ import annotations

import logging
import os

from core.plugin import Plugin
from libs.config_legacy import BASE_DIR, SAVED_CHARS_DB
from libs.db import DatabaseManager, SavedCharsDB

logger = logging.getLogger(__name__)


class PersistencePlugin(Plugin):
    name = "persistence"
    version = "0.2.0"
    description = "SQLite data layer (DatabaseManager + SavedCharsDB) — base persistence service"
    author = "core refactor"
    provides = ["db_manager", "saved_chars_db"]

    async def setup(self, app, ctx) -> None:
        sessions_dir = os.path.join(BASE_DIR, "data", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        db_manager = DatabaseManager(sessions_dir)
        saved_chars = SavedCharsDB(SAVED_CHARS_DB)

        ctx.register_service("db_manager", db_manager)
        ctx.register_service("saved_chars_db", saved_chars)

        from libs import bot_handlers
        bot_handlers.db_manager = db_manager
        bot_handlers.saved_chars_db = saved_chars

        logger.info("Persistence ready: sessions dir=%s, saved_chars db=%s",
                    sessions_dir, SAVED_CHARS_DB)
