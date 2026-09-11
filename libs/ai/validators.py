"""ValidatorsMixin — thin back-compat wrapper over CharacterValidationService.

REFACTOR (code-review recommendation): the domain logic (LLM math validation,
verdict parsing, SRD checks, combined verdict) now lives in
``libs/services/character_validation.py`` as a service class with EXPLICIT
dependencies (db_bot, anti_cheat). This mixin only adapts the old
``dm_engine.validate_*`` call surface to the new service, so every existing
caller (libs/handlers/character_cmds.py, libs/ai/engine.py) keeps working
unchanged.

NOTE: the service import is LAZY (inside methods) — ``libs/ai/__init__.py``
imports engine → validators at package init, and the service package must
stay importable WITHOUT dragging in the whole engine stack (no cycles,
no Telegram deps at import time).
"""
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from libs.character_parser import ParsedCharacter
from libs.ai.anti_cheat import AntiCheatValidator, CharacterValidationResult

if TYPE_CHECKING:  # pragma: no cover
    from libs.services.character_validation import CharacterValidationService

logger = logging.getLogger(__name__)


class ValidatorsMixin:
    """Back-compat adapter: delegates all validation to CharacterValidationService."""

    # Lazy-initialised anti-cheat validator (shared across all instances)
    _anti_cheat: Optional[AntiCheatValidator] = None

    @classmethod
    def _get_anti_cheat(cls) -> AntiCheatValidator:
        """Return a shared AntiCheatValidator instance (created once)."""
        if cls._anti_cheat is None:
            cls._anti_cheat = AntiCheatValidator()
        return cls._anti_cheat

    def _validation_service(self) -> "CharacterValidationService":
        """Plain service: db_bot + anti_cheat, NO routing overrides.

        Used by the direct delegating methods (validate_character_sheet /
        validate_character_srd) — calling their own impls without looping."""
        from libs.services.character_validation import CharacterValidationService
        return CharacterValidationService(
            db_bot=getattr(self, "db_bot", None),
            anti_cheat=self._get_anti_cheat(),
        )

    # ───────────────────────────────────────────────────────────────
    # Delegating methods (signatures unchanged)
    # ───────────────────────────────────────────────────────────────

    async def validate_character_sheet(self, sheet_text: str) -> Dict[str, str]:
        """Validate character sheet (arithmetic/mechanics ONLY) — via service."""
        return await self._validation_service().validate_sheet_math(sheet_text)

    @staticmethod
    def _parse_verdict(content: str) -> str:
        """Strict verdict parsing — see CharacterValidationService.parse_verdict."""
        from libs.services.character_validation import CharacterValidationService
        return CharacterValidationService.parse_verdict(content)

    def validate_character_srd(self, character: ParsedCharacter) -> CharacterValidationResult:
        """SRD anti-cheat validation of a parsed character — via service."""
        return self._validation_service().validate_srd(character)

    def validate_character_srd_text(self, sheet_text: str) -> Dict[str, Any]:
        """Quick text-based SRD validation (no LLM) — via service."""
        return self._validation_service().validate_srd_text(sheet_text)

    async def validate_character_full(
        self,
        sheet_text: str,
        character: Optional[ParsedCharacter] = None,
    ) -> Dict[str, Any]:
        """Full validation: math (LLM) + SRD — via service.

        Builds an override-aware service so the combined check routes through
        THIS engine's (overridable) methods — subclass overrides and test
        patches of validate_character_sheet keep their effect."""
        from libs.services.character_validation import CharacterValidationService
        service = CharacterValidationService(
            db_bot=getattr(self, "db_bot", None),
            anti_cheat=self._get_anti_cheat(),
            math_validator=self.validate_character_sheet,
            srd_validator=self.validate_character_srd,
        )
        return await service.validate_full(sheet_text, character)
