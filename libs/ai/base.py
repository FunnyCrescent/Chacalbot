"""BaseEngine — конструктор с 6 LLM-клиентами + _execute_roll helper."""
import json
import logging
import random
from typing import Dict, List, Optional

from libs.config_legacy import (
    MASTER_MODEL, MASTER_TEMP, MASTER_MAX_TOKENS,
    DB_MODEL, DB_TEMP, DB_MAX_TOKENS,
    RENDERER_MODEL, RENDERER_TEMP, RENDERER_MAX_TOKENS,
    MEMORY_MODEL, MEMORY_TEMP, MEMORY_MAX_TOKENS,
    EMBEDDING_MODEL, NPC_AI_MODEL, NPC_AI_TEMP, NPC_AI_MAX_TOKENS,
    MODER_AI_MODEL, MODER_AI_TEMP, MODER_AI_MAX_TOKENS,
)
from .client import OpenAIClient

logger = logging.getLogger(__name__)


class BaseEngine:
    """Базовый класс DMEngine — создаёт 6 LLM-клиентов."""

    def __init__(self, base_url: str = None, api_key: str = None, db_manager=None):
        self.master = OpenAIClient(MASTER_MODEL, MASTER_TEMP, MASTER_MAX_TOKENS, base_url, api_key, role="master")
        self.db_bot = OpenAIClient(DB_MODEL, DB_TEMP, DB_MAX_TOKENS, base_url, api_key, role="db")
        self.renderer = OpenAIClient(RENDERER_MODEL, RENDERER_TEMP, RENDERER_MAX_TOKENS, base_url, api_key, role="renderer")
        self.memory = OpenAIClient(MEMORY_MODEL, MEMORY_TEMP, MEMORY_MAX_TOKENS, base_url, api_key, role="memory")
        self.embedder = OpenAIClient(EMBEDDING_MODEL, 0.0, 8, base_url, api_key, role="embedding")
        self.npc_ai = OpenAIClient(NPC_AI_MODEL, NPC_AI_TEMP, NPC_AI_MAX_TOKENS, base_url, api_key, role="npc_ai")
        self.moder_ai = OpenAIClient(MODER_AI_MODEL, MODER_AI_TEMP, MODER_AI_MAX_TOKENS, base_url, api_key, role="moder_ai")
        self.db_manager = db_manager


    def _execute_roll(self, args: Dict) -> Dict:
        """Execute a dice roll. count>1 always SUMS (damage dice, multi-target rolls).
        Advantage/disadvantage is a SEPARATE mechanic that only applies to a single d20 check:
        it rolls TWO d20s and keeps the better/worse one — it never sums them, and it never
        touches `count`. This is the fix for the old bug where the model faked advantage by
        setting count=2, which summed both dice instead of picking one."""
        count = max(1, args.get("count", 1))
        sides = args.get("sides", 20)
        modifier = args.get("modifier", 0)
        label = args.get("label", "roll")
        visible = args.get("visible", True)
        roll_type = args.get("roll_type", "normal")
        if roll_type not in ("normal", "advantage", "disadvantage"):
            roll_type = "normal"

        is_adv_check = sides == 20 and count == 1 and roll_type in ("advantage", "disadvantage")

        if is_adv_check:
            r1, r2 = random.randint(1, 20), random.randint(1, 20)
            natural = max(r1, r2) if roll_type == "advantage" else min(r1, r2)
            total = natural + modifier
            rolls = [r1, r2]
            adv_label = " (преимущество)" if roll_type == "advantage" else " (помеха)"
            roll_str = f"[{r1},{r2}]→{natural}"
        else:
            rolls = [random.randint(1, sides) for _ in range(count)]
            natural = rolls[0] if rolls else 0
            total = sum(rolls) + modifier
            adv_label = ""
            roll_str = " + ".join(str(r) for r in rolls)

        mod_str = f"{modifier:+d}" if modifier else ""

        crit = ""
        if sides == 20:
            crit_roll = natural if is_adv_check else (rolls[0] if len(rolls) == 1 else None)
            if crit_roll == 20:
                crit = " 💥 КРИТИЧЕСКИЙ УСПЕХ!"
            elif crit_roll == 1:
                crit = " 💀 КРИТИЧЕСКИЙ ПРОВАЛ!"

        notation = f"{count}d{sides}{adv_label}"
        display = f"🎲 {label}: {notation}{mod_str} → [{roll_str}]{mod_str} = **{total}**{crit}"

        return {
            "visible": visible,
            "display": display,
            "result": f"nat {natural}, total {total}, rolls {rolls}",
            "natural": natural,
            "total": total,
        }


