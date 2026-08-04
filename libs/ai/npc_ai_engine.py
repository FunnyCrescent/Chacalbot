"""NpcAIEngineMixin — emergent NPC AI in combat."""
import json
import logging
import asyncio
import random
import re
from typing import Dict, List, Optional, Any, Callable, Awaitable

from libs.character_parser import ParsedCharacter
from libs.config_legacy import NPC_AI_ENABLED
from .tools import (
    ROLL_TYPE_ENUM, DICE_TOOLS, GAME_TOOLS, ALL_TOOLS,
    COMBAT_TOOLS, MASTER_TOOLS, GET_TOOLS, DB_TOOLS, DB_WRITE_TOOL_NAMES,
)
from .prompts import (
    MASTER_PROMPT, ASK_PROMPT, RENDERER_PROMPT, NPC_AI_PROMPT, DB_BOT_PROMPT,
)
from .utils import strip_stray_tags

logger = logging.getLogger(__name__)


class NpcAIEngineMixin:
    """NpcAIEngineMixin — emergent NPC AI in combat."""

    async def npc_ai_decision(
        self,
        npc_group: List[Dict],
        context: str,
    ) -> List[Dict]:
        """
        Ask the NPC AI model for tactical decisions for a group of NPCs.
        Returns parsed list of decision dicts, or empty list on failure.
        """
        if not NPC_AI_ENABLED:
            logger.info("NPC AI disabled, returning empty decisions")
            return []

        try:
            user_msg = json.dumps(
                {"npc_group": npc_group, "context": context},
                ensure_ascii=False,
                indent=2,
            )
            messages = [{"role": "user", "content": user_msg}]

            raw = await self.npc_ai.chat(
                messages,
                system_prompt=NPC_AI_PROMPT,
            )

            # Defensive: chat() may return dict or str depending on API response format
            if isinstance(raw, dict):
                raw = json.dumps(raw, ensure_ascii=False)
            elif not isinstance(raw, str):
                raw = str(raw)

            # Try to parse JSON from the response
            text = raw.strip()
            # Strip potential markdown code fences
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            if text.startswith("json"):
                text = text[4:].strip()

            decisions = json.loads(text)
            if isinstance(decisions, list):
                logger.info(f"NPC AI returned {len(decisions)} decisions for {len(npc_group)} NPCs")
                return decisions
            else:
                logger.warning(f"NPC AI returned non-list: {type(decisions)}")
                return []
        except json.JSONDecodeError as e:
            logger.error(f"NPC AI JSON parse error: {e}")
            return []
        except Exception as e:
            logger.error(f"NPC AI decision error: {e}")
            return []


