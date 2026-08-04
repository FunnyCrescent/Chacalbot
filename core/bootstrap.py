"""
core.bootstrap — Application builder + post_init/post_shutdown hooks.
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from telegram.ext import Application, ApplicationBuilder

from .config import AppConfig
from .manager import PluginManager
from libs.proxy_helper import apply_telegram_proxy, log_status as log_proxy_status

if TYPE_CHECKING:
    from telegram.ext import Application as PTBApplication

logger = logging.getLogger(__name__)


class Bootstrap:
    def __init__(self, config: AppConfig, manager: PluginManager):
        self.config = config
        self.manager = manager

    def build(self) -> "PTBApplication":
        log_proxy_status()  # информационная строка в лог
        builder = (
            ApplicationBuilder()
            .token(self.config.bot_token)
            .concurrent_updates(self.config.concurrent_updates)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        # Применяем прокси к httpx-реквестам PTB (Telegram API + getUpdates).
        # Если GLOBAL_PROXY / TELEGRAM_PROXY пусто — apply_telegram_proxy возвращает builder as-is.
        builder = apply_telegram_proxy(builder)
        app = builder.build()
        logger.info("Telegram Application built (token: %s...)", self.config.bot_token[:10])
        return app

    async def _post_init(self, application: "PTBApplication") -> None:
        logger.info("post_init: loading plugins...")
        await self.manager.setup_all(application)
        logger.info("post_init: plugins loaded. Load order: %s", self.manager.load_order())
        logger.info(self.manager.status_table())

    async def _post_shutdown(self, application: "PTBApplication") -> None:
        logger.info("post_shutdown: cleaning up %d plugins", len(self.manager.list_loaded()))
        for plugin in self.manager.list_loaded():
            shutdown_fn = getattr(plugin, "on_shutdown", None)
            if callable(shutdown_fn):
                try:
                    await shutdown_fn(application)
                except Exception:
                    logger.exception("Plugin '%s' on_shutdown() failed", plugin.name)
