"""
Генерация .md файла персонажа из JSON-данных от LLM.
"""

import re
from typing import Any

from .dice import modifier


STAT_NAMES = {
    "strength": "СИЛ",
    "dexterity": "ЛОВ",
    "constitution": "ТЕЛ",
    "intelligence": "ИНТ",
    "wisdom": "МУД",
    "charisma": "ХАР",
}

STAT_ORDER = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]


def _mod_str(score: int) -> str:
    m = modifier(score)
    return f"+{m}" if m >= 0 else str(m)


def _list(items: list[str] | None, indent: str = "- ") -> str:
    if not items:
        return "_Нет_\n"
    return "".join(f"{indent}{item}\n" for item in items)


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}"


def format_character_md(data: dict) -> str:
    """Генерирует полный .md лист персонажа из JSON."""
    lines: list[str] = []

    # ── Заголовок ──
    name = data.get("name", "Безымянный")
    lines.append(f"# {name}")
    lines.append("")

    race = data.get("race", "?")
    subrace = data.get("subrace", "")
    race_str = f"{race} ({subrace})" if subrace else race

    cls = data.get("class", "?")
    subcls = data.get("subclass", "")
    cls_str = f"{cls} ({subcls})" if subcls else cls
    level = data.get("level", 1)

    lines.append(f"**Раса:** {race_str}")
    lines.append(f"**Класс:** {cls_str}, {level} уровень")
    lines.append(f"**Предыстория:** {data.get('background', '?')}")
    lines.append(f"**Мировоззрение:** {data.get('alignment', '?')}")
    if data.get("gender"):
        lines.append(f"**Пол:** {data['gender']}")
    lines.append("")
    lines.append("---")

    # ── Характеристики ──
    lines.append(_section("Характеристики", ""))
    lines.append("")
    header = "| " + " | ".join(STAT_NAMES[s] for s in STAT_ORDER) + " |"
    sep = "|" + "|:---:|" * 6 + "|"
    vals = "| " + " | ".join(
        f"{data.get(s, 10)} ({_mod_str(data.get(s, 10))})" for s in STAT_ORDER
    ) + " |"
    lines.append(header)
    lines.append(sep)
    lines.append(vals)
    lines.append("")
    lines.append("---")

    # ── Производные значения ──
    derived_lines = [
        f"**HP:** {data.get('hp_max', '?')}",
        f"**AC:** {data.get('ac', '?')}",
        f"**Скорость:** {data.get('speed', '?')} фт.",
        f"**Инициатива:** {_mod_str(data.get('dexterity', 10))}",
        f"**Бонус мастерства:** +{data.get('proficiency_bonus', 2)}",
    ]
    lines.append(_section("Производные значения", "\n".join(f"- {l}" for l in derived_lines)))
    lines.append("---")

    # ── Спасброски ──
    saves = data.get("saving_throws", [])
    if saves:
        lines.append(_section("Спасброски", _list(saves)))

    # ── Навыки ──
    skills = data.get("skills", [])
    if skills:
        lines.append(_section("Навыки", _list(skills)))

    # ── Черты и способности ──
    features = data.get("features_and_traits", [])
    if features:
        lines.append(_section("Черты и способности", _list(features)))

    # ── Снаряжение ──
    equip = data.get("equipment", [])
    weapons = data.get("weapons", [])
    if equip or weapons:
        body = ""
        if weapons:
            body += "**Оружие:**\n" + _list(weapons)
        if data.get("armor"):
            body += f"**Броня:** {data['armor']}\n"
        if equip:
            body += "**Снаряжение:**\n" + _list(equip)
        gold = data.get("gold", 0)
        if gold:
            body += f"**Золото:** {gold} зм\n"
        lines.append(_section("Снаряжение", body))

    # ── Заклинания ──
    spells = data.get("spells", [])
    if spells:
        body = ""
        if data.get("spell_slots"):
            body += f"**Слоты заклинаний:** {data['spell_slots']}\n\n"
        body += _list(spells)
        lines.append(_section("Заклинания", body))

    # ── Внешность ──
    if data.get("appearance"):
        lines.append(_section("Внешность", data["appearance"]))

    # ── Личность ──
    personality_parts = []
    for key, label in [
        ("personality_traits", "Черты характера"),
        ("ideals", "Идеалы"),
        ("bonds", "Привязанности"),
        ("flaws", "Слабости"),
    ]:
        val = data.get(key, "")
        if val:
            personality_parts.append(f"**{label}:** {val}")
    if personality_parts:
        lines.append(_section("Личность", "\n".join(f"- {p}" for p in personality_parts)))

    # ── Предыстория ──
    if data.get("backstory"):
        lines.append(_section("Предыстория", data["backstory"]))

    return "\n".join(lines)
