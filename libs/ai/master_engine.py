"""MasterEngineMixin — main narrative + Master LLM calls."""
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


class MasterEngineMixin:
    """MasterEngineMixin — main narrative + Master LLM calls."""

    async def process_master_turn(
        self,
        session_history: List[Dict],
        player_actions: Dict[str, str],
        character_sheets: Optional[List[str]] = None,
        context: str = "",
        roll_mode: str = "mixed",
        summary: str = "",
        db_journal: str = "",
        verified_rolls: Optional[Dict[str, List[str]]] = None,
        player_roll_requester: Optional[Callable[[str, Dict, str], Awaitable[Dict]]] = None,
        memory_digest: str = "",
        combat_starter: Optional[Callable[[List[str], str], Awaitable[str]]] = None,
        combat_ender: Optional[Callable[[str], Awaitable[str]]] = None,
        combat_joiner: Optional[Callable[[str], Awaitable[str]]] = None,
        npc_state_syncer: Optional[Callable[[Dict], Awaitable[str]]] = None,
        npc_ai_enabled: bool = False,
        combat_turn_mode: bool = False,
        pc_names: Optional[List[str]] = None,
        session_id: str = "",
    ) -> Dict[str, str]:
        """
        Step 1: Master (Kimi) writes narrative + rolls dice.

        player_actions: nick -> "Дн. ..." text (what the player typed).
        verified_rolls: nick -> list of already-resolved, system-verified roll strings
            (from the /roll command). Rendered in a SEPARATE [ROLL] tag, never nested
            inside the player's [PLAYER] action text — so the Master can never confuse
            "what the player claims happened" with "a number the server actually rolled".
        player_roll_requester: async callback(character_name, tool_args, tool_call_id) -> dict
            invoked whenever the Master calls request_player_roll. Must return a dict shaped
            like _execute_roll()'s output (display/result/visible/natural/total), or
            {"timeout": True} if the player never pressed the button. If not provided
            (e.g. pre-game narration where no player queue exists yet), player rolls are
            executed immediately as a fallback instead of blocking forever.
        session_id: current session id — needed for ModerAI dispatch (to fetch character
            sheets from DB by @username). Stored as self._current_session_id so the
            request_roll_intent tool handler can pass it through.

        Returns: {"player_text": str, "gm_log": str, "raw_narrative": str, "tool_audit": List[str]}
        """
        MAX_ITERATIONS = 20
        iteration = 0

        # Store session_id on self so the request_roll_intent tool handler can use it
        # when calling moder_ai_dispatch_roll (which needs it to look up character sheets).
        self._current_session_id = session_id

        messages = []
        for msg in session_history[-30:]:
            messages.append(msg)

        # BUG #4 & #5 FIX: extract the most recent DM narratives from session_history
        # and inject a "do not repeat these" block into the turn prompt. The LLM can
        # still see them in session_history (as assistant messages), but this explicit
        # block gives a hard instruction and surfaces them at the top where they're
        # harder to ignore. We also pre-process session_history so recent assistant
        # narratives are kept but flagged.
        recent_narratives: List[str] = []
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                # Strip leading roll block (📑 **Броски:**...) — we only care about prose.
                content = msg["content"]
                # Truncate to a reasonable preview
                recent_narratives.append(content[:600])
            if len(recent_narratives) >= 3:
                break
        anti_repeat_block = ""
        if recent_narratives:
            joined = "\n\n---\n\n".join(
                f"ПОСЛЕДНИЙ {i+1} НАРРАТИВ (НЕ повторяй его формулировки дословно):\n{narr[:500]}"
                for i, narr in enumerate(reversed(recent_narratives))
            )
            anti_repeat_block = (
                f"\n\n🚫 АНТИПОВТОР (БАГ #4 И #5):\n"
                f"Ниже твои последние реплики — НЕ дублируй их формулировки в новом ответе. "
                f"Новый ход должен описывать ТОЛЬКО то, что в [PLAYER] сейчас — не «доклеивая» "
                f"прошлые действия и не повторяя прошлые описания.\n\n{joined}"
            )

        verified_rolls = verified_rolls or {}
        action_blocks = []
        for nick, action in player_actions.items():
            action_blocks.append(f"[PLAYER]{nick}: {action}[/PLAYER]")
            rolls_for_nick = verified_rolls.get(nick)
            if rolls_for_nick:
                action_blocks.append(f"[ROLL]{nick}: " + "; ".join(rolls_for_nick) + "[/ROLL]")
        actions_text = "\n".join(action_blocks)

        sheets_text = ""
        if character_sheets:
            sheets_text = "\n\nАКТУАЛЬНОЕ СОСТОЯНИЕ ПЕРСОНАЖЕЙ (из базы данных, а не со старого листа):\n" + "\n---\n".join(character_sheets)

        mode_instructions = {
            "gmroll": "For roll_dice (your own GM rolls): visible=false. request_player_roll is always visible to its player regardless of mode.",
            "playerroll": "For roll_dice: visible=true. request_player_roll is always visible to its player.",
            "mixed": "roll_dice (NPC/monster rolls): visible=false by default. request_player_roll: always visible to its player.",
        }
        mode_text = mode_instructions.get(roll_mode, mode_instructions["mixed"])
        summary_block = f"\n\n📜 СВОДКА КАМПАНИИ:\n{summary}" if summary else ""
        journal_block = f"\n\n📊 ЖУРНАЛ БД (последние операции):\n{db_journal}" if db_journal else ""
        memory_block = f"\n\n🧠 ЧТО ТЫ ПОМНИШЬ ОБ ЭТОМ МЕСТЕ И СОБЫТИЯХ:\n{memory_digest}" if memory_digest else ""

        turn_prompt = f"""Игроки объявили действия. [PLAYER]...[/PLAYER] — то, что написал игрок. [ROLL]...[/ROLL] — отдельный, уже подтверждённый сервером бросок этого игрока (если есть) для этого раунда: не перебрасывай его через request_player_roll заново, используй как есть. Эти теги никогда не вложены друг в друга.

{actions_text}{sheets_text}
{context}{summary_block}{journal_block}{memory_block}{anti_repeat_block}

Разреши по правилам D&D 5e 2024.

КРИТИЧЕСКИ ВАЖНО (НЕ НАРУШАЙ):
- Броски NPC/мира — ТОЛЬКО через roll_dice (visible=false).
- ЛЮБОЙ бросок, принадлежащий игроку (атака, урон, спасбросок, проверка, инициатива, advantage, disadvantage, d100 и т.д.) — ТОЛЬКО через request_player_roll. НИКОГДА не используй roll_dice для бросков игрока.
- Игрок сам бросает свой урон через request_player_roll. Ты НЕ бросаешь урон за игрока.
- Вызывай ОТДЕЛЬНЫЙ request_player_roll для КАЖДОГО броска игрока: атака = одна кнопка, урон = отдельная кнопка, спасбросок = ещё одна.
- НЕ вызывай change_hp, add_item и другие инструменты БД - это делает другая система.
- НЕ пиши мета-комментарии о процессе («броски получены», «теперь пишу нарратив», «разрешаю раунд») — выдавай сразу готовый нарратив.
{mode_text}
- НИКОГДА не говори за игроков. Персонажи МОГУТ умирать.
- Если HP падает до 0 - опиши падение и начни спасброски от смерти.
- Учитывай текущую погоду и время суток.
- Если бой активен: сначала вызови npc_ai_action для NPC/монстров, потом пиши нарратив В ПОРЯДКЕ ИНИЦИАТИВЫ.
- В КОНЦЕ narrative ОБЯЗАТЕЛЬНО добавь блок ─── СВОДКА ─── ... ─── КОНЕЦ СВОДКИ ─── с HP, состояниями, предметами, локацией и временем. Без сводки DB-бот не обновит состояние.

Отвечай на русском.""" if not combat_turn_mode else f"""ОДИН ХОД В БОЮ. Сейчас ходит один персонаж — напиши нарратив ТОЛЬКО для него. Не описывай ходы других — каждый ход отдельным сообщением.

[PLAYER]...[/PLAYER] — действие текущего хода. [ROLL]...[/ROLL] — уже подтверждённый бросок (если есть).

{actions_text}{sheets_text}
{context}{summary_block}{journal_block}{memory_block}{anti_repeat_block}

Разреши этот ОДИН ход по правилам D&D 5e 2024.

КРИТИЧЕСКИ ВАЖНО:
- Это ОДИН ход — пиши нарратив ТОЛЬКО для текущего персонажа. Другие ходы будут отдельными сообщениями.
- НЕ вызывай dechrauymladd — бой уже идёт.
- Если текущий ход NPC: вызови npc_ai_action, потом брось кубики через roll_dice (visible=false).
- Если текущий ход игрока: ЛЮБОЙ бросок игрока — ТОЛЬКО через request_player_roll. Атака = одна кнопка, урон = отдельная.
- НЕ вызывай change_hp, add_item и другие инструменты БД - это делает другая система.
{mode_text}
- СКРЫТЫЙ КАНАЛ: если НПС/монстр получил урон, лечение или новое состояние — вызови diweddaru_npc с его актуальным HP/состояниями ДО финального нарратива. Игроки этого не видят.
- НЕ показывай игрокам HP и скрытые состояния НПС, ЕСЛИ они не узнали это законно (успешная Внимательность/Расследование/Медицина/Анализ в их действии — тогда назови точно).
- Если все враги мертвы/без сознания, сдались, сбежали или бой окончен по сюжету — вызови diweddymladd.
- В КОНЦЕ добавь ─── СВОДКА ─── ... ─── КОНЕЦ СВОДКИ ─── с HP, состояниями.

Отвечай на русском."""

        messages.append({"role": "user", "content": turn_prompt})

        tool_audit: List[str] = []

        try:
            response = await self.master.chat(
                messages, system_prompt=get_prompt("master", session_id), tools=MASTER_TOOLS,
            )

            visible_rolls: List[str] = []
            hidden_rolls: List[str] = []
            raw_narrative = ""
            # Fallback accumulator: some models (especially Gemini) write partial
            # narrative alongside tool_calls, then return empty content on the
            # final stop iteration. Stash any non-empty content from tool_call
            # iterations so we can fall back to it if the final pass is blank.
            fallback_narrative = ""

            while iteration < MAX_ITERATIONS:
                iteration += 1
                choice = response["choices"][0]["message"]
                finish_reason = response["choices"][0].get("finish_reason", "unknown")
                logger.info(f"[master] Iteration {iteration}, finish_reason={finish_reason}")

                tool_calls = choice.get("tool_calls")
                if tool_calls:
                    # Stash any content the model produced alongside tool_calls
                    partial_content = (choice.get("content") or "").strip()
                    if partial_content and len(partial_content) > len(fallback_narrative):
                        fallback_narrative = partial_content
                    tool_results = []
                    for tc in tool_calls:
                        tool_name = tc["function"]["name"]
                        args = json.loads(tc["function"]["arguments"])

                        if tool_name == "roll_dice":
                            result = self._execute_roll(args)
                            result["call_id"] = tc["id"]
                            tool_results.append(result)
                            hidden = not result.get("visible", True)
                            audit = f"[MASTER TOOL] roll_dice: '{args.get('label','')}' hidden={hidden} -> {result['result']}"
                            logger.info(audit)
                            tool_audit.append(audit)
                            if result.get("visible"):
                                visible_rolls.append(result["display"])
                            else:
                                hidden_rolls.append(result["display"])

                        elif tool_name == "request_player_roll":
                            char_name = args.get("character_name", "?")
                            audit_start = f"[MASTER TOOL] request_player_roll: character='{char_name}' label='{args.get('label','')}' — awaiting player button"
                            logger.info(audit_start)
                            tool_audit.append(audit_start)

                            if player_roll_requester is not None:
                                try:
                                    result = await player_roll_requester(char_name, args, tc["id"])
                                except Exception as e:
                                    logger.error(f"player_roll_requester failed: {e}")
                                    result = {"timeout": True}
                            else:
                                # No queue/button plumbing available (e.g. pre-game opening
                                # narration) — fall back to an immediate server roll so the
                                # flow never hangs.
                                result = self._execute_roll(args)

                            if result.get("timeout"):
                                tool_results.append({
                                    "call_id": tc["id"],
                                    "result": f"Player {char_name} did not press the roll button in time. "
                                              f"Do not invent a numeric result — describe hesitation, distraction, "
                                              f"or ask the DM/players to prompt them, and move on.",
                                })
                                audit_end = f"[MASTER TOOL] request_player_roll: character='{char_name}' -> TIMEOUT"
                            else:
                                result["call_id"] = tc["id"]
                                tool_results.append(result)
                                if result.get("visible", True):
                                    visible_rolls.append(result["display"])
                                else:
                                    hidden_rolls.append(result["display"])
                                audit_end = f"[MASTER TOOL] request_player_roll: character='{char_name}' -> {result.get('result','')}"
                            logger.info(audit_end)
                            tool_audit.append(audit_end)

                        elif tool_name == "dechrauymladd":
                            participants = args.get("participants", [])
                            reason = args.get("reason", "")
                            # ИТЕРАЦИЯ 10 (Раздел 4): тай-брейк инициативы — Мастер
                            # фиксирует им то, что уже сказал в нарративе («Хравна
                            # ходит первой» при равных инициативах).
                            raw_priorities = args.get("priorities") or {}
                            priorities = {}
                            if isinstance(raw_priorities, dict):
                                for p_name, p_val in raw_priorities.items():
                                    try:
                                        priorities[str(p_name)] = int(p_val)
                                    except (TypeError, ValueError):
                                        continue
                            audit = (f"[MASTER TOOL] dechrauymladd: participants={participants} "
                                     f"reason='{reason}' priorities={priorities}")
                            logger.info(audit)
                            tool_audit.append(audit)

                            # ── V10: Request initiative rolls from EACH PC participant ──
                            # Player initiative MUST go through request_player_roll (inline buttons).
                            # NPC initiative is rolled server-side by combat_starter.
                            pc_initiatives = {}  # name -> {natural, total}
                            pc_set = set(n.lower() for n in (pc_names or []))
                            if player_roll_requester is not None and combat_starter is not None:
                                # Only request player rolls for PCs (known names), NOT for NPCs.
                                for p_name in participants:
                                    if p_name.lower() not in pc_set:
                                        continue  # Skip NPC — server-side roll in combat_starter
                                    init_args = {
                                        "character_name": p_name,
                                        "count": 1,
                                        "sides": 20,
                                        "modifier": 0,  # DEX mod will be added server-side in combat_starter
                                        "roll_type": "normal",
                                        "label": f"Инициатива ({p_name})",
                                        "visible": True,
                                    }
                                    try:
                                        init_result = await player_roll_requester(p_name, init_args, tc["id"] + "_init_" + p_name)
                                        if init_result and not init_result.get("timeout"):
                                            pc_initiatives[p_name] = {
                                                "natural": init_result.get("natural", 0),
                                                "total": init_result.get("total", 0),
                                            }
                                            init_audit = f"[MASTER TOOL] dechrauymladd: initiative for {p_name} = {init_result.get('total', '?')} (player rolled {init_result.get('natural', '?')})"
                                            logger.info(init_audit)
                                            tool_audit.append(init_audit)
                                            visible_rolls.append(init_result.get("display", f"🎲 {p_name} initiative: {init_result.get('total', '?')}"))
                                        else:
                                            init_audit = f"[MASTER TOOL] dechrauymladd: initiative for {p_name} -> TIMEOUT, will use server roll"
                                            logger.info(init_audit)
                                            tool_audit.append(init_audit)
                                    except Exception as e:
                                        logger.warning(f"dechrauymladd initiative roll for {p_name} failed: {e}")
                                        tool_audit.append(f"[MASTER TOOL] dechrauymladd: initiative for {p_name} -> error: {e}")

                            if combat_starter is not None:
                                try:
                                    combat_result_text = await combat_starter(
                                        participants, reason,
                                        pc_initiatives=pc_initiatives, priorities=priorities)
                                except TypeError:
                                    # Back-compat: старый combat_starter без kwargs priorities
                                    combat_result_text = await combat_starter(
                                        participants, reason, pc_initiatives=pc_initiatives)
                                except Exception as e:
                                    logger.error(f"combat_starter failed: {e}")
                                    combat_result_text = f"Error starting combat: {e}"
                            else:
                                combat_result_text = "Combat system unavailable this turn — describe tension building instead, do not invent combat mechanics."
                            tool_results.append({"call_id": tc["id"], "result": combat_result_text})

                        elif tool_name == "diweddymladd":
                            reason = args.get("reason", "")
                            audit = f"[MASTER TOOL] diweddymladd: reason='{reason}'"
                            logger.info(audit)
                            tool_audit.append(audit)
                            if combat_ender is not None:
                                try:
                                    combat_result_text = await combat_ender(reason)
                                except Exception as e:
                                    logger.error(f"combat_ender failed: {e}")
                                    combat_result_text = f"Error ending combat: {e}"
                            else:
                                combat_result_text = "Combat end system unavailable — describe the end of combat narratively."
                            tool_results.append({"call_id": tc["id"], "result": combat_result_text})

                        elif tool_name == "ymuno_ymladd":
                            # ── БОЙ-FIX v2: вступление игрока в идущий бой ──
                            # Мастер по тексту Дн. решает, что игрок вмешивается
                            # в бой, и добавляет его в инициативу. Раньше такого
                            # механизма не было — игрок запирался в non-combat
                            # lane, его атаки резолвились параллельным Мастером,
                            # а главная очередь вечно ждала его (дедлок).
                            char_name = args.get("character_name", "")
                            join_reason = args.get("reason", "")
                            audit = (f"[MASTER TOOL] ymuno_ymladd: character='{char_name}' "
                                     f"reason='{join_reason}'")
                            logger.info(audit)
                            tool_audit.append(audit)
                            if combat_joiner is not None:
                                try:
                                    join_result_text = await combat_joiner(char_name)
                                except Exception as e:
                                    logger.error(f"combat_joiner failed: {e}")
                                    join_result_text = f"Error joining combat: {e}"
                            else:
                                join_result_text = ("Combat join system unavailable this turn — "
                                                    "resolve the action narratively instead.")
                            tool_results.append({"call_id": tc["id"], "result": join_result_text})

                        elif tool_name == "diweddaru_npc":
                            # ── ДОПОЛНЕНИЕ 1: скрытый канал Мастер → DB ──
                            # Мастер сохраняет актуальное состояние НПС (HP,
                            # состояния вроде «сбит с ног», секретные заметки).
                            # Вызов и результат НИКОГДА не попадают в чат —
                            # это внутренняя связь с DB-слоем. Состояние
                            # сохраняется в Combatant (в бою) или в скрытую
                            # мету сессии (вне боя) и возвращается Мастеру в
                            # боевой сводке на следующих ходах.
                            npc_name = args.get("npc_name", "")
                            payload = {
                                "npc_name": npc_name,
                                "hp_current": args.get("hp_current"),
                                "hp_max": args.get("hp_max"),
                                "conditions": args.get("conditions"),
                                "is_alive": args.get("is_alive"),
                                "note": args.get("note", ""),
                                "revealed": bool(args.get("revealed", False)),
                            }
                            audit = (
                                f"[MASTER TOOL] diweddaru_npc (СКРЫТО): npc='{npc_name}' "
                                f"hp={args.get('hp_current', '?')}/{args.get('hp_max', '?')} "
                                f"conditions={args.get('conditions')} "
                                f"revealed={bool(args.get('revealed', False))}"
                            )
                            logger.info(audit)
                            tool_audit.append(audit)
                            if npc_state_syncer is not None:
                                try:
                                    sync_result_text = await npc_state_syncer(payload)
                                except Exception as e:
                                    logger.error(f"npc_state_syncer failed: {e}")
                                    sync_result_text = f"Error saving NPC state: {e}"
                            else:
                                sync_result_text = ("NPC state sync unavailable — keep tracking the NPC state yourself this turn.")
                            tool_results.append({"call_id": tc["id"], "result": sync_result_text})

                        elif tool_name == "npc_ai_action":
                            npc_group = args.get("npc_group", [])
                            battle_context = args.get("context", "")
                            audit = f"[MASTER TOOL] npc_ai_action: {len(npc_group)} NPCs, context='{battle_context[:80]}'"
                            logger.info(audit)
                            tool_audit.append(audit)
                            if npc_ai_enabled:
                                try:
                                    decisions = await self.npc_ai_decision(npc_group, battle_context, session_id=session_id)
                                    if decisions:
                                        decision_text = json.dumps(decisions, ensure_ascii=False, indent=2)
                                    else:
                                        decision_text = "NPC AI returned no decisions — use your own judgment."
                                except Exception as e:
                                    logger.error(f"npc_ai_action failed: {e}")
                                    decision_text = f"Error getting NPC AI decision: {e}"
                            else:
                                decision_text = "NPC AI disabled this turn — make tactical decisions yourself using NPC traits."
                            tool_results.append({"call_id": tc["id"], "result": decision_text})

                        elif tool_name == "calculate":
                            expr = args.get("expression", "0")
                            try:
                                result_val = eval(expr, {"__builtins__": {}}, {})
                                tool_results.append({"call_id": tc["id"], "result": str(result_val)})
                            except Exception as calc_e:
                                tool_results.append({"call_id": tc["id"], "result": f"Calculation error: {calc_e}"})

                        elif tool_name == "request_roll_intent":
                            # ── ModerAI dispatch (Idea 1) ──
                            # Master signals intent with @username; ModerAI fetches the
                            # character sheet from DB, determines the modifier, and dispatches
                            # the actual roll (inline button for player / server-side for NPC).
                            # The result comes back as text — Master uses it to write narrative.
                            username = args.get("username", "npc")
                            roll_type = args.get("roll_type", "skill")
                            description = args.get("description", "")
                            audit_start = (
                                f"[MASTER TOOL] request_roll_intent: username='{username}' "
                                f"roll_type='{roll_type}' description='{description[:80]}'"
                            )
                            logger.info(audit_start)
                            tool_audit.append(audit_start)

                            try:
                                dispatch_result = await self.moder_ai_dispatch_roll(
                                    username=username,
                                    roll_type=roll_type,
                                    description=description,
                                    player_roll_requester=player_roll_requester,
                                    session_id=getattr(self, "_current_session_id", ""),
                                    pc_names=pc_names,
                                )
                            except Exception as e:
                                logger.error(f"[moder_ai_dispatch] failed: {e}", exc_info=True)
                                dispatch_result = f"[ModerAI dispatch failed: {e}]"

                            tool_results.append({
                                "call_id": tc["id"],
                                "result": dispatch_result,
                            })
                            audit_end = (
                                f"[MASTER TOOL] request_roll_intent: username='{username}' "
                                f"-> {dispatch_result[:120]}"
                            )
                            logger.info(audit_end)
                            tool_audit.append(audit_end)

                            # If the dispatch produced a roll result (visible to player),
                            # also surface it in the visible_rolls block. We detect this by
                            # looking for "nat" + "total" in the result text.
                            if "nat" in dispatch_result and "total" in dispatch_result:
                                visible_rolls.append(f"🎲 {dispatch_result}")

                        else:
                            tool_results.append({
                                "call_id": tc["id"],
                                "result": f"Error: Master should not call {tool_name}",
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

                    response = await self.master.chat(
                        messages, system_prompt=get_prompt("master", session_id), tools=MASTER_TOOLS,
                    )
                    continue

                # Final narrative — fall back to accumulated partial content
                # if the model returned empty on the stop iteration (Gemini quirk).
                raw_narrative = (choice.get("content") or "").strip()
                if not raw_narrative and fallback_narrative:
                    logger.warning(f"[master] Final iteration had empty content; using fallback ({len(fallback_narrative)} chars from tool_call iteration)")
                    raw_narrative = fallback_narrative
                # If STILL empty after fallback, construct a minimal narrative from
                # visible rolls so the player isn't left with nothing.
                if not raw_narrative:
                    if visible_rolls:
                        raw_narrative = "Мастер бросил кости, но мир замер в ожидании..."
                        logger.warning("[master] Empty narrative even after fallback; using roll-based placeholder")
                    elif hidden_rolls:
                        raw_narrative = "Что-то произошло за кулисами... Мастер молчит."
                        logger.warning("[master] Empty narrative with only hidden rolls; using generic placeholder")
                    else:
                        raw_narrative = "Мастер задумался, но мир не изменился."
                        logger.warning("[master] Empty narrative with no rolls at all")
                else:
                    # BUG #4 POST-PROCESSING: detect if the new narrative is mostly a
                    # verbatim copy of any of the recent_narratives (>=60% overlap of
                    # the first 300 chars). If so, log a warning so we know the model
                    # ignored the anti-repeat instruction. We do NOT modify the text —
                    # the prompt-level instruction is the right place to fix this, and
                    # silently editing the LLM's output would be worse than letting it
                    # through. The warning is for debugging/observability.
                    if recent_narratives:
                        new_check = raw_narrative[:300].lower()
                        for prev in recent_narratives:
                            prev_check = prev[:300].lower()
                            if not prev_check:
                                continue
                            # Simple substring-overlap heuristic: if 60%+ of the
                            # previous narrative's words appear in the new one, flag it.
                            prev_words = set(prev_check.split())
                            new_words = set(new_check.split())
                            if prev_words and new_words:
                                overlap = len(prev_words & new_words) / len(prev_words)
                                if overlap >= 0.6:
                                    logger.warning(
                                        f"[master] BUG #4: new narrative has {overlap:.0%} "
                                        f"word overlap with a recent narrative — model may "
                                        f"be repeating itself."
                                    )
                                    tool_audit.append(
                                        f"[ANTI-REPEAT] {overlap:.0%} overlap with a recent narrative"
                                    )
                                    break

                if visible_rolls:
                    dice_block = "📑 **Броски:**\n" + "\n".join(visible_rolls) + "\n\n"
                else:
                    dice_block = ""

                player_text = dice_block + raw_narrative
                gm_log = "\n".join(hidden_rolls) if hidden_rolls else ""

                return {
                    "player_text": player_text,
                    "gm_log": gm_log,
                    "raw_narrative": raw_narrative,
                    "tool_audit": tool_audit,
                }

            logger.error(f"[master] Max iterations reached")
            return {
                "player_text": "[Ошибка: Мастер зациклился на костях]",
                "gm_log": "",
                "raw_narrative": "",
                "tool_audit": tool_audit,
            }

        except Exception as e:
            # BUG #13: ApiError carries a human-readable message and trace_id;
            # surface both to the player instead of dumping the raw error JSON.
            try:
                from .client import ApiError
            except ImportError:
                ApiError = None  # type: ignore
            if ApiError and isinstance(e, ApiError):
                logger.error(f"Master error: {e} (trace_id={e.trace_id})")
                user_msg = e.args[0] if e.args else str(e)
                return {
                    "player_text": f"❌ *[Ошибка Мастера: {user_msg}]*",
                    "gm_log": f"ERROR: {e}",
                    "raw_narrative": "",
                    "tool_audit": tool_audit,
                }
            logger.error(f"Master error: {e}")
            return {
                "player_text": f"*[Ошибка Мастера: {str(e)}]*",
                "gm_log": f"ERROR: {e}",
                "raw_narrative": "",
                "tool_audit": tool_audit,
            }

    DB_WRITE_TOOL_NAMES = (
        "change_hp", "add_condition", "remove_condition",
        "add_item", "remove_item", "change_gold",
        "advance_time", "update_quest", "set_location",
        "change_reputation", "use_resource", "add_world_event",
        "create_location", "get_location", "create_npc", "get_npc",
        "set_npc_relation", "create_lore", "get_lore",
        "set_market_price", "add_economic_event", "add_effect", "create_timer",
        "get_srd_monster", "get_srd_item", "get_srd_spell",
        "level_up_character", "set_ability_score", "add_feature",
        "add_proficiency", "add_spell_known", "update_location_relation",
        "set_character_goal",
    )


    async def answer_question(
        self,
        session_history: List[Dict],
        question: str,
        player_name: str,
        character_context: str = "",
        session_id: str = "",
    ) -> str:
        """character_context: optional block with the asking player's own character info
        (backstory, active quests, known NPCs) — without this, /ask only ever saw the
        last 30 lines of game narrative and had no way to answer questions about the
        character's own backstory/history, even when it was asked directly."""
        messages = []
        for msg in session_history[-30:]:
            messages.append(msg)
        context_block = f"\n\nКонтекст о персонаже игрока (используй, если вопрос о нём):\n{character_context}" if character_context else ""
        messages.append({
            "role": "user",
            "content": f"[УТОЧНЕНИЕ - НЕ игровой ход]\n\nИгрок **{player_name}** спрашивает: {question}{context_block}\n\nОтветь кратко.",
        })
        try:
            response = await self.master.chat(messages, system_prompt=get_prompt("ask", session_id), max_tokens=1024)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Ask error: {e}")
            return f"*[Ошибка: {str(e)}]*"


    async def generate_encounter(self, context: str, party_level: int, party_size: int, terrain: str = "") -> str:
        enc_prompt = f"""Придумай случайное столкновение для D&D 5e (2024). Тёмное фэнтези.
Контекст: {context} | Уровень партии: {party_level} | Размер: {party_size} | Местность: {terrain or "любая"}
Ответь: **Столкновение**, **Описание**, **Существа**, **Сложность**, **Возможности**."""
        messages = [{"role": "user", "content": enc_prompt}]
        try:
            response = await self.master.chat(messages)
            return response["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Encounter error: {e}")
            return f"*[Ошибка: {e}]*"


    async def generate_world_seed(self, theme: str = "dark fantasy", character_briefs: Optional[List[str]] = None) -> str:
        """F1: theme is now an actual parameter (was hardcoded before).
        F2: character_briefs (short bio/goal per PC, e.g. 'Эйра — лесной эльф-паладин на
        службе у Высших, хочет их же и свергнуть') are woven into the generated world
        instead of being ignored, so the setting doesn't contradict what players wrote."""
        briefs_block = ""
        if character_briefs:
            briefs_block = "\n\nПЕРСОНАЖИ ПАРТИИ (обязательно впиши крючки их предысторий в мир — фракции/NPC/локации из их биографий ДОЛЖНЫ существовать и быть согласованы с тем, что они написали):\n" + "\n".join(f"- {b}" for b in character_briefs)

        w_prompt = f"""Создай начальный мир для кампании D&D 5e. Тема: {theme}.{briefs_block}

Опиши мир текстом: ключевые локации, важные NPC, лор, фракции, экономику. Укажи основную валюту мира и, если она нестандартная (не золотые монеты), опиши кратко: название, какие бывают монеты/номиналы, как соотносятся.
Если у персонажей партии есть заявленные в предыстории цели, враги, организации или родственники — ОБЯЗАТЕЛЬНО создай их в мире (например, если персонаж хочет отомстить конкретной фракции — эта фракция должна существовать и быть тем, чем игрок её описал, а не отсутствовать или быть переименованной/уничтоженной без причины).
НЕ вызывай инструменты — просто опиши мир текстом. Закончи описание полностью, не обрывай посреди предложения. На русском."""
        messages = [{"role": "user", "content": w_prompt}]
        try:
            response = await self.master.chat(messages, max_tokens=8000)
            text = response["choices"][0]["message"].get("content", "")
            # If model returned tool calls instead of text, return empty to trigger fallback
            if not text or text.strip().startswith("{"):
                return ""
            return text
        except Exception as e:
            logger.error(f"World gen error: {e}")
            return "*[Ошибка генерации мира]*"


