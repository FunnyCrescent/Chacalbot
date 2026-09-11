"""RendererEngineMixin — MD→Telegram HTML conversion."""
import json
import logging
import asyncio
import random
import re
from typing import Dict, List, Optional, Any, Callable, Awaitable

from libs.character_parser import ParsedCharacter
from .tools import (
    ROLL_TYPE_ENUM, DICE_TOOLS, GAME_TOOLS, ALL_TOOLS,
    COMBAT_TOOLS, MASTER_TOOLS, GET_TOOLS, DB_TOOLS, DB_WRITE_TOOL_NAMES,
)
from .prompts import (
    MASTER_PROMPT, ASK_PROMPT, RENDERER_PROMPT, NPC_AI_PROMPT, DB_BOT_PROMPT,
    get_prompt,
)
from .utils import strip_stray_tags

logger = logging.getLogger(__name__)


class RendererEngineMixin:
    """RendererEngineMixin — MD→Telegram HTML conversion."""

    async def process_renderer(self, text: str, session_id: str = "") -> str:
        """
        Step 3: Renderer converts text to Telegram HTML.
        session_id: per-session промпт (MD-консолидация, Фаза 2).
        Returns: HTML string.
        """
        try:
            messages = [{"role": "user", "content": text}]
            response = await self.renderer.chat(messages, system_prompt=get_prompt("renderer", session_id))
            html = response["choices"][0]["message"].get("content", text)
            # Strip any markdown code blocks if the model wrapped HTML
            html = html.strip()
            if html.startswith("```html"):
                html = html[7:]
            if html.startswith("```"):
                html = html[3:]
            if html.endswith("```"):
                html = html[:-3]
            return html.strip()
        except Exception as e:
            logger.error(f"Renderer error: {e}")
            # Fallback: basic md_to_html
            return self._fallback_render(text)

    @staticmethod

    def _fallback_render(text: str) -> str:
        import re as _re
        text = _re.sub(r'\*\*(.+?)\*\*', r'<b></b>', text)
        text = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i></i>', text)
        text = _re.sub(r'`(.+?)`', r'<code></code>', text)
        # Whole message is wrapped in <blockquote expandable> externally
        return text


