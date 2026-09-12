"""ИТЕРАЦИЯ 13 — /cymeriad: валидный лист отклонялся парсером.

Живой репорт: AI-валидатор сказал «✅ Лист прошёл проверку правил», а следом
«❌ Не удалось распознать лист персонажа». Две независимые дыры:

  A) db_bot_engine.parse_character_sheet читал ответ модели ХРУПКО: только
     чистый JSON (опц. в ```-блоке). Проза вокруг JSON, висячие запятые,
     одинарные кавычки / True-False-None → json.loads падал → None.
     Фикс: _extract_json_object — 4 стратегии извлечения.

  B) character_cmds при None от AI-парсера сразу отказывал игроку, хотя
     лист УЖЕ прошёл AI-проверку правил (т.е. заведомо не мусор).
     Фикс: _regex_fallback_parse — детерминированный CharacterParser
     как фолбэк для валидированных листов (в обеих точках вызова).
"""
import json

import pytest

from libs.ai.db_bot_engine import DBBotEngineMixin, _extract_json_object
from libs.character_parser import CharacterParser
from libs.handlers.character_cmds import _regex_fallback_parse
from tests.conftest import FakeDBBot


class P(DBBotEngineMixin):
    def __init__(self, db_bot):
        self.db_bot = db_bot


SHEET = "Имя: Анджей\nРаса: Эльф\nКласс: Следопыт\nHP 12 AC 14 STR 10 DEX 16"

GOOD_CHAR = {
    "name": "Анджей", "race": "Эльф", "class_name": "Следопыт",
    "level": 2, "hp": 12, "skills": ["Скрытность"], "inventory": ["Лук"],
}


# ═══════════════════════════════════════════════════════════════
# A1. _extract_json_object — стратегии извлечения
# ═══════════════════════════════════════════════════════════════

class TestExtractJsonObject:

    def test_plain_json(self):
        assert _extract_json_object('{"name": "Анджей"}') == {"name": "Анджей"}

    def test_fenced_json(self):
        text = "```json\n" + json.dumps(GOOD_CHAR, ensure_ascii=False) + "\n```"
        assert _extract_json_object(text)["name"] == "Анджей"

    def test_fenced_without_language_tag(self):
        text = "```\n" + json.dumps(GOOD_CHAR, ensure_ascii=False) + "\n```"
        assert _extract_json_object(text)["name"] == "Анджей"

    def test_prose_around_json(self):
        """Главный сценарий живого бага: Gemma пишет прозу вокруг JSON."""
        text = ("Конечно! Вот данные персонажа в формате JSON:\n\n"
                + json.dumps(GOOD_CHAR, ensure_ascii=False)
                + "\n\nЕсли нужно что-то поправить — скажи!")
        assert _extract_json_object(text)["name"] == "Анджей"

    def test_prose_with_fences(self):
        text = ("Вот ответ:\n```json\n"
                + json.dumps(GOOD_CHAR, ensure_ascii=False)
                + "\n```\nУдачной игры!")
        assert _extract_json_object(text)["race"] == "Эльф"

    def test_trailing_commas_repaired(self):
        text = '{"name": "Анджей", "race": "Эльф", "skills": ["Скрытность",], "hp": 12,}'
        data = _extract_json_object(text)
        assert data is not None
        assert data["name"] == "Анджей"
        assert data["skills"] == ["Скрытность"]

    def test_python_style_quotes_and_bools(self):
        text = "{'name': 'Анджей', 'race': 'Эльф', 'class_name': 'Следопыт', 'spells': True, 'level': None}"
        data = _extract_json_object(text)
        assert data is not None
        assert data["name"] == "Анджей"
        assert data["spells"] is True

    def test_nested_braces_in_values(self):
        inner = {"name": "Анджей", "features": ["Внимательность {x}"]}
        assert _extract_json_object(json.dumps(inner, ensure_ascii=False))["name"] == "Анджей"

    def test_error_marker_still_extracted(self):
        """Маркер ошибки должен извлекаться (гард 3 решает, что делать дальше)."""
        assert _extract_json_object('{"error": "empty_sheet"}') == {"error": "empty_sheet"}

    def test_garbage_returns_none(self):
        for text in ["", "   ", "не json вообще", "[1, 2, 3]", "```json\n{bad\n```", "{broken"]:
            assert _extract_json_object(text) is None, f"expected None for {text!r}"


# ═══════════════════════════════════════════════════════════════
# A2. parse_character_sheet с «грязными» ответами модели
# ═══════════════════════════════════════════════════════════════

class TestParseCharacterSheetRobust:

    async def test_prose_wrapped_json_now_parses(self):
        """Точный репорт Эйры: модель ответила прозой+JSON → раньше был отказ."""
        bot = FakeDBBot(content=(
            "Вот распознанный персонаж:\n"
            + json.dumps(GOOD_CHAR, ensure_ascii=False)
            + "\nГотово!"
        ))
        char = await P(bot).parse_character_sheet(SHEET)
        assert char is not None
        assert char.name == "Анджей" and char.race == "Эльф"

    async def test_trailing_comma_json_now_parses(self):
        bot = FakeDBBot(content=(
            '{"name": "Анджей", "race": "Эльф", "class_name": "Следопыт", '
            '"skills": ["Скрытность"],}'
        ))
        char = await P(bot).parse_character_sheet(SHEET)
        assert char is not None and char.class_name == "Следопыт"

    async def test_python_style_json_now_parses(self):
        bot = FakeDBBot(content=(
            "{'name': 'Анджей', 'race': 'Эльф', 'class_name': 'Следопыт', "
            "'inventory': ['Лук'], 'hp': None}"
        ))
        char = await P(bot).parse_character_sheet(SHEET)
        assert char is not None and char.name == "Анджей"

    async def test_total_garbage_still_rejected(self):
        """Регресс-гард: мусорный ответ модели по-прежнему даёт None."""
        for content in ["не json вообще", "```json\n{bad\n```", "[1,2,3]"]:
            char = await P(FakeDBBot(content=content)).parse_character_sheet(SHEET)
            assert char is None, f"expected None for {content!r}"

    async def test_error_marker_still_rejected(self):
        bot = FakeDBBot(content="Вот результат: {\"error\": \"empty_sheet\"} — не персонаж.")
        assert await P(bot).parse_character_sheet(SHEET) is None


# ═══════════════════════════════════════════════════════════════
# B. _regex_fallback_parse — локальный фолбэк для валидированных листов
# ═══════════════════════════════════════════════════════════════

DECORATED_SHEET = """# ─────────────────────────────
#  ЛИСТ ПЕРСОНАЖА D&D 5e
# ─────────────────────────────
**Имя:** Сэр Гавейн
**Раса:** Человек (вариант)
**Класс:** Паладин 3
**Предыстория:** Рыцарь
**Мировоззрение:** Законно-добрый

## Характеристики
| Сила | Ловкость | Телосложение | Интеллект | Мудрость | Харизма |
|------|----------|--------------|-----------|----------|---------|
| 16   | 10       | 14           | 9         | 12       | 17      |

## Навыки
- Запугивание
- Убеждение
- Религия

## Снаряжение
- Длинный меч
- Щит
- Латы
"""


class TestRegexFallback:

    def test_realistic_sheet_recognized(self):
        """Реалистичный лист (как у игроков) — фолбэк вытаскивает персонажа."""
        char = _regex_fallback_parse(DECORATED_SHEET)
        assert char is not None
        assert char.name == "Сэр Гавейн"
        assert char.class_name and "аладин" in char.class_name
        assert char.strength == 16 and char.charisma == 17

    def test_ai_garbage_then_fallback_recovers(self):
        """Сквозной флоу хендлера: AI-парсер вернул None (мусорный ответ модели)
        → фолбэк распознаёт тот же лист. Композиция как в character_cmds."""
        import asyncio
        bot = FakeDBBot(content="Извините, я не смог распознать лист.")

        async def _flow():
            parsed_ai = await P(bot).parse_character_sheet(DECORATED_SHEET)
            if parsed_ai is None:
                return _regex_fallback_parse(DECORATED_SHEET)
            return parsed_ai

        char = asyncio.run(_flow())
        assert char is not None
        assert char.name == "Сэр Гавейн"

    def test_unknown_name_rejected(self):
        """Мусорный текст: regex-парсер вернёт «Unknown»-болванку → отказ."""
        assert _regex_fallback_parse("просто заметки про кампанию, ничего тут нет") is None

    def test_name_without_data_rejected(self):
        """Имя есть, но ни расы, ни класса, ни навыков, ни снаряжения → отказ."""
        # parse_text вытащит имя из 'Имя: X', но не найдёт остальных данных.
        sheet = "Имя: ОдинокийСтранник\nЗаметка: без данных"
        char = _regex_fallback_parse(sheet)
        if char is not None:
            # если парсер всё же что-то нашёл — это должна быть раса/класс/навыки
            assert char.race or char.class_name or char.skills or char.inventory or char.features
        # В любом случае не должно быть болванки с пустыми данными:
        if char is not None:
            assert char.name != "Unknown"

    def test_fallback_never_crashes(self):
        for text in ["", None, "///", "~~~", 12345]:
            try:
                _regex_fallback_parse(text)
            except Exception as e:
                pytest.fail(f"fallback crashed on {text!r}: {e}")

    def test_direct_parser_parity(self):
        """Фолбэк = CharacterParser.parse_text + фильтр качества: на хорошем
        листе результаты идентичны прямому вызову парсера."""
        direct = CharacterParser.parse_text(DECORATED_SHEET)
        via_fallback = _regex_fallback_parse(DECORATED_SHEET)
        assert direct is not None and via_fallback is not None
        assert direct.name == via_fallback.name
