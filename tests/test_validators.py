"""Unit tests for libs/ai/validators.py (ValidatorsMixin).

Focus:
1. _parse_verdict — STRICT parsing. The original "Эйра «Чертила»" bug:
   an INVALID reply was classified as VALID because "VALID" is a substring
   of "INVALID". Word boundaries + NEEDS_FIX-first + UNKNOWN fallback.
2. validate_character_sheet — empty / garbage sheets ("абвг") must be
   REJECTed WITHOUT an LLM call (token-saver + the Bug 1 fix).
3. validate_character_full — combination logic of math + SRD verdicts.
"""
import pytest

from libs.ai.validators import ValidatorsMixin
from tests.conftest import FakeDBBot


class V(ValidatorsMixin):
    """Minimal host for the mixin — no engine init, no network."""
    def __init__(self, db_bot):
        self.db_bot = db_bot


# ═══════════════════════════════════════════════════════════════
# 1. _parse_verdict
# ═══════════════════════════════════════════════════════════════

class TestParseVerdict:

    @pytest.mark.parametrize("content,expected", [
        # canonical formats the DB-Bot is instructed to emit
        ("**Вердикт**: NEEDS_FIX", "NEEDS_FIX"),
        ("**Вердикт**: VALID", "VALID"),
        ("Вердикт: REJECT", "REJECT"),
        ("**Вердикт**: REJECTED", "REJECT"),
        # case-insensitivity
        ("verdict: needs_fix", "NEEDS_FIX"),
        ("вердикт: valid", "VALID"),
        ("Valid", "VALID"),
        # THE ORIGINAL BUG: "VALID" inside "INVALID"
        ("Вердикт: INVALID — числа не сходятся", "NEEDS_FIX"),
        ("INVALID", "NEEDS_FIX"),
        ("**INVALID**", "NEEDS_FIX"),
        # NEEDS_FIX must win even if the reply also mentions VALID
        ("Не VALID, а NEEDS_FIX: HP отрицательный", "NEEDS_FIX"),
        # REJECT beats VALID too (checked before it)
        ("REJECT, nothing VALID here", "REJECT"),
    ])
    def test_known_verdicts(self, content, expected):
        assert ValidatorsMixin._parse_verdict(content) == expected

    @pytest.mark.parametrize("content", [
        "INVALIDATED",        # contains "VALID", but \bVALID\b fails
        "UNVALID",
        "",
        "   ",
        "Не удалось получить ответ модели",
        "Вердикт: ВАЛИДНО",   # russian token — prompt demands english
        "qwerty",
    ])
    def test_unparseable_is_unknown(self, content):
        """Anything unparseable → UNKNOWN; callers must treat it as NOT passed."""
        assert ValidatorsMixin._parse_verdict(content) == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════
# 2. validate_character_sheet — empty/garbage guard
# ═══════════════════════════════════════════════════════════════

class TestEmptySheetGuard:
    """Bug 1 fix: empty or garbage files are rejected BEFORE the LLM call.

    "А если в файле написано абвг, а не лист персонажа?" — Нет: 4 word
    characters < 10 → REJECT, ноль токенов, ни один персонаж не сохранён."""

    @pytest.mark.parametrize("sheet", [
        "",                          # totally empty
        "   \n\t  \n",               # whitespace only
        "абвг",                      # THE user's question: 4 cyrillic chars
        "123456789",                 # 9 digits — just under the threshold
        "!!!!! ,,, ;;; !!!",         # punctuation only
        "...",                       # the original bug input (an empty file)
    ])
    async def test_garbage_rejected_without_llm_call(self, sheet):
        v = V(FakeDBBot(strict=True))          # any LLM call fails the test
        result = await v.validate_character_sheet(sheet)
        assert result["verdict"] == "REJECT"
        assert "пустой" in result["details"].lower()

    @pytest.mark.parametrize("sheet", [
        "абвгдееёжзийклмнопрст",     # 20 cyrillic chars — passes the length gate
        "1234567890",                # exactly 10 digits
    ])
    async def test_len10_reaches_llm(self, sheet):
        """10+ word chars → the guard passes and the LLM is consulted
        (its verdict decides). Proves the threshold is >= 10, not > 10."""
        bot = FakeDBBot(content="**Вердикт**: NEEDS_FIX\n**Ошибки**: не лист")
        v = V(bot)
        result = await v.validate_character_sheet(sheet)
        assert len(bot.calls) == 1
        assert result["verdict"] == "NEEDS_FIX"

    async def test_llm_verdicts_flow_through(self):
        cases = [
            ("**Вердикт**: VALID\n**Ошибки**: нет", "VALID"),
            ("**Вердикт**: INVALID — STR 300", "NEEDS_FIX"),
            ("Вердикт: REJECT", "REJECT"),
            ("модель ответила бессмыслицей", "UNKNOWN"),
        ]
        for content, expected in cases:
            v = V(FakeDBBot(content=content))
            sheet = "Имя: Анджей\nРаса: Эльф\nКласс: Следопыт\nHP 12 AC 14"
            result = await v.validate_character_sheet(sheet)
            assert result["verdict"] == expected

    async def test_llm_exception_is_error_not_pass(self):
        class BoomBot:
            async def chat(self, *a, **k):
                raise ConnectionError("timeout")
        v = V(BoomBot())
        sheet = "Имя: Анджей\nРаса: Эльф\nКласс: Следопыт\nHP 12 AC 14"
        result = await v.validate_character_sheet(sheet)
        assert result["verdict"] == "ERROR"
        # callers must stop on ERROR — asserted here via the dict contract
        assert result["verdict"] != "VALID"


# ═══════════════════════════════════════════════════════════════
# 3. validate_character_full — verdict combination
# ═══════════════════════════════════════════════════════════════

class TestValidateCharacterFull:

    def _mk(self, math_verdict):
        v = V(FakeDBBot())
        async def fake_math(sheet_text):
            return {"verdict": math_verdict, "details": "x"}
        v.validate_character_sheet = fake_math
        return v

    async def test_math_valid_and_clean_srd_is_valid(self):
        v = self._mk("VALID")
        out = await v.validate_character_full("лист", character=None)
        assert out["overall_verdict"] == "VALID"

    async def test_math_needs_fix_fails_overall(self):
        v = self._mk("NEEDS_FIX")
        out = await v.validate_character_full("лист", character=None)
        assert out["overall_verdict"] == "NEEDS_FIX"

    async def test_unknown_math_fails_overall(self):
        """UNKNOWN must NOT be treated as success (Bug 1 family)."""
        v = self._mk("UNKNOWN")
        out = await v.validate_character_full("лист", character=None)
        assert out["overall_verdict"] == "NEEDS_FIX"

    async def test_reject_math_fails_overall(self):
        """REJECT (empty sheet) must also fail the combined verdict.
        Regression: old code checked `!= NEEDS_FIX`, so REJECT/UNKNOWN/ERROR
        silently counted as passed."""
        v = self._mk("REJECT")
        out = await v.validate_character_full("лист", character=None)
        assert out["overall_verdict"] == "NEEDS_FIX"

    async def test_error_math_fails_overall(self):
        v = self._mk("ERROR")
        out = await v.validate_character_full("лист", character=None)
        assert out["overall_verdict"] == "NEEDS_FIX"

    async def test_srd_flags_propagate(self):
        v = self._mk("VALID")
        # garbage text extracts no race/class → srd check vacuously passes
        out = await v.validate_character_full("абвг абвг абвг", character=None)
        assert out["overall_verdict"] == "VALID"
        assert out["srd_result"]["is_valid"] is True
