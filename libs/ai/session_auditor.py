"""Session Auditor — periodically analyzes game sessions for problems.

Checks four dimensions of session quality:
  1. Character validity   — are characters using valid races/classes/backgrounds?
  2. Rule compliance      — did the Master allow actions that violate rules?
  3. Dice integrity       — are dice roll distributions statistically normal?
  4. World consistency    — NPCs in wrong locations, impossible inventory, etc.

Uses a lightweight LLM (deepseek-v4-flash by default) for cost-efficient
analysis of narrative text against rule .md files, plus pure-python
statistical tests for dice anomaly detection.

Typical usage::

    from libs.ai.session_auditor import SessionAuditor
    from libs.ai.client import OpenAIClient
    from libs.db.manager import DatabaseManager
    from libs.config_legacy import AUDITOR_MODEL

    llm = OpenAIClient(model=AUDITOR_MODEL)
    db  = DatabaseManager()
    auditor = SessionAuditor(llm_client=llm, db_manager=db)

    report = await auditor.audit_session("session_123")
    print(report)
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from libs.config_legacy import (
    AUDITOR_DICE_ANOMALY_THRESHOLD,
    AUDITOR_DICE_TURNS,
    AUDITOR_ENABLED,
    AUDITOR_MODEL,
    AUDITOR_REPORTS_DIR,
    AUDITOR_RULE_COMPLIANCE_TURNS,
    AUDITOR_SCHEDULE_HOURS,
    AUDITOR_TEMP,
    AUDITOR_MAX_TOKENS,
    BASE_DIR,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Data classes for audit results
# ═══════════════════════════════════════════════════════════════

@dataclass
class CharacterAuditResult:
    """Result of auditing characters in a session."""
    characters_checked: int = 0
    invalid_characters: List[str] = field(default_factory=list)  # character names
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleAuditResult:
    """Result of auditing rule compliance in recent turns."""
    turns_checked: int = 0
    violations: List[str] = field(default_factory=list)  # human-readable
    severity_counts: Dict[str, int] = field(default_factory=lambda: {"high": 0, "medium": 0, "low": 0})
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DiceAuditResult:
    """Result of auditing dice roll distributions."""
    rolls_checked: int = 0
    suspicious_patterns: List[str] = field(default_factory=list)
    stat_significance: Dict[str, float] = field(default_factory=dict)  # player -> p-value
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    """Complete audit report for a single session."""
    session_id: str = ""
    timestamp: str = ""
    character_issues: CharacterAuditResult = field(default_factory=CharacterAuditResult)
    rule_violations: RuleAuditResult = field(default_factory=RuleAuditResult)
    dice_anomalies: DiceAuditResult = field(default_factory=DiceAuditResult)
    world_inconsistencies: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def summary(self) -> str:
        """Short one-line summary."""
        issues = (
            len(self.character_issues.invalid_characters)
            + len(self.rule_violations.violations)
            + len(self.dice_anomalies.suspicious_patterns)
            + len(self.world_inconsistencies)
        )
        return f"Session {self.session_id}: {issues} issue(s) found at {self.timestamp}"


# ═══════════════════════════════════════════════════════════════
# Chi-squared goodness-of-fit test (no scipy dependency)
# ═══════════════════════════════════════════════════════════════

def _chi_squared_gof(observed: List[int], expected: List[float]) -> Tuple[float, float]:
    """Chi-squared goodness-of-fit test.

    Args:
        observed: Observed counts per category (e.g. 20 bins for d20).
        expected: Expected counts per category (uniform: n/20 for d20).

    Returns:
        (chi2_statistic, p_value) — p_value approximated via chi2 cdf
        with df = len(observed) - 1.
    """
    k = len(observed)
    if k < 2:
        return 0.0, 1.0

    chi2 = 0.0
    for o, e in zip(observed, expected):
        if e > 0:
            chi2 += (o - e) ** 2 / e

    # Approximate p-value using the regularized incomplete gamma function
    # For chi2 distribution with df degrees of freedom:
    #   P(X > chi2) = 1 - regularized_gamma(df/2, chi2/2)
    # We use a series expansion for the incomplete gamma function.
    df = k - 1
    p_value = _chi2_sf(chi2, df)
    return chi2, p_value


def _chi2_sf(x: float, df: int) -> float:
    """Survival function of chi-squared distribution: P(X > x) for df degrees of freedom.

    Uses the regularized incomplete gamma function series expansion.
    """
    if x <= 0:
        return 1.0
    if df <= 0:
        return 0.0

    # P(X > x) = regularized_gamma_upper(df/2, x/2)
    a = df / 2.0
    x_half = x / 2.0

    # Use series expansion for the upper incomplete gamma function
    # normalized by gamma(a)
    return _regularized_gamma_upper(a, x_half)


def _regularized_gamma_upper(a: float, x: float) -> float:
    """Regularized upper incomplete gamma function Q(a, x) = Gamma(a, x) / Gamma(a).

    Uses continued fraction representation for numerical stability.
    """
    if x < a + 1:
        # Use 1 - lower
        return 1.0 - _regularized_gamma_lower(a, x)
    else:
        # Use continued fraction for upper directly
        return _gamma_upper_cf(a, x)


def _regularized_gamma_lower(a: float, x: float) -> float:
    """Regularized lower incomplete gamma function P(a, x) via series expansion."""
    if x == 0:
        return 0.0

    # Series: P(a, x) = e^(-x) * x^a * sum_{n=0}^{inf} x^n / (a * (a+1) * ... * (a+n))
    term = 1.0 / a
    total = term
    for n in range(1, 300):
        term *= x / (a + n)
        total += term
        if abs(term) < abs(total) * 1e-12:
            break

    log_prefactor = a * math.log(x) - x - _log_gamma(a)
    prefactor = math.exp(log_prefactor)
    return prefactor * total


def _gamma_upper_cf(a: float, x: float) -> float:
    """Upper incomplete gamma via Lentz's continued fraction."""
    log_prefactor = a * math.log(x) - x - _log_gamma(a)
    prefactor = math.exp(log_prefactor)

    # Lentz's algorithm for continued fraction
    tiny = 1e-30
    b = x + 1 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d

    for i in range(1, 300):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break

    return prefactor * h


def _log_gamma(x: float) -> float:
    """Log of gamma function via Lanczos approximation."""
    if x < 0.5:
        # Reflection formula
        return math.log(math.pi / math.sin(math.pi * x)) - _log_gamma(1 - x)

    # Lanczos coefficients (g=7)
    cof = [
        0.99999999999980993,
        676.5203681218851,
        -1259.1392167224028,
        771.32342877765313,
        -176.61502916214059,
        12.507343278686905,
        -0.13857109526572012,
        9.9843695780195716e-6,
        1.5056327351493116e-7,
    ]

    g = 7
    x -= 1.0
    a = cof[0]
    t = x + g + 0.5
    for i in range(1, len(cof)):
        a += cof[i] / (x + i)

    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


# ═══════════════════════════════════════════════════════════════
# SessionAuditor
# ═══════════════════════════════════════════════════════════════

class SessionAuditor:
    """Periodically analyzes game sessions for problems.

    Uses an LLM (lightweight model for cost efficiency) plus pure-python
    statistical tests to check:
      - Character validity (via anti_cheat.py)
      - Rule compliance (compare narrative against rule .md files)
      - Dice integrity (chi-squared test on d20 distributions)
      - World consistency (NPCs in wrong locations, impossible inventory, etc.)
    """

    def __init__(
        self,
        llm_client: Any,
        db_manager: Any,
        rule_engine: Any = None,
    ):
        """
        Args:
            llm_client: An OpenAIClient instance for the auditor model.
            db_manager: A DatabaseManager instance.
            rule_engine: Optional RuleEngine instance for rule compliance checks.
        """
        self.llm = llm_client
        self.db = db_manager
        self.rule_engine = rule_engine
        self._reports_dir = Path(AUDITOR_REPORTS_DIR)
        self._reports_dir.mkdir(parents=True, exist_ok=True)

    # ───────────────────────────────────────────────────────────────
    # Main audit entry points
    # ───────────────────────────────────────────────────────────────

    async def audit_session(
        self,
        session_id: str,
        full_history: bool = True,
    ) -> AuditReport:
        """Run a full audit on a single session.

        Args:
            session_id: The session to audit.
            full_history: If True, audit all history; if False, only recent turns.

        Returns:
            Complete AuditReport with all sub-results.
        """
        report = AuditReport(
            session_id=session_id,
            timestamp=datetime.utcnow().isoformat(),
        )

        try:
            report.character_issues = await self.audit_character_validity(session_id)
        except Exception as e:
            logger.error(f"[auditor] Character audit failed for {session_id}: {e}")
            report.character_issues.warnings.append(f"Character audit error: {e}")

        try:
            report.rule_violations = await self.audit_rule_compliance(
                session_id,
                recent_turns=AUDITOR_RULE_COMPLIANCE_TURNS if not full_history else 100,
            )
        except Exception as e:
            logger.error(f"[auditor] Rule audit failed for {session_id}: {e}")
            report.rule_violations.violations.append(f"Rule audit error: {e}")

        try:
            report.dice_anomalies = await self.audit_dice_integrity(
                session_id,
                recent_turns=AUDITOR_DICE_TURNS,
            )
        except Exception as e:
            logger.error(f"[auditor] Dice audit failed for {session_id}: {e}")
            report.dice_anomalies.suspicious_patterns.append(f"Dice audit error: {e}")

        try:
            report.world_inconsistencies = await self._audit_world_consistency(session_id)
        except Exception as e:
            logger.error(f"[auditor] World consistency audit failed for {session_id}: {e}")
            report.world_inconsistencies.append(f"World consistency error: {e}")

        # Generate recommendations
        report.recommendations = self._generate_recommendations(report)

        logger.info(f"[auditor] Audit complete for {session_id}: {report.summary()}")
        return report

    async def audit_recent_sessions(self, hours: int = 24) -> List[AuditReport]:
        """Audit all sessions that have been active in the last N hours.

        Args:
            hours: Look-back window for active sessions.

        Returns:
            List of AuditReport, one per active session.
        """
        reports: List[AuditReport] = []
        try:
            active = self.db.get_all_active_sessions()
        except Exception as e:
            logger.error(f"[auditor] Cannot get active sessions: {e}")
            return reports

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        for session in active:
            # Check if session was recently updated
            try:
                updated = datetime.fromisoformat(session.updated_at) if session.updated_at else None
            except (ValueError, TypeError):
                updated = None

            if updated and updated < cutoff:
                continue

            report = await self.audit_session(session.id, full_history=False)
            reports.append(report)

        logger.info(f"[auditor] Audited {len(reports)} sessions from last {hours}h")
        return reports

    # ───────────────────────────────────────────────────────────────
    # Character validity audit
    # ───────────────────────────────────────────────────────────────

    async def audit_character_validity(self, session_id: str) -> CharacterAuditResult:
        """Check all characters in a session for invalid races/classes/backgrounds.

        Uses the anti_cheat validator for SRD checks, plus an LLM pass
        for detecting suspicious backstory claims.
        """
        result = CharacterAuditResult()

        try:
            db = self.db.get_db(session_id)
            characters = db.get_session_characters(session_id)
        except Exception as e:
            result.warnings.append(f"Cannot fetch characters: {e}")
            return result

        result.characters_checked = len(characters)
        if not characters:
            return result

        # Import anti-cheat lazily to avoid circular imports
        try:
            from libs.ai.anti_cheat import AntiCheatValidator
            validator = AntiCheatValidator()
        except Exception as e:
            result.warnings.append(f"Cannot init anti-cheat: {e}")
            validator = None

        for char in characters:
            char_name = char.name

            # --- SRD validation (sync, no LLM) ---
            if validator:
                try:
                    from libs.character_parser import ParsedCharacter
                    # Build a minimal ParsedCharacter from the Character dataclass
                    parsed = ParsedCharacter(
                        name=char_name,
                        race=char.race,
                        class_name=char.class_name,
                        background="",  # not stored on Character model
                        level=char.level,
                        stats=json.loads(char.stats) if char.stats else {},
                        backstory=char.backstory or "",
                    )
                    val_result = validator.validate_character(parsed)
                    if not val_result.is_valid:
                        errors = [e.message for e in val_result.errors()]
                        result.invalid_characters.append(char_name)
                        result.details[char_name] = {
                            "srd_errors": errors,
                            "srd_warnings": [w.message for w in val_result.warnings_only()],
                        }
                    elif val_result.warnings_only():
                        result.warnings.append(
                            f"{char_name}: " + "; ".join(w.message for w in val_result.warnings_only())
                        )
                except Exception as e:
                    result.warnings.append(f"SRD check failed for {char_name}: {e}")

            # --- LLM-based backstory audit ---
            if char.backstory and len(char.backstory) > 50:
                try:
                    backstory_issues = await self._audit_backstory(char_name, char.backstory)
                    if backstory_issues:
                        if char_name not in result.invalid_characters:
                            result.invalid_characters.append(char_name)
                        result.details.setdefault(char_name, {})["backstory_issues"] = backstory_issues
                except Exception as e:
                    result.warnings.append(f"Backstory audit failed for {char_name}: {e}")

        return result

    # ───────────────────────────────────────────────────────────────
    # Rule compliance audit
    # ───────────────────────────────────────────────────────────────

    async def audit_rule_compliance(
        self,
        session_id: str,
        recent_turns: int = 20,
    ) -> RuleAuditResult:
        """Check if the Master allowed actions that violate rules.

        Compares recent narrative text against rule .md files using
        the rule_engine (if available) for semantic similarity search,
        plus an LLM pass for common rule violations.
        """
        result = RuleAuditResult()

        # Fetch recent history
        try:
            db = self.db.get_db(session_id)
            history = db.get_history(session_id, limit=recent_turns)
        except Exception as e:
            result.violations.append(f"Cannot fetch history: {e}")
            return result

        if not history:
            return result

        result.turns_checked = len(history)

        # Build a summary of recent narrative for LLM analysis
        narrative_text = ""
        for entry in history[-recent_turns:]:
            narrative_text += f"[{entry.author}] {entry.content}\n\n"

        if len(narrative_text.strip()) < 50:
            return result

        # Use rule_engine for embedding-based rule checks
        rule_violations_from_engine: List[str] = []
        if self.rule_engine:
            try:
                # Check each narrative entry for rule violations
                for entry in history[-recent_turns:]:
                    if entry.entry_type == "narrative" and entry.content:
                        check = self.rule_engine.validate_action(entry.content)
                        if not check.is_valid:
                            for v in check.violations:
                                rule_violations_from_engine.append(
                                    f"Turn {entry.id}: {v}"
                                )
            except Exception as e:
                logger.warning(f"[auditor] Rule engine check failed: {e}")

        # LLM-based rule compliance check
        llm_violations = await self._audit_rules_with_llm(narrative_text)

        # Combine results
        all_violations = rule_violations_from_engine + llm_violations
        result.violations = all_violations

        # Count severities
        for v in all_violations:
            v_lower = v.lower()
            if any(kw in v_lower for kw in ["critical", "impossible", "severe"]):
                result.severity_counts["high"] += 1
            elif any(kw in v_lower for kw in ["violation", "rule", "invalid"]):
                result.severity_counts["medium"] += 1
            else:
                result.severity_counts["low"] += 1

        return result

    # ───────────────────────────────────────────────────────────────
    # Dice integrity audit
    # ───────────────────────────────────────────────────────────────

    async def audit_dice_integrity(
        self,
        session_id: str,
        recent_turns: int = 50,
    ) -> DiceAuditResult:
        """Check dice roll distributions for statistical anomalies.

        Performs:
        1. Chi-squared goodness-of-fit test on d20 natural roll distributions
           per player — flags if p-value < threshold (too uniform OR too skewed).
        2. Runs test for randomness on roll sequences.
        3. Impossible sequence detection (too many 20s in a row, etc.).
        """
        result = DiceAuditResult()

        # Extract dice rolls from history
        try:
            db = self.db.get_db(session_id)
            history = db.get_history(session_id, limit=recent_turns)
        except Exception as e:
            result.suspicious_patterns.append(f"Cannot fetch history: {e}")
            return result

        if not history:
            return result

        # Parse dice rolls from narrative text
        # Format: "nat N" or "natural N" or "🎲 N" or "[ROLL: d20=N]"
        roll_pattern = re.compile(
            r'(?:nat|natural|🎲|\[ROLL:\s*d20=)\s*(\d{1,2})',
            re.IGNORECASE
        )

        # Collect rolls per player
        player_rolls: Dict[str, List[int]] = {}
        all_rolls: List[int] = []

        for entry in history:
            author = entry.author or "unknown"
            rolls_found = roll_pattern.findall(entry.content)
            for r in rolls_found:
                roll_val = int(r)
                if 1 <= roll_val <= 20:  # Valid d20 range
                    player_rolls.setdefault(author, []).append(roll_val)
                    all_rolls.append(roll_val)

        result.rolls_checked = len(all_rolls)
        if result.rolls_checked < 20:
            # Not enough data for statistical significance
            result.details["note"] = f"Only {result.rolls_checked} rolls found; need ≥20 for chi-squared"
            return result

        # --- Chi-squared test per player ---
        threshold = AUDITOR_DICE_ANOMALY_THRESHOLD
        for player, rolls in player_rolls.items():
            if len(rolls) < 20:
                continue

            # Build observed frequency distribution (bins 1-20)
            observed = [0] * 20
            for r in rolls:
                observed[r - 1] += 1

            # Expected: uniform distribution
            n = len(rolls)
            expected = [n / 20.0] * 20

            chi2, p_value = _chi_squared_gof(observed, expected)
            result.stat_significance[player] = p_value

            # Flag anomalies
            if p_value < threshold:
                if p_value < 0.001:
                    severity = "CRITICAL"
                elif p_value < 0.0001:
                    severity = "EXTREME"
                else:
                    severity = "SUSPICIOUS"

                # Determine what kind of anomaly
                mean_roll = sum(rolls) / len(rolls)
                expected_mean = 10.5
                if mean_roll > expected_mean + 1.5:
                    anomaly_type = "high-biased"
                elif mean_roll < expected_mean - 1.5:
                    anomaly_type = "low-biased"
                else:
                    anomaly_type = "non-uniform"

                result.suspicious_patterns.append(
                    f"{severity}: Player '{player}' has {anomaly_type} d20 distribution "
                    f"(n={len(rolls)}, χ²={chi2:.2f}, p={p_value:.6f}, "
                    f"mean={mean_roll:.2f}, expected_mean=10.5)"
                )

        # --- Impossible sequence detection ---
        for player, rolls in player_rolls.items():
            # Check for too many consecutive 20s (3+ is very suspicious)
            max_consec_20s = 0
            current_streak = 0
            for r in rolls:
                if r == 20:
                    current_streak += 1
                    max_consec_20s = max(max_consec_20s, current_streak)
                else:
                    current_streak = 0

            if max_consec_20s >= 3:
                prob = (1/20) ** max_consec_20s
                result.suspicious_patterns.append(
                    f"IMPROBABLE: Player '{player}' rolled {max_consec_20s} consecutive natural 20s "
                    f"(probability = {prob:.2e})"
                )

            # Check for too many 20s overall (>15% of all rolls)
            count_20s = rolls.count(20)
            expected_20s = len(rolls) / 20
            if count_20s > expected_20s * 3 and len(rolls) >= 30:
                result.suspicious_patterns.append(
                    f"HIGH_CRIT_RATE: Player '{player}' has {count_20s}/{len(rolls)} "
                    f"natural 20s ({count_20s/len(rolls)*100:.1f}%, expected 5%)"
                )

            # Runs test for randomness
            if len(rolls) >= 30:
                runs_p = self._runs_test(rolls)
                if runs_p < threshold:
                    result.suspicious_patterns.append(
                        f"NON_RANDOM: Player '{player}' fails runs test "
                        f"(p={runs_p:.6f}) — sequence may be non-random"
                    )

        # Overall session-level chi-squared
        if len(all_rolls) >= 40:
            observed_all = [0] * 20
            for r in all_rolls:
                observed_all[r - 1] += 1
            n_all = len(all_rolls)
            expected_all = [n_all / 20.0] * 20
            chi2_all, p_all = _chi_squared_gof(observed_all, expected_all)
            result.stat_significance["_session_overall"] = p_all
            if p_all < threshold:
                result.suspicious_patterns.append(
                    f"SESSION_ANOMALY: Overall d20 distribution is non-uniform "
                    f"(χ²={chi2_all:.2f}, p={p_all:.6f})"
                )

        return result

    @staticmethod
    def _runs_test(values: List[int]) -> float:
        """Wald-Wolfowitz runs test for randomness.

        Returns the p-value (two-tailed) for the hypothesis that
        the sequence is random.  Low p-value → non-random.
        """
        n = len(values)
        if n < 2:
            return 1.0

        median = sorted(values)[n // 2]

        # Count runs (a run is a sequence of values above/below median)
        runs = 1
        for i in range(1, n):
            above_i = values[i] > median
            above_prev = values[i - 1] > median
            if above_i != above_prev:
                runs += 1

        # Count n1 (above median) and n2 (below/at median)
        n1 = sum(1 for v in values if v > median)
        n2 = n - n1

        if n1 == 0 or n2 == 0:
            return 1.0  # All same side, not enough variation

        # Expected runs and variance
        expected_runs = (2 * n1 * n2) / n + 1
        var_runs = (2 * n1 * n2 * (2 * n1 * n2 - n)) / (n * n * (n - 1))

        if var_runs <= 0:
            return 1.0

        z = (runs - expected_runs) / math.sqrt(var_runs)

        # Two-tailed p-value from normal distribution
        p = 2 * (1 - _normal_cdf(abs(z)))
        return p

    # ───────────────────────────────────────────────────────────────
    # World consistency audit
    # ───────────────────────────────────────────────────────────────

    async def _audit_world_consistency(self, session_id: str) -> List[str]:
        """Check for world inconsistencies using LLM analysis.

        Detects:
        - NPCs in wrong locations (e.g., merchant in dungeon)
        - Impossible inventory (e.g., commoner with +3 sword)
        - Timeline contradictions
        - State inconsistencies (dead NPC still acting)
        """
        inconsistencies: List[str] = []

        try:
            db = self.db.get_db(session_id)

            # Gather world state
            npcs = db.get_session_npcs(session_id) if hasattr(db, 'get_session_npcs') else []
            locations = db.get_session_locations(session_id) if hasattr(db, 'get_session_locations') else []
            characters = db.get_session_characters(session_id)

        except Exception as e:
            inconsistencies.append(f"Cannot fetch world state: {e}")
            return inconsistencies

        # --- NPC-Location consistency (rule-based, no LLM) ---
        # Check merchants are in appropriate locations
        merchant_types = {"merchant", "trader", "shopkeeper", "vendor"}
        inappropriate_locations = {"dungeon", "wilderness", "cave", "ruins", "road"}

        for npc in npcs:
            try:
                npc_data = npc  # WorldNpc dataclass
                occupation = (getattr(npc_data, 'occupation', '') or '').lower()
                location_id = getattr(npc_data, 'location_id', '') or ''

                if occupation in merchant_types and location_id:
                    # Check if location is inappropriate
                    for loc in locations:
                        if loc.id == location_id:
                            loc_type = (getattr(loc, 'type', '') or '').lower()
                            if loc_type in inappropriate_locations:
                                inconsistencies.append(
                                    f"NPC '{npc_data.name}' (occupation: {occupation}) "
                                    f"is in inappropriate location '{loc.name}' (type: {loc_type})"
                                )
            except Exception:
                continue

        # --- Dead NPC still acting ---
        try:
            history = db.get_history(session_id, limit=20)
            dead_npc_names = set()
            for npc in npcs:
                if hasattr(npc, 'is_alive') and not npc.is_alive:
                    dead_npc_names.add(npc.name.lower())

            # Check if dead NPCs are mentioned as acting in recent narrative
            for entry in history[-20:]:
                content_lower = entry.content.lower()
                for dead_name in dead_npc_names:
                    if dead_name in content_lower:
                        # Heuristic: if the narrative mentions the dead NPC doing something
                        action_words = ["атакует", "говорит", "бросает", "кастует",
                                       "attacks", "says", "casts", "moves"]
                        if any(aw in content_lower for aw in action_words):
                            inconsistencies.append(
                                f"Dead NPC '{dead_name}' appears to be acting in "
                                f"turn {entry.id}: '{entry.content[:100]}...'"
                            )
        except Exception:
            pass

        # --- LLM-based consistency check for larger issues ---
        if inconsistencies:
            # If we already found rule-based issues, also do an LLM pass
            try:
                llm_issues = await self._audit_world_with_llm(session_id, db)
                inconsistencies.extend(llm_issues)
            except Exception as e:
                logger.warning(f"[auditor] LLM world audit failed: {e}")

        return inconsistencies

    # ───────────────────────────────────────────────────────────────
    # Report generation
    # ───────────────────────────────────────────────────────────────

    async def generate_audit_report(self, session_id: str) -> str:
        """Generate a full markdown audit report for a session.

        Returns the markdown string.
        """
        report = await self.audit_session(session_id)
        md = self._report_to_markdown(report)
        return md

    async def write_audit_to_md(self, session_id: str, report_path: Optional[str] = None) -> str:
        """Write an audit report to a .md file.

        Args:
            session_id: The session to audit.
            report_path: Optional path; defaults to data/audits/<session_id>_<timestamp>.md

        Returns:
            The path the report was written to.
        """
        md = await self.generate_audit_report(session_id)

        if report_path is None:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            report_path = str(self._reports_dir / f"{session_id}_{ts}.md")

        # Ensure directory exists
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(md)

        logger.info(f"[auditor] Report written to {report_path}")
        return report_path

    # ───────────────────────────────────────────────────────────────
    # Internal helpers
    # ───────────────────────────────────────────────────────────────

    def _report_to_markdown(self, report: AuditReport) -> str:
        """Convert an AuditReport to markdown."""
        lines: List[str] = []

        lines.append(f"# Audit Report: Session `{report.session_id}`")
        lines.append(f"**Timestamp**: {report.timestamp}")
        lines.append("")

        # Character issues
        lines.append("## 1. Character Validity")
        ci = report.character_issues
        lines.append(f"- Characters checked: {ci.characters_checked}")
        lines.append(f"- Invalid characters: {len(ci.invalid_characters)}")
        if ci.invalid_characters:
            for name in ci.invalid_characters:
                lines.append(f"  - ❌ **{name}**")
                details = ci.details.get(name, {})
                if details:
                    for key, vals in details.items():
                        if isinstance(vals, list):
                            for v in vals:
                                lines.append(f"    - {key}: {v}")
                        else:
                            lines.append(f"    - {key}: {vals}")
        if ci.warnings:
            lines.append("- Warnings:")
            for w in ci.warnings:
                lines.append(f"  - ⚠️ {w}")
        lines.append("")

        # Rule violations
        lines.append("## 2. Rule Compliance")
        rv = report.rule_violations
        lines.append(f"- Turns checked: {rv.turns_checked}")
        lines.append(f"- Violations found: {len(rv.violations)}")
        lines.append(f"  - High: {rv.severity_counts.get('high', 0)}")
        lines.append(f"  - Medium: {rv.severity_counts.get('medium', 0)}")
        lines.append(f"  - Low: {rv.severity_counts.get('low', 0)}")
        if rv.violations:
            for v in rv.violations:
                lines.append(f"  - 🚫 {v}")
        lines.append("")

        # Dice anomalies
        lines.append("## 3. Dice Integrity")
        da = report.dice_anomalies
        lines.append(f"- Rolls checked: {da.rolls_checked}")
        lines.append(f"- Suspicious patterns: {len(da.suspicious_patterns)}")
        if da.stat_significance:
            lines.append("- Statistical significance (p-values):")
            for player, pval in da.stat_significance.items():
                flag = " ⚠️" if pval < AUDITOR_DICE_ANOMALY_THRESHOLD else ""
                lines.append(f"  - {player}: p={pval:.6f}{flag}")
        if da.suspicious_patterns:
            for p in da.suspicious_patterns:
                lines.append(f"  - 🎲 {p}")
        lines.append("")

        # World inconsistencies
        lines.append("## 4. World Consistency")
        lines.append(f"- Inconsistencies found: {len(report.world_inconsistencies)}")
        if report.world_inconsistencies:
            for w in report.world_inconsistencies:
                lines.append(f"  - 🌍 {w}")
        lines.append("")

        # Recommendations
        lines.append("## 5. Recommendations")
        if report.recommendations:
            for r in report.recommendations:
                lines.append(f"- {r}")
        else:
            lines.append("- ✅ No issues found. Session looks healthy.")

        return "\n".join(lines)

    def _generate_recommendations(self, report: AuditReport) -> List[str]:
        """Generate actionable recommendations based on audit findings."""
        recs: List[str] = []

        # Character recommendations
        if report.character_issues.invalid_characters:
            recs.append(
                f"Review {len(report.character_issues.invalid_characters)} character(s) with "
                f"invalid SRD data: {', '.join(report.character_issues.invalid_characters)}"
            )

        # Rule recommendations
        high = report.rule_violations.severity_counts.get("high", 0)
        if high > 0:
            recs.append(
                f"Address {high} high-severity rule violation(s) — "
                f"these may indicate the Master is ignoring core mechanics"
            )
        medium = report.rule_violations.severity_counts.get("medium", 0)
        if medium > 3:
            recs.append(
                f"Consider reviewing the Master's prompt — "
                f"{medium} medium-severity rule violations suggest systematic issues"
            )

        # Dice recommendations
        if report.dice_anomalies.suspicious_patterns:
            suspicious_players = set()
            for p in report.dice_anomalies.suspicious_patterns:
                # Extract player name from pattern string
                match = re.search(r"Player '([^']+)'", p)
                if match:
                    suspicious_players.add(match.group(1))

            if suspicious_players:
                recs.append(
                    f"Investigate dice roll patterns for player(s): "
                    f"{', '.join(suspicious_players)} — "
                    f"chi-squared test indicates non-uniform distributions"
                )

        # World consistency recommendations
        if report.world_inconsistencies:
            recs.append(
                f"Review {len(report.world_inconsistencies)} world inconsistency(ies) — "
                f"these may break immersion or indicate DB corruption"
            )

        return recs

    # ───────────────────────────────────────────────────────────────
    # LLM-based audit helpers
    # ───────────────────────────────────────────────────────────────

    async def _audit_backstory(self, char_name: str, backstory: str) -> List[str]:
        """Use LLM to check for suspicious backstory claims."""
        prompt = f"""You are a D&D character auditor. Check this character's backstory for impossible or
exploitative claims that a simple SRD check might miss. Look for:

1. Claims of immunity to common damage types without justification
2. Claims of permanent flight at level 1 without racial ability
3. Claims of spellcasting ability that doesn't match their class
4. Claims of ownership of powerful magic items at low level
5. Claims of abilities that replicate high-level spells

Character: {char_name}
Backstory: {backstory[:1000]}

Respond in JSON format:
{{"issues": ["issue1", "issue2"], "severity": "none|low|medium|high"}}

If no issues, respond: {{"issues": [], "severity": "none"}}"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self.llm.chat(messages)
            content = response["choices"][0]["message"].get("content", "")

            # Try to parse JSON from the response
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result.get("issues", [])
        except Exception as e:
            logger.warning(f"[auditor] Backstory LLM check failed: {e}")

        return []

    async def _audit_rules_with_llm(self, narrative_text: str) -> List[str]:
        """Use LLM to check for rule violations in narrative text."""
        # Limit text to avoid token overflow
        text_sample = narrative_text[-3000:]

        prompt = f"""You are a D&D 5e rule auditor. Review this recent game narrative and identify
any rule violations the DM may have allowed. Look for:

1. Characters taking more than one action per turn (without Haste/Action Surge)
2. Spells cast without required components or spell slots
3. Attacks at impossible range
4. Characters moving more than their speed allows
5. Saving throws or checks that should have been called but weren't
6. Concentration broken when it shouldn't have been (or not broken when it should)
7. Death saves not handled correctly
8. Critical hit damage calculated incorrectly

Recent narrative (last few turns):
---
{text_sample}
---

List ONLY clear, mechanical rule violations — not judgment calls or house rules.
Respond in JSON format:
{{"violations": ["violation1", "violation2"]}}

If no violations found: {{"violations": []}}"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self.llm.chat(messages)
            content = response["choices"][0]["message"].get("content", "")

            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result.get("violations", [])
        except Exception as e:
            logger.warning(f"[auditor] Rule compliance LLM check failed: {e}")

        return []

    async def _audit_world_with_llm(self, session_id: str, db: Any) -> List[str]:
        """Use LLM to check for world inconsistencies beyond rule-based checks."""
        # Build a summary of the current world state
        world_summary_parts: List[str] = []

        try:
            # Get recent history for context
            history = db.get_history(session_id, limit=10)
            for entry in history:
                world_summary_parts.append(f"[{entry.author}] {entry.content[:200]}")
        except Exception:
            pass

        if not world_summary_parts:
            return []

        world_text = "\n".join(world_summary_parts[-5:])  # Last 5 entries

        prompt = f"""You are a D&D world consistency auditor. Review this recent game state and
identify any inconsistencies such as:

1. Characters in two places at once
2. Items used that were never acquired
3. NPC behavior contradicting their established personality
4. Timeline contradictions (day/night, travel time)
5. Dead characters mentioned as alive (or vice versa)
6. Currency spent that exceeds what the character had

Recent game state:
---
{world_text}
---

List ONLY clear inconsistencies, not assumptions.
Respond in JSON format:
{{"inconsistencies": ["issue1", "issue2"]}}

If none found: {{"inconsistencies": []}}"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self.llm.chat(messages)
            content = response["choices"][0]["message"].get("content", "")

            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result.get("inconsistencies", [])
        except Exception as e:
            logger.warning(f"[auditor] World consistency LLM check failed: {e}")

        return []


# ═══════════════════════════════════════════════════════════════
# Standalone helper: normal CDF (for runs test p-value)
# ═══════════════════════════════════════════════════════════════

def _normal_cdf(x: float) -> float:
    """Standard normal CDF using the Abramowitz & Stegun approximation."""
    # Constants
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)

    return 0.5 * (1.0 + sign * y)
