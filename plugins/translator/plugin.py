"""
translator — перевод [LANG:xx] блоков в нарративе DM.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from core.plugin import Plugin
from libs.bot_handlers import cyfieithu_cmd, _resolve_lang_callback

logger = logging.getLogger(__name__)


class TranslatorPlugin(Plugin):
    name = "translator"
    version = "0.2.0"
    description = "Async translation of [LANG:xx] blocks in DM narrative"
    author = "core refactor"
    depends_on = ["persistence", "session-core", "ai-engine"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("cyfieithu", cyfieithu_cmd))
        app.add_handler(CallbackQueryHandler(_resolve_lang_callback, pattern=r"^lang:"))
        logger.info("translator: 1 command + 1 callback registered")
