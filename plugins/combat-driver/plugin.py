"""
combat-driver — подплагин (mixin) для narrator.

Управляет initiative-ordered combat loop.
"""
from __future__ import annotations

import logging
import asyncio

from core.plugin import Plugin
from libs.bot_handlers import (
    _start_combat_turn_loop, _resolve_npc_combat_turn,
    _auto_resolve_npcs_then_pc, _setup_pc_combat_turn,
    _resolve_pc_combat_turn, _send_combat_turn_result,
    _resolve_non_combat_round,
)

logger = logging.getLogger(__name__)


class CombatDriverPlugin(Plugin):
    name = "combat-driver"
    version = "0.2.0"
    description = "Initiative-based combat loop driver — mixin for narrator"
    author = "core refactor"
    depends_on = ["narrator", "persistence", "ai-engine", "session-core"]

    async def setup(self, app, ctx) -> None:
        async def _on_combat_turn_started(*, session_id, chat_id, bot, **kwargs):
            logger.info("combat-driver: starting combat loop for session=%s chat=%s",
                        session_id, chat_id)
            try:
                asyncio.create_task(_start_combat_turn_loop(session_id, chat_id, bot))
            except Exception:
                logger.exception("combat-driver: failed to start combat loop")

        ctx.hooks.register(
            "narrator.combat_turn_started",
            _on_combat_turn_started,
            plugin_name=self.name,
        )
        logger.info("combat-driver: hook registered for 'narrator.combat_turn_started'")
