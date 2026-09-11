"""
logging — Markdown-транскрипты сессий + rotating file log.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from core.plugin import Plugin
from libs.config_legacy import BASE_DIR, LOG_PATH
from libs.bot_handlers import MarkdownLogger

logger = logging.getLogger(__name__)


class LoggingPlugin(Plugin):
    name = "logging"
    version = "0.2.0"
    description = "Markdown transcripts + rotating file log + round-message tracking"
    author = "core refactor"
    provides = ["markdown_logger"]
    depends_on = ["persistence"]

    async def setup(self, app, ctx) -> None:
        logs_dir = os.path.join(BASE_DIR, "logs")
        markdown_dir = os.path.join(logs_dir, "markdown")
        os.makedirs(markdown_dir, exist_ok=True)

        root = logging.getLogger()
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            try:
                fh = RotatingFileHandler(
                    LOG_PATH or os.path.join(logs_dir, "bot.log"),
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5, encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                ))
                root.addHandler(fh)
                logger.info("File log handler attached: %s", LOG_PATH)
            except Exception:
                logger.exception("Failed to attach file handler (non-fatal)")

        ctx.register_service("markdown_logger", MarkdownLogger)
        logger.info("logging: MarkdownLogger registered as service")
