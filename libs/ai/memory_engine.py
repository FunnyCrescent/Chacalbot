"""MemoryEngineMixin — summarization + SRD lookup + world extractors."""
import json
import logging
import asyncio
import random
import re
from typing import Dict, List, Optional, Any, Callable, Awaitable

from libs.character_parser import ParsedCharacter
from .tools import (
    ROLL_TYPE_ENUM, DICE_TOOLS, GAME_TOOLS, ALL_TOOLS,
    COMBAT_TOOLS, MASTER_TOOLS, GET_TOOLS, DB_TOOLS, DB_WRITE_TOOL_NAMES,
)
from .prompts import (
    MASTER_PROMPT, ASK_PROMPT, RENDERER_PROMPT, NPC_AI_PROMPT, DB_BOT_PROMPT,
)
from .utils import strip_stray_tags

logger = logging.getLogger(__name__)


class MemoryEngineMixin:
    """MemoryEngineMixin — summarization + SRD lookup + world extractors."""

    async def summarize(self, text: str) -> str:
        # Trim to avoid OpenRouter 400 on oversized payloads
        MAX_CHARS = 15000
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n...[truncated]"
        messages = [{
            "role": "user",
            "content": f"Создай детальную сводку кампании D&D для Dungeon Master. Сохрани ВСЕ важные детали.\n\nИстория:\n{text}",
        }]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Summarize error: {e}")
            return ""


    async def srd_lookup(self, query: str) -> str:
        srd_prompt = f"""Ты - справочник по правилам D&D 5e (2024). Ответь кратко и точно.
Если вопрос про конкретное заклинание, класс, способность - приведи основные цифры.
Вопрос: {query}"""
        messages = [{"role": "user", "content": srd_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "[Нет данных]")
        except Exception as e:
            logger.error(f"SRD error: {e}")
            return f"*[Ошибка SRD: {e}]*"


    async def generate_weather(self, season: str, terrain: str = "", current_weather: str = "") -> str:
        w_prompt = f"""Опиши погоду для D&D в 2-3 предложения.
Сезон: {season} | Местность: {terrain or "открытая"} | Предыдущая: {current_weather or "ясно"}
Укажи: погоду, температуру, видимость, эффект на игру. Отвечай на русском."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "Ясная погода, умеренная температура.")
        except Exception as e:
            logger.error(f"Weather error: {e}")
            return "Ясная погода, умеренная температура."


    async def generate_world_event(self, context: str) -> str:
        w_prompt = f"""Придумай событие в мире D&D, которое происходит НЕЗАВИСИМО от игроков.
Контекст: {context}
Формат: **Событие**, **Описание**, **Влияние**. Не более 100 слов."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.memory.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"World event error: {e}")
            return ""


    async def extract_currency_from_world(self, world_text: str) -> dict:
        """Extract currency system from generated world lore. Returns dict with
        name, symbol, sub_name, sub_symbol, sub_value, super_name, super_symbol, super_value
        or empty dict if no custom currency found."""
        if not world_text:
            return {}
        prompt = f"""Прочитай описание мира D&D и определи основную валюту (деньги), которая используется в этом мире.

Текст мира:
---
{world_text[:6000]}
---

Если валюта НЕ стандартная (не золотые монеты/gp), извлеки:
- name: название основной валюты (например "Серебро", "Медные гроши", "Кроны")
- symbol: краткий символ (например "см", "гр", "кр")
- sub_name: название более мелкой валюты (опционально)
- sub_symbol: символ мелкой валюты
- sub_value: курс (сколько единиц мелкой = 1 основной, например 0.1 значит 10 мелких = 1 основная)
- super_name: название более дорогой валюты (опционально)
- super_symbol: символ дорогой валюты
- super_value: курс (1 дорогая = X основных)

Если валюта стандартная (золото/серебро/медь) или не упомянута явно — верни {{"standard": true}}.
Если упомянута кастомная валюта — верни JSON-объект с полями.

Ответь СТРОГО JSON, без markdown и пояснений."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.memory.chat(messages, max_tokens=500)
            raw = response["choices"][0]["message"].get("content", "{}").strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            if not raw:
                return {}
            data = json.loads(raw)
            if data.get("standard"):
                return {}  # standard currency, no custom extraction needed
            if data.get("name"):
                return data
            return {}
        except Exception as e:
            logger.error(f"extract_currency_from_world error: {e}")
            return {}


    async def extract_quest_hooks(self, character_briefs: List[str]) -> List[Dict]:
        """F3: pull explicit goals out of character backstories so they land in /quest
        automatically instead of being silently lost. Returns a list of
        {"title": str, "description": str, "character_brief": str} — the caller matches
        character_brief back to a real character/player to fill assignee fields."""
        if not character_briefs:
            return []
        prompt = f"""Прочитай краткие биографии персонажей D&D и вытащи ЯВНО заявленные личные цели/квесты
(месть, поиск кого-то, служение фракции, обещание и т.д.) — только то, что реально написано, не выдумывай.

Биографии:
{chr(10).join(f"- {b}" for b in character_briefs)}

Ответь СТРОГО JSON-массивом, без markdown и пояснений, в формате:
[{{"title": "Короткое название цели", "description": "1-2 предложения", "character_brief": "точная строка биографии, из которой это взято"}}]

Если ни у кого нет явной цели — верни []."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.memory.chat(messages, max_tokens=1500)
            raw = response["choices"][0]["message"].get("content", "[]").strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"extract_quest_hooks error: {e}")
            return []


    async def extract_all_npcs_from_lore(self, lore_text: str) -> List[Dict]:
        """A guaranteed, exhaustive pass over the /dndstart-generated world lore text to
        pull out EVERY named NPC mentioned — not just the ones the DB-Bot's tool-calling
        loop happened to create (that loop has a limited iteration budget and picks what
        it judges 'significant', so named NPCs buried deep in a long lore text could be
        silently skipped). Returns a list of dicts:
          {"name": str, "race": str, "occupation": str, "location_name": str,
           "personality": str, "backstory": str}
        Only pulls NPCs actually named in the text — never invents new ones."""
        if not lore_text or not lore_text.strip():
            return []
        prompt = f"""Текст лора мира (сгенерирован для кампании D&D):
---
{lore_text[:12000]}
---

Найди КАЖДОГО именованного NPC, упомянутого в этом тексте (лидеры фракций, правители,
наставники, ключевые фигуры — любой, у кого есть собственное имя). Не пропускай тех,
кто упомянут только один раз мимоходом. Не выдумывай NPC, которых нет в тексте.

Ответь СТРОГО JSON-массивом, без markdown, без пояснений. Формат каждого элемента:
{{"name": "...", "race": "...", "occupation": "...", "location_name": "...", "personality": "1-2 слова", "backstory": "1-2 предложения из текста"}}

Поля race/occupation/location_name/personality/backstory можно оставить пустой строкой,
если в тексте об этом ничего не сказано. Если NPC вообще нет — верни []."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.memory.chat(messages, max_tokens=4000)
            raw = response["choices"][0]["message"].get("content", "[]").strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"extract_all_npcs_from_lore error: {e}")
            return []


    async def extract_backstory_relations(self, character_briefs: List[str], npc_names: List[str],
                                          location_names: List[str]) -> List[Dict]:
        """After world generation (NPCs/locations already exist in DB), pull out any
        relations that should ALREADY exist given what the character backstories say —
        e.g. an NPC who already knows the PC, or a city that already thinks of them a
        certain way. Without this, /npc and /city stay empty on session start even when
        the generated lore text clearly establishes a pre-existing relationship.
        Returns a list of dicts, each one of:
          {"type": "npc", "npc_name": str, "character_name": str, "attitude": str,
           "known_fact": str, "reputation_delta": int}
          {"type": "location", "location_name": str, "character_name": str,
           "fame_delta": int, "reputation_delta": int, "is_wanted": bool, "notoriety_note": str}
        Only relations with clear grounding in the backstories are returned — never invented."""
        if not character_briefs or not (npc_names or location_names):
            return []
        prompt = f"""Биографии персонажей:
{chr(10).join(f"- {b}" for b in character_briefs)}

NPC, существующие в этом мире: {', '.join(npc_names) if npc_names else '(нет)'}
Локации, существующие в этом мире: {', '.join(location_names) if location_names else '(нет)'}

Найди ЯВНЫЕ связи между персонажами (по их биографиям) и уже существующими NPC/локациями —
то, что NPC уже знает о персонаже, или что локация (город) уже думает/знает о персонаже
(слава, розыск, репутация) — ТОЛЬКО если это прямо следует из текста биографии. Не выдумывай
связей, которых нет в тексте.

Ответь СТРОГО JSON-массивом, без markdown, без пояснений. Каждый элемент — один из форматов:
{{"type": "npc", "npc_name": "...", "character_name": "...", "attitude": "friendly/helpful/neutral/unfriendly/hostile", "known_fact": "...", "reputation_delta": 0}}
{{"type": "location", "location_name": "...", "character_name": "...", "fame_delta": 0, "reputation_delta": 0, "is_wanted": false, "notoriety_note": "..."}}

Если ничего явного нет — верни []."""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.memory.chat(messages, max_tokens=1500)
            raw = response["choices"][0]["message"].get("content", "[]").strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"extract_backstory_relations error: {e}")
            return []

    # ═══════════════════════════════════════════════════════════
    # CHARACTER SHEET PARSING - Llama 4 (DB-Bot)
    # ═══════════════════════════════════════════════════════════


