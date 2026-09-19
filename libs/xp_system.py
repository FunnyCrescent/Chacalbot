"""Система опыта (XP) — ИТЕРАЦИЯ 15.

Единый источник правды по ТЗ пользователя:
  1. Пороги уровней (накопленный суммарный XP).
  2. XP за монстров по CR.
  3. XP за социальные действия (по сложности и уровню).
  4. XP за квесты (сложность квеста → та же таблица, что для социалки).
  5. Пороги XP по уровням для социалки/квестов.
  6. Правила начисления (делёж за монстров, индивидуальные за социалку/квесты,
     без тривиальных/повторных действий, только участникам, мгновенный level-up).

XP — СКРЫТАЯ характеристика: игроки её не видят; видят только Мастер-нейросеть
(через progression summary / контекст) и DB-бот (через db_state / get_character_state).
Модуль ничего не знает о БД и Telegram — только таблицы, чистые функции и
генерация текстового блока для промптов ({{XP_SYSTEM}} в MD/prompts/master.md).
"""
from fractions import Fraction
from typing import Dict, Optional, Union

# ───────────────────────────────────────────────────────────────
# 1. Пороги уровней (накопленный суммарный XP)
# ───────────────────────────────────────────────────────────────

LEVEL_XP_THRESHOLDS: Dict[int, int] = {
    1: 0, 2: 300, 3: 900, 4: 2700, 5: 6500,
    6: 14000, 7: 23000, 8: 34000, 9: 48000, 10: 64000,
    11: 85000, 12: 100000, 13: 120000, 14: 140000, 15: 165000,
    16: 195000, 17: 225000, 18: 265000, 19: 305000, 20: 355000,
}

MIN_LEVEL = min(LEVEL_XP_THRESHOLDS)   # 1
MAX_LEVEL = max(LEVEL_XP_THRESHOLDS)   # 20

# ───────────────────────────────────────────────────────────────
# 2. XP за монстров по CR
# ───────────────────────────────────────────────────────────────

MONSTER_XP_BY_CR: Dict[str, int] = {
    "0": 10, "1/8": 25, "1/4": 50, "1/2": 100,
    "1": 200, "2": 450, "3": 700, "4": 1100, "5": 1800, "6": 2300,
    "7": 2900, "8": 3900, "9": 5000, "10": 5900, "11": 7200, "12": 8400,
    "13": 10000, "14": 11500, "15": 13000, "16": 15000,
    "17": 18000, "18": 20000, "19": 22000, "20": 25000,
    "21": 33000, "22": 41000, "23": 50000, "24": 62000, "25": 75000,
    "26": 90000, "27": 105000, "28": 120000, "29": 135000, "30": 155000,
}

# ───────────────────────────────────────────────────────────────
# 5. Пороги XP по уровням для социалки и квестов (на персонажа)
# ───────────────────────────────────────────────────────────────

DIFFICULTY_XP_BY_LEVEL: Dict[int, Dict[str, int]] = {
    1:  {"easy": 25, "medium": 50, "hard": 75, "deadly": 100},
    2:  {"easy": 50, "medium": 100, "hard": 150, "deadly": 200},
    3:  {"easy": 75, "medium": 150, "hard": 225, "deadly": 400},
    4:  {"easy": 125, "medium": 250, "hard": 375, "deadly": 500},
    5:  {"easy": 250, "medium": 500, "hard": 750, "deadly": 1100},
    6:  {"easy": 300, "medium": 600, "hard": 900, "deadly": 1400},
    7:  {"easy": 350, "medium": 750, "hard": 1100, "deadly": 1700},
    8:  {"easy": 450, "medium": 900, "hard": 1400, "deadly": 2100},
    9:  {"easy": 550, "medium": 1100, "hard": 1600, "deadly": 2400},
    10: {"easy": 600, "medium": 1200, "hard": 1900, "deadly": 2800},
    11: {"easy": 800, "medium": 1600, "hard": 2400, "deadly": 3600},
    12: {"easy": 1000, "medium": 2000, "hard": 3000, "deadly": 4500},
    13: {"easy": 1100, "medium": 2200, "hard": 3400, "deadly": 5100},
    14: {"easy": 1250, "medium": 2500, "hard": 3800, "deadly": 5700},
    15: {"easy": 1400, "medium": 2800, "hard": 4300, "deadly": 6400},
    16: {"easy": 1600, "medium": 3200, "hard": 4800, "deadly": 7200},
    17: {"easy": 2000, "medium": 3900, "hard": 5900, "deadly": 8800},
    18: {"easy": 2100, "medium": 4200, "hard": 6300, "deadly": 9500},
    19: {"easy": 2400, "medium": 4900, "hard": 7300, "deadly": 10900},
    20: {"easy": 2800, "medium": 5700, "hard": 8500, "deadly": 12700},
}

# Русские/английские алиасы сложностей → канонический ключ
DIFFICULTY_ALIASES: Dict[str, str] = {
    "легкая": "easy", "лёгкая": "easy", "easy": "easy",
    "средняя": "medium", "medium": "medium",
    "сложная": "hard", "hard": "hard",
    "смертельная": "deadly", "deadly": "deadly",
}


def normalize_level(level: int) -> int:
    """Уровень в диапазоне [1..20]."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return MIN_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, level))


def level_for_xp(xp: int) -> int:
    """Уровень по накопленному суммарному XP (наивысший порог <= xp)."""
    try:
        xp = int(xp)
    except (TypeError, ValueError):
        return MIN_LEVEL
    if xp < 0:
        xp = 0
    level = MIN_LEVEL
    for lvl in sorted(LEVEL_XP_THRESHOLDS):
        if xp >= LEVEL_XP_THRESHOLDS[lvl]:
            level = lvl
    return level


def next_level_threshold(level: int) -> Optional[int]:
    """XP-порог следующего уровня (None — если уже максимум, 20-й)."""
    level = normalize_level(level)
    if level >= MAX_LEVEL:
        return None
    return LEVEL_XP_THRESHOLDS[level + 1]


def monster_xp(cr: Union[str, int, float, None]) -> Optional[int]:
    """XP за существо по CR ('1/4', '1/2', 3, '30'...). None — неизвестный CR."""
    if cr is None:
        return None
    key = str(cr).strip().replace(",", ".")
    if key in MONSTER_XP_BY_CR:
        return MONSTER_XP_BY_CR[key]
    # Дробный/числовой CR, которого нет в таблице — ближайший снизу из известных
    try:
        value = float(Fraction(key))
    except (ValueError, ZeroDivisionError):
        return None
    best = None
    best_val = -1.0
    for known, xp in MONSTER_XP_BY_CR.items():
        try:
            known_val = float(Fraction(known))
        except (ValueError, ZeroDivisionError):
            continue
        if known_val <= value and known_val > best_val:
            best_val, best = known_val, xp
    return best


def difficulty_award(level: int, difficulty: str) -> Optional[int]:
    """XP на персонажа за социалку/квест по уровню и сложности (RU/EN)."""
    level = normalize_level(level)
    key = DIFFICULTY_ALIASES.get(str(difficulty or "").strip().lower())
    if not key:
        return None
    return DIFFICULTY_XP_BY_LEVEL[level][key]


def average_party_level(levels) -> int:
    """Средний уровень группы (по ТЗ: если уровни разные — средний, всем одинаково).
    Округление обычное математическое (0.5 → вверх)."""
    clean = [normalize_level(l) for l in (levels or []) if l is not None]
    if not clean:
        return MIN_LEVEL
    avg = sum(clean) / len(clean)
    return normalize_level(int(avg + 0.5))


def split_monster_xp(total_xp: int, participants: int) -> int:
    """Делёж XP за бой поровну (округление вниз)."""
    if participants <= 0:
        return 0
    return max(0, int(total_xp)) // int(participants)


# ───────────────────────────────────────────────────────────────
# Текстовый блок для промптов ({{XP_SYSTEM}} в MD/prompts/*.md)
# ───────────────────────────────────────────────────────────────

_SOCIAL_TABLE_LINES = "\n".join(
    f"Уровень {lvl}: Легкая {v['easy']} / Средняя {v['medium']} / "
    f"Сложная {v['hard']} / Смертельная {v['deadly']}"
    for lvl, v in sorted(DIFFICULTY_XP_BY_LEVEL.items())
)

_MONSTER_LINES_1 = " | ".join(f"CR {cr} → {xp}" for cr, xp in list(MONSTER_XP_BY_CR.items())[:16])
_MONSTER_LINES_2 = " | ".join(f"CR {cr} → {xp}" for cr, xp in list(MONSTER_XP_BY_CR.items())[16:])

_LEVEL_LINES_1 = " | ".join(f"ур. {l}: {xp}" for l, xp in sorted(LEVEL_XP_THRESHOLDS.items())[:10])
_LEVEL_LINES_2 = " | ".join(f"ур. {l}: {xp}" for l, xp in sorted(LEVEL_XP_THRESHOLDS.items())[10:])


def build_prompt_block() -> str:
    """Полный блок правил XP для вставки в промпты ({{XP_SYSTEM}}).

    Генерируется из таблиц выше — числа в промпте и в коде всегда совпадают.
    """
    return f"""### 1. ПОРОГИ УРОВНЕЙ (накопленный суммарный XP)
{_LEVEL_LINES_1}
{_LEVEL_LINES_2}

### 2. ОПЫТ ЗА МОНСТРОВ (за одно побеждённое существо)
{_MONSTER_LINES_1}
{_MONSTER_LINES_2}

### 3. ОПЫТ ЗА СОЦИАЛЬНЫЕ ДЕЙСТВИЯ
Оцени сложность социальной сцены как боевое столкновение:
- Легкая — мелкая просьба, флирт, запугивание слабого NPC.
- Средняя — убедить стражника пропустить, выторговать скидку, разузнать слух.
- Сложная — заключить союз с враждебной фракцией, переубедить фанатика.
- Смертельная — остановить войну речью, склонить на свою сторону архимага.

### 4. ОПЫТ ЗА КВЕСТЫ
Мелкий квест = Легкая сложность. Средний квест = Средняя.
Крупный квест = Сложная. Сюжетный квест = Смертельная. XP — сразу по завершении квеста.

### 5. XP ЗА СОЦИАЛКУ/КВЕСТ (на персонажа, по уровню)
{_SOCIAL_TABLE_LINES}

### 6. ПРАВИЛА НАЧИСЛЕНИЯ (строго)
- XP за монстров: сумма XP всех побеждённых существ ДЕЛИТСЯ ПОРОВНУ на всех участников боя (округление вниз).
- XP за социальные действия и квесты: КАЖДОМУ персонажу индивидуально, НЕ делится.
- НЕ начисляй XP за тривиальные или повторные действия.
- За проваленное социальное действие XP НЕ начисляется.
- Если персонаж не участвовал в сцене или бою — он НЕ получает XP.
- Если уровни в группе разные (социалка/квесты) — используй средний уровень группы и давай всем одинаково.
- Уровень повышается АВТОМАТИЧЕСКИ сразу, как только накопленный XP достигает порога из раздела 1 (это делает БД при award_xp).

### 7. КАК ВЫВОДИТЬ НАЧИСЛЕНИЕ (формат — ТОЛЬКО в блоке СВОДКА)
XP — СКРЫТАЯ характеристика: ИГРОКИ НЕ ВИДЯТ XP. Никогда не называй цифры опыта в видимом нарративе — пиши начисление СТРОКАМИ ВНУТРИ блока СВОДКА (блок удаляется перед отправкой игрокам):
- XP: <имя персонажа> +<N> (<источник>)
- Если одинаково всем участникам сцены: XP: всем участникам +<N> (<источник>)
Примеры:
- XP: Эйра +200 (гоблин CR 1)
- XP: всем участникам +100 (гоблин CR 1, делённый на двоих)
- XP: Кейн +500 (убедили стражника — Средняя, уровень 5)
- XP: всем участникам +750 (квест «Пропавший караван» завершён — Средний)
Если ничего не даёт XP — строк XP в СВОДКЕ не пиши. DB-бот применит начисления инструментом award_xp."""
