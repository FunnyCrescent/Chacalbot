"""
core — ядро плагинной системы D&D бота.
"""

from .plugin import Plugin
from .context import PluginContext
from .hooks import HookRegistry
from .manager import PluginManager
from .config import AppConfig
from .bootstrap import Bootstrap

__all__ = [
    "Plugin", "PluginContext", "HookRegistry",
    "PluginManager", "AppConfig", "Bootstrap",
]

__version__ = "0.2.0"
