"""Unit tests for libs/ai/db_bot_engine.py — parse_character_sheet guards.

The original "Эйра «Чертила» Вальенте" bug: an EMPTY file was parsed into a
fully-populated character because the model echoed the few-shot example from
its own prompt. Five guards now exist:
  1. empty text → None, no LLM call
  2. <10 word chars ("абвг") → None, no LLM call
  3. model's {"error": "empty_sheet"} marker → None
  4. template/placeholder names → None
  5. no race AND no class AND no skills AND no inventory → None
"""
import json

import pytest

from libs.ai.db_bot_engine import DBBotEngineMixin, _re_word_chars
from tests.conftest import FakeDBBot


class P(DBBotEngineMixin):
    def __init__(self, db_bot):
        self.db_bot = db_bot


SHEET = "Имя: Анджей\nРаса: Эльф\nКласс: Следопыт\nHP 12 AC 14 STR 10 DEX 16"


def _reply(obj) -> FakeDBBot:
    return FakeDBBot(content=json.dumps(obj, ensure_ascii=False))


# ═══════════════════════════════════════════════════════════════
# Guard 1 + 2: no LLM call for empty/garbage input
# ═══════════════════════════════════════════════════════════════

class TestNoLLMGuards:

    @pytest.mark.parametrize("text", ["", "   \n\t", "абвг", "...", "123456789"])
    async def test_garbage_returns_none_without_llm(self, text):
        p = P(FakeDBBot(strict=True))   # any LLM call fails the test
        assert await p.parse_character_sheet(text) is None

    def test_re_word_chars_counts_letters_and_digits_only(self):
        assert len(_re_word_chars("абвг")) == 4
        assert len(_re_word_chars("!!! ,,, ...")) == 0
        assert len(_re_word_chars("abc123")) == 6


# ═══════════════════════════════════════════════════════════════
# Guards 3–5: model replies that must NOT become a character
# ═══════════════════════════════════════════════════════════════

class TestModelReplyGuards:

    async def test_error_marker_rejected(self):
        p = P(_reply({"error": "empty_sheet"}))
        assert await p.parse_character_sheet("абвгдеёжзи ещё текст") is None

    async def test_template_echo_name_rejected(self):
        """The model echoing the JSON template's own field names — the exact
        mechanism behind the cross-player character hallucination."""
        p = P(_reply({"name": "ИмяПерсонажа", "race": "РасаПерсонажа"}))
        assert await p.parse_character_sheet(SHEET) is None

    @pytest.mark.parametrize("bad", [
        "", "unknown", "Неизвестно", "Персонаж", "name", "Character Name",
    ])
    async def test_placeholder_names_rejected(self, bad):
        p = P(_reply({"name": bad, "race": "Эльф", "class_name": "Следопыт"}))
        assert await p.parse_character_sheet(SHEET) is None

    async def test_name_only_sheet_rejected(self):
        """Guard 5: a name with no race/class/skills/inventory is unusable."""
        p = P(_reply({"name": "Боб", "level": 1}))
        assert await p.parse_character_sheet(SHEET) is None

    async def test_non_dict_and_broken_json_rejected(self):
        for content in ["[1, 2, 3]", "не json вообще", "```json\n{bad\n```"]:
            p = P(FakeDBBot(content=content))
            assert await p.parse_character_sheet(SHEET) is None

    async def test_llm_crash_returns_none(self):
        class BoomBot:
            async def chat(self, *a, **k):
                raise ConnectionError("timeout")
        p = P(BoomBot())
        assert await p.parse_character_sheet(SHEET) is None


# ═══════════════════════════════════════════════════════════════
# Happy path: a real sheet parses with derived fields
# ═══════════════════════════════════════════════════════════════

class TestHappyPath:

    def _sheet_reply(self):
        return _reply({
            "name": "Анджей", "race": "Эльф", "class_name": "Следопыт",
            "level": 2, "dexterity": 16, "hp": 12, "ac": 10,
            "skills": ["Скрытность"], "inventory": ["Лук"],
        })

    async def test_real_sheet_parses(self):
        p = P(self._sheet_reply())
        char = await p.parse_character_sheet(SHEET)
        assert char is not None
        assert char.name == "Анджей"
        assert char.race == "Эльф"
        assert char.class_name == "Следопыт"

    async def test_hp_backfills_max_hp(self):
        p = P(self._sheet_reply())
        char = await p.parse_character_sheet(SHEET)
        assert char.hp == 12 and char.max_hp == 12

    async def test_ac10_with_dex_gets_modifier(self):
        p = P(self._sheet_reply())
        char = await p.parse_character_sheet(SHEET)
        assert char.ac == 13          # 10 + DEX mod (+3)

    async def test_markdown_fences_stripped(self):
        bot = FakeDBBot(
            content="```json\n" + json.dumps({
                "name": "Анджей", "race": "Эльф", "class_name": "Следопыт",
            }, ensure_ascii=False) + "\n```"
        )
        p = P(bot)
        char = await p.parse_character_sheet(SHEET)
        assert char is not None and char.name == "Анджей"
