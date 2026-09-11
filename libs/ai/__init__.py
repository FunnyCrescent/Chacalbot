"""libs.ai — LLM integration (split into mixins).

Backward-compat: `from libs.ai_client import DMEngine, OpenAIClient, ...`
still works — keep ai_client.py as a shim that re-exports from here.
"""
from .client import OpenAIClient
from . import usage_ledger
from .engine import DMEngine
from .tools import (
    ROLL_TYPE_ENUM, DICE_TOOLS, GAME_TOOLS, ALL_TOOLS,
    COMBAT_TOOLS, MASTER_TOOLS, GET_TOOLS, DB_TOOLS, DB_WRITE_TOOL_NAMES,
    REQUEST_ROLL_INTENT_TOOL, MODER_AI_DISPATCH_TOOLS,
    ASK_MASTER_TOOL, DISPATCH_ROLL_TOOL,
)
from .prompts import (
    MASTER_PROMPT, ASK_PROMPT, RENDERER_PROMPT, NPC_AI_PROMPT, DB_BOT_PROMPT,
    MODER_AI_PROMPT, MODER_AI_DISPATCH_PROMPT,
    get_prompt,
)
from .utils import strip_stray_tags

__all__ = [
    "DMEngine", "OpenAIClient", "strip_stray_tags",
    "ROLL_TYPE_ENUM", "DICE_TOOLS", "GAME_TOOLS", "ALL_TOOLS",
    "COMBAT_TOOLS", "MASTER_TOOLS", "GET_TOOLS", "DB_TOOLS",
    "DB_WRITE_TOOL_NAMES",
    "REQUEST_ROLL_INTENT_TOOL", "MODER_AI_DISPATCH_TOOLS",
    "ASK_MASTER_TOOL", "DISPATCH_ROLL_TOOL",
    "MASTER_PROMPT", "ASK_PROMPT", "RENDERER_PROMPT",
    "NPC_AI_PROMPT", "DB_BOT_PROMPT", "MODER_AI_PROMPT", "MODER_AI_DISPATCH_PROMPT",
    "get_prompt",
]
