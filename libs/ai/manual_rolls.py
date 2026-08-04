"""ManualRollsMixin — resolve /roll <check> via DB-Bot."""
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
)
from .utils import strip_stray_tags

logger = logging.getLogger(__name__)


class ManualRollsMixin:
    """ManualRollsMixin — resolve /roll <check> via DB-Bot."""

    async def resolve_manual_roll(self, character_name: str, check_name: str,
                                  sheet_text: str, db_progression_text: str) -> Dict:
        """A4: /roll <характеристика> command. The DB-Bot (not the Master) reads the RAW
        sheet text + current DB progression (level-ups, new proficiencies since upload)
        to figure out the right modifier, then performs a REAL roll_dice call — this is
        a system-verified number, not something the player typed. Returns:
        {"ok": bool, "display": str, "natural": int, "total": int, "error": str}"""
        prompt = f"""Персонаж {character_name} запрашивает бросок: "{check_name}".

Сырой лист персонажа (для деталей вроде экспертизы/особых бонусов):
---
{sheet_text}
---

АКТУАЛЬНОЕ состояние из базы данных (приоритетнее листа — персонаж мог повыситься в уровне, получить владения после регистрации):
---
{db_progression_text}
---

Определи: какая характеристика используется, владеет ли персонаж этим навыком/спасброском (тогда прибавляется бонус мастерства, посчитай его по уровню: +2 на 1-4 ур., +3 на 5-8, +4 на 9-12, +5 на 13-16, +6 на 17-20), есть ли expertise (двойной бонус мастерства) или иные явные модификаторы из листа.
Вызови ОДИН раз инструмент roll_dice: count=1, sides=20, modifier=<посчитанный модификатор>, label="{character_name} — {check_name}", visible=true.
Если "{check_name}" не является валидной характеристикой/навыком/спасброском D&D 5e — не вызывай инструмент, вместо этого объясни проблему текстом."""

        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages, system_prompt=None, tools=DICE_TOOLS, tool_choice="auto")
            choice = response["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")
            if not tool_calls:
                return {"ok": False, "error": choice.get("content") or "DB-Bot не смог определить модификатор."}

            for tc in tool_calls:
                if tc["function"]["name"] == "roll_dice":
                    args = json.loads(tc["function"]["arguments"])
                    result = self._execute_roll(args)
                    logger.info(f"[MANUAL ROLL /roll] {character_name} — {check_name}: {result['result']}")
                    return {
                        "ok": True,
                        "display": result["display"],
                        "natural": result["natural"],
                        "total": result["total"],
                    }
            return {"ok": False, "error": "DB-Bot вызвал не тот инструмент."}
        except Exception as e:
            logger.error(f"resolve_manual_roll error: {e}")
            return {"ok": False, "error": str(e)}


