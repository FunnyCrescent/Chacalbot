"""
renderer — подплагин (mixin) для narrator.

MD→HTML конверсия + chunked send.
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.bot_handlers import (
    md_to_html, _chunk_html, send_safe, _send_long_blockquote,
)
from libs.ai import RENDERER_PROMPT

logger = logging.getLogger(__name__)


class RendererPlugin(Plugin):
    name = "renderer"
    version = "0.2.0"
    description = "MD→HTML conversion + chunked send — mixin for narrator"
    author = "core refactor"
    depends_on = ["narrator", "ai-engine"]

    async def setup(self, app, ctx) -> None:
        ctx.register_service("renderer", {
            "md_to_html": md_to_html,
            "chunk_html": _chunk_html,
            "send_safe": send_safe,
            "send_long_blockquote": _send_long_blockquote,
            "prompt": RENDERER_PROMPT,
        })

        async def _on_master_output(*, session_id, master_text, chat_id=None, **kwargs):
            try:
                if chat_id:
                    html = md_to_html(master_text)
                    await send_safe(app.bot, chat_id, html)
                    return True
            except Exception:
                logger.exception("renderer: failed to send rendered output")
                return None

        ctx.hooks.register(
            "narrator.on_master_output",
            _on_master_output,
            plugin_name=self.name,
        )
        logger.info("renderer: helpers registered + hook for 'narrator.on_master_output'")
