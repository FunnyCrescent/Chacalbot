"""Edition differences between D&D 5e (2014 PHB) and D&D 5e (2024).

Provides a single source of truth for edition-specific rule differences,
so prompts and validators can adapt dynamically based on the active edition
configured in config_legacy.DND_EDITION.

Typical usage::

    from libs.srd.edition_diff import EditionDiff

    diff = EditionDiff()
    print(diff.get_prompt_edition_note())
    # "В данной игре используется D&D 5e (2014 PHB). Бонусы к характеристикам ..."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from libs.config_legacy import DND_EDITION

logger = logging.getLogger(__name__)


@dataclass
class EditionRule:
    """A single rule that differs between editions."""
    name: str
    rule_2014: str
    rule_2024: str
    impact: str  # "high" | "medium" | "low" — how much it affects gameplay


class EditionDiff:
    """Documents the mechanical differences between D&D 5e 2014 and 2024 editions.

    All methods are pure — no I/O, no LLM calls.  The class reads
    DND_EDITION from config at init time and exposes helpers that
    return the correct string / value for the active edition.
    """

    EDITION: str = ""

    KEY_DIFFERENCES: Dict[str, EditionRule] = {}

    def __init__(self, edition: Optional[str] = None):
        self.EDITION = edition or DND_EDITION
        self._build_differences()

    # ───────────────────────────────────────────────────────────────
    # Internal: populate differences dict
    # ───────────────────────────────────────────────────────────────

    def _build_differences(self) -> None:
        self.KEY_DIFFERENCES = {
            "ability_bonus_source": EditionRule(
                name="Источник бонусов к характеристикам",
                rule_2014="Бонусы к характеристикам (+2/+1) даёт РАСА (Race) и подраса. "
                          "Например, Hill Dwarf: CON +2, WIS +1. Background не даёт бонусов к статам.",
                rule_2024="Бонусы к характеристикам (+2/+1) даёт ПРЕДЫСТОРИЯ (Background). "
                          "Раса/вид (Species) не даёт фиксированных плюсов к характеристикам.",
                impact="high",
            ),
            "critical_hits": EditionRule(
                name="Критические удары",
                rule_2014="При натуральной 20 на атаке — удваиваются ВСЕ кости урона "
                          "(например 2d6 → 4d6). Модификатор не удваивается.",
                rule_2024="При натуральной 20 на атаке — максимизируются кости урона "
                          "+ бросок обычного урона (например 2d6 → 12 + 2d6). "
                          "Модификатор не удваивается.",
                impact="high",
            ),
            "weapon_mastery": EditionRule(
                name="Овладение оружием (Weapon Mastery)",
                rule_2014="Не существует. Оружие не имеет свойств mastery-типа.",
                rule_2024="Новые mastery-свойства у оружия (Cleave, Graze, Push, Sap, Slow, Topple, Vex). "
                          "Боец с Weapon Mastery может использовать эти свойства.",
                impact="medium",
            ),
            "spell_preparation": EditionRule(
                name="Подготовка заклинаний",
                rule_2014="Каждый класс имеет фиксированный список заклинаний для подготовки. "
                          "Количество = модификатор характеристики класса + уровень класса.",
                rule_2024="Более гибкая подготовка: можно менять подготовленные заклинания "
                          "при долгом отдыхе. Списки классов расширены.",
                impact="medium",
            ),
            "exhaustion": EditionRule(
                name="Истощение (Exhaustion)",
                rule_2014="6 уровней. Каждый уровень даёт специфический штраф: "
                          "1: disadvantage on ability checks; 2: speed halved; "
                          "3: disadvantage on attacks/saves; 4: HP halved; "
                          "5: speed 0; 6: death.",
                rule_2024="10 уровней. Каждый уровень даёт -1 ко ВСЕМ d20 броскам "
                          "(атаки, проверки, спасброски) и -5 к speed за уровень. "
                          "На 10-м уровне — смерть.",
                impact="high",
            ),
            "feats_at_level_1": EditionRule(
                name="Фиты на 1-м уровне",
                rule_2014="Нет фитов на 1-м уровне. Фиты берутся вместо ASI (обычно на 4-м уровне).",
                rule_2024="Origin feat на 1-м уровне (из предыстории). Дополнительный фит "
                          "при каждом ASI на чётных уровнях (4, 8, 12, 16, 19).",
                impact="medium",
            ),
            "backgrounds": EditionRule(
                name="Предыстории (Backgrounds)",
                rule_2014="Фиксированные предыстории: каждая даёт конкретные навыки, инструменты, "
                          "снаряжение и черту (feature). Примеры: Soldier, Sage, Criminal.",
                rule_2024="Настраиваемые предыстории: игрок выбирает 3 навыка, 1 инструмент, "
                          "1 черту origin feat, бонусы к характеристикам (+2/+1). "
                          "Снаряжение — фиксированный пакет.",
                impact="high",
            ),
            "inspiration": EditionRule(
                name="Вдохновение (Inspiration)",
                rule_2014="Вдохновение позволяет ПЕРЕБРОСИТЬ один d20 бросок "
                          "(must use the new roll).",
                rule_2024="Вдохновение даёт ADVANTAGE на один d20 бросок "
                          "(roll two d20, take the higher).",
                impact="medium",
            ),
            "multiclass": EditionRule(
                name="Мультикласс",
                rule_2014="Те же правила: требования к характеристикам, "
                          "прогрессия заклинаний, hit dice.",
                rule_2024="Те же правила с минимальными изменениями: "
                          "требования к характеристикам сохранены, "
                          "прогрессия заклинаний чуть гибче.",
                impact="low",
            ),
            "crafting": EditionRule(
                name="Создание предметов (Crafting)",
                rule_2014="Создание предмета: 1 день за 5 gp стоимости предмета. "
                          "Нужна proficiency в инструментах.",
                rule_2024="Упрощённые правила: стоимость в днях = стоимость предмета / 10. "
                          "Нужна proficiency. Больше видов ремесла.",
                impact="low",
            ),
            "rest_healing": EditionRule(
                name="Отдых и лечение",
                rule_2014="Long rest: восстанавливает ВСЕ HP. Short rest: можно тратить hit dice.",
                rule_2024="Long rest: восстанавливает половину MAX HP (не все). "
                          "Short rest: можно тратить hit dice (как раньше). "
                          "Кости здоровья восстанавливаются при long rest: половина уровня.",
                impact="high",
            ),
            "hide_action": EditionRule(
                name="Скрытность (Hide)",
                rule_2014="Hide — действие (action). Нельзя спрятаться и атаковать в один ход "
                          "(без Haste или Extra Attack).",
                rule_2024="Hide — бонусное действие (bonus action) для Rogues через Cunning Action. "
                          "Для остальных — action, но правила Hide упрощены.",
                impact="medium",
            ),
            "two_weapon_fighting": EditionRule(
                name="Бой двумя оружиями (Two-Weapon Fighting)",
                rule_2014="Бонусная атака рукой — без модификатора к урону. "
                          "Нужен Two-Weapon Fighting style для модификатора.",
                rule_2024="Никакой бонусной атаки по умолчанию. "
                          "Нужен feat Dual Wielder или Weapon Mastery (Vex/Nick). "
                          "С Nick mastery — бонусная атака как часть Attack action.",
                impact="medium",
            ),
        }

    # ───────────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────────

    @property
    def is_2014(self) -> bool:
        return self.EDITION == "5e_2014"

    @property
    def is_2024(self) -> bool:
        return self.EDITION == "5e_2024"

    @property
    def edition_label(self) -> str:
        """Human-readable edition label: 'D&D 5e (2014 PHB)' or 'D&D 5e (2024)'."""
        if self.is_2014:
            return "D&D 5e (2014 PHB)"
        elif self.is_2024:
            return "D&D 5e (2024)"
        else:
            return f"D&D 5e ({self.EDITION})"

    def get_prompt_edition_note(self) -> str:
        """Return a note to append to system prompts about the active edition."""
        if self.is_2014:
            return (
                "## РЕДАКЦИЯ: D&D 5e (2014 PHB)\n"
                "В данной игре используется **D&D 5e (2014 PHB)**, а НЕ 2024-я редакция. "
                "Это влияет на следующие ключевые механики:\n\n"
                "1. **Бонусы к характеристикам даёт РАСА** (а не предыстория). "
                "Например, Hill Dwarf: CON +2, WIS +1. Подраса добавляет бонусы.\n"
                "2. **Критические удары**: удваиваются ВСЕ кости урона (2d6 → 4d6). "
                "Модификатор не удваивается.\n"
                "3. **Нет Weapon Mastery** — этого свойства не существует в 2014.\n"
                "4. **Истощение**: 6 уровней с конкретными штрафами за каждый уровень.\n"
                "5. **Нет Origin feat на 1-м уровне** — фиты берутся вместо ASI.\n"
                "6. **Предыстории фиксированные** — каждая даёт конкретные навыки, инструменты и feature.\n"
                "7. **Вдохновение**: позволяет ПЕРЕБРОСИТЬ один d20 (не advantage).\n"
                "8. **Long rest**: восстанавливает ВСЕ HP (не половину).\n"
                "9. **Подготовка заклинаний**: фиксированные списки по классу.\n"
                "10. **Two-Weapon Fighting**: бонусная атака рукой без модификатора к урону.\n"
            )
        elif self.is_2024:
            return (
                "## РЕДАКЦИЯ: D&D 5e (2024)\n"
                "В данной игре используется **D&D 5e (2024)**. Ключевые отличия от 2014:\n\n"
                "1. **Бонусы к характеристикам даёт ПРЕДЫСТОРИЯ** (Background), а не раса.\n"
                "2. **Критические удары**: max damage + roll (2d6 → 12 + 2d6).\n"
                "3. **Weapon Mastery** — новые свойства оружия (Cleave, Graze, Push, Sap, Slow, Topple, Vex).\n"
                "4. **Истощение**: 10 уровней, -1 ко всем d20 и -5 speed за уровень.\n"
                "5. **Origin feat на 1-м уровне** — из предыстории.\n"
                "6. **Предыстории настраиваемые** — игрок выбирает навыки, инструменты, feat, бонусы.\n"
                "7. **Вдохновение**: даёт advantage на один d20 бросок.\n"
                "8. **Long rest**: восстанавливает половину MAX HP.\n"
                "9. **Подготовка заклинаний**: можно менять при долгом отдыхе.\n"
            )
        else:
            return f"## РЕДАКЦИЯ: {self.edition_label}\n"

    def get_ability_bonus_source(self) -> str:
        """Return 'race' for 2014, 'background' for 2024."""
        return "race" if self.is_2014 else "background"

    def get_crit_rule(self) -> str:
        """Return the critical hit rule description for the active edition."""
        rule = self.KEY_DIFFERENCES.get("critical_hits")
        if not rule:
            return "Unknown"
        return rule.rule_2014 if self.is_2014 else rule.rule_2024

    def get_exhaustion_levels(self) -> int:
        """Return the number of exhaustion levels: 6 for 2014, 10 for 2024."""
        return 6 if self.is_2014 else 10

    def get_inspiration_rule(self) -> str:
        """Return the inspiration rule for the active edition."""
        rule = self.KEY_DIFFERENCES.get("inspiration")
        if not rule:
            return "Unknown"
        return rule.rule_2014 if self.is_2014 else rule.rule_2024

    def get_rest_rule(self) -> str:
        """Return the long rest healing rule for the active edition."""
        rule = self.KEY_DIFFERENCES.get("rest_healing")
        if not rule:
            return "Unknown"
        return rule.rule_2014 if self.is_2014 else rule.rule_2024

    def has_weapon_mastery(self) -> bool:
        """Return True if Weapon Mastery is available (2024 only)."""
        return self.is_2024

    def has_origin_feat_at_level_1(self) -> bool:
        """Return True if characters get an Origin feat at level 1 (2024 only)."""
        return self.is_2024

    def get_rule(self, key: str) -> str:
        """Get a specific rule value for the active edition.

        Args:
            key: One of the KEY_DIFFERENCES keys.

        Returns:
            The rule description for the active edition.
        """
        rule = self.KEY_DIFFERENCES.get(key)
        if not rule:
            return ""
        return rule.rule_2014 if self.is_2014 else rule.rule_2024

    def get_all_differences_summary(self) -> str:
        """Return a markdown summary of ALL differences for the active edition."""
        lines = [f"# Отличия {self.edition_label}\n"]
        for key, rule in self.KEY_DIFFERENCES.items():
            active = rule.rule_2014 if self.is_2014 else rule.rule_2024
            impact_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(rule.impact, "⚪")
            lines.append(f"## {impact_icon} {rule.name}\n")
            lines.append(f"{active}\n")
        return "\n".join(lines)

    def get_validator_note(self) -> str:
        """Return a note specifically for character validation prompts."""
        if self.is_2014:
            return (
                "## ВАЖНО: РЕДАКЦИЯ 2014 PHB\n\n"
                "В D&D 5e (2014 PHB) бонусы к характеристикам (+2/+1 и т.д.) даёт РАСА (Race) "
                "и подраса (Subrace). Предыстория (Background) НЕ даёт бонусов к характеристикам — "
                "она даёт навыки, инструменты, снаряжение и черту (feature).\n\n"
                "Поэтому, если в листе персонажа указаны бонусы от расы — это ОЖИДАЕМО и КОРРЕКТНО "
                "для 2014 редакции. Не утверждай, что раса \"не должна давать\" бонусы — "
                "это правило 2014 года.\n\n"
                "Дополнительно:\n"
                "- Критические удары: удваиваются ВСЕ кости урона.\n"
                "- Нет Weapon Mastery.\n"
                "- Истощение: 6 уровней с конкретными штрафами.\n"
                "- Нет Origin feat на 1-м уровне.\n"
                "- Предыстории фиксированные (не настраиваемые).\n"
                "- Вдохновение: переброс одного d20.\n"
                "- Long rest: восстанавливает ВСЕ HP.\n"
            )
        else:
            return (
                "## ВАЖНО: РЕДАКЦИЯ 2024\n\n"
                "В D&D 5e (2024) бонусы к характеристикам (+2/+1 и т.д.) даёт ПРЕДЫСТОРИЯ (Background), "
                "а НЕ раса/вид (Species). У видов в этой редакции нет фиксированных плюсов/минусов "
                "к характеристикам.\n\n"
                "Поэтому НЕ утверждай, что раса \"должна давать\" или \"не даёт заявленные бонусы\" — "
                "это ожидаемо и корректно для 2024 года.\n"
            )
