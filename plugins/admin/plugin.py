"""
admin — глобальный error handler + admin/meta команды.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    ask_cmd, dbask_cmd, private_action_cmd, clear_cmd, summary_cmd,
    cancel_cmd, forceresolve_cmd, skip_cmd, endcombat_cmd, pvp_cmd, error_handler,
)

logger = logging.getLogger(__name__)


class AdminPlugin(Plugin):
    name = "admin"
    version = "0.2.0"
    description = "Global error handler + admin/meta commands (ask/dbask/private/clear/summary/skip/etc.)"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("gofyn", ask_cmd))
        app.add_handler(CommandHandler("dbgofyn", dbask_cmd))
        app.add_handler(CommandHandler("gwneud", private_action_cmd))
        app.add_handler(CommandHandler("clirio", clear_cmd))
        app.add_handler(CommandHandler("crynodeb", summary_cmd))
        app.add_handler(CommandHandler("diddymu", cancel_cmd))
        app.add_handler(CommandHandler("gorfoddatrys", forceresolve_cmd))
        app.add_handler(CommandHandler("sgipio", skip_cmd))
        app.add_handler(CommandHandler("gorffenymladd", endcombat_cmd))
        app.add_handler(CommandHandler("cccg", pvp_cmd))
        app.add_error_handler(error_handler)
        logger.info("admin: 10 commands + global error_handler registered")
