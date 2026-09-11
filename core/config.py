"""
core.config — AppConfig (.env + plugins.toml loader).
"""

from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@dataclass
class AppConfig:
    bot_token: str = ""
    admin_chat_id: int = 0
    base_dir: Path = field(default_factory=Path.cwd)
    plugins_dir: Path = field(default_factory=lambda: Path("plugins"))
    plugins_config_path: Path = field(default_factory=lambda: Path("plugins.toml"))
    log_level: str = "INFO"
    concurrent_updates: bool = True
    _plugins_raw: dict[str, dict[str, Any]] = field(default_factory=dict)
    _disabled: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, base_dir: Path | str = ".") -> "AppConfig":
        base = Path(base_dir).resolve()
        env_path = base / ".env"
        if env_path.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path, override=False)
            except ImportError:
                logger.warning("python-dotenv not installed — .env not loaded")

        cfg = cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            admin_chat_id=int(os.environ.get("ADMIN_CHAT_ID", "0") or "0"),
            base_dir=base,
            plugins_dir=base / "plugins",
            plugins_config_path=base / "plugins.toml",
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            concurrent_updates=os.environ.get("CONCURRENT_UPDATES", "1") not in ("0", "false", "no"),
        )
        cfg._load_plugins_toml()
        return cfg

    def _load_plugins_toml(self) -> None:
        if not self.plugins_config_path.exists():
            logger.info("No plugins.toml — all discovered plugins enabled by default")
            return
        with open(self.plugins_config_path, "rb") as f:
            data = tomllib.load(f)
        core_section = data.get("core", {})
        if "log_level" in core_section:
            self.log_level = core_section["log_level"]
        if "concurrent_updates" in core_section:
            self.concurrent_updates = bool(core_section["concurrent_updates"])
        plugins_section = data.get("plugins", {})
        for name, section in plugins_section.items():
            if not isinstance(section, dict):
                continue
            self._plugins_raw[name] = section
            if section.get("enabled") is False:
                self._disabled.add(name)
        logger.info("Loaded plugins.toml: %d sections, %d disabled",
                    len(self._plugins_raw), len(self._disabled))

    def plugin_enabled(self, name: str) -> bool:
        return name not in self._disabled

    def plugin_section(self, name: str) -> dict[str, Any]:
        return dict(self._plugins_raw.get(name, {}))

    def all_plugin_sections(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._plugins_raw.items()}

    def validate_required(self) -> None:
        if not self.bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not set. Create .env with TELEGRAM_BOT_TOKEN=<token>."
            )

    def __repr__(self) -> str:
        return (f"<AppConfig base_dir={self.base_dir} "
                f"plugins_dir={self.plugins_dir} "
                f"disabled={sorted(self._disabled)}>")
