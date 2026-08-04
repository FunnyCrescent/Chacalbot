"""
dice — броски кубиков, режимы бросков, кнопки запроса броска от DM.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    roll_dispatch_cmd, mode_cmd, concentration_cmd,
    _roll_button_callback, _private_roll_callback,
)

logger = logging.getLogger(__name__)


class DicePlugin(Plugin):
    name = "dice"
    version = "0.2.0"
    description = "Dice rolling + inline buttons + concentration tracker"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("rholio", roll_dispatch_cmd))
        app.add_handler(CommandHandler("modd", mode_cmd))
        app.add_handler(CommandHandler("canolbwyntio", concentration_cmd))
        app.add_handler(CallbackQueryHandler(_roll_button_callback, pattern=r"^roll:"))
        app.add_handler(CallbackQueryHandler(_private_roll_callback, pattern=r"^gwroll:"))
        logger.info("dice: 3 commands + 2 callback handlers registered")
