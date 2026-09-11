"""
anti-spam — Clean Book Mode (cross-cutting, no own handlers).
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.config_legacy import (
    ANTISPAM_ENABLED, ANTISPAM_DELETE_DELAY, ANTISPAM_DELETE_TYPES,
    ANTISPAM_DM_ONLY_COMMANDS,
)

logger = logging.getLogger(__name__)


class AntiSpamPlugin(Plugin):
    name = "anti-spam"
    version = "0.2.0"
    description = "Clean Book Mode — auto-delete Дн./ask/confirm messages after narrative"
    author = "core refactor"
    depends_on: list[str] = []

    async def setup(self, app, ctx) -> None:
        ctx.register_service("anti_spam_config", {
            "enabled": ANTISPAM_ENABLED,
            "delay": ANTISPAM_DELETE_DELAY,
            "delete_types": ANTISPAM_DELETE_TYPES,
            "dm_only_commands": ANTISPAM_DM_ONLY_COMMANDS,
        })
        if ANTISPAM_ENABLED:
            logger.info("anti-spam: Clean Book Mode ENABLED (delay=%ss, types=%s)",
                        ANTISPAM_DELETE_DELAY, ANTISPAM_DELETE_TYPES)
        else:
            logger.info("anti-spam: Clean Book Mode DISABLED in config")
