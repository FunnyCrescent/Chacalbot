"""Database — composite class собирающий все mixin's.

Это единственный класс, который инстанциируется. Все mixin's
подключены через multiple inheritance.
"""
from .base import BaseDatabase
from .session_repo import SessionRepoMixin
from .character_repo import CharacterRepoMixin
from .world_repo import WorldRepoMixin
from .combat_repo import CombatRepoMixin
from .memory_repo import MemoryRepoMixin
from .srd_repo import SrdRepoMixin
from .economy_repo import EconomyRepoMixin
from .settings_repo import SettingsRepoMixin


class Database(
    SessionRepoMixin,
    CharacterRepoMixin,
    WorldRepoMixin,
    CombatRepoMixin,
    MemoryRepoMixin,
    SrdRepoMixin,
    EconomyRepoMixin,
    SettingsRepoMixin,
    BaseDatabase,
):
    """Per-session SQLite database. Mixin composition."""
    pass
