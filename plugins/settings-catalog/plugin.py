"""
settings-catalog — каталог сеттингов миров, выбор при старте сессии, кастомные загрузки.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    categori_cmd, categori_add_cmd, dyfroddi_cmd, dndstart_cmd,
    setcurrency_cmd, _categori_callback,
)

logger = logging.getLogger(__name__)


class SettingsCatalogPlugin(Plugin):
    name = "settings-catalog"
    version = "0.2.0"
    description = "World/genre settings catalog: browse, upload, pick at session start"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("categori", categori_cmd))
        app.add_handler(CommandHandler("categori_add", categori_add_cmd))
        app.add_handler(CommandHandler("dyfroddi", dyfroddi_cmd))
        app.add_handler(CommandHandler("dndcychwyn", dndstart_cmd))
        app.add_handler(CommandHandler("setcurrency", setcurrency_cmd))
        app.add_handler(CallbackQueryHandler(
            _categori_callback,
            pattern=r"^(cat:|set:|sel:|start:|genworld:)",
        ))
        logger.info("settings-catalog: 5 commands + 1 callback registered")
