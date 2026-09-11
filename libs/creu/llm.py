"""
LLM-клиент для creu-модуля.
Использует aiohttp для совместимости с существующим ботом.
Поддерживает OpenAI и OpenRouter (любой совместимый API).

Два типа вызовов:
1. get_options() — лёгкий запрос, возвращает список строк для кнопок
2. generate_character() — тяжёлый запрос, возвращает полный JSON персонажа
"""

import json
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

from libs.config_legacy import (
    OPENAI_API_KEY as LLM_API_KEY,
    OPENAI_BASE_URL as LLM_BASE_URL,
    DB_MODEL as CREU_MODEL,
)
from libs.proxy_helper import aiohttp_session_kwargs, aiohttp_request_kwargs

# ── Промпты для получения списков опций ────────────────────────────
OPTIONS_SYSTEM = (
    "Ты — эксперт по D&D 5e. Отвечай ТОЛЬКО валидным JSON-массивом строк."
    "Без пояснений, без markdown-обёрток, без нумерации."
    "Пример: [\"Эльф\", \"Дварф\", \"Человек\"]"
)

# ── Промпт для генерации полного персонажа ────────────────────────
CHARACTER_SYSTEM = """Ты — мастер создания персонажей Dungeons & Dragons 5e.
Твоя задача — по собранным данным игрока и брошенным характеристикам
сгенерировать ПОЛНЫЙ лист персонажа.

ПРАВИЛА:
1. Строго следуй правилам D&D 5e (PHB + указанная книга источника).
2. HP считай по формуле класса + модификатор Телосложения.
3. AC — 10 + модификатор Ловкости (без брони) или по броне.
4. Бонус мастерства = +2 на 1-4 уровне, +3 на 5-8, +4 на 9-12, +5 на 13-16, +6 на 17-20.
5. Навыки и спасброски соответствуют классу и предыстории.
6. Снаряжение соответствует классу и уровню.
7. Если заклинатель — укажи заклинания соответствующего уровня со слотами.
8. Черты и способности — все классовые и расовые фичи на указанном уровне.
9. Все тексты на РУССКОМ (кроме имён собственных и названий D&D терминов).
10. Заполни КАЖДОЕ поле JSON-схемы.

Ты ДОЛЖЕН вернуть ТОЛЬКО валидный JSON (без markdown, без комментариев):
{
  "name": "строка",
  "race": "строка",
  "subrace": "строка или пустая",
  "class": "строка",
  "subclass": "строка или пустая",
  "level": число,
  "background": "строка",
  "alignment": "строка (формат D&D: LG, CN и т.д.)",
  "gender": "строка",
  "strength": число,
  "dexterity": число,
  "constitution": число,
  "intelligence": число,
  "wisdom": число,
  "charisma": число,
  "hp_max": число,
  "ac": число,
  "speed": число,
  "proficiency_bonus": число,
  "initiative": число,
  "saving_throws": ["строка", ...],
  "skills": ["строка", ...],
  "features_and_traits": ["строка", ...],
  "equipment": ["строка", ...],
  "weapons": ["строка", ...],
  "armor": "строка",
  "gold": число,
  "spells": ["строка", ...],
  "spell_slots": "строка",
  "appearance": "строка (2-3 предложения)",
  "personality_traits": "строка",
  "ideals": "строка",
  "bonds": "строка",
  "flaws": "строка",
  "backstory": "строка (3-4 предложения)"
}"""


def _parse_options(raw: str) -> list[str]:
    text = raw.strip()
    # Чистый JSON
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return [str(x).strip() for x in items if str(x).strip()][:15]
    except json.JSONDecodeError:
        pass
    # JSON в markdown-блоке
    if "```" in text:
        block = text.split("```")[1]
        block = re.sub(r"^json\s*", "", block, flags=re.IGNORECASE)
        try:
            items = json.loads(block.strip())
            if isinstance(items, list):
                return [str(x).strip() for x in items if str(x).strip()][:15]
        except json.JSONDecodeError:
            pass
    # Фоллбэк: нумерованный/маркированный список
    lines = text.strip().split("\n")
    out: list[str] = []
    for line in lines:
        cleaned = re.sub(r'^[\d\.\-\*\)\]]+\s*', "", line).strip()
        if cleaned and len(cleaned) < 60 and not cleaned.startswith(("#", "//", "```")):
            out.append(cleaned)
        if len(out) >= 15:
            break
    return out


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


async def chat_completion(
    messages: list[dict],
    *,
    model: str = CREU_MODEL,
    temperature: float = 0.7,
    max_tokens: int = 1000,
    response_format: dict | None = None,
    api_key: str = LLM_API_KEY,
    base_url: str = LLM_BASE_URL,
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
            **aiohttp_request_kwargs(),
        ) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(f"LLM error: {data['error']}")
            return data["choices"][0]["message"]["content"]


# ═══════════════════════════════════════════════════════════════
# Публичный API
# ═══════════════════════════════════════════════════════════════

async def get_options(prompt: str, **kwargs) -> list[str]:
    """Лёгкий вызов — возвращает список строк для inline-кнопок."""
    try:
        raw = await chat_completion(
            messages=[
                {"role": "system", "content": OPTIONS_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
            **kwargs,
        )
        return _parse_options(raw)
    except Exception as e:
        logger.warning(f"[creu] get_options failed: {e}")
        return []


async def has_subraces(race: str, **kwargs) -> list[str]:
    """Возвращает список подрас, или пустой список если их нет."""
    return await get_options(
        f"Есть ли у расы \"{race}\" в D&D 5e подрасы? "
        "Если да — перечисли их. Если нет — верни пустой массив [].",
        **kwargs,
    )


async def get_races(book: str, **kwargs) -> list[str]:
    return await get_options(
        f"Перечисли все играбельные расы из книги \"{book}\" в D&D 5e.",
        **kwargs,
    )


async def get_classes(**kwargs) -> list[str]:
    return await get_options(
        "Перечисли все классы персонажей из D&D 5e (Player's Handbook и основных дополнений).",
        **kwargs,
    )


async def get_subclasses(class_name: str, **kwargs) -> list[str]:
    return await get_options(
        f"Перечисли все подклассы (архетипы) для класса \"{class_name}\" в D&D 5e.",
        **kwargs,
    )


async def get_backgrounds(**kwargs) -> list[str]:
    return await get_options(
        "Перечисли все стандартные предыстории (backgrounds) из D&D 5e.",
        **kwargs,
    )


async def generate_character(collected: dict, **kwargs) -> dict:
    """Тяжёлый вызов — генерация полного JSON персонажа."""
    user_prompt = (
        f"Создай полный лист персонажа D&D 5e со следующими параметрами:\n\n"
        f"- Книга источника: {collected.get('source_book', 'Player\'s Handbook')}\n"
        f"- Раса: {collected.get('race', '?')}\n"
    )
    if collected.get("subrace"):
        user_prompt += f"- Подраса: {collected['subrace']}\n"
    user_prompt += (
        f"- Класс: {collected.get('class_', '?')}\n"
    )
    if collected.get("subclass"):
        user_prompt += f"- Подкласс: {collected['subclass']}\n"
    user_prompt += (
        f"- Уровень: {collected.get('level', 1)}\n"
        f"- Пол: {collected.get('gender', '?')}\n"
        f"- Брошенные характеристики (распредели их оптимально для класса): {collected.get('rolled_stats', [])}\n"
        f"- Предыстория: {collected.get('background', '?')}\n"
        f"- Мировоззрение: {collected.get('alignment', '?')}\n"
    )
    if collected.get("details"):
        user_prompt += f"\nДополнительные пожелания игрока: {collected['details']}\n"

    raw = await chat_completion(
        messages=[
            {"role": "system", "content": CHARACTER_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
        max_tokens=4000,
        response_format={"type": "json_object"},
        **kwargs,
    )
    return _parse_json(raw)
