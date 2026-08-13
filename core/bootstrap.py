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

# APScheduler for daily audit
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    logger.warning("apscheduler not installed — daily audit scheduling disabled")


class Bootstrap:
    def __init__(self, config: AppConfig, manager: PluginManager):
        self.config = config
        self.manager = manager
        self.scheduler = None

    def build(self) -> "PTBApplication":
        log_proxy_status()
        builder = (
            ApplicationBuilder()
            .token(self.config.bot_token)
            .concurrent_updates(self.config.concurrent_updates)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        builder = apply_telegram_proxy(builder)
        app = builder.build()

        # Set up APScheduler for daily audit
        if _HAS_APSCHEDULER:
            self.scheduler = AsyncIOScheduler()

        logger.info("Telegram Application built (token: %s...)", self.config.bot_token[:10])
        return app

    async def _post_init(self, application: "PTBApplication") -> None:
        logger.info("post_init: loading plugins...")
        await self.manager.setup_all(application)
        logger.info("post_init: plugins loaded. Load order: %s", self.manager.load_order())
        logger.info(self.manager.status_table())

        # Start daily audit scheduler
        if self.scheduler:
            try:
                from libs.config_legacy import AUDITOR_ENABLED, AUDITOR_SCHEDULE_HOURS
                if AUDITOR_ENABLED:
                    from libs.handlers._state import dm_engine

                    async def _run_audit_job():
                        """Scheduled audit job — runs on all active sessions."""
                        try:
                            if dm_engine and hasattr(dm_engine, 'run_daily_audit'):
                                report_paths = await dm_engine.run_daily_audit()
                                if report_paths:
                                    logger.info(f"[audit scheduler] Audit complete: {len(report_paths)} report(s)")
                        except Exception as e:
                            logger.error(f"[audit scheduler] Audit job failed: {e}")

                    self.scheduler.add_job(
                        _run_audit_job,
                        IntervalTrigger(hours=AUDITOR_SCHEDULE_HOURS),
                        id="daily_audit",
                        name="Daily Session Audit",
                        replace_existing=True,
                    )
                    self.scheduler.start()
                    logger.info(f"[audit scheduler] Started — running every {AUDITOR_SCHEDULE_HOURS}h")
                else:
                    logger.info("[audit scheduler] AUDITOR_ENABLED=False, scheduler not started")
            except Exception as e:
                logger.error(f"[audit scheduler] Failed to start: {e}")

    async def _post_shutdown(self, application: "PTBApplication") -> None:
        # Stop scheduler
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("[audit scheduler] Stopped")

        logger.info("post_shutdown: cleaning up %d plugins", len(self.manager.list_loaded()))
        for plugin in self.manager.list_loaded():
            shutdown_fn = getattr(plugin, "on_shutdown", None)
            if callable(shutdown_fn):
                try:
                    await shutdown_fn(application)
                except Exception:
                    logger.exception("Plugin '%s' on_shutdown() failed", plugin.name)
