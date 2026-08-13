"""AuditMixin — integration mixin for the main engine.

Provides two entry points:
  - run_daily_audit()     — called by cron/scheduler every N hours
  - audit_on_demand()    — manual trigger from admin command

The mixin reads audit results from .md files and can feed them
into the Master's context so the DM is aware of detected issues.

Typical usage (inside the main Engine class)::

    class Engine(AuditMixin, ...otherMixins...):
        ...

    # Cron:
    await engine.run_daily_audit()

    # Admin command:
    report_path = await engine.audit_on_demand("session_123")
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from libs.config_legacy import (
    AUDITOR_ENABLED,
    AUDITOR_MODEL,
    AUDITOR_TEMP,
    AUDITOR_MAX_TOKENS,
    AUDITOR_REPORTS_DIR,
    AUDITOR_SCHEDULE_HOURS,
)

logger = logging.getLogger(__name__)


class AuditMixin:
    """Mixin that adds audit capabilities to the main engine.

    Expects the host class to have:
      - self.db_manager: DatabaseManager
      - self.rule_engine: Optional[RuleEngine]
      - self.auditor: SessionAuditor (lazy-initialised by this mixin)
    """

    _auditor_instance: Optional[Any] = None
    _last_audit_time: Optional[datetime] = None

    # ───────────────────────────────────────────────────────────────
    # Lazy auditor initialisation
    # ───────────────────────────────────────────────────────────────

    def _get_auditor(self) -> Any:
        """Lazy-initialise the SessionAuditor instance."""
        if self._auditor_instance is None:
            from libs.ai.session_auditor import SessionAuditor
            from libs.ai.client import OpenAIClient

            llm_client = OpenAIClient(
                model=AUDITOR_MODEL,
                temperature=AUDITOR_TEMP,
                max_tokens=AUDITOR_MAX_TOKENS,
            )
            self._auditor_instance = SessionAuditor(
                llm_client=llm_client,
                db_manager=self.db_manager,
                rule_engine=getattr(self, 'rule_engine', None),
            )
        return self._auditor_instance

    # ───────────────────────────────────────────────────────────────
    # Scheduled audit
    # ───────────────────────────────────────────────────────────────

    async def run_daily_audit(self) -> List[str]:
        """Run scheduled audit on all active sessions.

        Called by a cron job / scheduler.  Only runs if:
          1. AUDITOR_ENABLED is True
          2. Enough time has passed since the last audit (AUDITOR_SCHEDULE_HOURS)

        Returns:
            List of paths to written .md report files.
        """
        if not AUDITOR_ENABLED:
            logger.debug("[audit_mixin] Auditor disabled, skipping")
            return []

        # Check if enough time has passed since last audit
        now = datetime.utcnow()
        if self._last_audit_time:
            elapsed = now - self._last_audit_time
            if elapsed < timedelta(hours=AUDITOR_SCHEDULE_HOURS):
                logger.debug(
                    f"[audit_mixin] Last audit was {elapsed.total_seconds()/3600:.1f}h ago, "
                    f"schedule is every {AUDITOR_SCHEDULE_HOURS}h — skipping"
                )
                return []

        logger.info("[audit_mixin] Starting daily audit...")
        self._last_audit_time = now

        auditor = self._get_auditor()
        report_paths: List[str] = []

        try:
            # Audit all recent sessions
            reports = await auditor.audit_recent_sessions(hours=AUDITOR_SCHEDULE_HOURS)

            # Write each report to .md
            for report in reports:
                try:
                    path = await auditor.write_audit_to_md(report.session_id)
                    report_paths.append(path)

                    # Log summary
                    issues = (
                        len(report.character_issues.invalid_characters)
                        + len(report.rule_violations.violations)
                        + len(report.dice_anomalies.suspicious_patterns)
                        + len(report.world_inconsistencies)
                    )
                    if issues > 0:
                        logger.warning(
                            f"[audit_mixin] Session {report.session_id}: "
                            f"{issues} issue(s) — report at {path}"
                        )
                    else:
                        logger.info(
                            f"[audit_mixin] Session {report.session_id}: clean"
                        )
                except Exception as e:
                    logger.error(f"[audit_mixin] Failed to write report for {report.session_id}: {e}")

        except Exception as e:
            logger.error(f"[audit_mixin] Daily audit failed: {e}")

        logger.info(f"[audit_mixin] Daily audit complete: {len(report_paths)} report(s)")
        return report_paths

    # ───────────────────────────────────────────────────────────────
    # On-demand audit
    # ───────────────────────────────────────────────────────────────

    async def audit_on_demand(self, session_id: str) -> Optional[str]:
        """Run an audit on a specific session immediately.

        This is triggered manually (e.g. by an admin command).

        Args:
            session_id: The session to audit.

        Returns:
            Path to the written .md report, or None on error.
        """
        if not AUDITOR_ENABLED:
            logger.info(f"[audit_mixin] Auditor disabled, cannot audit {session_id}")
            return None

        logger.info(f"[audit_mixin] On-demand audit for {session_id}")
        auditor = self._get_auditor()

        try:
            path = await auditor.write_audit_to_md(session_id)
            logger.info(f"[audit_mixin] On-demand audit report: {path}")
            return path
        except Exception as e:
            logger.error(f"[audit_mixin] On-demand audit failed for {session_id}: {e}")
            return None

    # ───────────────────────────────────────────────────────────────
    # Feed audit results into Master context
    # ───────────────────────────────────────────────────────────────

    def get_audit_context_for_master(self, session_id: str) -> str:
        """Read the latest audit report for a session and return a context
        string suitable for injection into the Master's system prompt.

        This allows the Master to be aware of detected issues and
        adjust its behavior accordingly.

        Args:
            session_id: The session to get audit context for.

        Returns:
            A string to append to the Master's context, or "" if no
            recent audit report exists.
        """
        reports_dir = Path(AUDITOR_REPORTS_DIR)
        if not reports_dir.exists():
            return ""

        # Find the most recent report for this session
        session_reports = sorted(
            reports_dir.glob(f"{session_id}_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not session_reports:
            return ""

        latest = session_reports[0]

        # Only include if report is from the last 48 hours
        try:
            mtime = datetime.fromtimestamp(latest.stat().st_mtime)
            if datetime.utcnow() - mtime > timedelta(hours=48):
                return ""
        except Exception:
            return ""

        try:
            content = latest.read_text(encoding="utf-8")

            # Extract only the actionable parts (issues + recommendations)
            # Don't feed the entire report — just the key findings
            sections_to_extract = [
                "1. Character Validity",
                "2. Rule Compliance",
                "3. Dice Integrity",
                "4. World Consistency",
                "5. Recommendations",
            ]

            extracted_lines: List[str] = []
            include = False
            for line in content.split("\n"):
                if any(s in line for s in sections_to_extract):
                    include = True
                elif line.startswith("## ") and include:
                    # New top-level section not in our list — stop
                    if not any(s in line for s in sections_to_extract):
                        include = False

                if include:
                    extracted_lines.append(line)

            if not extracted_lines:
                return ""

            audit_context = "\n".join(extracted_lines)

            # Truncate to avoid context bloat
            if len(audit_context) > 2000:
                audit_context = audit_context[:2000] + "\n... (truncated)"

            return (
                "\n\n## ПОСЛЕДНИЙ АУДИТ СЕССИИ\n"
                "Автоматический аудитор обнаружил следующие проблемы. Учти их:\n\n"
                + audit_context
            )

        except Exception as e:
            logger.warning(f"[audit_mixin] Cannot read audit report {latest}: {e}")
            return ""

    # ───────────────────────────────────────────────────────────────
    # Audit summary for admin display
    # ───────────────────────────────────────────────────────────────

    def get_audit_summary(self, session_id: str) -> Dict[str, Any]:
        """Return a summary dict of the latest audit for admin display.

        This is used by admin commands to show a quick overview
        without reading the full .md file.
        """
        reports_dir = Path(AUDITOR_REPORTS_DIR)
        if not reports_dir.exists():
            return {"status": "no_reports", "message": "No audit reports found"}

        session_reports = sorted(
            reports_dir.glob(f"{session_id}_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not session_reports:
            return {"status": "no_reports", "message": f"No audit reports for session {session_id}"}

        latest = session_reports[0]
        try:
            mtime = datetime.fromtimestamp(latest.stat().st_mtime)
            content = latest.read_text(encoding="utf-8")

            # Quick scan for issue counts
            char_issues = content.count("❌")
            rule_violations = content.count("🚫")
            dice_anomalies = content.count("🎲")
            world_issues = content.count("🌍")
            warnings = content.count("⚠️")
            total = char_issues + rule_violations + dice_anomalies + world_issues

            return {
                "status": "ok" if total == 0 else "issues_found",
                "report_path": str(latest),
                "last_audit": mtime.isoformat(),
                "total_issues": total,
                "character_issues": char_issues,
                "rule_violations": rule_violations,
                "dice_anomalies": dice_anomalies,
                "world_issues": world_issues,
                "warnings": warnings,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
