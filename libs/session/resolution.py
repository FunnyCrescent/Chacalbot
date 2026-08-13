"""ResolutionMixin — round resolution (Master + DB-Bot + apply actions)."""
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
    NPC_AI_ENABLED, NPC_AI_MODE,
)
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)


class ResolutionMixin:
    """ResolutionMixin — round resolution (Master + DB-Bot + apply actions)."""

    def _build_sheets_with_usernames(self, db, session_id: str, chars) -> List[str]:
        """Build character sheets for the Master, prepending @username to each.

        ModerAI dispatch (Idea 1) requires the Master to know each player's
        @username so it can pass it to `request_roll_intent`. This helper wraps
        each character's progression summary with a `@username: ...` header.

        Falls back gracefully if a character has no linked player or the player
        has no telegram username (uses character name as the username placeholder).
        """
        if not chars:
            return []
        try:
            players = db.get_players(session_id)
            player_by_id = {p.user_id: p for p in players}
        except Exception as e:
            logger.warning(f"[sheets_with_usernames] failed to load players: {e}")
            player_by_id = {}

        sheets = []
        for c in chars:
            try:
                summary = db.get_character_progression_summary(c.id)
            except Exception as e:
                logger.warning(f"[sheets_with_usernames] failed to load sheet for {c.name}: {e}")
                summary = f"{c.name} ({c.race} {c.class_name}, уровень {c.level})"

            player = player_by_id.get(c.player_id)
            if player and player.username:
                uname = f"@{player.username}"
            elif player and player.display_name:
                uname = f"@{player.display_name}"
            else:
                uname = f"@{c.name}"

            sheets.append(f"@username: {uname}\n{summary}")
        return sheets

    async def resolve_round_master_only(self, session_id: str, roll_mode: str = "mixed",
                            player_roll_requester=None) -> Dict[str, str]:
        """
        Phase 1 of round resolution: build context, call the Master, save the narrative
        + action history, advance time, and open the NEXT round's action collection.
        Deliberately does NOT run the DB-Bot — that's run_db_bot_phase(), which the
        caller (bot.py's _resolve_and_send) launches as a background task right after
        this phase's narrative has already been sent to players. Players used to wait
        for BOTH the Master and the DB-Bot before seeing anything; now they see the
        story as soon as the Master is done, while the DB-Bot catches up in the
        background (see is_db_busy/mark_pending_resolve for how the NEXT round's
        resolution waits for that background pass to finish before calling the Master
        again with fresh state).

        player_roll_requester: async callback(character_name, tool_args, tool_call_id) -> dict,
        implemented by bot.py (sends an inline button to the specific player, awaits the
        click). Forwarded straight through to DMEngine.process_master_turn — see A3.
        """
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return {"player_text": "Error: No queue state found.", "gm_log": ""}

        state.is_resolving = True
        db.set_queue_state(state)

        try:
            collected = json.loads(state.collected_actions)

            player_actions = {}
            nick_by_player_id: Dict[int, str] = {}
            for player_id_str, action in collected.items():
                player = db.get_player(int(player_id_str), session_id)
                if player:
                    nick = player.username or player.display_name
                    player_actions[nick] = f"Дн. {action}"
                    nick_by_player_id[int(player_id_str)] = nick

            # A4: pull any /roll results submitted this round, keyed by nick instead of
            # player_id, so DMEngine can slot them into a separate [ROLL] tag per player —
            # never nested inside [PLAYER], per the no-nesting rule.
            pending_rolls = self._pop_pending_manual_rolls(session_id)
            verified_rolls: Dict[str, List[str]] = {}
            for pid, rolls in pending_rolls.items():
                nick = nick_by_player_id.get(pid)
                if nick:
                    verified_rolls[nick] = rolls

            # Only narrative/action history goes into the Master's context. entry_type
            # "ooc" (from /ask, /dbask) used to be pulled in unfiltered here, and after a
            # few OOC exchanges the Master would start imitating their "[ASK] ..."
            # out-of-character formatting in its own IN-character narration.
            history = db.get_history(session_id, limit=30)
            history_msgs = []
            for h in history:
                if h.entry_type == "ooc":
                    continue
                role = "user" if h.author != "DM" else "assistant"
                # 'ask' entries (from /gofyn) ARE included in Master context so the DM
                # remembers what it told players about the world. Marked with [ВОПРОС]/[ОТВЕТ ДМ].
                if h.entry_type == "ask" and h.author != "DM":
                    role = "user"
                # G2 fix: bake the author's name into the content itself. Role alone
                # (user/assistant) collapses every different player into one voice —
                # after a few rounds with 3+ players the Master can no longer tell who
                # is who in old history. DM/system entries are left as-is (already clear).
                content = h.content if h.author in ("DM", "SYSTEM", "GM_SECRET") else f"{h.author}: {h.content}"
                history_msgs.append({"role": role, "content": content})

            raw_sheets = db.get_all_character_sheets(session_id)

            # Build context
            context_lines = []
            session = db.get_session(session_id)

            if session and session.pvp_active:
                context_lines.append("⚔️ PvP АКТИВЕН: игроки могут атаковать друг друга.")

            chars = db.get_session_characters(session_id)
            if chars:
                context_lines.append("\n--- СТАТУС И РАСЫ ПЕРСОНАЖЕЙ (учитывай расу для соцжестокости мира) ---")
                for c in chars:
                    status = "💀" if not c.is_alive else f"❤️ {c.hp}/{c.max_hp} HP"
                    conds = db.get_conditions(session_id, c.id)
                    cond_str = ", ".join(cc.condition for cc in conds) if conds else ""
                    loc = db.get_location(session_id, c.id)
                    loc_str = f"📍 {loc.location_name}" if loc and loc.location_name else ""
                    race_str = f"[{c.race}]" if c.race else ""
                    context_lines.append(f"{c.name} {race_str}: {status}{f' [{cond_str}]' if cond_str else ''} {loc_str}")

            # B2: the Master now sees the CURRENT, DB-backed progression of each
            # character (level-ups, new features/proficiencies) instead of only the
            # static text of the file they uploaded on day one.
            # ModerAI dispatch (Idea 1): prepend @username to each sheet so the Master
            # can use @username in `request_roll_intent` tool calls.
            progression_summaries = self._build_sheets_with_usernames(db, session_id, chars)

            quests = db.get_quests(session_id, status="active")
            if quests:
                context_lines.append("\n--- АКТИВНЫЕ КВЕСТЫ ---")
                for q in quests[:5]:
                    assignee = f" ({q.assignee_name})" if q.assignee_name else ""
                    context_lines.append(f"• {q.title}{assignee}")

            goals = db.get_character_goals(session_id, status="active")
            if goals:
                context_lines.append("\n--- ЛИЧНЫЕ ЦЕЛИ ПЕРСОНАЖЕЙ (не квесты — не требуют подтверждения NPC) ---")
                for g in goals[:8]:
                    context_lines.append(f"• {g['character_name']}: {g['title']}")

            factions = db.get_factions(session_id)
            if factions:
                context_lines.append("\n--- ФРАКЦИИ ---")
                for f in factions[:5]:
                    context_lines.append(f"• {f.name}: {f.attitude} (реп: {f.reputation:+d})")

            gt = db.get_game_time(session_id)
            context_lines.append(f"\n--- ВРЕМЯ: День {gt.day}, {gt.hour:02d}:{gt.minute:02d} | {gt.weather} | {gt.temperature} ---")

            # Currency context — tell the Master about the world's custom currency
            if session and session.currency_name and session.currency_name != "золото":
                curr_line = f"\n--- ВАЛЮТА МИРА: {session.currency_name} ({session.currency_symbol})"
                if session.currency_sub_name:
                    curr_line += f", {session.currency_sub_name} ({session.currency_sub_symbol}) = {session.currency_sub_value}{session.currency_symbol}"
                if session.currency_super_name:
                    curr_line += f", {session.currency_super_name} ({session.currency_super_symbol}) = {session.currency_super_value}{session.currency_symbol}"
                curr_line += " ---"
                context_lines.append(curr_line)

            # DB Journal context — NEW!
            db_journal = db.get_journal_summary(session_id)

            context_block = "\n".join(context_lines) if context_lines else ""

            # G1: "doppelganger" retrieval step — ask what we already know about the
            # current scene (current location(s) + this round's declared actions)
            # BEFORE the Master writes narrative, from the embedding-based world diary.
            memory_query = " ".join(player_actions.values())
            if chars:
                # LocationBinding is a plain (unfrozen) dataclass, so it has no __hash__ —
                # putting instances directly into a {set} crashed every round with
                # "unhashable type: 'LocationBinding'". Dedupe by the name string instead.
                loc_names = set()
                for c in chars:
                    loc = db.get_location(session_id, c.id)
                    if loc and loc.location_name:
                        loc_names.add(loc.location_name)
                memory_query += " " + " ".join(loc_names)
            memory_digest = await self.memory_store.build_context_digest(session_id, memory_query)

            pc_names_list = [c.name for c in chars] if chars else []

            result = await self.dm.process_master_turn(
                session_history=history_msgs,
                player_actions=player_actions,
                character_sheets=progression_summaries if progression_summaries else None,
                context=context_block,
                roll_mode=roll_mode,
                summary=session.summary if session else "",
                db_journal=db_journal,
                verified_rolls=verified_rolls,
                player_roll_requester=player_roll_requester,
                memory_digest=memory_digest,
                combat_starter=lambda participants, reason, **kwargs: self._combat_starter_async(session_id, participants, reason, pc_initiatives=kwargs.get("pc_initiatives")),
                combat_ender=lambda reason: self._combat_ender_async(session_id, reason),
                npc_ai_enabled=True,
                pc_names=pc_names_list,
                session_id=session_id,
            )

            # Defensive cleanup: strip any [DM]/[ROLL]/[PLAYER]-style tags the model may
            # have echoed from its own input markup into its output (see strip_stray_tags).
            player_text = strip_stray_tags(result.get("player_text", "*[No response]*"))
            gm_log = strip_stray_tags(result.get("gm_log", ""))
            raw_narrative = strip_stray_tags(result.get("raw_narrative", ""))
            tool_audit = result.get("tool_audit", [])

            # A2: every roll_dice/request_player_roll call the Master made is logged
            # loudly to the console/markdown journal — not just buried in debug logs.
            for line in tool_audit:
                logger.info(line)

            # Save to history
            for nick, full_action in player_actions.items():
                db.add_history(HistoryEntry(
                    session_id=session_id,
                    author=nick,
                    content=full_action,
                    entry_type="action",
                ))

            db.add_history(HistoryEntry(
                session_id=session_id,
                author="DM",
                content=player_text,
                entry_type="narrative",
            ))

            if gm_log:
                db.add_history(HistoryEntry(
                    session_id=session_id,
                    author="GM_SECRET",
                    content=gm_log,
                    entry_type="gm_secret",
                ))

            # G1: write this round's narrative into the semantic world diary so future
            # rounds (even much later, past the ~30-message history window) can recall it.
            await self.memory_store.remember(session_id, player_text, source_type="narrative")

            # Auto-advance time
            if session and session.combat_active:
                db.advance_time(session_id, minutes=1)
            else:
                db.advance_time(session_id, minutes=10)

            db.clear_queue_state(session_id)

            if session and session.combat_active and COMBAT_INITIATIVE_ENABLED:
                # V10b: Per-turn combat — don't start collection here.
                # The per-turn orchestrator in bot.py handles each turn as a separate message.
                result["combat_active_after"] = True
            elif session and session.combat_active:
                self.advance_turn(session_id)
                self.start_action_collection(session_id)
            else:
                self.start_action_collection(session_id)

            # G3 fix: get_history(limit=30) is CAPPED at 30, so "len(...) >= 30" was true
            # forever after the 30th message and fired a full re-summarize EVERY round.
            # Use the real uncapped count against a stored watermark instead — only
            # fires once per +30 messages, and only if there's really new content.
            total_history_count = db.count_history(session_id)
            if session and (total_history_count - session.summary_at_count) >= 30:
                asyncio.create_task(self.summarize_and_save(session_id))

            return {
                "player_text": player_text,
                "gm_log": gm_log,
                "raw_narrative": raw_narrative,
                "tool_audit": tool_audit,
                "raw_sheets": raw_sheets,
            }

        except Exception as e:
            logger.error(f"Error resolving round (master phase): {e}")
            state.is_resolving = False
            db.set_queue_state(state)
            return {
                "player_text": f"*[Ошибка разрешения раунда: {str(e)}]*",
                "gm_log": f"ERROR: {e}",
                "raw_narrative": "",
                "tool_audit": [],
                "raw_sheets": [],
                "error": True,
            }


    async def resolve_non_combat_round(self, session_id: str, roll_mode: str = "mixed",
                                        player_roll_requester=None) -> Dict[str, str]:
        """
        DUAL NARRATIVE — resolves non-combat players' actions with a SEPARATE Master call.
        This runs in parallel with the combat turn loop, so non-combat players don't wait
        for combat to finish.

        Called from bot.py when a non-combat player submits Дн. during active combat.
        Returns the same shape as resolve_round_master_only.
        """
        if not DUAL_NARRATIVE_ENABLED:
            return {"player_text": "", "gm_log": "", "raw_narrative": "", "tool_audit": []}

        db = self.db_manager.get_db(session_id)

        # Pop the queued actions atomically
        async with self.get_non_combat_lock(session_id):
            non_combat_actions = self.pop_non_combat_queue(session_id)

        if not non_combat_actions:
            return {"player_text": "", "gm_log": "", "raw_narrative": "", "tool_audit": []}

        # Build player_actions from non-combat players
        player_actions = {}
        nick_by_player_id: Dict[int, str] = {}
        for player_id, action in non_combat_actions.items():
            player = db.get_player(player_id, session_id)
            if player:
                nick = player.username or player.display_name
                player_actions[nick] = f"Дн. {action}"
                nick_by_player_id[player_id] = nick

        if not player_actions:
            return {"player_text": "", "gm_log": "", "raw_narrative": "", "tool_audit": []}

        # Pull any /roll results for non-combat players
        pending_rolls = self._pop_pending_manual_rolls(session_id)
        verified_rolls: Dict[str, List[str]] = {}
        for pid, rolls in pending_rolls.items():
            nick = nick_by_player_id.get(pid)
            if nick:
                verified_rolls[nick] = rolls

        # Build context — same as resolve_round_master_only but with non-combat context
        history = db.get_history(session_id, limit=30)
        history_msgs = []
        for h in history:
            if h.entry_type == "ooc":
                continue
            role = "user" if h.author != "DM" else "assistant"
            content = h.content if h.author in ("DM", "SYSTEM", "GM_SECRET") else f"{h.author}: {h.content}"
            history_msgs.append({"role": role, "content": content})

        chars = db.get_session_characters(session_id)
        progression_summaries = self._build_sheets_with_usernames(db, session_id, chars)

        session = db.get_session(session_id)
        context_lines = ["⚠️ ВНИМАНИЕ: Параллельно идёт БОЙ с другими персонажами. Эти игроки НЕ в бою — "
                         "пиши нарратив только для них. Бой происходит отдельно и одновременно."]
        if chars:
            context_lines.append("\n--- СТАТУС ПЕРСОНАЖЕЙ ---")
            for c in chars:
                status = "💀" if not c.is_alive else f"❤️ {c.hp}/{c.max_hp} HP"
                conds = db.get_conditions(session_id, c.id)
                cond_str = ", ".join(cc.condition for cc in conds) if conds else ""
                context_lines.append(f"{c.name}: {status}{f' [{cond_str}]' if cond_str else ''}")

        quests = db.get_quests(session_id, status="active")
        if quests:
            context_lines.append("\n--- АКТИВНЫЕ КВЕСТЫ ---")
            for q in quests[:5]:
                assignee = f" ({q.assignee_name})" if q.assignee_name else ""
                context_lines.append(f"• {q.title}{assignee}")

        gt = db.get_game_time(session_id)
        context_lines.append(f"\n--- ВРЕМЯ: День {gt.day}, {gt.hour:02d}:{gt.minute:02d} | {gt.weather} | {gt.temperature} ---")

        # Currency context — tell the Master about the world's custom currency
        session = db.get_session(session_id)
        if session and session.currency_name and session.currency_name != "золото":
            curr_line = f"\n--- ВАЛЮТА МИРА: {session.currency_name} ({session.currency_symbol})"
            if session.currency_sub_name:
                curr_line += f", {session.currency_sub_name} ({session.currency_sub_symbol}) = {session.currency_sub_value}{session.currency_symbol}"
            if session.currency_super_name:
                curr_line += f", {session.currency_super_name} ({session.currency_super_symbol}) = {session.currency_super_value}{session.currency_symbol}"
            curr_line += " ---"
            context_lines.append(curr_line)

        db_journal = db.get_journal_summary(session_id)
        context_block = "\n".join(context_lines) if context_lines else ""

        memory_digest = await self.memory_store.build_context_digest(session_id, " ".join(player_actions.values()))

        pc_names_list = [c.name for c in chars] if chars else []

        result = await self.dm.process_master_turn(
            session_history=history_msgs,
            player_actions=player_actions,
            character_sheets=progression_summaries if progression_summaries else None,
            context=context_block,
            roll_mode=roll_mode,
            summary=session.summary if session else "",
            db_journal=db_journal,
            verified_rolls=verified_rolls,
            player_roll_requester=player_roll_requester,
            memory_digest=memory_digest,
            combat_starter=None,  # NON-combat — never start combat from here
            combat_ender=None,    # NON-combat — never end combat from here
            npc_ai_enabled=False, # NON-combat — no NPC AI
            pc_names=pc_names_list,
            session_id=session_id,
        )

        player_text = strip_stray_tags(result.get("player_text", "*[No response]*"))
        gm_log = strip_stray_tags(result.get("gm_log", ""))
        raw_narrative = strip_stray_tags(result.get("raw_narrative", ""))
        tool_audit = result.get("tool_audit", [])

        for line in tool_audit:
            logger.info(line)

        # Save to history
        for nick, full_action in player_actions.items():
            db.add_history(HistoryEntry(
                session_id=session_id,
                author=nick,
                content=full_action,
                entry_type="action",
            ))

        db.add_history(HistoryEntry(
            session_id=session_id,
            author="DM",
            content=player_text,
            entry_type="narrative",
        ))

        if gm_log:
            db.add_history(HistoryEntry(
                session_id=session_id,
                author="GM_SECRET",
                content=gm_log,
                entry_type="gm_secret",
            ))

        await self.memory_store.remember(session_id, player_text, source_type="narrative")

        # Advance time (non-combat: 10 min per round)
        db.advance_time(session_id, minutes=10)

        logger.info(f"[dual-narrative] Non-combat round resolved for {len(player_actions)} players in session {session_id}")

        return {
            "player_text": player_text,
            "gm_log": gm_log,
            "raw_narrative": raw_narrative,
            "tool_audit": tool_audit,
            "raw_sheets": [],
            "non_combat": True,
        }


    async def run_db_bot_phase(self, session_id: str, raw_narrative: str, player_text: str,
                               raw_sheets: Optional[List[str]] = None,
                               player_roll_requester=None,
                               session_history: Optional[List[Dict]] = None) -> Dict:
        """
        Phase 2 of round resolution — runs the DB-Bot against the narrative phase 1 JUST
        produced (already sent to players) and applies the resulting game actions.
        Meant to be launched as a fire-and-forget asyncio background task right after
        phase 1 returns — see bot.py's _resolve_and_send, which also flips
        is_db_busy(session_id) around this call so read commands can warn instead of
        showing possibly-stale state, and resolves any round that finished collecting
        while this was running (see mark_pending_resolve/pop_pending_resolve).

        Before this split, a DB-Bot exception here would discard the ALREADY-SUCCESSFUL
        Master narrative too (both phases shared one try/except) — players would see
        only an error even though the story had actually been generated fine. Now a
        DB-Bot failure only means this round's state changes are lost, not the story.

        New (Idea 2 — reconciliation):
        - player_roll_requester: passed through to moder_ai_dispatch_roll when DB-Bot
          calls `dispatch_roll`, so ModerAI can dispatch the inline-button roll.
        - session_history: passed through to moder_ai_ask_master when DB-Bot calls
          `ask_master`, so Master has context to answer the clarifying question.
        """
        db = self.db_manager.get_db(session_id)
        try:
            db_state = []
            chars = db.get_session_characters(session_id)
            if chars:
                db_state.append("--- Current DB State ---")
                for c in chars:
                    gold = db.get_gold_balance(session_id, c.id)
                    inv = db.get_inventory(session_id, c.id)
                    loc = db.get_location(session_id, c.id)
                    conds = db.get_conditions(session_id, c.id)
                    gold_str = ", ".join(f"{v}{k}" for k, v in gold.items() if v)
                    db_state.append(f"{c.name}: HP={c.hp}/{c.max_hp}, Валюта=[{gold_str or '0gp'}], Loc={loc.location_name if loc else '?'}, Inv={[i['item'] for i in inv]}, Conds={[cc.condition for cc in conds]}")

            existing_npcs = db.get_npcs(session_id, alive_only=False)
            if existing_npcs:
                db_state.append("")
                db_state.append("--- Уже существующие NPC в базе (ИСПОЛЬЗУЙ ТОЧНО ЭТИ ЖЕ ИМЕНА/ТИТУЛЫ для тех же персонажей — не создавай дубликат с другой формулировкой одного и того же NPC) ---")
                for n in existing_npcs:
                    db_state.append(f"• {n.name}")

            db_state_text = chr(10).join(db_state) if db_state else ""
            db_context = f"Current DB state:{chr(10)}{db_state_text}{chr(10)}{chr(10)}Master text:{chr(10)}{raw_narrative or player_text}"

            # For world generation (large narratives), allow more DB-Bot iterations
            # since there may be dozens of locations, NPCs, and lore entries to create.
            world_gen_iters = 30 if len(raw_narrative or "") > 10000 else None

            logger.info(f"[DB-BOT] Calling process_db_bot for session {session_id}")
            game_actions = await self.dm.process_db_bot(
                raw_narrative=db_context,
                context="",
                character_sheets=raw_sheets,
                session_id=session_id,
                max_iterations_override=world_gen_iters,
                player_roll_requester=player_roll_requester,
                session_history=session_history,
            )
            logger.info(f"[DB-BOT] Received {len(game_actions)} actions from DB-Bot")

            applied, errors = 0, []
            if game_actions:
                applied, errors = self._apply_game_actions(session_id, game_actions)
                if errors:
                    logger.warning(f"Game action errors: {errors}")
                logger.info(f"Applied {applied} game actions from DB-Bot")

            # G3: opportunistic memory consolidation ("sleep") — cheap no-op check if
            # there aren't enough raw entries yet, so safe to call every round.
            await self.memory_store.maybe_sleep(session_id)

            return {"game_actions_applied": applied, "errors": errors}
        except Exception as e:
            logger.error(f"Error in DB-Bot phase: {e}")
            return {"game_actions_applied": 0, "errors": [str(e)]}


    def _apply_game_actions(self, session_id: str, actions: List[Dict]) -> Tuple[int, List[str]]:
        """Apply game state changes returned by AI tools."""
        db = self.db_manager.get_db(session_id)
        applied = 0
        errors: List[str] = []

        chars = {c.name.lower(): c for c in db.get_session_characters(session_id)}

        def find_char(name: str):
            name_lower = name.lower()
            if name_lower in chars:
                return chars[name_lower]
            for c in chars.values():
                if name_lower in c.name.lower() or c.name.lower() in name_lower:
                    return c
            return None

        for act in actions:
            tool = act.get("tool_name")
            args = act.get("arguments", {})
            try:
                if tool == "change_hp":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"change_hp: char not found '{args.get('character_name')}'")
                        continue
                    delta = args.get("delta", 0)
                    source = args.get("source", "AI")
                    self.change_hp(session_id, char.id, char.name, delta, source)
                    applied += 1

                elif tool == "add_condition":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"add_condition: char not found '{args.get('character_name')}'")
                        continue
                    cond = args.get("condition", "").lower()
                    source = args.get("source", "AI")
                    duration = args.get("duration", "")
                    self.add_condition(session_id, char.id, char.name, cond, source, duration)
                    applied += 1

                elif tool == "remove_condition":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"remove_condition: char not found '{args.get('character_name')}'")
                        continue
                    cond = args.get("condition", "").lower()
                    self.remove_condition(session_id, char.id, cond)
                    applied += 1

                elif tool == "add_item":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"add_item: char not found '{args.get('character_name')}'")
                        continue
                    item = args.get("item_name", "")
                    qty = args.get("quantity", 1)
                    desc = args.get("description", "")
                    self.add_item(session_id, char.id, char.name, item, qty, desc)
                    applied += 1

                elif tool == "remove_item":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"remove_item: char not found '{args.get('character_name')}'")
                        continue
                    item = args.get("item_name", "")
                    qty = args.get("quantity", 1)
                    self.remove_item(session_id, char.id, item, qty)
                    applied += 1

                elif tool == "change_gold":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"change_gold: char not found '{args.get('character_name')}'")
                        continue
                    amount = args.get("amount", 0)
                    currency = args.get("currency", "gp")
                    if currency not in ("cp", "sp", "ep", "gp", "pp"):
                        currency = "gp"
                    reason = args.get("reason", "AI")
                    self.add_gold(session_id, char.id, char.name, reason=reason, **{currency: amount})
                    applied += 1

                elif tool == "advance_time":
                    minutes = args.get("minutes", 0)
                    hours = args.get("hours", 0)
                    reason = args.get("reason", "AI")
                    self.advance_time(session_id, minutes=minutes, hours=hours)
                    applied += 1

                elif tool == "update_quest":
                    title = args.get("title", "")
                    status = args.get("status", "active")
                    quests = db.get_quests(session_id, status="active")
                    matched = None
                    for q in quests:
                        if title.lower() in q.title.lower() or q.title.lower() in title.lower():
                            matched = q
                            break
                    if matched:
                        self.update_quest(matched.id, status=status)
                        applied += 1
                    else:
                        errors.append(f"update_quest: no matching quest '{title}'")

                elif tool == "set_location":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"set_location: char not found '{args.get('character_name')}'")
                        continue
                    loc_name = args.get("location_name", "")
                    loc_desc = args.get("location_description", "")
                    self.set_location(session_id, char.id, loc_name, loc_desc)
                    applied += 1

                elif tool == "change_reputation":
                    faction_name = args.get("faction_name", "")
                    delta = args.get("delta", 0)
                    factions = db.get_factions(session_id)
                    matched = None
                    for f in factions:
                        if faction_name.lower() in f.name.lower() or f.name.lower() in faction_name.lower():
                            matched = f
                            break
                    if matched:
                        self.change_reputation(session_id, matched.id, delta)
                        applied += 1
                    else:
                        errors.append(f"change_reputation: no matching faction '{faction_name}'")

                elif tool == "use_resource":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"use_resource: char not found '{args.get('character_name')}'")
                        continue
                    resource = args.get("resource_name", "")
                    amount = args.get("amount", 1)
                    self.use_resource(session_id, char.id, resource, amount)
                    applied += 1

                elif tool == "add_world_event":
                    event_type = args.get("event_type", "narrative")
                    description = args.get("description", "")
                    self.add_world_event(session_id, event_type, description)
                    applied += 1

                elif tool == "create_location":
                    loc_id = str(uuid.uuid4())[:8]
                    db.create_location(Location(
                        id=loc_id, session_id=session_id,
                        name=args.get("name", ""),
                        description=args.get("description", ""),
                        type=args.get("type", "wilderness"),
                        parent_location_id="",
                        danger_level=args.get("danger_level", 1),
                    ))
                    applied += 1

                elif tool == "create_npc":
                    name = args.get("name", "").strip()
                    if not name:
                        errors.append("create_npc: missing name")
                        continue

                    # Dedup guard (fixes repeated duplicate NPCs like "Старший страж" /
                    # "Старший стражник" / "Старший стражник у ворот Гнезно" all being
                    # separate DB rows for the same guard). create_npc used to ALWAYS
                    # insert a new row with a fresh uuid, with no check against NPCs that
                    # already exist — so every time the DB-Bot (which never sees the
                    # existing NPC list per-call, only the current narrative) reworded a
                    # title slightly, a brand-new duplicate was created. Fuzzy-match by
                    # substring against existing NPC names first, same convention already
                    # used for characters (find_char) and for set_npc_relation lookups.
                    existing_npcs = db.get_npcs(session_id, alive_only=False)
                    name_lower = name.lower()
                    matched_npc = None
                    for n in existing_npcs:
                        n_lower = n.name.lower()
                        if name_lower == n_lower or name_lower in n_lower or n_lower in name_lower:
                            matched_npc = n
                            break

                    if matched_npc:
                        logger.info(f"[create_npc dedup] '{name}' matched existing NPC '{matched_npc.name}' — skipped duplicate insert")
                        applied += 1
                        continue

                    npc_id = str(uuid.uuid4())[:8]
                    loc_id = ""
                    loc_type = ""
                    occupation = args.get("occupation", "")
                    if args.get("location_name"):
                        locs = db.get_locations(session_id)
                        for l in locs:
                            if args["location_name"].lower() in l.name.lower():
                                loc_id = l.id
                                loc_type = getattr(l, 'type', '') or getattr(l, 'location_type', '')
                                break

                    # ── NPC SPAWN VALIDATION (occupation → location binding) ──
                    if occupation and loc_type:
                        try:
                            from libs.ai.npc_spawn_engine import NpcSpawnEngine
                            spawn_engine = NpcSpawnEngine(db)
                            spawn_result = spawn_engine.validate_spawn(occupation, loc_type)
                            if not spawn_result.is_valid:
                                # Auto-relocate to nearest valid location
                                alt_loc = spawn_engine.get_nearest_valid_location(occupation, session_id)
                                if alt_loc:
                                    logger.warning(
                                        f"[NPC spawn] '{name}' ({occupation}) relocated from "
                                        f"'{loc_type}' to '{alt_loc.type}' — occupation-location mismatch"
                                    )
                                    loc_id = alt_loc.id
                                else:
                                    logger.warning(
                                        f"[NPC spawn] '{name}' ({occupation}) at '{loc_type}' — "
                                        f"no valid location found, allowing with warning"
                                    )
                        except Exception as e:
                            logger.warning(f"[NPC spawn] Validation failed (non-blocking): {e}")

                    db.create_npc(WorldNpc(
                        id=npc_id, session_id=session_id,
                        name=name,
                        race=args.get("race", ""),
                        occupation=args.get("occupation", ""),
                        location_id=loc_id,
                        personality=json.dumps({"traits": args.get("personality", "")}),
                        backstory=args.get("backstory", ""),
                    ))
                    applied += 1

                elif tool == "set_npc_relation":
                    npcs = db.get_npcs(session_id)
                    _chars_list = db.get_session_characters(session_id)
                    npc_id = None
                    char_id = None
                    for n in npcs:
                        if args.get("npc_name", "").lower() in n.name.lower():
                            npc_id = n.id
                            break
                    for c in _chars_list:
                        if args.get("character_name", "").lower() in c.name.lower():
                            char_id = c.id
                            break
                    if npc_id and char_id:
                        db.set_npc_relation(NpcRelation(
                            session_id=session_id, npc_id=npc_id, character_id=char_id,
                            reputation=args.get("delta", 0),
                            attitude=args.get("attitude", ""),
                            known_facts=args.get("known_fact", "") or args.get("reason", ""),
                        ))
                        applied += 1
                    else:
                        errors.append(f"set_npc_relation: npc or char not found")

                elif tool == "create_lore":
                    lore_id = str(uuid.uuid4())[:8]
                    db.create_lore(LoreArticle(
                        id=lore_id, session_id=session_id,
                        title=args.get("title", ""),
                        category=args.get("category", "general"),
                        content=args.get("content", ""),
                        tags=json.dumps(args.get("tags", "").split(",") if args.get("tags") else []),
                    ))
                    applied += 1

                elif tool == "set_market_price":
                    locs = db.get_locations(session_id)
                    loc_id = None
                    for l in locs:
                        if args.get("location_name", "").lower() in l.name.lower():
                            loc_id = l.id
                            break
                    db.set_market_price(MarketPrice(
                        session_id=session_id, location_id=loc_id or "",
                        item_id=args.get("item_name", ""),
                        base_price_gp=args.get("base_price_gp", 0),
                        current_price_gp=args.get("current_price_gp", 0),
                    ))
                    applied += 1

                elif tool == "add_economic_event":
                    db.add_economic_event(EconomicEvent(
                        session_id=session_id,
                        name=args.get("name", ""),
                        description=args.get("description", ""),
                        affected_locations=json.dumps(args.get("affected_locations", "").split(",") if args.get("affected_locations") else []),
                        price_multiplier=args.get("price_multiplier", 1.0),
                        duration_days=args.get("duration_days", 7),
                    ))
                    applied += 1

                elif tool == "add_effect":
                    _chars_list = db.get_session_characters(session_id)
                    npcs = db.get_npcs(session_id)
                    entity_id = None
                    entity_type = args.get("entity_type", "character")
                    target_name = args.get("entity_name", "")
                    for c in _chars_list:
                        if target_name.lower() in c.name.lower():
                            entity_id = c.id
                            break
                    if not entity_id:
                        for n in npcs:
                            if target_name.lower() in n.name.lower():
                                entity_id = n.id
                                entity_type = "npc"
                                break
                    if entity_id:
                        db.add_effect(ActiveEffect(
                            session_id=session_id, entity_type=entity_type, entity_id=entity_id,
                            name=args.get("name", ""), effect_type=args.get("effect_type", "curse"),
                            source=args.get("source", ""), duration_type=args.get("duration", "permanent"),
                            mechanics=args.get("mechanics", ""),
                        ))
                        applied += 1
                    else:
                        errors.append(f"add_effect: entity not found {target_name}")

                elif tool == "create_timer":
                    _chars_list = db.get_session_characters(session_id)
                    npcs = db.get_npcs(session_id)
                    entity_id = None
                    entity_type = args.get("entity_type", "character")
                    target_name = args.get("entity_name", "")
                    for c in _chars_list:
                        if target_name.lower() in c.name.lower():
                            entity_id = c.id
                            break
                    if not entity_id:
                        for n in npcs:
                            if target_name.lower() in n.name.lower():
                                entity_id = n.id
                                entity_type = "npc"
                                break
                    if entity_id:
                        db.create_timer(Timer(
                            session_id=session_id, entity_type=entity_type, entity_id=entity_id,
                            event_type=args.get("event_type", ""),
                            trigger_round=args.get("trigger_in_rounds", 0),
                            action=args.get("action", ""),
                            is_recurring=args.get("is_recurring", False),
                        ))
                        applied += 1
                    else:
                        errors.append(f"create_timer: entity not found {target_name}")

                elif tool in ("get_srd_monster", "get_srd_item", "get_srd_spell", "get_location", "get_npc", "get_lore"):
                    applied += 1

                # ─── Character progression (B1) — the DB is the live source of truth ───
                elif tool == "level_up_character":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"level_up_character: char not found '{args.get('character_name')}'")
                        continue
                    new_level = args.get("new_level", char.level)
                    new_max_hp = args.get("new_max_hp")
                    db.set_character_level(char.id, new_level, new_max_hp)
                    applied += 1

                elif tool == "set_ability_score":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"set_ability_score: char not found '{args.get('character_name')}'")
                        continue
                    ability = args.get("ability", "")
                    new_value = args.get("new_value", 10)
                    db.set_character_ability_score(char.id, ability, new_value)
                    applied += 1

                elif tool == "add_feature":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"add_feature: char not found '{args.get('character_name')}'")
                        continue
                    db.add_character_feature(char.id, args.get("feature_name", ""))
                    applied += 1

                elif tool == "add_proficiency":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"add_proficiency: char not found '{args.get('character_name')}'")
                        continue
                    db.add_character_proficiency(char.id, args.get("proficiency_name", ""))
                    applied += 1

                elif tool == "add_spell_known":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"add_spell_known: char not found '{args.get('character_name')}'")
                        continue
                    db.add_character_spell(char.id, args.get("spell_name", ""))
                    applied += 1

                # ─── H8: what a LOCATION knows/feels about a character ───
                elif tool == "update_location_relation":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"update_location_relation: char not found '{args.get('character_name')}'")
                        continue
                    locs = db.get_locations(session_id)
                    loc_name = args.get("location_name", "")
                    matched_loc = None
                    for l in locs:
                        if loc_name.lower() in l.name.lower() or l.name.lower() in loc_name.lower():
                            matched_loc = l
                            break
                    # If the location isn't in the DB yet, still record the relation using
                    # the name as its own id — better to have it than to silently drop it.
                    loc_id = matched_loc.id if matched_loc else loc_name
                    resolved_name = matched_loc.name if matched_loc else loc_name
                    is_wanted = args.get("is_wanted")
                    db.adjust_location_relation(
                        session_id, loc_id, resolved_name, char.id, char.name,
                        fame_delta=args.get("fame_delta", 0),
                        reputation_delta=args.get("reputation_delta", 0),
                        is_wanted=is_wanted,
                        notoriety_note=args.get("notoriety_note", ""),
                    )
                    applied += 1

                elif tool == "set_character_goal":
                    char = find_char(args.get("character_name", ""))
                    if not char:
                        errors.append(f"set_character_goal: char not found '{args.get('character_name')}'")
                        continue
                    title = args.get("title", "")
                    status = args.get("status", "active")
                    source = args.get("source", "session")
                    if status == "active":
                        db.add_character_goal(session_id, char.id, char.name, title, source=source)
                        applied += 1
                    else:
                        # status update on an existing goal — find best-matching active goal
                        goals = db.get_character_goals(session_id, character_id=char.id, status="active")
                        matched_goal = None
                        for g in goals:
                            if title.lower() in g["title"].lower() or g["title"].lower() in title.lower():
                                matched_goal = g
                                break
                        if matched_goal:
                            db.update_character_goal_status(matched_goal["id"], status)
                            applied += 1
                        else:
                            errors.append(f"set_character_goal: no matching active goal '{title}' to mark {status}")

                else:
                    errors.append(f"Unknown tool: {tool}")
            except Exception as e:
                errors.append(f"{tool}: {str(e)}")
                logger.error(f"Game action error: {tool} {args} -> {e}")

        return applied, errors


    async def summarize_and_save(self, session_id: str) -> str:
        """
        Summarize session history using Granite and save to DB.
        """
        db = self.db_manager.get_db(session_id)
        all_history = db.get_history(session_id, limit=50)
        if len(all_history) < 10:
            return ""

        text_parts = []
        for h in all_history:
            text_parts.append(f"{h.author}: {h.content}")

        full_text = "\n".join(text_parts)

        try:
            summary = await self.dm.summarize(full_text)

            db.add_history(HistoryEntry(
                session_id=session_id,
                author="SYSTEM",
                content=f"[SUMMARY] {summary}",
                entry_type="summary",
            ))

            session = db.get_session(session_id)
            if session:
                session.summary = summary
                session.summary_at_count = db.count_history(session_id)
                db.update_session(session)

            logger.info(f"[SUMMARY] Session {session_id}: history summarized ({len(all_history)} entries)")
            return summary

        except Exception as e:
            logger.error(f"Summarize error: {e}")
            return ""


    async def answer_question(self, session_id: str, player_id: int,
                              question: str) -> str:
        """Answer an out-of-turn question"""
        db = self.db_manager.get_db(session_id)
        player = db.get_player(player_id, session_id)
        if not player:
            return "You're not in this session."

        char = db.get_character_by_player(player_id, session_id)
        name = char.name if char else player.display_name

        history = db.get_history(session_id, limit=10)
        history_msgs = []
        for h in history:
            role = "user" if h.author != "DM" else "assistant"
            history_msgs.append({"role": role, "content": h.content})

        answer = await self.dm.answer_question(history_msgs, question, name)

        db.add_history(HistoryEntry(
            session_id=session_id,
            author=name,
            content=f"[OOC Question]: {question}",
            entry_type="ooc",
        ))
        db.add_history(HistoryEntry(
            session_id=session_id,
            author="DM",
            content=f"[OOC Answer]: {answer}",
            entry_type="ooc",
        ))

        return answer

    # ═══════════════════════════════════════════════════════════
    # HP Tracker (#1)
    # ═══════════════════════════════════════════════════════════


