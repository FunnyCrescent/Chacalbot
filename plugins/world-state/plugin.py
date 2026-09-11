"""
world-state — read-only view команд состояния мира.
"""
from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler

from core.plugin import Plugin
from libs.bot_handlers import (
    time_cmd, weather_cmd, factions_cmd, npc_cmd, city_cmd,
    world_cmd, event_cmd, location_cmd, resources_cmd,
)

logger = logging.getLogger(__name__)


class WorldStatePlugin(Plugin):
    name = "world-state"
    version = "0.2.0"
    description = "World state (time/weather/factions/NPC/city/world/events/location/resources)"
    author = "core refactor"
    depends_on = ["persistence", "session-core"]

    async def setup(self, app: Application, ctx) -> None:
        app.add_handler(CommandHandler("amser", time_cmd))
        app.add_handler(CommandHandler("tywydd", weather_cmd))
        app.add_handler(CommandHandler("ffactiynau", factions_cmd))
        app.add_handler(CommandHandler("cymeriadnc", npc_cmd))
        app.add_handler(CommandHandler("dinas", city_cmd))
        app.add_handler(CommandHandler("byd", world_cmd))
        app.add_handler(CommandHandler("digwyddiad", event_cmd))
        app.add_handler(CommandHandler("lleoliad", location_cmd))
        app.add_handler(CommandHandler("adnoddau", resources_cmd))
        logger.info("world-state: 9 commands registered")
