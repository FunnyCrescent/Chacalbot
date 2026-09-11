"""SessionManager — composite class собирающий все mixin's."""
from .base import BaseSessionMixin
from .session_lifecycle import SessionLifecycleMixin
from .round_coordinator import RoundCoordinatorMixin
from .combat_coordinator import CombatCoordinatorMixin
from .resolution import ResolutionMixin
from .character_service import CharacterServiceMixin
from .world_service import WorldServiceMixin
from .generators import GeneratorsMixin
from .manual_rolls import ManualRollsMixin


class SessionManager(
    SessionLifecycleMixin,
    RoundCoordinatorMixin,
    CombatCoordinatorMixin,
    ResolutionMixin,
    CharacterServiceMixin,
    WorldServiceMixin,
    GeneratorsMixin,
    ManualRollsMixin,
    BaseSessionMixin,
):
    """Composite SessionManager — game-state orchestrator."""
    pass
