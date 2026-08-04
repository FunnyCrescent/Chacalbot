"""libs.session — game-state orchestrator (split into mixins).

Backward-compat: `from libs.session_manager import SessionManager`
still works via the shim in libs/session_manager.py.
"""
from .manager import SessionManager

__all__ = ["SessionManager"]
