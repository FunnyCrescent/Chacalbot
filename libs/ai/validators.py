"""ValidatorsMixin — character sheet validation."""
import json
import logging
import asyncio
import random
import re
from dataclasses import asdict
from typing import Dict, List, Optional, Any, Callable, Awaitable

from libs.character_parser import ParsedCharacter
from .anti_cheat import AntiCheatValidator, CharacterValidationResult, ValidationResult
from .tools import (
    ROLL_TYPE_ENUM, DICE_TOOLS, GAME_TOOLS, ALL_TOOLS,
    COMBAT_TOOLS, MASTER_TOOLS, GET_TOOLS, DB_TOOLS, DB_WRITE_TOOL_NAMES,
)
from .prompts import (
    MASTER_PROMPT, ASK_PROMPT, RENDERER_PROMPT, NPC_AI_PROMPT, DB_BOT_PROMPT,
)
from .utils import strip_stray_tags
from libs.srd.edition_diff import EditionDiff

logger = logging.getLogger(__name__)


class ValidatorsMixin:
    """ValidatorsMixin — character sheet validation."""

    # Lazy-initialised anti-cheat validator (shared across all instances)
    _anti_cheat: Optional[AntiCheatValidator] = None

    @classmethod
    def _get_anti_cheat(cls) -> AntiCheatValidator:
        """Return a shared AntiCheatValidator instance (created once)."""
        if cls._anti_cheat is None:
            cls._anti_cheat = AntiCheatValidator()
        return cls._anti_cheat

    async def validate_character_sheet(self, sheet_text: str) -> Dict[str, str]:
        """Validate character sheet using DB-Bot — checks ARITHMETIC/MECHANICS ONLY.
        Never judges race legitimacy, homebrew balance, or feat/background matching."""
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
   между классами (например "Воин 1 / Маг 1"). Составное имя класса как флейвор ("Паладин-Мракобес")
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

Ответь СТРОГО в формате:
**Вердикт**: [VALID / NEEDS_FIX]
**Ошибки**: список найденных МАТЕМАТИЧЕСКИХ/МЕХАНИЧЕСКИХ проблем (пусто, если их нет)
**Исправления**: что конкретно поправить (только если NEEDS_FIX)

Если вердикт VALID - поля "Ошибки" и "Исправления" должны быть пустыми или содержать "Нет"."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages)
            content = response["choices"][0]["message"].get("content", "")
            verdict = "NEEDS_FIX" if "NEEDS_FIX" in content.upper() else "VALID" if "VALID" in content.upper() else "UNKNOWN"
            return {"verdict": verdict, "details": content}
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {"verdict": "ERROR", "details": str(e)}

    # ───────────────────────────────────────────────────────────────
    # Anti-cheat / SRD validation
    # ───────────────────────────────────────────────────────────────

    def validate_character_srd(self, character: ParsedCharacter) -> CharacterValidationResult:
        """Validate a parsed character against SRD 5e (2014) reference data.

        This is a **synchronous** check (no LLM calls) that validates:
        - Race (SRD race? hybrid exploit? homebrew?)
        - Class (SRD class? multiclass prereqs? homebrew?)
        - Background (SRD background? homebrew?)
        - Backstory abilities (claims immunity/resistance/flight without justification?)
        - Math basics (stat ranges, level, HP)

        Returns a CharacterValidationResult with per-category results.
        """
        validator = self._get_anti_cheat()
        result = validator.validate_character(character)
        logger.info(
            f"[anti_cheat] Character '{character.name}': "
            f"valid={result.is_valid}, "
            f"errors={len(result.errors())}, "
            f"warnings={len(result.warnings_only())}"
        )
        return result

    def validate_character_srd_text(self, sheet_text: str) -> Dict[str, Any]:
        """Quick SRD validation from raw sheet text (without LLM parsing).

        Parses basic fields from text using regex and validates them.
        Less thorough than validate_character_srd (which needs a ParsedCharacter),
        but useful when you don't have a parsed character yet.

        Returns dict with keys: is_valid, results, summary.
        """
        validator = self._get_anti_cheat()

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
            results["race"] = asdict(validator.validate_race(race))
            if not results["race"]["is_valid"]:
                has_error = True
        if class_name:
            results["class"] = asdict(validator.validate_class(class_name))
            if not results["class"]["is_valid"]:
                has_error = True
        if background:
            results["background"] = asdict(validator.validate_background(background))
            if not results["background"]["is_valid"]:
                has_error = True
        if backstory:
            results["backstory"] = asdict(validator.validate_backstory_ability(backstory))
            if not results["backstory"]["is_valid"]:
                has_error = True

        return {
            "is_valid": not has_error,
            "results": results,
            "summary": f"Validated race={race!r}, class={class_name!r}, "
                       f"background={background!r} — "
                       f"{'PASS' if not has_error else 'ISSUES FOUND'}",
        }

    async def validate_character_full(
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
        # 1. Math validation (existing LLM-based check)
        math_result = await self.validate_character_sheet(sheet_text)

        # 2. SRD anti-cheat validation
        if character:
            srd_result = self.validate_character_srd(character)
            srd_dict = {
                "is_valid": srd_result.is_valid,
                "race": asdict(srd_result.race_result) if srd_result.race_result else None,
                "class": asdict(srd_result.class_result) if srd_result.class_result else None,
                "background": asdict(srd_result.background_result) if srd_result.background_result else None,
                "backstory": asdict(srd_result.backstory_result) if srd_result.backstory_result else None,
                "math": asdict(srd_result.math_result) if srd_result.math_result else None,
                "warnings": [asdict(w) for w in srd_result.warnings],
                "errors": [asdict(e) for e in srd_result.errors()],
            }
        else:
            srd_dict = self.validate_character_srd_text(sheet_text)

        # 3. Combine verdicts
        math_ok = math_result.get("verdict") != "NEEDS_FIX"
        srd_ok = srd_dict.get("is_valid", True)
        overall = "VALID" if (math_ok and srd_ok) else "NEEDS_FIX"

        return {
            "math_verdict": math_result.get("verdict", "UNKNOWN"),
            "math_details": math_result.get("details", ""),
            "srd_result": srd_dict,
            "overall_verdict": overall,
        }


