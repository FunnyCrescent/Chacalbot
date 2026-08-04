"""
ai-engine — сервисный плагин: LLM-клиенты и DMEngine.

Регистрирует в ctx.services:
    - 'dm_engine': DMEngine (6 LLM clients: master, db_bot, renderer, memory, embedder, npc_ai)
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.ai import DMEngine

logger = logging.getLogger(__name__)


class AIEnginePlugin(Plugin):
    name = "ai-engine"
    version = "0.2.0"
    description = "LLM integration (OpenAIClient + DMEngine with 6 model clients)"
    author = "core refactor"
    provides = ["dm_engine"]
    depends_on = ["persistence"]

    async def setup(self, app, ctx) -> None:
        db_manager = ctx.require_service("db_manager")
        dm_engine = DMEngine(db_manager=db_manager)
        ctx.register_service("dm_engine", dm_engine)

        from libs import bot_handlers
        bot_handlers.dm_engine = dm_engine

        logger.info("AI Engine ready: DMEngine initialized (6 LLM clients)")
