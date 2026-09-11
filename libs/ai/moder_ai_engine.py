"""ModerAIEngineMixin — extended ModerAI: dice dispatcher + Master reconciliation.

Idea 1: Master calls `request_roll_intent(@username, roll_type, description)` →
        routes here → ModerAI fetches character sheet from DB, dispatches the
        actual roll (inline button for player / server-side for NPC), returns
        the result text to Master. Master writes narrative AFTER knowing outcome.

Idea 2: DB-Bot calls `ask_master(question)` or `dispatch_roll(...)` → routes
        here → ModerAI either asks Master for clarification or dispatches a
        missing roll. Result is returned to DB-Bot, which continues processing.
"""
import json
import logging
from typing import Dict, List, Optional, Any, Callable, Awaitable

from .tools import MODER_AI_DISPATCH_TOOLS
from .prompts import MODER_AI_DISPATCH_PROMPT, MASTER_PROMPT, get_prompt

logger = logging.getLogger(__name__)


class ModerAIEngineMixin:
    """ModerAIEngineMixin — ModerAI as dice dispatcher + Master reconciler.

    Two public methods:
      - moder_ai_dispatch_roll(username, roll_type, description, player_roll_requester,
                               session_id, pc_names) -> str
        Returns text result for the Master (e.g. "Кейн: скрытность 1d20+5 → nat 12, total 17").

      - moder_ai_ask_master(question, narrative_context, session_history) -> str
        Returns Master's text answer to DB-Bot's clarifying question.
    """

    async def moder_ai_dispatch_roll(
        self,
        username: str,
        roll_type: str,
        description: str,
        player_roll_requester: Optional[Callable[[str, Dict, str], Awaitable[Dict]]] = None,
        session_id: str = "",
        pc_names: Optional[List[str]] = None,
    ) -> str:
        """Dispatch a dice roll via ModerAI.

        Flow:
          1. Build a ModerAI prompt: "Master wants X for @username, roll_type=Y, description=Z".
          2. ModerAI calls get_character_sheet(@username) → fetches sheet from DB.
          3. ModerAI calls request_roll_dice (player) OR roll_npc_dice (NPC) → executes roll.
          4. ModerAI returns text result to feed back to Master.

        Returns: text result like "Кейн: скрытность 1d20+5 → nat 12, total 17"
                 or "Player @username not found" on lookup failure.
        """
        # Normalize username — strip leading @ for internal lookup, but keep @ in messages
        clean_username = username.lstrip("@")

        is_npc = (username.lower() == "npc" or username.lower() == "@npc" or not username)

        if is_npc:
            intent_text = (
                f"Мастер просит бросок за NPC/монстра/ловушку.\n"
                f"roll_type: {roll_type}\n"
                f"description: {description}\n\n"
                f"Не ищи персонажа в БД — это NPC. Сразу вызови roll_npc_dice с подходящими "
                f"параметрами (count, sides, modifier, label). Если в description не указаны "
                f"конкретные кости — выбери подходящие по ситуации (атака NPC = 1d20+модификатор, "
                f"урон = обычно 1d6 или 1d8 + модификатор Силы). Верни результат Мастеру."
            )
        else:
            intent_text = (
                f"Мастер просит бросок для игрока @{clean_username}.\n"
                f"roll_type: {roll_type}\n"
                f"description: {description}\n\n"
                f"1. Вызови get_character_sheet(username='@{clean_username}') чтобы получить лист.\n"
                f"2. Определи модификатор: для skill — DEX/STR/CON/INT/WIS/CHA мод + proficiency "
                f"(если владеет, видно в proficiencies). Для attack — модификатор атаки (STR или DEX "
                f"+ proficiency). Для save — модификатор спасброска (стат-мод + proficiency если владеет).\n"
                f"3. Вызови request_roll_dice с найденным character_name и модификатором.\n"
                f"4. Дождись результата и верни его Мастеру."
            )

        messages = [{"role": "user", "content": intent_text}]

        MAX_ITERATIONS = 8
        iteration = 0
        final_text = ""

        try:
            response = await self.moder_ai.chat(
                messages,
                system_prompt=get_prompt("moder_ai_dispatch", session_id),
                tools=MODER_AI_DISPATCH_TOOLS,
            )

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                tool_calls = choice.get("tool_calls")

                if tool_calls:
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError as je:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: invalid JSON arguments: {je}",
                            })
                            continue

                        # ── get_character_sheet: fetch from DB by @username ──
                        if tool_name == "get_character_sheet":
                            sheet_text = self._fetch_character_sheet_by_username(
                                args.get("username", "").lstrip("@"), session_id
                            )
                            tool_results.append({"call_id": tc["id"], "result": sheet_text})
                            logger.info(f"[moder_ai_dispatch] get_character_sheet({args.get('username')}) -> {len(sheet_text)} chars")

                        # ── request_roll_dice: dispatch inline button to player ──
                        elif tool_name == "request_roll_dice":
                            char_name = args.get("character_name", "?")
                            # Reuse the Master's player_roll_requester callback — it already
                            # knows how to send the inline button and wait for the press.
                            if player_roll_requester is not None:
                                try:
                                    roll_result = await player_roll_requester(
                                        char_name, args, tc["id"]
                                    )
                                except Exception as e:
                                    logger.error(f"[moder_ai_dispatch] player_roll_requester failed: {e}")
                                    roll_result = {"timeout": True}
                            else:
                                # No button plumbing — fall back to immediate server roll
                                logger.warning(
                                    f"[moder_ai_dispatch] No player_roll_requester; "
                                    f"executing roll immediately as fallback"
                                )
                                roll_result = self._execute_roll(args)

                            if roll_result.get("timeout"):
                                tool_results.append({
                                    "call_id": tc["id"],
                                    "result": (
                                        f"Player {char_name} did not press the roll button in time. "
                                        f"Result: TIMEOUT — no numeric value."
                                    ),
                                })
                            else:
                                tool_results.append({
                                    "call_id": tc["id"],
                                    "result": (
                                        f"Roll result for {char_name}: "
                                        f"{roll_result.get('display', '')} | "
                                        f"natural={roll_result.get('natural', '?')}, "
                                        f"total={roll_result.get('total', '?')}"
                                    ),
                                })
                            logger.info(f"[moder_ai_dispatch] request_roll_dice({char_name}) -> {roll_result.get('result', 'timeout')}")

                        # ── roll_npc_dice: server-side NPC roll ──
                        elif tool_name == "roll_npc_dice":
                            # Force visible=False regardless of what ModerAI said — cheatproof
                            args["visible"] = False
                            roll_result = self._execute_roll(args)
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": (
                                    f"NPC roll: {roll_result.get('display', '')} | "
                                    f"natural={roll_result.get('natural', '?')}, "
                                    f"total={roll_result.get('total', '?')}"
                                ),
                            })
                            logger.info(f"[moder_ai_dispatch] roll_npc_dice -> {roll_result.get('result', '?')}")

                        # ── calculate: math helper ──
                        elif tool_name == "calculate":
                            expr = args.get("expression", "0")
                            try:
                                result_val = eval(expr, {"__builtins__": {}}, {})
                                tool_results.append({"call_id": tc["id"], "result": str(result_val)})
                            except Exception as calc_e:
                                tool_results.append({
                                    "call_id": tc["id"],
                                    "result": f"Calculation error: {calc_e}",
                                })

                        else:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: ModerAI should not call {tool_name}",
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

                    response = await self.moder_ai.chat(
                        messages,
                        system_prompt=get_prompt("moder_ai_dispatch", session_id),
                        tools=MODER_AI_DISPATCH_TOOLS,
                    )
                    continue

                # No more tool calls — final text
                final_text = (choice.get("content") or "").strip()
                if not final_text:
                    final_text = "[ModerAI returned no result text]"
                break

            return final_text or "[ModerAI dispatch ended without final text]"

        except Exception as e:
            logger.error(f"[moder_ai_dispatch] error: {e}", exc_info=True)
            return f"[ModerAI dispatch error: {e}]"

    async def moder_ai_ask_master(
        self,
        question: str,
        narrative_context: str = "",
        session_history: Optional[List[Dict]] = None,
        session_id: str = "",
    ) -> str:
        """Ask the Master LLM a clarifying question (used by DB-Bot).

        This is a direct call to the Master LLM (NOT ModerAI) — the DB-Bot is
        asking the Master to clarify something in the Master's own narrative.

        Returns: Master's text answer.
        """
        prompt = f"""DB-Bot (субагент, обновляющий базу данных) задаёт тебе вопрос по твоему нарративу.

Вопрос от DB-Bot: {question}

Контекст нарратива:
{narrative_context or "(нет дополнительного контекста)"}

Ответь КРАТКО и по существу — только то, что спросил DB-Bot. Не пиши новый нарратив, не продолжай историю. Просто дай конкретный ответ (число, имя, источник и т.д.)."""

        messages = list(session_history or [])
        messages = messages[-10:]  # last 10 messages for context
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self.master.chat(
                messages,
                system_prompt=get_prompt("master", session_id),
                max_tokens=512,
            )
            answer = response["choices"][0]["message"].get("content", "").strip()
            if not answer:
                answer = "[Мастер не дал ответа]"
            logger.info(f"[moder_ai_ask_master] Q: {question[:80]}... A: {answer[:80]}...")
            return answer
        except Exception as e:
            logger.error(f"[moder_ai_ask_master] error: {e}")
            return f"[Ошибка запроса к Мастеру: {e}]"

    # ── Helpers ──

    def _fetch_character_sheet_by_username(self, username: str, session_id: str) -> str:
        """Look up a character by Telegram @username in the DB.

        Returns a JSON string with the character sheet (name, level, stats, AC, HP,
        proficiencies, saves, etc.) — or an error message if not found.
        """
        if not self.db_manager or not session_id or not username:
            return json.dumps({"error": f"Cannot look up username='{username}' (no DB or session_id)"}, ensure_ascii=False)

        db = self.db_manager.get_db(session_id)
        if not db:
            return json.dumps({"error": "DB not available"}, ensure_ascii=False)

        # Find the player by telegram username (case-insensitive)
        # BUG #3 FIX: previously this used substring fallback ("username_lower in
        # p.username.lower()") which meant "@crescent" would match "@crescentfunny"
        # — i.e. the wrong player received the roll. Use EXACT match only.
        players = db.get_players(session_id)
        target_player = None
        username_lower = username.lower().lstrip("@")
        for p in players:
            if p.username and p.username.lower().lstrip("@") == username_lower:
                target_player = p
                break

        if not target_player:
            available = [p.username for p in players if p.username]
            return json.dumps({
                "error": f"Player with telegram username '@{username}' not found in session",
                "available_usernames": available,
            }, ensure_ascii=False)

        # Get the player's character
        char = db.get_character_by_player(target_player.user_id, session_id)
        if not char:
            return json.dumps({
                "error": f"Player @{username} has no character in session",
                "player_display_name": target_player.display_name,
            }, ensure_ascii=False)

        # Build a sheet summary
        try:
            stats = json.loads(char.stats) if char.stats else {}
        except (json.JSONDecodeError, TypeError):
            stats = {}

        try:
            proficiencies = json.loads(char.proficiencies) if char.proficiencies else []
        except (json.JSONDecodeError, TypeError):
            proficiencies = char.proficiencies.split(",") if char.proficiencies else []

        try:
            features = json.loads(char.features) if char.features else []
        except (json.JSONDecodeError, TypeError):
            features = char.features.split(",") if char.features else []

        try:
            languages = json.loads(char.languages) if char.languages else []
        except (json.JSONDecodeError, TypeError):
            languages = char.languages.split(",") if char.languages else []

        # Level → proficiency bonus
        level = char.level or 1
        proficiency_bonus = 2 + ((level - 1) // 4)

        sheet = {
            "username": f"@{target_player.username}",
            "player_display_name": target_player.display_name,
            "name": char.name,
            "race": char.race,
            "class_name": char.class_name,
            "level": level,
            "proficiency_bonus": proficiency_bonus,
            "hp": char.hp,
            "max_hp": char.max_hp,
            "ac": char.ac,
            "stats": {
                "strength": stats.get("strength", 10),
                "dexterity": stats.get("dexterity", 10),
                "constitution": stats.get("constitution", 10),
                "intelligence": stats.get("intelligence", 10),
                "wisdom": stats.get("wisdom", 10),
                "charisma": stats.get("charisma", 10),
            },
            "stat_modifiers": {
                "strength": (stats.get("strength", 10) - 10) // 2,
                "dexterity": (stats.get("dexterity", 10) - 10) // 2,
                "constitution": (stats.get("constitution", 10) - 10) // 2,
                "intelligence": (stats.get("intelligence", 10) - 10) // 2,
                "wisdom": (stats.get("wisdom", 10) - 10) // 2,
                "charisma": (stats.get("charisma", 10) - 10) // 2,
            },
            "proficiencies": proficiencies,
            "features": features,
            "languages": languages,
            "backstory": (char.backstory or "")[:500],
        }
        return json.dumps(sheet, ensure_ascii=False, indent=2)
