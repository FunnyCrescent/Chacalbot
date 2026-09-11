"""
core.manager — PluginManager.

Отвечает за:
  1. Auto-discovery: сканирует папку plugins/ (рекурсивно, до 2 уровней).
  2. Dependency resolution: топологическая сортировка по depends_on.
  3. Жизненный цикл: создаёт инстанс, вызывает setup(app, ctx).
  4. Реестр: хранит все загруженные плагины.
"""

from __future__ import annotations
import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .plugin import Plugin
from .context import PluginContext
from .hooks import HookRegistry
from .config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class PluginRecord:
    name: str
    plugin_class: type[Plugin]
    instance: Optional[Plugin] = None
    enabled_in_config: bool = True
    loaded: bool = False
    skip_reason: Optional[str] = None
    file_path: Optional[Path] = None


class PluginManager:
    def __init__(self, plugins_dir: Path, config: AppConfig):
        self.plugins_dir = plugins_dir
        self.config = config
        self.hooks = HookRegistry()
        self.ctx = PluginContext(config=config, manager=self, hooks=self.hooks)
        self._records: dict[str, PluginRecord] = {}
        self._load_order: list[str] = []

    def discover(self) -> None:
        if not self.plugins_dir.exists():
            logger.warning("Plugins directory not found: %s", self.plugins_dir)
            return

        parent = str(self.plugins_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        self._ensure_package_init(self.plugins_dir)

        for entry in sorted(self.plugins_dir.iterdir()):
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            if entry.is_dir() and (entry / "plugin.py").exists():
                self._discover_module(f"plugins.{entry.name}.plugin", entry / "plugin.py")
            elif entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
                mod_name = f"plugins.{entry.stem}"
                self._discover_module(mod_name, entry)

        logger.info(
            "Discovery complete: %d plugin classes found, %d enabled in config",
            len(self._records),
            sum(1 for r in self._records.values() if r.enabled_in_config),
        )

    def _ensure_package_init(self, pkg_dir: Path) -> None:
        init = pkg_dir / "__init__.py"
        if not init.exists():
            init.write_text('"""Auto-generated plugins package init."""\n')

    def _discover_module(self, mod_name: str, file_path: Path) -> None:
        try:
            module = importlib.import_module(mod_name)
        except Exception:
            logger.exception("Failed to import plugin module %s (%s)", mod_name, file_path)
            return

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Plugin)
                and obj is not Plugin
                and obj.__module__ == mod_name
            ):
                self._register_class(obj, file_path)

    def _register_class(self, plugin_class: type[Plugin], file_path: Path) -> None:
        try:
            instance_for_name = plugin_class()
        except Exception:
            logger.exception("Failed to instantiate %s", plugin_class.__name__)
            return

        name = instance_for_name.name
        if not name:
            logger.warning("Plugin class %s has empty 'name' — skipping", plugin_class.__name__)
            return

        if name in self._records:
            logger.warning(
                "Duplicate plugin name '%s' (in %s) — already from %s. Skipping.",
                name, file_path, self._records[name].file_path,
            )
            return

        enabled = self.config.plugin_enabled(name)
        self._records[name] = PluginRecord(
            name=name, plugin_class=plugin_class,
            enabled_in_config=enabled, file_path=file_path,
        )
        logger.debug("Discovered plugin '%s' v%s [%s]",
                     name, instance_for_name.version,
                     "enabled" if enabled else "DISABLED")

    def resolve(self) -> None:
        enabled = {name: rec for name, rec in self._records.items() if rec.enabled_in_config}
        order: list[str] = []
        visited: set[str] = set()
        visiting: set[str] = set()
        skipped: dict[str, str] = {}

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                skipped[name] = f"Circular dependency detected involving '{name}'"
                return
            visiting.add(name)
            rec = enabled.get(name)
            if rec is None:
                return
            for dep in rec.plugin_class.depends_on:
                if dep not in enabled:
                    skipped[name] = f"Dependency '{dep}' is not enabled (disabled or not discovered)"
                    visiting.discard(name)
                    return
                visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for name in enabled:
            visit(name)

        for name, reason in skipped.items():
            self._records[name].skip_reason = reason
            logger.warning("Plugin '%s' skipped: %s", name, reason)

        self._load_order = order
        logger.info("Resolved load order (%d plugins): %s",
                    len(order), " -> ".join(order) if order else "(empty)")

    async def setup_all(self, app) -> None:
        for name in self._load_order:
            rec = self._records[name]
            try:
                instance = rec.plugin_class()
                rec.instance = instance
                await instance.setup(app, self.ctx)
                rec.loaded = True
                logger.info("Plugin loaded: %s v%s", name, instance.version)
            except Exception:
                rec.skip_reason = "setup() raised exception"
                logger.exception("Plugin '%s' setup() failed", name)

    def get_plugin(self, name: str) -> Optional[Plugin]:
        rec = self._records.get(name)
        return rec.instance if rec and rec.loaded else None

    def is_enabled(self, name: str) -> bool:
        rec = self._records.get(name)
        return bool(rec and rec.enabled_in_config and rec.loaded)

    def list_plugins(self) -> list[PluginRecord]:
        return list(self._records.values())

    def list_loaded(self) -> list[Plugin]:
        return [rec.instance for rec in self._records.values()
                if rec.loaded and rec.instance is not None]

    def load_order(self) -> list[str]:
        return list(self._load_order)

    def status_table(self) -> str:
        lines = ["", "Plugin Manager Status:", "=" * 70]
        lines.append("NAME                   VER        STATE      DEPS                      NOTE")
        lines.append("-" * 70)
        for rec in self._records.values():
            deps = ",".join(rec.plugin_class.depends_on) or "-"
            state = (
                "LOADED" if rec.loaded
                else "SKIPPED" if rec.skip_reason
                else "DISABLED" if not rec.enabled_in_config
                else "PENDING"
            )
            note = rec.skip_reason or ""
            lines.append(f"{rec.name:<22} {rec.plugin_class.version:<10} {state:<10} {deps:<25} {note}")
        lines.append("=" * 70)
        return "\n".join(lines)
