"""Unit tests for libs/services/character_validation.py — the SERVICE itself.

This is the first "mixins → services" extraction (code-review recommendation).
These tests prove the service works STANDALONE with explicit dependencies
(no DMEngine, no .env) — the mixin is just an adapter on top of it.
"""
import json

import pytest

from libs.services.character_validation import CharacterValidationService
from tests.conftest import FakeDBBot


SHEET = "Имя: Анджей\nРаса: Эльф\nКласс: Следопыт\nHP 12 AC 14 STR 10 DEX 16"


class TestStandaloneService:

    def test_default_anti_cheat_is_created(self):
        svc = CharacterValidationService(db_bot=FakeDBBot())
        assert svc.anti_cheat is not None

    @pytest.mark.parametrize("sheet", ["", "   ", "абвг", "..."])
    async def test_garbage_rejected_without_llm(self, sheet):
        bot = FakeDBBot(strict=True)          # LLM call → test failure
        svc = CharacterValidationService(db_bot=bot)
        out = await svc.validate_sheet_math(sheet)
        assert out["verdict"] == "REJECT"

    async def test_llm_verdict_flows(self):
        svc = CharacterValidationService(
            db_bot=FakeDBBot(content="**Вердикт**: NEEDS_FIX"))
        out = await svc.validate_sheet_math(SHEET)
        assert out["verdict"] == "NEEDS_FIX"

    async def test_llm_crash_is_error(self):
        class Boom:
            async def chat(self, *a, **k):
                raise TimeoutError("no network")
        svc = CharacterValidationService(db_bot=Boom())
        out = await svc.validate_sheet_math(SHEET)
        assert out["verdict"] == "ERROR"


class TestOverrideRouting:
    """The mixin injects its overridable methods as math/srd validators —
    validate_full must route through them (and only them, no recursion)."""

    async def test_math_override_is_used_by_validate_full(self):
        async def fake_math(text):
            return {"verdict": "VALID", "details": "patched"}
        svc = CharacterValidationService(
            db_bot=FakeDBBot(strict=True),    # impl must never run
            math_validator=fake_math,
        )
        out = await svc.validate_full(SHEET)
        assert out["math_verdict"] == "VALID"
        assert out["overall_verdict"] == "VALID"

    async def test_no_recursion_via_override(self):
        """mixin-style override that itself delegates to a PLAIN service —
        the original recursion bug pattern."""
        async def delegating_math(text):
            plain = CharacterValidationService(db_bot=FakeDBBot(
                content="**Вердикт**: VALID"))
            return await plain.validate_sheet_math(text)
        svc = CharacterValidationService(
            db_bot=FakeDBBot(strict=True),
            math_validator=delegating_math,
        )
        out = await svc.validate_full(SHEET)
        assert out["math_verdict"] == "VALID"

    async def test_srd_override_is_used_by_validate_full(self):
        from libs.ai.anti_cheat import CharacterValidationResult, ValidationResult

        def fake_srd(character):
            res = CharacterValidationResult(
                is_valid=False,
                race_result=ValidationResult(
                    is_valid=False, severity="error",
                    message="homebrew race",
                ),
            )
            return res

        async def fake_math(text):                # must be async — service awaits it
            return {"verdict": "VALID", "details": "ok"}

        svc = CharacterValidationService(
            db_bot=FakeDBBot(strict=True),
            math_validator=fake_math,
            srd_validator=fake_srd,
        )
        out = await svc.validate_full(SHEET, character=object())
        assert out["overall_verdict"] == "NEEDS_FIX"
        assert out["srd_result"]["race"]["is_valid"] is False


def _valid_math():
    return {"verdict": "VALID", "details": "ok"}


class TestParseVerdictParity:
    """Service static method must behave exactly like the mixin's."""

    @pytest.mark.parametrize("content,expected", [
        ("**Вердикт**: NEEDS_FIX", "NEEDS_FIX"),
        ("**Вердикт**: VALID", "VALID"),
        ("INVALID", "NEEDS_FIX"),
        ("INVALIDATED", "UNKNOWN"),
        ("", "UNKNOWN"),
    ])
    def test_parity(self, content, expected):
        assert CharacterValidationService.parse_verdict(content) == expected
