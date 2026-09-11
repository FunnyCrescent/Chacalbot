"""ManualRollsMixin — /roll command + pending roll registry."""
import json
import logging
import uuid
import asyncio
import random
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from libs.ai_client import DMEngine, strip_stray_tags
from libs.db import (
    Database, DatabaseManager, HistoryEntry, Player, QueueState, Session,
    Location, LocationPath, WorldNpc, NpcRelation, LoreArticle,
    MarketPrice, EconomicEvent, ActiveEffect, Timer, LocationRelation,
    CombatEncounter, Combatant, PlayerLanguage,
)
from libs.config_legacy import (
    COMBAT_INITIATIVE_ENABLED, DUAL_NARRATIVE_ENABLED,
    NPC_AI_ENABLED, NPC_AI_MODE, MAX_MANUAL_ROLLS_PER_ROUND,
)
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# ИТЕРАЦИЯ 8 (Баг 2): детерминированный серверный резолвер /rholio.
# Раньше бросок ПОЛНОСТЬЮ зависел от LLM: модель должна была вызвать
# инструмент roll_dice с посчитанным модификатором. Локальные модели
# (Gemma через LM Studio/Ollama/vLLM) часто не отдают tool-calls или
# ломают JSON — /rholio падал с «DB-Bot не смог определить модификатор».
# Теперь стандартные навыки/спасброски считаются СТРОГО на сервере из
# данных персонажа в БД (stats/proficiencies/level) — мгновенно, без
# токенов, с любым бэкендом. LLM остаётся fallback'ом для экзотики.
# ═══════════════════════════════════════════════════════════════

_ABILITY_STEMS = ("сил", "ловк", "тел", "интел", "мудр", "хар")

# Навык → характеристика (RU/EN, поиск по корню подстрокой)
_SKILL_STEMS: List[Tuple[Tuple[str, ...], str]] = [
    (("атлетик", "athletic"), "strength"),
    (("акробатик", "acrobatic"), "dexterity"),
    (("ловкость рук", "sleight of hand"), "dexterity"),
    (("скрытн", "stealth"), "dexterity"),
    (("анализ", "расследован", "investigat"), "intelligence"),
    (("истор", "histor"), "intelligence"),
    (("маг", "arcan"), "intelligence"),
    (("природ", "nature"), "intelligence"),
    (("религ", "religio"), "intelligence"),
    (("обращение с животными", "animal handling"), "wisdom"),
    (("проницательн", "insight"), "wisdom"),
    (("медицин", "medicin"), "wisdom"),
    (("внимательн", "perception"), "wisdom"),
    (("выживан", "survival"), "wisdom"),
    (("обман", "decept"), "charisma"),
    (("запугиван", "intimidat"), "charisma"),
    (("выступлен", "performanc"), "charisma"),
    (("убежден", "persuas"), "charisma"),
]

# Спасброски: корни генитивных форм («спасбросок ловкости», «спас Тела»)
_SAVE_STEMS: List[Tuple[Tuple[str, ...], str]] = [
    (("сил", "strength"), "strength"),
    (("ловк", "dexterity"), "dexterity"),
    (("тел", "constitut"), "constitution"),
    (("интел", "intellig"), "intelligence"),
    (("мудр", "wisdom"), "wisdom"),
    (("хар", "charism"), "charisma"),
]

# ИТЕРАЦИЯ 8: для сверки ВЛАДЕНИЙ в листе нужны ДЛИННЫЕ однозначные корни.
# Короткие стемы разбора запроса («тел», «сил») ложно матчатся внутри других
# слов листа: «внимательность» содержит «тел» → ложное владение спасброском
# Телосложения. Поэтому сверка владений — только по этим:
_SAVE_PROF_STEMS: Dict[str, Tuple[str, ...]] = {
    "strength": ("сила", "силе", "силы", "strength"),
    "dexterity": ("ловкост", "dexterity"),
    "constitution": ("телослож", "constitut"),
    "intelligence": ("интеллект", "intelligen"),
    "wisdom": ("мудрост", "wisdom"),
    "charisma": ("харизм", "charism"),
}

_ADV_RE = re.compile(r"преимущ|advantage|с преим", re.IGNORECASE)
_DIS_RE = re.compile(r"помех|disadvantage|с помех", re.IGNORECASE)


def _match_ability_stems(text: str, stems: List[Tuple[Tuple[str, ...], str]]) -> Optional[str]:
    """Find an ability by stems. Longest stem wins («ловкость рук» бьёт «ловк»)."""
    best, best_len = None, 0
    for variants, ability in stems:
        for stem in variants:
            if stem in text and len(stem) > best_len:
                best, best_len = ability, len(stem)
    return best


def _skill_ability(check_norm: str) -> Optional[str]:
    return _match_ability_stems(check_norm, _SKILL_STEMS)


def _save_ability(check_norm: str) -> Optional[str]:
    stripped = re.sub(r"спасбросок|спас|saving throw|\bsave\b", " ", check_norm)
    return _match_ability_stems(stripped, _SAVE_STEMS)


class ManualRollsMixin:
    """ManualRollsMixin — /roll command + pending roll registry."""

    def _add_pending_manual_roll(self, session_id: str, player_id: int, check_name: str, display: str):
        """Store a verified roll result. Also tracks the skill name for duplicate/limit checks."""
        self._pending_manual_rolls.setdefault(session_id, {}).setdefault(player_id, []).append(display)
        self._pending_manual_roll_skills.setdefault(session_id, {}).setdefault(player_id, []).append(check_name.lower().strip())

    def _get_manual_roll_count(self, session_id: str, player_id: int) -> int:
        """How many rolls this player has submitted this round."""
        return len(self._pending_manual_roll_skills.get(session_id, {}).get(player_id, []))

    def _has_skill_roll(self, session_id: str, player_id: int, check_name: str) -> bool:
        """Has this player already rolled this exact skill this round?"""
        normalized = check_name.lower().strip()
        return normalized in self._pending_manual_roll_skills.get(session_id, {}).get(player_id, [])

    def _pop_pending_manual_rolls(self, session_id: str) -> Dict[int, List[str]]:
        """Consume (and clear) all pending /roll results for this session."""
        self._pending_manual_roll_skills.pop(session_id, None)
        return self._pending_manual_rolls.pop(session_id, {})

    # ── Детерминированный резолвер (Баг 2) ──

    def _deterministic_manual_roll(self, char, check_name: str) -> Optional[Dict]:
        """Server-side verified d20 for standard skills/saves from DB data.
        Returns the same shape as the LLM path, or None if the check name is
        not a recognizable standard skill/save (then the LLM fallback runs)."""
        check_norm = check_name.lower().strip()
        check_norm = re.sub(r"^(проверк[аи]|проба|check)\s+", "", check_norm).strip()
        if not check_norm:
            return None

        # Спасбросок распознаётся ТОЛЬКО по явному слову «спас…»/«save» в
        # запросе. Раньше короткие стемы применялись к ЛЮБОМУ тексту —
        # «внимательность» содержала «тел» и детектировалась как спасбросок
        # Телосложения (с ложным владением из других строк листа).
        save_hit = re.search(r"спасбросок|\bспас\b|saving throw|\bsave\b", check_norm)
        ability = _match_ability_stems(
            re.sub(r"спасбросок|спас|saving throw|\bsave\b", " ", check_norm),
            _SAVE_STEMS,
        ) if save_hit else None
        if ability is None:
            ability = _match_ability_stems(check_norm, _SKILL_STEMS)
        if ability is None:
            return None

        score = getattr(char, ability, 10) or 10
        mod = (score - 10) // 2

        # Владения/экспертиза: подстрочный матч по ОДНОЗНАЧНЫМ корням в списке листа
        # (длинные стемы — см. комментарий у _SAVE_PROF_STEMS).
        try:
            profs = json.loads(char.proficiencies) if char.proficiencies else []
        except Exception:
            profs = []
        if not isinstance(profs, list):
            profs = []
        profs_lower = [str(p).lower() for p in profs if str(p).strip()]

        if save_hit:
            key_stems = _SAVE_PROF_STEMS.get(ability, (ability,))
        else:
            key_stems = [stem for stems, ab in _SKILL_STEMS if ab == ability for stem in stems]

        def _entry_matches(entry: str) -> bool:
            return any(stem in entry for stem in key_stems)

        is_proficient = any(_entry_matches(p) for p in profs_lower)
        has_expertise = any(
            _entry_matches(p) and ("экспертиз" in p or "expertise" in p)
            for p in profs_lower
        )

        pb = 2 + (char.level - 1) // 4 if char.level else 2
        modifier = mod
        prof_note = ""
        if has_expertise:
            modifier += pb * 2
            prof_note = " (экспертиза)"
        elif is_proficient:
            modifier += pb
            prof_note = f" (владение +{pb})"

        is_save = check_norm.startswith("спас") or "saving" in check_norm or "спасбросок" in check_norm
        kind = "Спасбросок" if is_save else "Проверка"

        roll_type = "normal"
        if _ADV_RE.search(check_name):
            roll_type = "advantage"
        elif _DIS_RE.search(check_name):
            roll_type = "disadvantage"

        if roll_type in ("advantage", "disadvantage"):
            r1, r2 = random.randint(1, 20), random.randint(1, 20)
            natural = max(r1, r2) if roll_type == "advantage" else min(r1, r2)
            roll_str = f"[{r1},{r2}]→{natural}"
            adv_label = " (преимущество)" if roll_type == "advantage" else " (помеха)"
        else:
            natural = random.randint(1, 20)
            roll_str = str(natural)
            adv_label = ""

        total = natural + modifier
        mod_str = f"{modifier:+d}" if modifier else ""
        crit = ""
        if natural == 20:
            crit = " 💥 КРИТИЧЕСКИЙ УСПЕХ!"
        elif natural == 1:
            crit = " 💀 КРИТИЧЕСКИЙ ПРОВАЛ!"

        ability_ru = {
            "strength": "Сила", "dexterity": "Ловкость", "constitution": "Телосложение",
            "intelligence": "Интеллект", "wisdom": "Мудрость", "charisma": "Харизма",
        }.get(ability, ability)

        display = (
            f"🎲 {char.name} — {kind} {ability_ru}{adv_label}{prof_note}: "
            f"1d20{adv_label}{mod_str} → [{roll_str}]{mod_str} = **{total}**{crit}"
        )
        return {
            "ok": True,
            "display": display,
            "natural": natural,
            "total": total,
            "source": "server",
        }

    async def resolve_manual_roll(self, session_id: str, player_id: int, check_name: str) -> Dict:
        """Handle /roll with MAX_MANUAL_ROLLS_PER_ROUND limit and duplicate skill check.
        Итерация 8: стандартные навыки/спасброски резолвятся детерминированно на
        сервере (работает с ЛЮБОЙ моделью, включая локальную Gemma без tool-calls);
        LLM — только fallback для нестандартных формулировок."""
        db = self.db_manager.get_db(session_id)
        char = db.get_character_by_player(player_id, session_id)
        if not char:
            return {"ok": False, "error": "У тебя нет персонажа в этой сессии."}

        current_count = self._get_manual_roll_count(session_id, player_id)
        if current_count >= MAX_MANUAL_ROLLS_PER_ROUND:
            return {"ok": False, "error": f"Лимит бросков: {MAX_MANUAL_ROLLS_PER_ROUND} за раунд. Ты уже кинул {current_count}."}

        if self._has_skill_roll(session_id, player_id, check_name):
            existing_skills = self._pending_manual_roll_skills.get(session_id, {}).get(player_id, [])
            return {"ok": False, "error": f"Ты уже кидал «{check_name}» в этом раунде. Каждая характеристика — только один бросок.\nУже кинул: {', '.join(existing_skills)}"}

        # 1) Детерминированный серверный бросок — мгновенно, без LLM.
        deterministic = self._deterministic_manual_roll(char, check_name)
        if deterministic is not None:
            result = deterministic
        else:
            # 2) Экзотика → LLM fallback (как раньше, но с защитой JSON).
            result = await self._llm_manual_roll(char, check_name, db, session_id, player_id)
            if not result.get("ok"):
                return result

        self._add_pending_manual_roll(session_id, player_id, check_name, f"{check_name} — {result['display']}")
        new_count = self._get_manual_roll_count(session_id, player_id)
        remaining = MAX_MANUAL_ROLLS_PER_ROUND - new_count
        result["remaining_rolls"] = remaining
        result["roll_number"] = new_count
        return result

    async def _llm_manual_roll(self, char, check_name: str, db, session_id: str, player_id: int) -> Dict:
        """Legacy LLM path for non-standard checks (kept as fallback)."""
        sheet_text = db.get_character_sheet(session_id, player_id) or "(лист не загружен, используй только базовые правила)"
        progression_text = db.get_character_progression_summary(char.id)

        prompt = f"""Персонаж {char.name} запрашивает бросок: "{check_name}".

Сырой лист персонажа (для деталей вроде экспертизы/особых бонусов):
---
{sheet_text}
---

АКТУАЛЬНОЕ состояние из базы данных (приоритетнее листа — персонаж мог повыситься в уровне, получить владения после регистрации):
---
{progression_text}
---

Определи: какая характеристика используется, владеет ли персонаж этим навыком/спасброском (тогда прибавляется бонус мастерства, посчитай его по уровню: +2 на 1-4 ур., +3 на 5-8, +4 на 9-12, +5 на 13-16, +6 на 17-20), есть ли expertise (двойной бонус мастерства) или иные явные модификаторы из листа.
Вызови ОДИН раз инструмент roll_dice: count=1, sides=20, modifier=<посчитанный модификатор>, label="{char.name} — {check_name}", visible=true.
Если "{check_name}" не является валидной характеристикой/навыком/спасброском D&D 5e — не вызывай инструмент, вместо этого объясни проблему текстом."""

        messages = [{"role": "user", "content": prompt}]
        try:
            from libs.ai.tools import DICE_TOOLS
            response = await self.dm.db_bot.chat(messages, system_prompt=None, tools=DICE_TOOLS, tool_choice="auto")
            choice = response["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")
            if not tool_calls:
                return {"ok": False, "error": choice.get("content") or "Не удалось определить модификатор для такого броска."}

            for tc in tool_calls:
                if tc["function"]["name"] != "roll_dice":
                    continue
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(args, dict):
                    continue
                result = self.dm._execute_roll(args)
                logger.info(f"[MANUAL ROLL /roll LLM] {char.name} — {check_name}: {result['result']}")
                return {
                    "ok": True,
                    "display": result["display"],
                    "natural": result["natural"],
                    "total": result["total"],
                }
            return {"ok": False, "error": "Не удалось выполнить бросок: некорректный ответ модели."}
        except Exception as e:
            logger.error(f"resolve_manual_roll LLM error: {e}")
            return {"ok": False, "error": str(e)}
