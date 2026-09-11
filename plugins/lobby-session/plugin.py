"""
lobby-session — Telegram handlers для управления сессией и игроками.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    start_cmd, help_cmd, new_cmd, join_cmd, leave_cmd, players_cmd,
    end_cmd, resume_cmd, status_cmd, transfer_cmd, kick_cmd, terms_cmd, delete_cmd,
    join_confirm_callback,
)

logger = logging.getLogger(__name__)


class LobbySessionPlugin(Plugin):
    name = "lobby-session"
    version = "0.2.0"
    description = "Session lifecycle: new/join/leave/end/resume/status/transfer/kick/help/terms/delete"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("dechrau", start_cmd))
        # Фича 2: /help — английский алиас. ВАЖНО: /help и /cymorth — ЕДИНСТВЕННАЯ
        # пара с двумя именами; все остальные команды остаются только на валлийском.
        app.add_handler(CommandHandler(["cymorth", "help"], help_cmd))
        app.add_handler(CommandHandler("newydd", new_cmd))
        app.add_handler(CommandHandler("ymuno", join_cmd))
        # ИТЕРАЦИЯ 10 (Раздел 6): подтверждение входа — реальный add_player
        # выполняется только по нажатию «Да» (см. lobby_cmds.join_confirm_callback).
        app.add_handler(CallbackQueryHandler(
            join_confirm_callback, pattern=r"^ymuno:(confirm|cancel):\d+$"))
        app.add_handler(CommandHandler("gadael", leave_cmd))
        app.add_handler(CommandHandler("chwaraewyr", players_cmd))
        app.add_handler(CommandHandler("statws", status_cmd))
        app.add_handler(CommandHandler("diwedd", end_cmd))
        app.add_handler(CommandHandler("ailddechrau", resume_cmd))
        app.add_handler(CommandHandler("trosglwyddo", transfer_cmd))
        app.add_handler(CommandHandler("cicio", kick_cmd))
        app.add_handler(CommandHandler("telerau", terms_cmd))
        app.add_handler(CommandHandler("dileu", delete_cmd))
        logger.info("lobby-session: 13 command handlers registered")
