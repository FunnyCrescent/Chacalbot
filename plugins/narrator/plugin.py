"""
narrator — главный ДМ-наративщик.

Декларирует extension points:
  - narrator.on_action_collected
  - narrator.on_master_output
  - narrator.on_round_resolved
  - narrator.combat_turn_started
  - narrator.combat_turn_ended

Sub-plugins (mixins): combat-driver, db-bot-phase, renderer.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, MessageHandler, filters

from core.plugin import Plugin
from libs.bot_handlers import handle_message

logger = logging.getLogger(__name__)


class NarratorPlugin(Plugin):
    name = "narrator"
    version = "0.2.0"
    description = "DM narrator: main message handler + extension points for combat/db-bot/renderer"
    author = "core refactor"
    provides = [
        "narrator.on_action_collected",
        "narrator.on_master_output",
        "narrator.on_round_resolved",
        "narrator.combat_turn_started",
        "narrator.combat_turn_ended",
    ]
    depends_on = ["persistence", "ai-engine", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        for point in self.provides:
            ctx.hooks.declare(point, owner=self.name)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        recovery = ctx.get_service("crash_recovery")
        if recovery:
            for session_id, chat_id in recovery.get("combat_sessions", []):
                try:
                    await ctx.hooks.trigger(
                        "narrator.combat_turn_started",
                        session_id=session_id, chat_id=chat_id, bot=app.bot,
                    )
                except Exception:
                    logger.exception("Failed to restart combat loop for session %s", session_id)

        logger.info("narrator: ready. Extension points declared: %s", ", ".join(self.provides))
