"""
memory — семантическая память и recall для DMEngine.

Регистрирует 'memory_store' в ctx.services. MemoryStore использует
DMEngine.embedder + DMEngine.memory, зависит от ai-engine.
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryPlugin(Plugin):
    name = "memory"
    version = "0.2.0"
    description = "Semantic memory store (embedding recall + sleep consolidation)"
    author = "core refactor"
    provides = ["memory_store"]
    depends_on = ["persistence", "ai-engine"]

    async def setup(self, app, ctx) -> None:
        db_manager = ctx.require_service("db_manager")
        dm_engine = ctx.require_service("dm_engine")
        memory = MemoryStore(db_manager, dm_engine)
        ctx.register_service("memory_store", memory)

        from libs import bot_handlers
        if not hasattr(bot_handlers, "memory_store"):
            bot_handlers.memory_store = memory

        logger.info("memory: MemoryStore registered as service")
