"""
srd-reference — поиск по SRD (monsters / items / spells).
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from core.plugin import Plugin
from libs.bot_handlers import srd_cmd

logger = logging.getLogger(__name__)


class SrdReferencePlugin(Plugin):
    name = "srd-reference"
    version = "0.2.0"
    description = "SRD lookup (monsters/items/spells) — command + AI fallback cache"
    author = "core refactor"
    depends_on = ["persistence", "session-core", "ai-engine"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("cyfeirlyfr", srd_cmd))
        logger.info("srd-reference: 1 command registered")
