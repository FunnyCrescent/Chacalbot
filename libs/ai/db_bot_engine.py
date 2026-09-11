"""DBBotEngineMixin — DB sub-agent (reads narrative, queues write actions)."""
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
    get_prompt,
)
from .utils import strip_stray_tags

logger = logging.getLogger(__name__)

_WORD_CHARS_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")


def _re_word_chars(text: str) -> str:
    """Return only the word characters (letters/digits) of `text` — used to detect
    'empty' character sheets that technically contain whitespace/punctuation."""
    return "".join(_WORD_CHARS_RE.findall(text or ""))


class DBBotEngineMixin:
    """DBBotEngineMixin — DB sub-agent (reads narrative, queues write actions)."""

    async def process_db_bot(
        self,
        raw_narrative: str,
        context: str = "",
        character_sheets: Optional[List[str]] = None,
        session_id: str = "",
        max_iterations_override: Optional[int] = None,
        player_roll_requester: Optional[Callable[[str, Dict, str], Awaitable[Dict]]] = None,
        session_history: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """
        Step 2: DB-Bot reads narrative + queries DB via GET tools, then returns DB actions.
        Returns: list of {"tool_name": str, "arguments": dict}

        New (Idea 2 — reconciliation):
        - player_roll_requester: passed through to moder_ai_dispatch_roll when DB-Bot
          calls dispatch_roll, so ModerAI can dispatch the inline-button roll.
        - session_history: passed to moder_ai_ask_master when DB-Bot calls ask_master,
          so Master has context to answer the clarifying question.
        """
        MAX_ITERATIONS = 15
        # For world generation (large text with many locations/NPCs/lore),
        # allow more iterations — DB-Bot may need to create dozens of entities.
        if max_iterations_override and max_iterations_override > MAX_ITERATIONS:
            MAX_ITERATIONS = max_iterations_override
        logger.info(f"[db_bot] Starting with MAX_ITERATIONS={MAX_ITERATIONS}")
        iteration = 0
        game_actions: List[Dict] = []

        # Store session_id on self for moder_ai_dispatch_roll (it needs DB access).
        self._current_session_id = session_id

        sheets_text = ""
        if character_sheets:
            sheets_text = chr(10) + chr(10) + "ЛИСТЫ ПЕРСОНАЖЕЙ:" + chr(10) + chr(10).join(["---" + chr(10) + s for s in character_sheets])

        prompt = f"""Текст Дунгеон Мастера:
---
{raw_narrative}
---
{context}{sheets_text}

Проанализируй текст и вызови ВСЕ необходимые инструменты базы данных.
Если нужно изменить золото, HP, предметы — СНАЧАЛА вызови get_character_state чтобы узнать текущее значение.
Если в тексте нет изменений состояния - не вызывай ничего, просто ответь "Нет изменений".
"""

        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.db_bot.chat(
                messages, system_prompt=get_prompt("db_bot", session_id), tools=DB_TOOLS,
            )

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                tool_calls = choice.get("tool_calls")

                if tool_calls:
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"])

                        get_result = self._read_db_get_tool(tool_name, args, session_id)
                        if get_result is not None:
                            tool_results.append({"call_id": tc["id"], "result": get_result})
                        elif tool_name in self.DB_WRITE_TOOL_NAMES:
                            game_actions.append({"tool_name": tool_name, "arguments": args})
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"OK: {tool_name} recorded.",
                            })
                        elif tool_name == "calculate":
                            expr = args.get("expression", "0")
                            try:
                                result_val = eval(expr, {"__builtins__": {}}, {})
                                tool_results.append({"call_id": tc["id"], "result": str(result_val)})
                                audit = f"[DB-BOT TOOL] calculate: '{expr}' = {result_val}"
                                logger.info(audit)
                                tool_audit.append(audit)
                            except Exception as calc_e:
                                tool_results.append({"call_id": tc["id"], "result": f"Calculation error: {calc_e}"})
                                logger.warning(f"[DB-BOT TOOL] calculate error: {calc_e}")
                        elif tool_name == "ask_master":
                            # ── Idea 2: DB-Bot asks Master a clarifying question ──
                            question = args.get("question", "")
                            audit = f"[DB-BOT TOOL] ask_master: '{question[:80]}'"
                            logger.info(audit)
                            try:
                                master_answer = await self.moder_ai_ask_master(
                                    question=question,
                                    narrative_context=raw_narrative[:2000],
                                    session_history=session_history,
                                    session_id=session_id,
                                )
                            except Exception as ask_e:
                                logger.error(f"[DB-BOT TOOL] ask_master failed: {ask_e}")
                                master_answer = f"[Master unavailable: {ask_e}]"
                            tool_results.append({"call_id": tc["id"], "result": f"Master answers: {master_answer}"})
                            logger.info(f"[DB-BOT TOOL] ask_master -> {master_answer[:120]}")

                        elif tool_name == "dispatch_roll":
                            # ── Idea 2: DB-Bot dispatches a missing roll via ModerAI ──
                            d_username = args.get("username", "npc")
                            d_roll_type = args.get("roll_type", "skill")
                            d_description = args.get("description", "")
                            audit = (
                                f"[DB-BOT TOOL] dispatch_roll: username='{d_username}' "
                                f"roll_type='{d_roll_type}' description='{d_description[:80]}'"
                            )
                            logger.info(audit)
                            try:
                                dispatch_result = await self.moder_ai_dispatch_roll(
                                    username=d_username,
                                    roll_type=d_roll_type,
                                    description=d_description,
                                    player_roll_requester=player_roll_requester,
                                    session_id=session_id,
                                )
                            except Exception as disp_e:
                                logger.error(f"[DB-BOT TOOL] dispatch_roll failed: {disp_e}")
                                dispatch_result = f"[dispatch failed: {disp_e}]"
                            tool_results.append({"call_id": tc["id"], "result": dispatch_result})
                            logger.info(f"[DB-BOT TOOL] dispatch_roll -> {dispatch_result[:120]}")

                        else:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: unknown tool {tool_name}",
                            })

                    messages.append({
                        "role": "assistant",
                        "content": choice.get("content") or "",
                        "tool_calls": tool_calls,
                    })
                    for tr in tool_results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tr["call_id"],
                            "content": tr["result"],
                        })

                    response = await self.db_bot.chat(
                        messages, system_prompt=get_prompt("db_bot", session_id), tools=DB_TOOLS,
                    )
                    continue

                # No more tool calls
                return game_actions

            logger.error(f"[db_bot] Max iterations reached")
            return game_actions

        except Exception as e:
            logger.error(f"DB-Bot error: {e}")
            return game_actions


    async def process_dbask(self, appeal: str, character_name: str, db_state_text: str,
                            recent_history_text: str, session_id: str) -> Dict:
        """
        D1+D2 fix: handle a player's /dbask appeal with the SAME tool-loop pattern as
        process_db_bot (so it doesn't cut off mid tool-call), and WITH recent narrative
        history in context (so the DB-Bot can actually verify the appeal instead of
        rejecting it out of ignorance).
        Returns: {"answer": str, "game_actions": List[Dict]}
        """
        MAX_ITERATIONS = 10
        iteration = 0
        game_actions: List[Dict] = []
        tool_audit: List[str] = []

        prompt = f"""Игрок {character_name} апеллирует: "{appeal}"

Текущее состояние БД:
{db_state_text}

Недавняя история игры (нарратив Мастера и действия игроков — используй её, чтобы проверить, было ли то, о чём говорит игрок):
{recent_history_text}

Проанализируй апелляцию против истории и текущего состояния БД. Если игрок прав — вызови нужные инструменты для исправления. Если неправ — объясни почему, сославшись на конкретный момент истории. НЕ выдумывай изменения, которых нет в апелляции или истории. В конце ОБЯЗАТЕЛЬНО дай текстовый ответ игроку — не заканчивай только вызовами инструментов."""

        messages = [{"role": "user", "content": prompt}]

        try:
            response = await self.db_bot.chat(messages, system_prompt=get_prompt("db_bot", session_id), tools=DB_TOOLS)

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                tool_calls = choice.get("tool_calls")

                if tool_calls:
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"])

                        get_result = self._read_db_get_tool(tool_name, args, session_id)
                        if get_result is not None:
                            tool_results.append({"call_id": tc["id"], "result": get_result})
                        elif tool_name in self.DB_WRITE_TOOL_NAMES:
                            game_actions.append({"tool_name": tool_name, "arguments": args})
                            tool_results.append({"call_id": tc["id"], "result": f"OK: {tool_name} recorded."})
                        elif tool_name == "calculate":
                            expr = args.get("expression", "0")
                            try:
                                result_val = eval(expr, {"__builtins__": {}}, {})
                                tool_results.append({"call_id": tc["id"], "result": str(result_val)})
                                audit = f"[DB-BOT TOOL] calculate: '{expr}' = {result_val}"
                                logger.info(audit)
                                tool_audit.append(audit)
                            except Exception as calc_e:
                                tool_results.append({"call_id": tc["id"], "result": f"Calculation error: {calc_e}"})
                                logger.warning(f"[DB-BOT TOOL] calculate error: {calc_e}")
                        else:
                            tool_results.append({"call_id": tc["id"], "result": f"Error: unknown tool {tool_name}"})

                    messages.append({"role": "assistant", "content": choice.get("content") or "", "tool_calls": tool_calls})
                    for tr in tool_results:
                        messages.append({"role": "tool", "tool_call_id": tr["call_id"], "content": tr["result"]})

                    response = await self.db_bot.chat(messages, system_prompt=get_prompt("db_bot", session_id), tools=DB_TOOLS)
                    continue

                # No more tool calls — this IS the final answer text.
                answer = choice.get("content") or "Апелляция обработана, но пояснения не дано."
                return {"answer": answer, "game_actions": game_actions}

            logger.error("[dbask] Max iterations reached")
            return {"answer": "*[DB-Bot не смог завершить обработку апелляции — слишком много шагов]*", "game_actions": game_actions}

        except Exception as e:
            logger.error(f"dbask error: {e}")
            return {"answer": f"*[Ошибка DB-Bot: {e}]*", "game_actions": game_actions}


    async def process_with_db_bot(self, raw_text: str, context: str = "") -> List[Dict]:
        """
        Universal wrapper: after ANY AI text, run DB-Bot to extract and apply DB actions.
        Returns list of game_actions.
        """
        if not raw_text or not raw_text.strip():
            return []
        return await self.process_db_bot(raw_text, context=context)


    def _read_db_get_tool(self, tool_name: str, args: Dict, session_id: str) -> Optional[str]:
        """Handle get_character_state / get_inventory reads. Returns a JSON string
        result, or None if tool_name isn't a recognized GET tool."""
        db = self.db_manager.get_db(session_id) if self.db_manager and session_id else None
        if not db or not session_id:
            return None

        if tool_name == "get_character_state":
            char_name = args.get("character_name", "")
            chars = db.get_session_characters(session_id)
            found = None
            for c in chars:
                if char_name.lower() in c.name.lower() or c.name.lower() in char_name.lower():
                    found = c
                    break
            if found:
                gold = db.get_gold_balance(session_id, found.id)
                loc = db.get_location(session_id, found.id)
                conds = db.get_conditions(session_id, found.id)
                return json.dumps({
                    "name": found.name, "level": found.level, "hp": found.hp, "max_hp": found.max_hp, "ac": found.ac,
                    "gold_cp": gold.get("cp", 0), "gold_sp": gold.get("sp", 0), "gold_ep": gold.get("ep", 0),
                    "gold_gp": gold.get("gp", 0), "gold_pp": gold.get("pp", 0),
                    "location": loc.location_name if loc else "",
                    "conditions": [cc.condition for cc in conds],
                }, ensure_ascii=False)
            return f'{{"error": "Character {char_name} not found"}}'

        if tool_name == "get_inventory":
            char_name = args.get("character_name", "")
            chars = db.get_session_characters(session_id)
            found = None
            for c in chars:
                if char_name.lower() in c.name.lower() or c.name.lower() in char_name.lower():
                    found = c
                    break
            if found:
                inv = db.get_inventory(session_id, found.id)
                return json.dumps({"items": [{"name": i["item"], "qty": i["qty"]} for i in inv]}, ensure_ascii=False)
            return f'{{"error": "Character {char_name} not found"}}'

        return None


    async def parse_character_sheet(self, sheet_text: str) -> Optional[ParsedCharacter]:
        """Parse raw character sheet text using Llama 4. Returns dict or None.

        BUG (Эйра «Чертила»): an EMPTY sheet used to come back as a fully-populated
        character — the model simply echoed the few-shot example embedded in its own
        prompt (rule 7 told it to use defaults for anything missing). Fixes:
        1. Empty/near-empty sheet → None WITHOUT an LLM call.
        2. The prompt example values are obviously-fake placeholders, never a real
           player's name.
        3. Rule 7 rewritten: defaults are allowed ONLY for numeric secondary fields;
           if the NAME (or race+class) is absent the model must return {"error": "empty_sheet"}.
        4. Post-parse guard: template/placeholder names and example-name echoes are rejected.
        """
        # Guard 1: empty sheet → no LLM call at all (also saves tokens).
        if not (sheet_text or "").strip():
            logger.warning("[parse_character_sheet] Empty sheet text — rejecting without LLM call")
            return None
        # Guard 2: a real sheet must contain at least SOME letters. A file with only
        # whitespace/punctuation/digits (or 2-3 stray chars) is not a character sheet.
        if len(_re_word_chars(sheet_text)) < 10:
            logger.warning("[parse_character_sheet] Sheet has <10 word characters — treating as empty")
            return None
        prompt = f"""Ты - парсер листов персонажей D&D 5e (2024).

Прочитай текст листа персонажа и верни СТРОГО JSON в таком формате:
{{
  "name": "ИмяПерсонажа",
  "race": "РасаПерсонажа",
  "class_name": "КлассПерсонажа",
  "level": 1,
  "background": "ПредысторияПерсонажа",
  "alignment": "МировоззрениеПерсонажа",
  "strength": 10,
  "dexterity": 10,
  "constitution": 10,
  "intelligence": 10,
  "wisdom": 10,
  "charisma": 10,
  "hp": 8,
  "max_hp": 8,
  "ac": 14,
  "speed": 30,
  "hit_dice": 8,
  "gold": 15,
  "gold_sp": 0,
  "gold_cp": 0,
  "gold_ep": 0,
  "gold_pp": 0,
  "proficiencies": ["владение1"],
  "skills": ["навык1"],
  "languages": ["язык1"],
  "inventory": ["предмет1"],
  "spells": [],
  "features": ["умение1"],
  "backstory": "Краткая предыстория"
}}

ПРАВИЛА:
1. Заполняй ТОЛЬКО тем, что РЕАЛЬНО написано в листе ниже. НЕ переноси в ответ ни слова из этого шаблона или из правил — шаблон показывает только структуру.
2. Имя может быть в рамке типа ║ ИМЯ ПЕРСОНАЖА ║ или таблицей — вытащи его из ТЕКСТА ЛИСТА.
3. Характеристики ищи в таблицах, key-value, любом формате.
4. HP = максимум хит-куба + мод CON (если явно не указано).
5. AC бери как указано, если не указано - 10 + мод DEX.
6. Золото — ищи ВСЕ номиналы отдельно: gold=зм/gp, gold_sp=см/sp, gold_cp=мм/cp, gold_ep=эм/ep, gold_pp=пм/pp. Не конвертируй одно в другое, бери как написано в листе.
7. Если в листе НЕТ имени, или нет НИ расы, НИ класса - верни ровно это: {{"error": "empty_sheet"}} — это ОБЯЗАТЕЛЬНЫЙ ответ для пустого/не-персонажного текста.
8. Для ЧИСЛОВЫХ второстепенных полей, которых нет в листе, используй значения по умолчанию (10 для статов, 0 для золота). Но никогда не выдумывай имя, расу, класс, навыки, снаряжение и предысторию — их нет, значит их нет и в ответе (пустой список/строка).
9. Ответь ТОЛЬКО JSON. Без markdown, без объяснений.

Лист персонажа:
---
{sheet_text}
---"""
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.db_bot.chat(messages, system_prompt=None, tools=None, tool_choice=None)
            raw = response["choices"][0]["message"].get("content", "")
            # Strip markdown code blocks if present
            logger.info(f"[parse_character_sheet] Llama 4 raw response: {raw[:500]}...")
            raw = raw.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
            data = json.loads(raw)
            logger.info(f"[parse_character_sheet] Parsed JSON keys: {list(data.keys())}")
            # Guard 3: the model explicitly told us there is no character here.
            if not isinstance(data, dict) or data.get("error"):
                logger.warning(f"[parse_character_sheet] Parser returned error marker: {data}")
                return None
            # Guard 4: reject template/placeholder names (the model echoing the
            # JSON template instead of the sheet — the original "Эйра «Чертила»" bug).
            name = str(data.get("name") or "").strip()
            _BAD_NAMES = {
                "имя персонажа", "имяперсонажа", "unknown", "неизвестно",
                "персонаж", "character name", "name",
            }
            if not name or name.lower() in _BAD_NAMES:
                logger.warning(f"[parse_character_sheet] Placeholder/empty name rejected: {name!r}")
                return None
            # Build ParsedCharacter from dict
            char = ParsedCharacter()
            for key, value in data.items():
                if hasattr(char, key):
                    setattr(char, key, value)
            # Guard 5: a sheet with NO race AND NO class AND no skills and no
            # inventory is not a usable character — treat as parse failure.
            if not (getattr(char, "race", "") or getattr(char, "class_name", "")
                    or getattr(char, "skills", []) or getattr(char, "inventory", [])):
                logger.warning("[parse_character_sheet] Parsed character has no race/class/skills/inventory — rejecting as empty")
                return None
            # Ensure hp/max_hp consistency
            if char.hp and not char.max_hp:
                char.max_hp = char.hp
            if char.max_hp and not char.hp:
                char.hp = char.max_hp
            # Calculate AC if missing
            if char.ac == 10 and char.dexterity > 10:
                char.ac = 10 + char.get_modifier("dexterity")
            return char
        except Exception as e:
            logger.error(f"Sheet parsing error: {e}")
            return None

