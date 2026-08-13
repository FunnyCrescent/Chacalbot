"""RuleValidatorMixin — integration mixin for rule-based action validation.

Provides a convenient interface for the Master engine and DB-Bot to validate
game actions against the loaded rules before allowing them.

This mixin is designed to be mixed into the DMEngine class (or any class
that has an `embedding_client` attribute). The RuleEngine is lazily
initialized on first use.

Usage::

    class DMEngine(RuleValidatorMixin, MemoryEngineMixin, ...):
        ...

    engine = DMEngine(...)
    result = await engine.check_rules_for_action(
        "spawn merchant at dungeon",
        context="The party found a trapped merchant in the dungeon."
    )
    if not result.is_valid:
        # Reject or flag the action
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any

from .rule_engine import RuleEngine, RuleCheckResult, RuleMatch

logger = logging.getLogger(__name__)


class RuleValidatorMixin:
    """Mixin that adds rule validation capabilities to an engine class.

    Expected attributes on the host class:
        - embedding_client: OpenAIClient (for embedding API calls).
        - Optionally: _rule_engine (pre-initialized RuleEngine instance).

    The RuleEngine is lazily created on first use and cached.
    """

    # Class-level shared engine (initialized once, reused across instances)
    _shared_rule_engine: Optional[RuleEngine] = None
    _rule_engine_initialized: bool = False

    def _get_rule_engine(self) -> RuleEngine:
        """Get or create the RuleEngine instance.

        Returns a RuleEngine configured with the embedding client
        from this engine instance and the default rules directory.
        """
        if RuleValidatorMixin._shared_rule_engine is not None:
            return RuleValidatorMixin._shared_rule_engine

        # Get the embedding client from the host class
        embedding_client = getattr(self, 'embedding_client', None)

        # Determine rules directory
        from pathlib import Path
        rules_dir = Path(__file__).resolve().parent.parent / "rules"

        engine = RuleEngine(
            embedding_client=embedding_client,
            rules_dir=str(rules_dir),
        )
        RuleValidatorMixin._shared_rule_engine = engine
        return engine

    async def _ensure_rule_engine_initialized(self) -> RuleEngine:
        """Ensure the RuleEngine is initialized (loaded + embeddings computed)."""
        engine = self._get_rule_engine()
        if not RuleValidatorMixin._rule_engine_initialized:
            try:
                await engine.initialize()
                RuleValidatorMixin._rule_engine_initialized = True
            except Exception as e:
                logger.error(f"[RuleValidator] Failed to initialize RuleEngine: {e}")
                # Engine will work in keyword-only mode if embeddings fail
                RuleValidatorMixin._rule_engine_initialized = True
        return engine

    # ─── Public API ────────────────────────────────────────────

    async def check_rules_for_action(
        self,
        action: str,
        context: str = "",
    ) -> RuleCheckResult:
        """Check if an action is valid according to the loaded rules.

        This is the main entry point for rule validation. It:
        1. Initializes the RuleEngine if needed.
        2. Validates the action against structured rules (NPC spawn binding, etc.).
        3. Searches for semantically relevant rules.
        4. Returns a RuleCheckResult with violations and suggestions.

        Args:
            action: Description of the action to validate (e.g., "spawn merchant at dungeon").
            context: Additional context about the action (e.g., "The party freed the merchant").

        Returns:
            RuleCheckResult with is_valid, violations, suggestions, and relevant_rules.
        """
        engine = await self._ensure_rule_engine_initialized()

        # Combine action and context for the query
        full_query = f"{action} {context}".strip() if context else action

        try:
            result = await engine.validate_action(full_query)
        except Exception as e:
            logger.error(f"[RuleValidator] validate_action error: {e}")
            # On error, return a permissive result (don't block gameplay)
            result = RuleCheckResult(
                is_valid=True,
                violations=[],
                suggestions=[f"Rule validation error: {e}. Action allowed by default."],
                relevant_rules=[],
                query=action,
                confidence=0.0,
            )

        # Log the result
        if not result.is_valid:
            logger.warning(
                f"[RuleValidator] Action VIOLATED rules: '{action[:100]}' — "
                f"violations: {result.violations[:2]}"
            )
        else:
            logger.info(
                f"[RuleValidator] Action OK: '{action[:100]}' — "
                f"confidence: {result.confidence:.2f}"
            )

        return result

    async def find_relevant_rules(
        self,
        query: str,
        top_k: int = 3,
    ) -> List[RuleMatch]:
        """Find rules relevant to a query.

        Useful for providing rule context to the Master engine before
        making narrative decisions.

        Args:
            query: The search query (e.g., "how does concentration work").
            top_k: Number of top results to return.

        Returns:
            List of RuleMatch objects sorted by similarity.
        """
        engine = await self._ensure_rule_engine_initialized()
        try:
            return await engine.find_relevant_rules(query, top_k=top_k)
        except Exception as e:
            logger.error(f"[RuleValidator] find_relevant_rules error: {e}")
            return []

    async def get_rule_context_for_prompt(
        self,
        action: str,
        max_rules: int = 3,
        max_chars: int = 2000,
    ) -> str:
        """Get formatted rule context for injection into a prompt.

        This produces a text block that can be appended to the Master's
        system prompt or context to remind it of relevant rules.

        Args:
            action: The action to find rules for.
            max_rules: Maximum number of rules to include.
            max_chars: Maximum total character length of the output.

        Returns:
            Formatted text block with relevant rules, or empty string.
        """
        matches = await self.find_relevant_rules(action, top_k=max_rules)
        if not matches:
            return ""

        lines = ["## Relevant Rules (from rule engine)"]
        total_chars = len(lines[0])

        for match in matches:
            rule_text = f"### {match.section_title} (similarity: {match.similarity_score:.2f})\n{match.text}"
            if total_chars + len(rule_text) > max_chars:
                # Truncate this rule
                remaining = max_chars - total_chars - 50
                if remaining > 100:
                    rule_text = rule_text[:remaining] + "\n[...truncated]"
                else:
                    break

            lines.append(rule_text)
            total_chars += len(rule_text) + 1

        return "\n\n".join(lines)

    async def validate_npc_spawn(
        self,
        npc_type: str,
        location_type: str,
        context: str = "",
    ) -> RuleCheckResult:
        """Validate an NPC spawn against location-binding rules.

        Convenience method that formats the action description and
        delegates to check_rules_for_action.

        Args:
            npc_type: Type of NPC (e.g., "merchant", "guard", "innkeeper").
            location_type: Type of location (e.g., "market", "dungeon", "road").
            context: Additional context.

        Returns:
            RuleCheckResult with validation outcome.
        """
        action = f"spawn {npc_type} at {location_type}"
        return await self.check_rules_for_action(action, context)

    # ─── Rule Management ───────────────────────────────────────

    async def reload_rules(self) -> Dict[str, Any]:
        """Hot-reload rules from .md files.

        Useful for updating rules without restarting the bot.
        """
        engine = await self._ensure_rule_engine_initialized()
        await engine.reload_rules()
        return engine.stats()

    def list_available_rules(self) -> List[Dict[str, Any]]:
        """List all available rules with metadata.

        Returns:
            List of dicts with rule_id, file_name, section_title, etc.
        """
        if RuleValidatorMixin._shared_rule_engine is None:
            return []
        return RuleValidatorMixin._shared_rule_engine.list_rules()

    @classmethod
    def reset_rule_engine(cls) -> None:
        """Reset the shared rule engine (for testing or reconfiguration)."""
        cls._shared_rule_engine = None
        cls._rule_engine_initialized = False
