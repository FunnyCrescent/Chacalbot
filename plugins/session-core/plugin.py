"""
session-core — сервисный плагин: SessionManager + восстановление сессий при старте.

Регистрирует в ctx.services:
    - 'sessions': SessionManager
    - 'crash_recovery': dict с combat_recovery_sessions list
"""
from __future__ import annotations

import logging

from core.plugin import Plugin
from libs.session import SessionManager

logger = logging.getLogger(__name__)


class SessionCorePlugin(Plugin):
    name = "session-core"
    version = "0.2.0"
    description = "SessionManager (game-state orchestrator, 9 mixins) + crash recovery on startup"
    author = "core refactor"
    provides = ["sessions", "crash_recovery"]
    depends_on = ["persistence", "ai-engine"]

    async def setup(self, app, ctx) -> None:
        db_manager = ctx.require_service("db_manager")
        dm_engine = ctx.require_service("dm_engine")

        sessions = SessionManager(db_manager, dm_engine)
        ctx.register_service("sessions", sessions)

        from libs import bot_handlers
        bot_handlers.sessions = sessions

        recovery_state = {"combat_sessions": [], "sessions_manager": sessions}
        ctx.register_service("crash_recovery", recovery_state)

        try:
            active = db_manager.get_all_active_sessions()
            logger.info("Crash recovery: %d active sessions found", len(active))

            for session in active:
                try:
                    db = db_manager.get_db(session.session_id)
                    history_count = db.count_history(session.session_id)
                    logger.info("  session %s: history=%d, combat=%s",
                                session.session_id, history_count,
                                getattr(session, "combat_active", False))

                    if getattr(session, "combat_active", False):
                        chat_id = getattr(session, "chat_id", None)
                        if chat_id:
                            recovery_state["combat_sessions"].append(
                                (session.session_id, chat_id)
                            )
                except Exception:
                    logger.exception("  failed to recover session %s",
                                     getattr(session, "session_id", "?"))
        except Exception:
            logger.exception("Crash recovery scan failed (non-fatal)")

        logger.info("Session-core ready: %d combat loops to restart",
                    len(recovery_state["combat_sessions"]))
