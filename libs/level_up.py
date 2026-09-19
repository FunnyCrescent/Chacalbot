"""Автоматическое повышение уровня (ИТЕРАЦИЯ 16).

Связывает систему опыта (libs/xp_system.py) с полной выдачей выгод уровня:
как только персонаж получает новый уровень (через award_xp по XP или через
level_up_character за сюжетную веху), система:
  1. АВТОМАТИЧЕСКИ применяет детерминированную часть: HP (средний бросок кости
     хитов + модификатор Телосложения за каждый gained-уровень), умения класса
     из таблицы SRD 5e, пересчёт бонуса мастерства (следует из уровня).
  2. Считает по таблицам SRD ВЫБОРЫ, которые должен оформить Мастер
     (заклинания, ASI/черта, подкласс, встраивания/таинства) — «не больше и
     не меньше» — и создаёт СКРЫТЫЙ бриф (GM_SECRET) + отложенное напоминание
     (pending_level_ups в БД), которое висит в контексте Мастера, пока он не
     передаст выборы строками «УРОВЕНЬ+: ...» в СВОДКЕ и не закроет повышение
     строкой «УРОВЕНЬ+ ГОТОВО: <имя>» (DB-бот применит их инструментами).

Модуль ничего не знает о БД и Telegram — только таблицы SRD 5e (2014 PHB),
чистые функции и генерация текстовых блоков ({{LEVEL_UP_SYSTEM}} в промптах).
"""
import re
from typing import Dict, List, Optional, Tuple

# ───────────────────────────────────────────────────────────────
# Классы: нормализация имён (RU/EN) + отображаемые названия
# ───────────────────────────────────────────────────────────────

CLASS_KEYS = [
    "barbarian", "bard", "cleric", "druid", "fighter", "monk",
    "paladin", "ranger", "rogue", "sorcerer", "warlock", "wizard",
]

# Канонические RU-названия (официальный перевод PHB) + английские
_CLASS_DISPLAY_RU = {
    "barbarian": "Варвар", "bard": "Бард", "cleric": "Жрец", "druid": "Друид",
    "fighter": "Воин", "monk": "Монах", "paladin": "Паладин",
    "ranger": "Следопыт", "rogue": "Плут", "sorcerer": "Чародей",
    "warlock": "Колдун", "wizard": "Волшебник",
}

_CLASS_ALIASES: Dict[str, str] = {
    # barbarian
    "barbarian": "barbarian", "варвар": "barbarian", "варвара": "barbarian",
    # bard
    "bard": "bard", "бард": "bard", "барда": "bard",
    # cleric
    "cleric": "cleric", "жрец": "cleric", "клирик": "cleric", "жрица": "cleric",
    # druid
    "druid": "druid", "друид": "druid", "друида": "druid",
    # fighter
    "fighter": "fighter", "воин": "fighter", "боец": "fighter",
    # monk
    "monk": "monk", "монах": "monk", "монахиня": "monk",
    # paladin
    "paladin": "paladin", "паладин": "paladin", "паладина": "paladin",
    # ranger
    "ranger": "ranger", "следопыт": "ranger", "рейнджер": "ranger",
    # rogue
    "rogue": "rogue", "плут": "rogue", "вор": "rogue",
    # sorcerer
    "sorcerer": "sorcerer", "чародей": "sorcerer", "чародейка": "sorcerer",
    # warlock
    "warlock": "warlock", "колдун": "warlock", "колдунья": "warlock",
    # wizard
    "wizard": "wizard", "волшебник": "wizard", "волшебница": "wizard",
    "маг": "wizard", "магус": "wizard",
}


def extract_subclass(class_name: str) -> Tuple[str, str]:
    """Разобрать 'Воин (Чемпион)' → ('Воин', 'Чемпион').

    Возвращает (class_name_без_скобок, subclass_или_'').
    """
    raw = (class_name or "").strip()
    m = re.search(r"[((](.+?)[))]", raw)
    if not m:
        return raw, ""
    subclass = m.group(1).strip()
    base = (raw[:m.start()].strip() + " " + raw[m.end():].strip()).strip()
    return base, subclass


def normalize_class_name(class_name: str) -> Optional[str]:
    """Имя класса (любой язык/падеж/с подклассом в скобках) → ключ 'wizard' и т.п.

    None — класс не распознан (план строится по обобщённым правилам).
    """
    if not class_name:
        return None
    base, _ = extract_subclass(class_name)
    candidates = [base, (class_name or "").strip()]
    for cand in candidates:
        low = cand.lower().strip()
        if low in _CLASS_ALIASES:
            return _CLASS_ALIASES[low]
        # подстрочный фолбэк: «уровень 5 воин», «Wizard (Evocation) 7» и т.п.
        for alias, key in _CLASS_ALIASES.items():
            if len(alias) >= 4 and alias in low:
                return key
    return None


def class_display_ru(class_key: str) -> str:
    return _CLASS_DISPLAY_RU.get(class_key, class_key.capitalize() if class_key else "—")


# ───────────────────────────────────────────────────────────────
# Общие таблицы прогрессии (SRD 5e, 2014 PHB)
# ───────────────────────────────────────────────────────────────

def proficiency_bonus(level: int) -> int:
    """Бонус мастерства: +2 (ур. 1-4), +3 (5-8), +4 (9-12), +5 (13-16), +6 (17-20)."""
    level = max(1, min(20, int(level or 1)))
    return 2 + (level - 1) // 4


HIT_DIE_BY_CLASS: Dict[str, int] = {
    "barbarian": 12, "bard": 8, "cleric": 8, "druid": 8, "fighter": 10,
    "monk": 8, "paladin": 10, "ranger": 10, "rogue": 8, "sorcerer": 6,
    "warlock": 8, "wizard": 6,
}


def hit_die_average(die: int) -> int:
    """Фиксированное среднее кости хитов (округление вверх, PHB): d6→4, d8→5, d10→6, d12→7."""
    return die // 2 + 1


# Ячейки заклинаний. Индекс = уровень персонажа (0-й не используется).
FULL_CASTER_SLOTS: List[List[int]] = [
    [],                       # 0
    [2],                      # 1
    [3],                      # 2
    [4, 2],                   # 3
    [4, 3],                   # 4
    [4, 3, 2],                # 5
    [4, 3, 3],                # 6
    [4, 3, 3, 1],             # 7
    [4, 3, 3, 2],             # 8
    [4, 3, 3, 3, 1],          # 9
    [4, 3, 3, 3, 2],          # 10
    [4, 3, 3, 3, 2, 1],       # 11
    [4, 3, 3, 3, 2, 1],       # 12
    [4, 3, 3, 3, 2, 1, 1],    # 13
    [4, 3, 3, 3, 2, 1, 1],    # 14
    [4, 3, 3, 3, 2, 1, 1, 1],  # 15
    [4, 3, 3, 3, 2, 1, 1, 1],  # 16
    [4, 3, 3, 3, 2, 1, 1, 1, 1],  # 17
    [4, 3, 3, 3, 3, 1, 1, 1, 1],  # 18
    [4, 3, 3, 3, 3, 2, 1, 1, 1],  # 19
    [4, 3, 3, 3, 3, 2, 2, 1, 1],  # 20
]

HALF_CASTER_SLOTS: List[List[int]] = [
    [],        # 0
    [],        # 1
    [2],       # 2
    [3],       # 3
    [3],       # 4
    [4, 2],    # 5
    [4, 2],    # 6
    [4, 3],    # 7
    [4, 3],    # 8
    [4, 3, 2],  # 9
    [4, 3, 2],  # 10
    [4, 3, 3],  # 11
    [4, 3, 3],  # 12
    [4, 3, 3, 1],  # 13
    [4, 3, 3, 1],  # 14
    [4, 3, 3, 2],  # 15
    [4, 3, 3, 2],  # 16
    [4, 3, 3, 3, 1],  # 17
    [4, 3, 3, 3, 1],  # 18
    [4, 3, 3, 3, 2],  # 19
    [4, 3, 3, 3, 2],  # 20
]


def warlock_pact_slots(level: int) -> Tuple[int, int]:
    """Pact Magic: (количество ячеек, круг ячейки)."""
    level = max(1, min(20, int(level or 1)))
    if level == 1:
        return (1, 1)
    if level == 2:
        return (2, 1)
    if level <= 4:
        return (2, 2)
    if level <= 6:
        return (2, 3)
    if level <= 8:
        return (2, 4)
    if level <= 10:
        return (2, 5)
    if level <= 16:
        return (3, 5)
    return (4, 5)


# Заговоры по уровням персонажа (уровень → прирост). Заданы порогами:
CANTRIP_PROGRESSION: Dict[str, Dict[int, int]] = {
    "bard": {1: 2, 4: 3, 10: 4},
    "cleric": {1: 3, 4: 4, 10: 5},
    "druid": {1: 2, 4: 3, 10: 4},
    "sorcerer": {1: 4, 4: 5, 10: 6},
    "warlock": {1: 2, 4: 3, 10: 4},
    "wizard": {1: 3, 4: 4, 10: 5},
}

# Известные заклинания (known) по уровням; список длиной 20, индекс = уровень.
_SPELLS_KNOWN: Dict[str, List[int]] = {
    # bard: PHB-таблица Bard (4,5,6,7,9,10,11,12,14,15,16,18,19,20,22,23,24,25,26,27)
    "bard": [0, 4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 16, 18, 19, 20, 22, 23, 24, 25, 26, 27],
    # sorcerer: +1 заклинание за уровень (2..21)
    "sorcerer": [0] + list(range(2, 22)),
    # ranger: известные с уровня 2
    "ranger": [0, 0, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11],
    # warlock: известные заклинания Pact Magic
    "warlock": [0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 10, 11, 11, 12, 12, 13, 13, 14, 14, 15, 15],
}

# Встраивания (Eldritch Invocations) — пороги количества: уровень → прирост
INVOCATION_GAIN: Dict[int, int] = {2: 2, 5: 1, 7: 1, 9: 1, 12: 1, 15: 1, 17: 1}
# Mystic Arcanum: уровень персонажа → круг заклинания
ARCANUM_GAIN: Dict[int, int] = {11: 6, 13: 7, 15: 8, 17: 9}

# Тип спелкастинга
CASTER_KIND: Dict[str, str] = {
    "bard": "known", "sorcerer": "known", "ranger": "known", "warlock": "pact",
    "cleric": "prepared", "druid": "prepared", "paladin": "prepared",
    "wizard": "book",       # книга заклинаний +2 заклинания за уровень, готовит ИНТ+уровень
    "barbarian": "none", "fighter": "none", "monk": "none", "rogue": "none",
}

SPELLCASTING_ABILITY: Dict[str, str] = {
    "bard": "харизма", "cleric": "мудрость", "druid": "мудрость",
    "paladin": "харизма", "ranger": "мудрость", "sorcerer": "харизма",
    "warlock": "харизма", "wizard": "интеллект",
}

# ───────────────────────────────────────────────────────────────
# Умения классов по уровням (SRD 5e, 2014 PHB)
# kind: "class" — добавляется автоматически; "subclass" — уровень подкласса
# ───────────────────────────────────────────────────────────────

CLASS_FEATURES: Dict[str, Dict[int, List[str]]] = {
    "barbarian": {
        1: ["Ярость", "Защита без доспехов"],
        2: ["Безрассудная атака", "Чутьё на опасность"],
        5: ["Дополнительная атака", "Быстрое передвижение"],
        7: ["Дикий инстинкт"],
        9: ["Свирепая критическая атака"],
        11: ["Неистовая ярость"],
        13: ["Свирепая критическая атака (2 кости)"],
        15: ["Стойкая ярость"],
        17: ["Свирепая критическая атака (3 кости)"],
        18: ["Непоколебимая мощь"],
        20: ["Первобытный чемпион"],
    },
    "bard": {
        1: ["Заклинания", "Вдохновение барда"],
        2: ["Мастер на все руки", "Песня отдыха"],
        3: ["Экспертность"],
        5: ["Источник вдохновения", "Вдохновение барда (d8)"],
        6: ["Контрзаклинание (Countercharm)"],
        9: ["Песня отдыха (d8)", "Экспертность (2)"],
        10: ["Магические тайны", "Вдохновение барда (d10)"],
        13: ["Песня отдыха (d10)"],
        14: ["Магические тайны (2)"],
        15: ["Вдохновение барда (d12)"],
        17: ["Песня отдыха (d12)"],
        18: ["Магические тайны (3)"],
        20: ["Высшее вдохновение"],
    },
    "cleric": {
        1: ["Заклинания", "Божественная область"],
        2: ["Божественный канал", "Изгнание нежити"],
        5: ["Сокрушение нежити (CR 1/2)"],
        6: ["Божественный канал (2/отдых)"],
        8: ["Сокрушение нежити (CR 1)"],
        10: ["Божественное вмешательство"],
        11: ["Сокрушение нежити (CR 2)"],
        14: ["Сокрушение нежити (CR 3)"],
        17: ["Сокрушение нежити (CR 4)"],
        18: ["Сокрушение нежити (CR 5)", "Божественный канал (3/отдых)"],
        20: ["Божественное вмешательство (улучшенное)"],
    },
    "druid": {
        1: ["Заклинания", "Друидический язык"],
        2: ["Дикий облик", "Друидический круг"],
        18: ["Нетленное тело", "Заклинания зверя"],
        20: ["Архидруид"],
    },
    "fighter": {
        1: ["Боевой стиль", "Второе дыхание"],
        2: ["Нападение"],
        5: ["Дополнительная атака"],
        9: ["Непоколебимость"],
        11: ["Дополнительная атака (2)"],
        13: ["Непоколебимость (2/отдых)"],
        17: ["Нападение (2/отдых)", "Непоколебимость (3/отдых)"],
        20: ["Дополнительная атака (3)"],
    },
    "monk": {
        1: ["Защита без доспехов", "Боевые искусства"],
        2: ["Ци", "Безоружное движение", "Град ударов", "Терпеливая защита", "Шаг ветра"],
        3: ["Отклонение снарядов"],
        4: ["Медленное падение"],
        5: ["Дополнительная атака", "Ошеломляющий удар"],
        6: ["Ци-усиленные удары"],
        7: ["Уклонение", "Покой разума"],
        9: ["Безоружное движение (улучшение)"],
        10: ["Чистота тела"],
        13: ["Язык солнца и луны"],
        14: ["Алмазная душа"],
        15: ["Нетленное тело"],
        18: ["Пустое тело"],
        20: ["Совершенное «я»"],
    },
    "paladin": {
        1: ["Божественное чувство", "Наложение рук"],
        2: ["Боевой стиль", "Заклинания", "Божественная кара"],
        3: ["Божественное здоровье", "Священная клятва", "Божественный канал"],
        5: ["Дополнительная атака"],
        6: ["Аура защиты"],
        10: ["Аура отваги"],
        11: ["Улучшенная божественная кара"],
        14: ["Очищающее прикосновение"],
        18: ["Ауры (радиус 30 футов)"],
    },
    "ranger": {
        1: ["Избранный враг", "Исследователь природы"],
        2: ["Боевой стиль", "Заклинания"],
        3: ["Первобытное чутьё"],
        5: ["Дополнительная атака"],
        6: ["Избранный враг (улучшение)", "Исследователь природы (улучшение)"],
        8: ["Уверенный шаг"],
        10: ["Сокрытие на виду", "Исследователь природы (улучшение 2)"],
        14: ["Исчезновение", "Избранный враг (улучшение 2)"],
        18: ["Избранный враг (улучшение 3)"],
        20: ["Гибель врага"],
    },
    "rogue": {
        1: ["Скрытая атака", "Экспертность", "Воровской жаргон"],
        2: ["Хитрое действие"],
        5: ["Нечестное уклонение"],
        6: ["Экспертность (2)"],
        7: ["Уклонение"],
        11: ["Надёжный талант"],
        14: ["Чутьё к слепым зонам"],
        15: ["Скользкий разум"],
        18: ["Неуловимый"],
        20: ["Удача плута"],
    },
    "sorcerer": {
        1: ["Заклинания", "Чародейское происхождение"],
        2: ["Источник магии"],
        3: ["Метамагия"],
        10: ["Метамагия (3-я опция)"],
        17: ["Метамагия (4-я опция)"],
        20: ["Чародейское восстановление"],
    },
    "warlock": {
        1: ["Договорная магия (Pact Magic)", "Иноземный патрон"],
        2: ["Элдрические встраивания"],
        3: ["Дар пакта"],
        11: ["Таинство мистики (6-й круг)"],
        13: ["Таинство мистики (7-й круг)"],
        15: ["Таинство мистики (8-й круг)"],
        17: ["Таинство мистики (9-й круг)"],
        20: ["Элдрический хозяин"],
    },
    "wizard": {
        1: ["Заклинания", "Магическое восстановление"],
        18: ["Владение заклинанием"],
        20: ["Фирменные заклинания"],
    },
}

# Уровни, на которых берётся умение подкласса (архетипа)
SUBCLASS_LEVELS: Dict[str, List[int]] = {
    "barbarian": [3, 6, 10, 14],
    "bard": [3, 6, 14],
    "cleric": [1, 2, 6, 8, 17],
    "druid": [2, 6, 10, 14],
    "fighter": [3, 7, 10, 15, 18],
    "monk": [3, 6, 11, 17],
    "paladin": [3, 7, 15, 20],
    "ranger": [3, 7, 11, 15],
    "rogue": [3, 9, 13, 17],
    "sorcerer": [1, 6, 14, 18],
    "warlock": [1, 6, 10, 14],
    "wizard": [2, 6, 10, 14],
}

# Уровни ASI (увеличение характеристик)
DEFAULT_ASI_LEVELS = [4, 8, 12, 16, 19]
ASI_LEVELS: Dict[str, List[int]] = {
    "fighter": [4, 6, 8, 12, 14, 16, 19],
    "rogue": [4, 8, 10, 12, 16, 19],
}
for _k in CLASS_KEYS:
    ASI_LEVELS.setdefault(_k, DEFAULT_ASI_LEVELS)

# Русские названия характеристик для строк «УРОВЕНЬ+ ... характеристика: ...»
ABILITY_NAMES_RU = {
    "сила": "strength", "ловкость": "dexterity", "телосложение": "constitution",
    "интеллект": "intelligence", "мудрость": "wisdom", "харизма": "charisma",
}


# ───────────────────────────────────────────────────────────────
# Генератор плана повышения уровня
# ───────────────────────────────────────────────────────────────

def _cantrips_gained(class_key: str, old_level: int, new_level: int) -> int:
    """Прирост числа известных заговоров в диапазоне (old, new].

    CANTRIP_PROGRESSION хранит СУММАРНЫЕ количества на пороговых уровнях
    ({1: 3, 4: 4, 10: 5} — «3 заговора с 1-го, 4-й с уровня 4, 5-й с 10-го»),
    поэтому на уровне действует наибольший порог <= уровня.
    """
    prog = CANTRIP_PROGRESSION.get(class_key)
    if not prog:
        return 0
    def known_at(level: int) -> int:
        best = 0
        for lvl, total in prog.items():
            if 1 <= lvl <= level and total > best:
                best = total
        return best
    return max(0, known_at(new_level) - known_at(old_level))


def _known_spells_gained(class_key: str, old_level: int, new_level: int) -> int:
    table = _SPELLS_KNOWN.get(class_key)
    if not table:
        return 0
    old = table[min(old_level, 20)] if 0 <= old_level <= 20 else 0
    new = table[min(new_level, 20)] if 0 <= new_level <= 20 else old
    return max(0, new - old)


def _invocations_gained(old_level: int, new_level: int) -> int:
    return sum(cnt for lvl, cnt in INVOCATION_GAIN.items() if old_level < lvl <= new_level)


def _arcanum_gained(old_level: int, new_level: int) -> List[int]:
    return [circle for lvl, circle in sorted(ARCANUM_GAIN.items()) if old_level < lvl <= new_level]


def _slot_circles(class_key: str, level: int) -> int:
    """Сколько кругов заклинаний доступно на уровне (0 — нет ячеек)."""
    kind = CASTER_KIND.get(class_key, "none")
    if kind == "pact":
        return warlock_pact_slots(level)[1]
    if kind in ("known", "prepared", "book"):
        table = FULL_CASTER_SLOTS if class_key in ("bard", "cleric", "druid", "sorcerer", "wizard") else HALF_CASTER_SLOTS
        lvl = max(0, min(20, level))
        return len(table[lvl])
    return 0


def build_level_up_plan(class_name: str, old_level: int, new_level: int,
                        con_mod: int = 0, subclass: str = "") -> Dict:
    """Полный план повышения уровня по таблицам SRD 5e (2014).

    class_name: как записан в БД («Воин (Чемпион)», «волшебник»...).
    con_mod: модификатор Телосложения (для прироста HP).
    subclass: подкласс, если известен («Чемпион»).

    Возвращает dict с auto-частью (HP, умения) и choices-частью (выборы мастера).
    """
    old_level = max(1, min(20, int(old_level or 1)))
    new_level = max(1, min(20, int(new_level or 1)))
    levels_gained = list(range(old_level + 1, new_level + 1))
    class_key = normalize_class_name(class_name) or ""
    subclass = (subclass or "").strip()
    con_mod = int(con_mod or 0)

    plan: Dict = {
        "class_key": class_key,
        "class_display": class_display_ru(class_key) if class_key else (class_name or "—"),
        "subclass": subclass,
        "levels_gained": levels_gained,
        "old_level": old_level,
        "new_level": new_level,
        "hit_die": HIT_DIE_BY_CLASS.get(class_key) if class_key else None,
        "hp_total": 0,
        "hp_detail": [],
        "prof_before": proficiency_bonus(old_level),
        "prof_after": proficiency_bonus(new_level),
        "auto_features": [],
        "subclass_feature_slots": [],   # уровни подкласса в диапазоне
        "subclass_pick": False,
        "asi_levels": [],
        "asi_count": 0,
        "cantrips_gained": 0,
        "spells_gained": 0,
        "spell_kind": CASTER_KIND.get(class_key, "none") if class_key else "none",
        "spellcasting_ability": SPELLCASTING_ABILITY.get(class_key, ""),
        "new_circle": 0,
        "circle_opened": 0,
        "invocations_gained": 0,
        "arcanum_gained": [],
        "wizard_book_gained": 0,
        "choices": [],
    }
    if not levels_gained:
        return plan

    # 1. HP: средний бросок кости + модификатор Телосложения за каждый уровень
    die = plan["hit_die"]
    if die:
        per_level = hit_die_average(die) + con_mod
        plan["hp_per_level"] = per_level
        plan["hp_total"] = max(0, per_level * len(levels_gained))
        plan["hp_detail"] = [
            f"{'+' if per_level >= 0 else ''}{per_level} (d{die}: {hit_die_average(die)} + ТЕЛ{con_mod:+d})"
            for _ in levels_gained
        ]

    # 2. Умения класса (автоматически) + уровни подкласса
    for lvl in levels_gained:
        for name in CLASS_FEATURES.get(class_key, {}).get(lvl, []):
            plan["auto_features"].append({"level": lvl, "name": name})
        if lvl in SUBCLASS_LEVELS.get(class_key, []):
            plan["subclass_feature_slots"].append(lvl)

    # 3. ASI
    plan["asi_levels"] = [l for l in levels_gained if l in ASI_LEVELS.get(class_key, [])]
    plan["asi_count"] = len(plan["asi_levels"])

    # 4. Заклинания
    kind = plan["spell_kind"]
    plan["cantrips_gained"] = _cantrips_gained(class_key, old_level, new_level)
    plan["new_circle"] = _slot_circles(class_key, new_level)
    plan["circle_opened"] = max(0, plan["new_circle"] - _slot_circles(class_key, old_level))
    if kind == "known":
        plan["spells_gained"] = _known_spells_gained(class_key, old_level, new_level)
    elif kind == "book":
        plan["wizard_book_gained"] = 2 * len(levels_gained)
        plan["spells_gained"] = plan["wizard_book_gained"]
    elif kind == "pact":
        plan["spells_gained"] = _known_spells_gained(class_key, old_level, new_level)
        plan["invocations_gained"] = _invocations_gained(old_level, new_level)
        plan["arcanum_gained"] = _arcanum_gained(old_level, new_level)

    # 5. Выборы мастера (строки «УРОВЕНЬ+: ...»)
    if plan["cantrips_gained"]:
        plan["choices"].append({
            "kind": "cantrip", "count": plan["cantrips_gained"],
            "hint": f"заговор(ы) — строки «заклинание:»",
        })
    if plan["spells_gained"]:
        if kind == "book":
            hint = "заклинания в книгу заклинаний — строки «заклинание:»"
        else:
            hint = "заклинания — строки «заклинание:»"
        if plan["circle_opened"] and plan["new_circle"]:
            hint += f", можно до {plan['new_circle']}-го круга включительно"
        plan["choices"].append({
            "kind": "spells", "count": plan["spells_gained"], "hint": hint,
        })
    if plan["invocations_gained"]:
        plan["choices"].append({
            "kind": "invocations", "count": plan["invocations_gained"],
            "hint": "встраивание(я) — строки «черта:»",
        })
    if plan["arcanum_gained"]:
        plan["choices"].append({
            "kind": "arcanum", "count": len(plan["arcanum_gained"]),
            "hint": ("таинство мистики: " +
                     ", ".join(f"{c}-й круг" for c in plan["arcanum_gained"]) +
                     " — строки «заклинание:»"),
        })
    if plan["asi_count"]:
        plan["choices"].append({
            "kind": "asi", "count": plan["asi_count"],
            "hint": ("увеличение характеристик: +2 очка — либо одна строка "
                     "«характеристика: <название> +2», либо две строки «+1»; "
                     "вместо этого можно взять черту — строка «черта:»"),
        })

    # 6. Подкласс
    if plan["subclass_feature_slots"]:
        if not subclass and any(SUBCLASS_LEVELS.get(class_key, [])[0] == l for l in plan["subclass_feature_slots"]):
            plan["subclass_pick"] = True
        plan["choices"].append({
            "kind": "subclass", "count": len(plan["subclass_feature_slots"]),
            "hint": (f"умение(я) подкласса за уровень(и) {plan['subclass_feature_slots']} — "
                     f"строки «черта: <умение подкласса>» (по таблице подкласса; "
                     f"на первом уровне подкласса обычно 1–2 умения)"),
        })

    return plan


# ───────────────────────────────────────────────────────────────
# Бриф для мастера (GM_SECRET) и блок напоминания
# ───────────────────────────────────────────────────────────────

_CHOICE_LINES = {
    "cantrip": "заклинание: <заговор>",
    "spells": "заклинание: <название>",
    "invocations": "черта: <встраивание>",
    "arcanum": "заклинание: <таинство>",
    "asi": "характеристика: <сила|ловкость|телосложение|интеллект|мудрость|харизма> +<N>",
    "subclass": "черта: <умение подкласса>",
}


def format_level_up_brief(plan: Dict, char_name: str) -> str:
    """Скрытый бриф Мастеру: что применено автоматически + что он ДОЛЖЕН оформить."""
    name = char_name or "?"
    old = plan.get("old_level", 1)
    new = plan.get("new_level", 1)
    lines = [f"[УРОВЕНЬ] {name}: уровень {old} → {new}."]

    auto = []
    if plan.get("hp_total"):
        detail = plan["hp_detail"][0] if plan.get("hp_detail") else ""
        if len(plan.get("levels_gained", [])) > 1:
            auto.append(f"HP +{plan['hp_total']} ({len(plan['levels_gained'])} уровня × {detail})")
        else:
            auto.append(f"HP +{plan['hp_total']} ({detail})")
    if plan.get("prof_after") != plan.get("prof_before"):
        auto.append(f"бонус мастерства +{plan['prof_after']}")
    feats = [f["name"] for f in plan.get("auto_features", [])]
    if feats:
        auto.append("умения класса: " + ", ".join(feats))
    if plan.get("circle_opened"):
        auto.append(f"доступен {plan['new_circle']}-й круг заклинаний")
    if plan.get("spell_kind") == "prepared":
        auto.append(f"пул подготовленных заклинаний вырос автоматически ({plan.get('spellcasting_ability','')} + уровень)")

    if auto:
        lines.append("ПРИМЕНЕНО АВТОМАТИЧЕСКИ (не дублируй): " + "; ".join(auto) + ".")

    choices = plan.get("choices", [])
    if choices:
        lines.append("ТЫ ДОЛЖЕН ОФОРМИТЬ (ровно по списку — не больше и не меньше) строками в СВОДКЕ:")
        for ch in choices:
            fmt = _CHOICE_LINES.get(ch["kind"], "...")
            cnt = ch["count"]
            if ch["kind"] == "asi":
                lvls = ", ".join(str(l) for l in plan.get("asi_levels", [])) or "?"
                lines.append(
                    f"  • ASI за уровень(и) {lvls}: распредели РОВНО +2 очка характеристик — "
                    f"либо одна строка «УРОВЕНЬ+: {name} характеристика: <название> +2», "
                    f"либо две строки «+1»; вместо ASI можно взять черту — одна строка «черта:»."
                )
            else:
                lines.append(f"  • ровно {cnt} × «УРОВЕНЬ+: {name} {fmt}» — {ch['hint']}")
        lines.append(f"После применения ВСЕГО добавь строку: «УРОВЕНЬ+ ГОТОВО: {name}».")
    else:
        lines.append("Выборов нет — всё применено автоматически. "
                     f"Объяви новый уровень в нарративе и добавь строку «УРОВЕНЬ+ ГОТОВО: {name}».")
    return "\n".join(lines)


def build_prompt_block() -> str:
    """Блок правил оформления повышения уровня для промптов ({{LEVEL_UP_SYSTEM}})."""
    return """### ПОВЫШЕНИЕ УРОВНЯ — АВТОМАТИКА (ровно по брифу)
Уровень повышается автоматически при достижении порога XP (award_xp) или за сюжетную веху
(level_up_character). Система сразу применяет детерминированную часть по таблицам SRD 5e:
HP (средняя кость хитов + Телосложение за уровень), умения класса, бонус мастерства, круги ячеек.
Мастеру приходит скрытый бриф [УРОВЕНЬ] (и блок «⬆️ НЕОФОРМЛЕННЫЕ ПОВЫШЕНИЯ» в контексте).

Формат строк выборов — ВНУТРИ блока СВОДКА (игроки его не видят), по строке на каждый выбор:
- УРОВЕНЬ+: <имя> заклинание: <точное название заклинания или заговора>
- УРОВЕНЬ+: <имя> навык: <название владения>
- УРОВЕНЬ+: <имя> черта: <название черты / встраивания / умения подкласса>
- УРОВЕНЬ+: <имя> характеристика: <сила|ловкость|телосложение|интеллект|мудрость|харизма> +<N>
- УРОВЕНЬ+ ГОТОВО: <имя>   ← последняя строка, когда ВСЁ из брифа передано

СТРОГО:
1. Количество строк должно совпадать с брифом: «ровно 2 × заклинание» = 2 строки, ни больше ни меньше.
2. HP и умения класса из брифа УЖЕ применены системой — НЕ дублируй их строками.
3. Не выдумывай выгоды, которых нет в брифе. Не оформляй повышение, которого не было (метагейминг).
4. В нарративе объяви новый уровень игрокам (это МОЖНО показывать) и дай игроку сделать выборы.
5. Если бриф требует подкласс — предложи игроку выбрать, умения подкласса передай строками «черта:».
6. Напоминание о неоформленном повышении приходит каждый раунд, пока не придёт строка ГОТОВО."""
