"""
core.context — общий контекст, передаваемый в Plugin.setup(app, ctx).
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .manager import PluginManager
    from .hooks import HookRegistry
    from .config import AppConfig

logger = logging.getLogger(__name__)


class PluginContext:
    def __init__(self, config: "AppConfig", manager: "PluginManager", hooks: "HookRegistry"):
        self.config = config
        self.manager = manager
        self.hooks = hooks
        self.services: dict[str, Any] = {}

    def register_service(self, name: str, instance: Any) -> None:
        if name in self.services:
            logger.warning("Service '%s' already registered — overwriting", name)
        self.services[name] = instance
        logger.debug("Service registered: %s", name)

    def get_service(self, name: str) -> Any:
        return self.services.get(name)

    def require_service(self, name: str) -> Any:
        if name not in self.services:
            raise KeyError(
                f"Required service '{name}' is not registered. "
                f"Check plugin load order — depends_on must list the provider."
            )
        return self.services[name]

    def get_plugin(self, name: str) -> Optional[Any]:
        return self.manager.get_plugin(name)

    def is_plugin_enabled(self, name: str) -> bool:
        return self.manager.is_enabled(name)

    def plugin_config(self, plugin_name: str) -> dict[str, Any]:
        return self.config.plugin_section(plugin_name)

    def __repr__(self) -> str:
        return (
            f"<PluginContext services={list(self.services)} "
            f"plugins_loaded={len(self.manager._instances)} "
            f"hooks={len(self.hooks._hooks)}>"
        )
