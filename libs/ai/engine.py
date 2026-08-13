"""DMEngine — composite class собирающий все mixin's."""
from .base import BaseEngine
from .master_engine import MasterEngineMixin
from .db_bot_engine import DBBotEngineMixin
from .renderer_engine import RendererEngineMixin
from .memory_engine import MemoryEngineMixin
from .npc_ai_engine import NpcAIEngineMixin
from .validators import ValidatorsMixin
from .rule_validator_mixin import RuleValidatorMixin
from .audit_mixin import AuditMixin
from .manual_rolls import ManualRollsMixin
from .moder_ai_engine import ModerAIEngineMixin


class DMEngine(
    MasterEngineMixin,
    DBBotEngineMixin,
    RendererEngineMixin,
    MemoryEngineMixin,
    NpcAIEngineMixin,
    ValidatorsMixin,
    RuleValidatorMixin,
    AuditMixin,
    ManualRollsMixin,
    ModerAIEngineMixin,
    BaseEngine,
):
    """Composite DMEngine — 6 LLM clients + ModerAI + RuleValidator + Audit + all pipelines."""
    pass
