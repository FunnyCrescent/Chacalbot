"""
character-state — read-only view команд состояния персонажа.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    hp_cmd, deathsave_cmd, condition_cmd, rest_cmd, gold_cmd,
    inventory_cmd, quest_cmd, goals_cmd, relations_cmd,
)

logger = logging.getLogger(__name__)


class CharacterStatePlugin(Plugin):
    name = "character-state"
    version = "0.2.0"
    description = "Read-only character state (HP/conditions/rest/gold/inventory/quests/goals/relations)"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("iechyd", hp_cmd))
        app.add_handler(CommandHandler("arbedmarwolaeth", deathsave_cmd))
        app.add_handler(CommandHandler("cyflwr", condition_cmd))
        app.add_handler(CommandHandler("gorffwys", rest_cmd))
        app.add_handler(CommandHandler("aur", gold_cmd))
        app.add_handler(CommandHandler("eiddo", inventory_cmd))
        app.add_handler(CommandHandler("cwest", quest_cmd))
        app.add_handler(CommandHandler("nodau", goals_cmd))
        app.add_handler(CommandHandler("perthynasau", relations_cmd))
        logger.info("character-state: 9 read-only commands registered")
