"""CharacterValidationService — доменная логика валидации листов персонажей.

Первый шаг рефакторинга «mixins → сервисы с явными зависимостями»
(рекомендация код-ревью):

- зависимости ЯВНЫЕ: ``db_bot`` (LLM-клиент арифметической проверки) и
  ``anti_cheat`` (SRD-валидатор) передаются в конструктор;
- класс полностью изолируем в тестах (tests/test_character_validation_service.py)
  — без Telegram, без движка, без .env;
- ValidatorsMixin остаётся тонкой обёрткой ради обратной совместимости:
  все существующие вызовы ``dm_engine.validate_*`` работают без изменений.

Контракт вердиктов (строгий, см. parse_verdict):
    VALID — лист явно прошёл проверку;
    NEEDS_FIX / REJECT / UNKNOWN / ERROR — ЛЮБОЙ другой исход считается
    «не прошёл». Единственный пропуск — явный VALID.
"""
import logging
import re
from dataclasses import asdict
from typing import Any, Dict, Optional

from libs.character_parser import ParsedCharacter
from libs.srd.edition_diff import EditionDiff
from libs.ai.anti_cheat import AntiCheatValidator, CharacterValidationResult

logger = logging.getLogger(__name__)

# Символов (буквы/цифры) должно быть минимум, чтобы текст считался листом.
MIN_SHEET_CHARS = 10


class CharacterValidationService:
    """Арифметическая (LLM) + SRD (офлайн) валидация листов персонажей."""

    def __init__(self, db_bot=None, anti_cheat: Optional[AntiCheatValidator] = None,
                 math_validator=None, srd_validator=None):
        # Явные зависимости: LLM-клиент и SRD-валидатор.
        self.db_bot = db_bot
        self.anti_cheat = anti_cheat if anti_cheat is not None else AntiCheatValidator()
        # Optional overrides: позволяют адаптеру (ValidatorsMixin) направить
        # комбинированную проверку через переопределяемые методы движка,
        # сохраняя патч-поверхность и поведение подклассов.
        self._math_validator = math_validator
        self._srd_validator = srd_validator

    # ═══════════════════════════════════════════════════════════
    # Verdict parsing (строгий)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def parse_verdict(content: str) -> str:
        """Parse the validator's verdict STRICTLY.

        Old bug: `"NEEDS_FIX" if "NEEDS_FIX" in content.upper() else "VALID" if "VALID" in ...`
        — the substring "VALID" is also inside "INVALID", so a reply that said INVALID
        was classified as VALID. Now: word-boundary matching, NEEDS_FIX checked first,
        and anything unparseable is UNKNOWN (which callers must treat as NOT passed)."""
        text = content or ""
        if re.search(r"NEEDS?[ _]?FIX", text, re.IGNORECASE):
            return "NEEDS_FIX"
        if re.search(r"\bREJECT(?:ED)?\b", text, re.IGNORECASE):
            return "REJECT"
        if re.search(r"\bINVALID\b", text, re.IGNORECASE):
            return "NEEDS_FIX"
        if re.search(r"(?<!IN)(?<!UN)\bVALID\b", text, re.IGNORECASE):
            return "VALID"
        return "UNKNOWN"

    # ═══════════════════════════════════════════════════════════
    # Math validation (LLM)
    # ═══════════════════════════════════════════════════════════

    async def validate_sheet_math(self, sheet_text: str) -> Dict[str, str]:
        if self._math_validator is not None:
            return await self._math_validator(sheet_text)
        return await self._validate_sheet_math_impl(sheet_text)

    async def _validate_sheet_math_impl(self, sheet_text: str) -> Dict[str, str]:
        """Validate character sheet using DB-Bot — checks ARITHMETIC/MECHANICS ONLY.
        Never judges race legitimacy, homebrew balance, or feat/background matching.

        BUG (Эйра «Чертила»): an EMPTY sheet got verdict VALID because the naive
        substring check treats any reply containing "VALID" as passed (and "INVALID"
        contains "VALID"!). Fixes: empty sheet → REJECT without an LLM call;
        word-boundary verdict parsing; UNKNOWN/ERROR verdicts are NOT passed."""
        _edition = EditionDiff()
        _edition_note = _edition.get_validator_note()
        prompt = f"""Ты - валидатор ЧИСЕЛ в листе персонажа {_edition.edition_label}. Твоя ЕДИНСТВЕННАЯ задача - проверить,
что цифры физически возможны и сходятся арифметически. Ты НЕ судья баланса, НЕ эксперт по лору
и НЕ решаешь, "честная" ли раса, фит или предыстория.

## ЧТО ТЫ ПРОВЕРЯЕШЬ (и только это)

1. Отрицательные значения: HP, AC, золото.
2. Характеристики вне диапазона 1-30.
3. Чистая арифметика: если в листе написано "X + Y = Z", а Z ≠ X + Y - посчитай сам и укажи расхождение.
   Форма записи (например "3к8-3" вместо "7+4+4") не имеет значения - важен только итог.
4. Заклинания класса, который физически не может их кастовать - НО только если в листе нет вообще
   никакого объяснения (архетипа, мультикласса, фита, расовой способности). Если есть хоть какая-то
   зацепка (например "Arcane Trickster", "Eldritch Knight", "Magic Initiate") - это НЕ ошибка.
5. Мультикласс на 1-м уровне - ТОЛЬКО если в листе буквально указано разделение по уровням
   между классами (например "Воин 1 / Маг 1"). Составное имя класса как флейтор ("Паладин-Мракобес")
   - это стиль игрока, а не механический мультикласс. НЕ ошибка.

## ЧТО ТЫ НИКОГДА НЕ ПРОВЕРЯЕШЬ И НЕ УПОМИНАЕШЬ КАК ОШИБКУ

- Легитимность расы/вида. ЛЮБАЯ раса - из SRD, из другой книги, гибридная, полностью выдуманная,
  с любым названием - по умолчанию ВАЛИДНА. Не требуй "компенсации" за бонусы, не оценивай баланс,
  не решай, что раса "недостаточно ограничена". Название расы само по себе никогда не ошибка.
- Соответствие черты (feat) предыстории. Фит мог быть взят отдельно, через хоумрул стола или
  через происхождение - не твоя зона.
- Языки, инструменты, стартовое снаряжение, любые "избыточные" на твой взгляд детали - это решает
  стол, не ты.

{_edition_note}

## ПРАВИЛО САМОПРОВЕРКИ (обязательно перед ответом)

Прежде чем писать пункт в "Ошибки" - перечитай, что ты сам только что написал. Если в своих же
рассуждениях ты называешь число или расчёт "верным"/"корректным" - оно НЕ может одновременно быть
в списке ошибок. Одно из двух. Если сомневаешься, что это математическая ошибка - промолчи, не пиши.

## МАТЕМАТИКА (справочно, чтобы не путаться)
- HP 1-го уровня = МАКСИМУМ хит-куба + мод CON.
- Характеристики 1-20 на старте - нормально (выше - если явно указан источник, например артефакт).
- Бонус мастерства +2 на 1-м уровне - нормально для всех классов.
- AC 10 + DEX для безбронного - нормально.

## ВЕРДИКТ
- **VALID**: числа сходятся, нет отрицательных или невозможных значений.
- **NEEDS_FIX**: используй ТОЛЬКО для пунктов из раздела "ЧТО ТЫ ПРОВЕРЯЕШЬ" выше. Ничего больше.

Лист персонажа:
---
{sheet_text}
---

ОБЯЗАТЕЛЬНО: если лист пустой, обрезанный или вообще не содержит листа персонажа —
сразу верни NEEDS_FIX с пояснением "лист пустой или не читается". Не проверяй "числа"
в пустом тексте.

Ответь СТРОГО в формате:
**Вердикт**: [VALID / NEEDS_FIX]
**Ошибки**: список найденных МАТЕМАТИЧЕСКИХ/МЕХАНИЧЕСКИХ проблем (пусто, если их нет)
**Исправления**: что конкретно поправить (только если NEEDS_FIX)

Если вердикт VALID - поля "Ошибки" и "Исправления" должны быть пустыми или содержать "Нет"."""
        # BUG: an empty file is not a character sheet — reject BEFORE the LLM call.
        if not (sheet_text or "").strip() or len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]", sheet_text)) < MIN_SHEET_CHARS:
            return {
                "verdict": "REJECT",
                "details": "Файл пустой или не содержит листа персонажа. Прикрепи .txt/.md с реальным листом.",
            }
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages)
            content = response["choices"][0]["message"].get("content", "")
            verdict = self.parse_verdict(content)
            return {"verdict": verdict, "details": content}
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {"verdict": "ERROR", "details": str(e)}

    # ═══════════════════════════════════════════════════════════
    # SRD validation (offline, anti-cheat)
    # ═══════════════════════════════════════════════════════════

    def validate_srd(self, character: ParsedCharacter) -> CharacterValidationResult:
        """Validate a parsed character against SRD 5e (2014) reference data.

        This is a **synchronous** check (no LLM calls) that validates:
        - Race (SRD race? hybrid exploit? homebrew?)
        - Class (SRD class? multiclass prereqs? homebrew?)
        - Background (SRD background? homebrew?)
        - Backstory abilities (claims immunity/resistance/flight without justification?)
        - Math basics (stat ranges, level, HP)

        Returns a CharacterValidationResult with per-category results.
        """
        if self._srd_validator is not None:
            return self._srd_validator(character)
        result = self.anti_cheat.validate_character(character)
        logger.info(
            f"[anti_cheat] Character '{character.name}': "
            f"valid={result.is_valid}, "
            f"errors={len(result.errors())}, "
            f"warnings={len(result.warnings_only())}"
        )
        return result

    def validate_srd_text(self, sheet_text: str) -> Dict[str, Any]:
        """Quick SRD validation from raw sheet text (without LLM parsing).

        Parses basic fields from text using regex and validates them.
        Less thorough than validate_srd (which needs a ParsedCharacter),
        but useful when you don't have a parsed character yet.

        Returns dict with keys: is_valid, results, summary.
        """
        # Extract basic fields from text using simple regex
        race = ""
        class_name = ""
        background = ""
        backstory = ""

        # Common patterns for race/class extraction
        race_match = re.search(
            r'(?:race|раса|вид)\s*[:=]\s*(.+?)(?:\n|$)',
            sheet_text, re.IGNORECASE
        )
        class_match = re.search(
            r'(?:class|класс)\s*[:=]\s*(.+?)(?:\n|$)',
            sheet_text, re.IGNORECASE
        )
        bg_match = re.search(
            r'(?:background|предыстория|происхождение)\s*[:=]\s*(.+?)(?:\n|$)',
            sheet_text, re.IGNORECASE
        )
        backstory_match = re.search(
            r'(?:backstory|предыстория|история|биография)\s*[:=]\s*(.+?)(?:\n\n|$)',
            sheet_text, re.IGNORECASE | re.DOTALL
        )

        if race_match:
            race = race_match.group(1).strip()
        if class_match:
            class_name = class_match.group(1).strip()
        if bg_match:
            background = bg_match.group(1).strip()
        if backstory_match:
            backstory = backstory_match.group(1).strip()[:500]  # Limit length

        results: Dict[str, Any] = {}
        has_error = False

        if race:
            results["race"] = asdict(self.anti_cheat.validate_race(race))
            if not results["race"]["is_valid"]:
                has_error = True
        if class_name:
            results["class"] = asdict(self.anti_cheat.validate_class(class_name))
            if not results["class"]["is_valid"]:
                has_error = True
        if background:
            results["background"] = asdict(self.anti_cheat.validate_background(background))
            if not results["background"]["is_valid"]:
                has_error = True
        if backstory:
            results["backstory"] = asdict(self.anti_cheat.validate_backstory_ability(backstory))
            if not results["backstory"]["is_valid"]:
                has_error = True

        return {
            "is_valid": not has_error,
            "results": results,
            "summary": f"Validated race={race!r}, class={class_name!r}, "
                       f"background={background!r} — "
                       f"{'PASS' if not has_error else 'ISSUES FOUND'}",
        }

    # ═══════════════════════════════════════════════════════════
    # Combined validation
    # ═══════════════════════════════════════════════════════════

    async def validate_full(
        self,
        sheet_text: str,
        character: Optional[ParsedCharacter] = None,
    ) -> Dict[str, Any]:
        """Full validation: BOTH math (LLM) + SRD anti-cheat.

        Args:
            sheet_text: Raw character sheet text for LLM math validation.
            character: Optional pre-parsed ParsedCharacter for SRD validation.
                      If None, a quick text-based SRD check is used instead.

        Returns dict with keys: math_verdict, math_details, srd_result, overall_verdict.
        """
        # 1. Math validation (LLM-based check)
        math_result = await self.validate_sheet_math(sheet_text)

        # 2. SRD anti-cheat validation
        if character:
            srd_result = self.validate_srd(character)
            srd_dict = {
                "is_valid": srd_result.is_valid,
                # NOTE: `is not None` on purpose! ValidationResult.__bool__
                # returns is_valid, so a PRESENT-but-invalid result is falsy —
                # a truthiness check would silently drop the error details
                # (found by unit tests).
                "race": asdict(srd_result.race_result) if srd_result.race_result is not None else None,
                "class": asdict(srd_result.class_result) if srd_result.class_result is not None else None,
                "background": asdict(srd_result.background_result) if srd_result.background_result is not None else None,
                "backstory": asdict(srd_result.backstory_result) if srd_result.backstory_result is not None else None,
                "math": asdict(srd_result.math_result) if srd_result.math_result is not None else None,
                "warnings": [asdict(w) for w in srd_result.warnings],
                "errors": [asdict(e) for e in srd_result.errors()],
            }
        else:
            srd_dict = self.validate_srd_text(sheet_text)

        # 3. Combine verdicts.
        # BUG (found by unit tests): old check was `verdict != "NEEDS_FIX"`,
        # so UNKNOWN / ERROR / REJECT silently counted as PASSED — the same
        # Bug-1 family as the naive substring verdict parser. The contract
        # (see parse_verdict docstring) is: ONLY an explicit VALID passes.
        math_ok = math_result.get("verdict") == "VALID"
        srd_ok = srd_dict.get("is_valid", True)
        overall = "VALID" if (math_ok and srd_ok) else "NEEDS_FIX"

        return {
            "math_verdict": math_result.get("verdict", "UNKNOWN"),
            "math_details": math_result.get("details", ""),
            "srd_result": srd_dict,
            "overall_verdict": overall,
        }
