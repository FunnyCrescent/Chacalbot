"""
db-bot-phase — подплагин (mixin) для narrator.

LLM-агент, который читает мастер-нарратив и мутирует game state.
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.bot_handlers import _run_db_bot_background
from libs.ai import DB_BOT_PROMPT, DB_TOOLS, GET_TOOLS

logger = logging.getLogger(__name__)


class DBBotPhasePlugin(Plugin):
    name = "db-bot-phase"
    version = "0.2.0"
    description = "DB sub-agent: LLM phase that mutates game state from narrative — mixin for narrator"
    author = "core refactor"
    depends_on = ["narrator", "ai-engine", "session-core"]

    async def setup(self, app, ctx) -> None:
        async def _on_master_output(*, session_id, master_text, **kwargs):
            try:
                await _run_db_bot_background(session_id, master_text)
                return True
            except Exception:
                logger.exception("db-bot-phase: _run_db_bot_background failed")
                return None

        ctx.hooks.register(
            "narrator.on_master_output",
            _on_master_output,
            plugin_name=self.name,
        )

        ctx.register_service("db_bot_phase", {
            "prompt": DB_BOT_PROMPT,
            "tools": DB_TOOLS + GET_TOOLS,
        })
        logger.info("db-bot-phase: hook registered for 'narrator.on_master_output'")
