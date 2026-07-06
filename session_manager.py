"""
Session Manager — handles game flow, combat queue, and turn resolution
Core concept: ALL players must submit actions before the DM resolves the round
"""

import json
import logging
import uuid
from typing import Dict, List, Optional, Tuple

import random

from ai_client import DMEngine
from db import (
    Database, DatabaseManager, HistoryEntry, Player, QueueState, Session,
    Location, LocationPath, WorldNpc, NpcRelation, LoreArticle,
    MarketPrice, EconomicEvent, ActiveEffect, Timer
)


logger = logging.getLogger(__name__)


class SessionManager:
    """Manages all game sessions and their state"""

    def __init__(self, db_manager: DatabaseManager, dm_engine: DMEngine):
        self.db_manager = db_manager
        self.dm = dm_engine

    # ═══════════════════════════════════════════════════════════
    # Session Lifecycle
    # ═══════════════════════════════════════════════════════════

    def create_session(self, chat_id: int, name: str, creator_id: int,
                       creator_name: str) -> Session:
        """Create a new game session"""
        session_id = str(uuid.uuid4())[:8]

        session = Session(
            id=session_id,
            chat_id=chat_id,
            name=name,
            creator_id=creator_id,
            status="active",
            current_scene="",
        )

        self.db_manager.create_session(session)

        player = Player(
            user_id=creator_id,
            session_id=session_id,
            username=creator_name,
            display_name=creator_name,
            is_creator=True,
        )
        self.db_manager.get_db(session_id).add_player(player)

        logger.info(f"Created session {session_id}: {name}")
        return session

    def get_active_session(self, chat_id: int) -> Optional[Session]:
        """Get the active session for a chat"""
        return self.db_manager.get_session_by_chat(chat_id)

    def end_session(self, session_id: str):
        """End a session"""
        self.db_manager.end_session(session_id)
        logger.info(f"Ended session {session_id}")

    # ═══════════════════════════════════════════════════════════
    # Player Management
    # ═══════════════════════════════════════════════════════════

    def add_player(self, session_id: str, user_id: int, username: str,
                   display_name: str) -> Player:
        """Add a player to a session"""
        player = Player(
            user_id=user_id,
            session_id=session_id,
            username=username,
            display_name=display_name,
            is_creator=False,
        )
        self.db_manager.get_db(session_id).add_player(player)
        return player

    def add_player_to_queue(self, session_id: str, user_id: int):
        """If a round is collecting actions, add new player to waiting list."""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return
        if state.is_resolving:
            return  # Round is already resolving, they'll join next round
        waiting_for = json.loads(state.waiting_for)
        if user_id not in waiting_for:
            waiting_for.append(user_id)
            state.waiting_for = json.dumps(waiting_for)
            db.set_queue_state(state)

    def remove_player(self, session_id: str, user_id: int):
        """Remove a player from a session"""
        self.db_manager.get_db(session_id).remove_player(user_id, session_id)

    def kick_player(self, session_id: str, target_user_id: int) -> bool:
        """Kick a player from session (DM only)"""
        db = self.db_manager.get_db(session_id)
        player = db.get_player(target_user_id, session_id)
        if not player:
            return False
        db.remove_player(target_user_id, session_id)
        return True

    def get_players(self, session_id: str) -> List[Player]:
        """Get all players in a session"""
        return self.db_manager.get_db(session_id).get_players(session_id)

    def is_creator(self, user_id: int, session_id: str) -> bool:
        """Check if user is the session creator"""
        player = self.db_manager.get_db(session_id).get_player(user_id, session_id)
        return player is not None and player.is_creator

    # ═══════════════════════════════════════════════════════════
    # Combat & Initiative
    # ═══════════════════════════════════════════════════════════

    def start_combat(self, session_id: str) -> str:
        """Start combat — roll initiative for all players"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "No active session."

        players = db.get_players(session_id)
        characters = db.get_session_characters(session_id)

        initiative_list = []
        results = []

        for char in characters:
            if not char.is_alive:
                continue

            stats = json.loads(char.stats) if char.stats else {}
            dex_mod = stats.get("dexterity", 10) // 2 - 5

            roll_nat = random.randint(1, 20)
            roll_total = roll_nat + dex_mod
            initiative_list.append({
                "name": char.name,
                "player_id": char.player_id,
                "initiative": roll_total,
                "natural": roll_nat,
                "dex_mod": dex_mod,
            })
            results.append(f"{char.name}: d20{roll_nat:+d}{dex_mod:+d} = **{roll_total}**")

        initiative_list.sort(key=lambda x: x["initiative"], reverse=True)

        session.combat_active = True
        session.initiative_order = json.dumps(initiative_list)
        session.current_turn_index = 0
        session.round_number = 1
        db.update_session(session)

        lines = ["⚔️ **БОЙ НАЧИНАЕТСЯ!** ⚔️", "", "*Броски инициативы:*"]
        lines.extend(results)
        lines.extend(["", "*Порядок ходов:*"])
        for i, entry in enumerate(initiative_list, 1):
            lines.append(f"{i}. {entry['name']} (Инициатива: {entry['initiative']})")
        lines.append(f"\n🎲 Раунд 1 — ход **{initiative_list[0]['name']}**!")

        return "\n".join(lines)

    def toggle_pvp(self, session_id: str):
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if session:
            session.pvp_active = not session.pvp_active
            db.update_session(session)

    def is_pvp(self, session_id: str) -> bool:
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        return session.pvp_active if session else False

    def get_world_status(self, session_id: str) -> dict:
        """Get world generation status for validation"""
        db = self.db_manager.get_db(session_id)
        locations = db.get_locations(session_id)
        npcs = db.get_npcs(session_id)
        lore = db.get_lore_by_tag(session_id, "")
        events = db.get_world_events(session_id)
        factions = db.get_factions(session_id)

        return {
            "locations_count": len(locations),
            "locations": [l.name for l in locations[:10]],
            "npcs_count": len(npcs),
            "npcs": [n.name for n in npcs[:10]],
            "lore_count": len(lore),
            "events_count": len(events),
            "factions_count": len(factions),
        }

    async def auto_start(self, session_id: str) -> str:
        """Auto-start game when all players have uploaded character sheets."""
        import asyncio

        db = self.db_manager.get_db(session_id)
        players = db.get_players(session_id)
        chars = db.get_session_characters(session_id)

        if len(chars) < len(players):
            missing = [p.display_name for p in players if not db.get_character_by_player(p.user_id, session_id)]
            return f"⏳ Ждём листы: {', '.join(missing)}"

        # All sheets loaded! Generate world and start
        logger.info(f"[AUTO_START] Session {session_id}: all {len(chars)} sheets loaded. Starting game...")

        # Generate world
        world_result = await self.generate_world(session_id, "dark fantasy")
        world_text = world_result.get("text", "")
        world_html = world_result.get("html", world_text)

        # Start action collection for first round
        self.start_action_collection(session_id)

        # Build welcome message
        char_names = [c.name for c in chars]
        welcome = (
            f"🌍 **Мир рождён!**\n\n"
            f"{world_html}\n\n"
            f"---\n\n"
            f"⚔️ **Игра начинается!**\n"
            f"Персонажи: {', '.join(char_names)}\n\n"
            f"📝 Пишите `Дн. ваше действие` — мастер разрешит, когда все сходят."
        )

        # Add to history
        db.add_history(HistoryEntry(
            session_id=session_id,
            author="SYSTEM",
            content="[AUTO_START] Game started automatically after all sheets loaded.",
            entry_type="system",
        ))

        return welcome

    def toggle_autostart(self, session_id: str) -> bool:
        """Toggle auto-start feature. Returns new state."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return False
        current = getattr(session, 'autostart', False)
        new_state = not current
        session.autostart = new_state
        db.update_session(session)
        return new_state


    def get_all_character_abilities(self, session_id: str) -> List[Dict]:
        """Get formatted abilities/stats for all characters in session."""
        db = self.db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)
        result = []
        for c in chars:
            stats = json.loads(c.stats) if c.stats else {}
            abilities = {
                "name": c.name,
                "race": c.race,
                "class": c.class_name,
                "level": c.level,
                "hp": f"{c.hp}/{c.max_hp}",
                "ac": c.ac,
                "str": stats.get("strength", 10),
                "dex": stats.get("dexterity", 10),
                "con": stats.get("constitution", 10),
                "int": stats.get("intelligence", 10),
                "wis": stats.get("wisdom", 10),
                "cha": stats.get("charisma", 10),
                "proficiencies": json.loads(c.proficiencies) if c.proficiencies else [],
                "features": json.loads(c.features) if c.features else [],
                "conditions": [cc.condition for cc in db.get_conditions(session_id, c.id)],
                "alive": c.is_alive,
            }
            result.append(abilities)
        return result

    def transfer_creator(self, session_id: str, old_user_id: int, new_user_id: int) -> bool:
        """Transfer session creator rights to another player."""
        db = self.db_manager.get_db(session_id)
        with db._connect() as conn:
            conn.execute(
                "UPDATE players SET is_creator = 0 WHERE user_id = ? AND session_id = ?",
                (old_user_id, session_id)
            )
            conn.execute(
                "UPDATE players SET is_creator = 1 WHERE user_id = ? AND session_id = ?",
                (new_user_id, session_id)
            )
        return True


    def end_combat(self, session_id: str) -> str:
        """End combat"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Нет активной сессии."

        session.combat_active = False
        session.initiative_order = "[]"
        session.current_turn_index = 0
        session.round_number = 0
        db.update_session(session)
        db.clear_queue_state(session_id)

        return "🏳️ **Бой завершён.**"

    def get_current_turn(self, session_id: str) -> Optional[Dict]:
        """Get whose turn it is"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return None

        initiative = json.loads(session.initiative_order)
        if not initiative:
            return None

        idx = session.current_turn_index % len(initiative)
        return initiative[idx]

    def advance_turn(self, session_id: str) -> Optional[Dict]:
        """Advance to next turn"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return None

        initiative = json.loads(session.initiative_order)
        if not initiative:
            return None

        session.current_turn_index += 1

        if session.current_turn_index >= len(initiative):
            session.current_turn_index = 0
            session.round_number += 1

        db.update_session(session)
        return self.get_current_turn(session_id)

    # ═══════════════════════════════════════════════════════════
    # Queue System — The Core Mechanic
    # ═══════════════════════════════════════════════════════════

    def start_action_collection(self, session_id: str) -> list:
        """Start collecting actions from ALL players for this round."""
        db = self.db_manager.get_db(session_id)
        players = db.get_players(session_id)
        waiting_for = [p.user_id for p in players]

        state = QueueState(
            session_id=session_id,
            waiting_for=json.dumps(waiting_for),
            collected_actions="{}",
            is_resolving=False,
        )
        db.set_queue_state(state)

        return waiting_for

    def submit_action(self, session_id: str, player_id: int,
                      action: str) -> Tuple[bool, Optional[str]]:
        """
        Submit a player action.
        Returns (is_complete, dm_response_or_none).
        """
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return False, None

        if state.is_resolving:
            return False, "*Resolving previous round... wait.*"

        waiting_for = json.loads(state.waiting_for)
        collected = json.loads(state.collected_actions)

        if str(player_id) in collected:
            return False, "*You already submitted your action for this round.*"

        if player_id not in waiting_for:
            return False, "*Not your turn to act, or you're not in this round.*"

        collected[str(player_id)] = action
        waiting_for.remove(player_id)

        state.waiting_for = json.dumps(waiting_for)
        state.collected_actions = json.dumps(collected)
        db.set_queue_state(state)

        if not waiting_for:
            return True, None

        return False, None

    async def resolve_round(self, session_id: str, roll_mode: str = "mixed") -> Dict[str, str]:
        """
        Resolve the current round by sending all actions to the DM.
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
            for player_id_str, action in collected.items():
                player = db.get_player(int(player_id_str), session_id)
                if player:
                    nick = player.username or player.display_name
                    player_actions[nick] = f"Дн. {action}"

            history = db.get_history(session_id, limit=30)
            history_msgs = []
            for h in history:
                role = "user" if h.author != "DM" else "assistant"
                history_msgs.append({"role": role, "content": h.content})

            character_sheets = db.get_all_character_sheets(session_id)

            # Build context
            context_lines = []
            session = db.get_session(session_id)

            if session and session.pvp_active:
                context_lines.append("⚔️ PvP АКТИВЕН: игроки могут атаковать друг друга.")

            chars = db.get_session_characters(session_id)
            if chars:
                context_lines.append("\n--- СТАТУС ПЕРСОНАЖЕЙ ---")
                for c in chars:
                    status = "💀" if not c.is_alive else f"❤️ {c.hp}/{c.max_hp} HP"
                    conds = db.get_conditions(session_id, c.id)
                    cond_str = ", ".join(cc.condition for cc in conds) if conds else ""
                    loc = db.get_location(session_id, c.id)
                    loc_str = f"📍 {loc.location_name}" if loc and loc.location_name else ""
                    context_lines.append(f"{c.name}: {status}{f' [{cond_str}]' if cond_str else ''} {loc_str}")

            quests = db.get_quests(session_id, status="active")
            if quests:
                context_lines.append("\n--- АКТИВНЫЕ КВЕСТЫ ---")
                for q in quests[:5]:
                    assignee = f" ({q.assignee_name})" if q.assignee_name else ""
                    context_lines.append(f"• {q.title}{assignee}")

            factions = db.get_factions(session_id)
            if factions:
                context_lines.append("\n--- ФРАКЦИИ ---")
                for f in factions[:5]:
                    context_lines.append(f"• {f.name}: {f.attitude} (реп: {f.reputation:+d})")

            gt = db.get_game_time(session_id)
            context_lines.append(f"\n--- ВРЕМЯ: День {gt.day}, {gt.hour:02d}:{gt.minute:02d} | {gt.weather} | {gt.temperature} ---")

            # DB Journal context — NEW!
            db_journal = db.get_journal_summary(session_id)

            context_block = "\n".join(context_lines) if context_lines else ""

            result = await self.dm.process_master_turn(
                session_history=history_msgs,
                player_actions=player_actions,
                character_sheets=character_sheets if character_sheets else None,
                context=context_block,
                roll_mode=roll_mode,
                summary=session.summary if session else "",
                db_journal=db_journal,
            )

            player_text = result.get("player_text", "*[No response]*")
            gm_log = result.get("gm_log", "")
            raw_narrative = result.get("raw_narrative", "")

            # STEP 2: DB-BOT analyzes full narrative + current state
            db_state = []
            chars = db.get_session_characters(session_id)
            if chars:
                db_state.append("--- Current DB State ---")
                for c in chars:
                    gold = db.get_gold_balance(session_id, c.id)
                    inv = db.get_inventory(session_id, c.id)
                    loc = db.get_location(session_id, c.id)
                    conds = db.get_conditions(session_id, c.id)
                    db_state.append(f"{c.name}: HP={c.hp}/{c.max_hp}, GP={gold['gp']}, Loc={loc.location_name if loc else '?'}, Inv={[i['item'] for i in inv]}, Conds={[cc.condition for cc in conds]}")

            db_state_text = chr(10).join(db_state) if db_state else ""
            db_context = f"Current DB state:{chr(10)}{db_state_text}{chr(10)}{chr(10)}Master text:{chr(10)}{raw_narrative or player_text}"

            logger.info(f"[DB-BOT] Calling process_db_bot for session {session_id}")
            game_actions = await self.dm.process_db_bot(
                raw_narrative=db_context,
                context="",
                character_sheets=character_sheets,
                session_id=session_id,
            )
            logger.info(f"[DB-BOT] Received {len(game_actions)} actions from DB-Bot")

            # STEP 3: Apply DB changes BEFORE sending to players
            applied, errors = 0, []
            if game_actions:
                applied, errors = self._apply_game_actions(session_id, game_actions)
                if errors:
                    logger.warning(f"Game action errors: {errors}")
                logger.info(f"Applied {applied} game actions from DB-Bot")

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

            # NOTE: game_actions from Master are not used; DB-Bot handles all DB changes


            # Auto-advance time
            if session and session.combat_active:
                db.advance_time(session_id, minutes=1)
            else:
                db.advance_time(session_id, minutes=10)

            db.clear_queue_state(session_id)

            if session and session.combat_active:
                self.advance_turn(session_id)
                self.start_action_collection(session_id)

            # Auto-summarize if history is long
            history_count = len(db.get_history(session_id, limit=30))
            if history_count >= 30:
                import asyncio
                asyncio.create_task(self.summarize_and_save(session_id))

            return {
                "player_text": player_text,
                "gm_log": gm_log,
                "game_actions_applied": len(game_actions) if game_actions else 0,
            }

        except Exception as e:
            logger.error(f"Error resolving round: {e}")
            state.is_resolving = False
            db.set_queue_state(state)
            return {
                "player_text": f"*[Error resolving round: {str(e)}]*",
                "gm_log": f"ERROR: {e}",
                "game_actions_applied": 0,
            }

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
                    reason = args.get("reason", "AI")
                    self.add_gold(session_id, char.id, char.name, gp=amount, reason=reason)
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
                    npc_id = str(uuid.uuid4())[:8]
                    loc_id = ""
                    if args.get("location_name"):
                        locs = db.get_locations(session_id)
                        for l in locs:
                            if args["location_name"].lower() in l.name.lower():
                                loc_id = l.id
                                break
                    db.create_npc(WorldNpc(
                        id=npc_id, session_id=session_id,
                        name=args.get("name", ""),
                        race=args.get("race", ""),
                        occupation=args.get("occupation", ""),
                        location_id=loc_id,
                        personality=json.dumps({"traits": args.get("personality", "")}),
                        backstory=args.get("backstory", ""),
                    ))
                    applied += 1

                elif tool == "set_npc_relation":
                    npcs = db.get_npcs(session_id)
                    chars = db.get_session_characters(session_id)
                    npc_id = None
                    char_id = None
                    for n in npcs:
                        if args.get("npc_name", "").lower() in n.name.lower():
                            npc_id = n.id
                            break
                    for c in chars:
                        if args.get("character_name", "").lower() in c.name.lower():
                            char_id = c.id
                            break
                    if npc_id and char_id:
                        db.set_npc_relation(NpcRelation(
                            session_id=session_id, npc_id=npc_id, character_id=char_id,
                            reputation=args.get("delta", 0),
                            known_facts=args.get("reason", ""),
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
                    chars = db.get_session_characters(session_id)
                    npcs = db.get_npcs(session_id)
                    entity_id = None
                    entity_type = args.get("entity_type", "character")
                    target_name = args.get("entity_name", "")
                    for c in chars:
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
                    chars = db.get_session_characters(session_id)
                    npcs = db.get_npcs(session_id)
                    entity_id = None
                    entity_type = args.get("entity_type", "character")
                    target_name = args.get("entity_name", "")
                    for c in chars:
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

                else:
                    errors.append(f"Unknown tool: {tool}")
            except Exception as e:
                errors.append(f"{tool}: {str(e)}")
                logger.error(f"Game action error: {tool} {args} -> {e}")

        return applied, errors

    def get_pending_players(self, session_id: str) -> List[str]:
        """Get list of display names still needed to act"""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return []

        waiting_for = json.loads(state.waiting_for)
        names = []
        for uid in waiting_for:
            player = db.get_player(uid, session_id)
            if player:
                char = db.get_character_by_player(uid, session_id)
                names.append(char.name if char else player.display_name)
        return names

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
                db.update_session(session)

            logger.info(f"[SUMMARY] Session {session_id}: history summarized ({len(all_history)} entries)")
            return summary

        except Exception as e:
            logger.error(f"Summarize error: {e}")
            return ""

    def skip_player(self, session_id: str, player_id: int) -> bool:
        """Skip a player's turn (creator only)"""
        db = self.db_manager.get_db(session_id)
        state = db.get_queue_state(session_id)
        if not state:
            return False

        waiting_for = json.loads(state.waiting_for)
        collected = json.loads(state.collected_actions)

        if player_id in waiting_for:
            waiting_for.remove(player_id)
            collected[str(player_id)] = "*[Turn skipped by DM]*"

            state.waiting_for = json.dumps(waiting_for)
            state.collected_actions = json.dumps(collected)
            db.set_queue_state(state)

            return len(waiting_for) == 0

        return False

    # ═══════════════════════════════════════════════════════════
    # Out-of-turn questions
    # ═══════════════════════════════════════════════════════════

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

    def change_hp(self, session_id: str, character_id: str, character_name: str,
                  delta: int, source: str = "") -> dict:
        """Change HP and log it."""
        db = self.db_manager.get_db(session_id)
        char = db.get_character(character_id)
        if not char:
            return {"error": "Character not found"}

        old_hp = char.hp
        new_hp = max(0, min(char.max_hp, old_hp + delta))
        actual_change = new_hp - old_hp

        db.update_character_hp(character_id, new_hp)
        db.add_hp_log(session_id, character_id, character_name, old_hp, new_hp, source)

        status = "alive"
        if new_hp == 0 and old_hp > 0:
            status = "dying"
        elif new_hp > 0 and old_hp == 0:
            status = "stabilized"

        return {"old_hp": old_hp, "new_hp": new_hp, "change": actual_change, "status": status}

    def death_save(self, session_id: str, character_id: str, success: bool = None) -> dict:
        """Roll death save."""
        import random
        db = self.db_manager.get_db(session_id)
        char = db.get_character(character_id)
        if not char or char.hp > 0:
            return {"error": "Character is not dying"}

        successes = char.death_saves_success
        failures = char.death_saves_failure

        if success is not None:
            if success:
                successes += 1
            else:
                failures += 1
        else:
            roll = random.randint(1, 20)
            if roll == 20:
                successes += 2
            elif roll >= 10:
                successes += 1
            elif roll == 1:
                failures += 2
            else:
                failures += 1

        successes = min(3, max(0, successes))
        failures = min(3, max(0, failures))

        db.update_character_death_saves(character_id, successes, failures)

        is_stable = successes >= 3
        is_dead = failures >= 3

        if is_dead:
            db.kill_character(character_id)

        return {
            "successes": successes,
            "failures": failures,
            "is_stable": is_stable,
            "is_dead": is_dead,
        }

    # ═══════════════════════════════════════════════════════════
    # Conditions (#3)
    # ═══════════════════════════════════════════════════════════

    def add_condition(self, session_id: str, character_id: str, character_name: str,
                      condition: str, source: str = "", duration: str = ""):
        self.db_manager.get_db(session_id).add_condition(session_id, character_id, character_name, condition, source, duration)

    def remove_condition(self, session_id: str, character_id: str, condition: str):
        self.db_manager.get_db(session_id).remove_condition(session_id, character_id, condition)

    def get_character_conditions(self, session_id: str, character_id: str) -> list:
        return self.db_manager.get_db(session_id).get_conditions(session_id, character_id)

    def clear_all_conditions(self, session_id: str, character_id: str):
        self.db_manager.get_db(session_id).remove_all_conditions(session_id, character_id)

    # ═══════════════════════════════════════════════════════════
    # Rest (#4)
    # ═══════════════════════════════════════════════════════════

    def short_rest(self, session_id: str, character_id: str, character_name: str,
                   hit_dice_to_spend: int = 1) -> dict:
        """Short rest: restore HP via hit dice, recover short-rest resources"""
        import random
        db = self.db_manager.get_db(session_id)
        char = db.get_character(character_id)
        if not char:
            return {"error": "Character not found"}

        if char.hp <= 0:
            return {"error": "Нельзя отдыхать при 0 HP"}

        con_mod = 0
        try:
            stats = json.loads(char.stats) if char.stats else {}
            con_mod = (stats.get("constitution", 10) - 10) // 2
        except:
            pass

        hp_restored = 0
        for _ in range(min(hit_dice_to_spend, 999)):
            roll = random.randint(1, 8)
            hp_restored += roll + con_mod

        old_hp = char.hp
        new_hp = min(char.max_hp, old_hp + hp_restored)
        actual_restore = new_hp - old_hp

        db.update_character_hp(character_id, new_hp)
        db.reset_resources(session_id, character_id, "short")
        db.add_rest(session_id, character_id, character_name, "short", actual_restore, hit_dice_to_spend)

        return {"hp_restored": actual_restore, "new_hp": new_hp, "max_hp": char.max_hp}

    def long_rest(self, session_id: str, character_id: str, character_name: str) -> dict:
        """Long rest: restore all HP, recover all resources, clear some conditions"""
        db = self.db_manager.get_db(session_id)
        char = db.get_character(character_id)
        if not char:
            return {"error": "Character not found"}

        if char.hp <= 0:
            return {"error": "Нельзя отдыхать при 0 HP"}

        old_hp = char.hp
        db.update_character_hp(character_id, char.max_hp)
        db.update_character_death_saves(character_id, 0, 0)
        db.reset_resources(session_id, character_id, "long")

        conditions = db.get_conditions(session_id, character_id)
        temp_conditions = ["poisoned", "frightened", "charmed", "stunned", "incapacitated", "grappled", "restrained"]
        for c in conditions:
            if c.condition in temp_conditions:
                db.remove_condition(session_id, character_id, c.condition)

        db.add_rest(session_id, character_id, character_name, "long", char.max_hp - old_hp, 0,
                        "HP полностью восстановлено, ресурсы восстановлены")

        return {"hp_restored": char.max_hp - old_hp, "new_hp": char.max_hp}

    # ═══════════════════════════════════════════════════════════
    # Gold & Inventory (#9)
    # ═══════════════════════════════════════════════════════════

    def add_gold(self, session_id: str, character_id: str, character_name: str,
                 cp: int = 0, sp: int = 0, ep: int = 0, gp: int = 0, pp: int = 0, reason: str = ""):
        self.db_manager.get_db(session_id).add_gold_transaction(session_id, character_id, character_name, cp, sp, ep, gp, pp, reason)

    def get_gold(self, session_id: str, character_id: str) -> dict:
        return self.db_manager.get_db(session_id).get_gold_balance(session_id, character_id)

    def add_item(self, session_id: str, character_id: str, character_name: str,
                 item: str, qty: int = 1, desc: str = ""):
        self.db_manager.get_db(session_id).add_inventory_item(session_id, character_id, character_name, item, qty, desc)

    def remove_item(self, session_id: str, character_id: str, item: str, qty: int = 1):
        self.db_manager.get_db(session_id).remove_inventory_item(session_id, character_id, item, qty)

    def get_inventory(self, session_id: str, character_id: str) -> list:
        return self.db_manager.get_db(session_id).get_inventory(session_id, character_id)

    # ═══════════════════════════════════════════════════════════
    # Quests (#10, #17)
    # ═══════════════════════════════════════════════════════════

    def add_quest(self, session_id: str, title: str, description: str = "",
                  assignee_id: str = "", assignee_name: str = "", status: str = "active") -> int:
        return self.db_manager.get_db(session_id).add_quest(session_id, title, description, assignee_id, assignee_name, status)

    def update_quest(self, quest_id: int, status: str = None, title: str = None, description: str = None):
        self.db_manager.get_db("").update_quest(quest_id, status, title, description)

    def get_quests(self, session_id: str, status: str = None, assignee_id: str = None) -> list:
        return self.db_manager.get_db(session_id).get_quests(session_id, status, assignee_id)

    # ═══════════════════════════════════════════════════════════
    # Game Time (#11)
    # ═══════════════════════════════════════════════════════════

    def get_time(self, session_id: str) -> dict:
        db = self.db_manager.get_db(session_id)
        gt = db.get_game_time(session_id)
        return {
            "day": gt.day, "hour": gt.hour, "minute": gt.minute,
            "time_str": f"День {gt.day}, {gt.hour:02d}:{gt.minute:02d}",
            "weather": gt.weather, "season": gt.season, "temperature": gt.temperature,
        }

    def advance_time(self, session_id: str, minutes: int = 0, hours: int = 0,
                     weather: str = None, temperature: str = None):
        db = self.db_manager.get_db(session_id)
        day, hour, minute = db.advance_time(session_id, minutes, hours)
        if weather:
            db.update_game_time(session_id, weather=weather)
        if temperature:
            db.update_game_time(session_id, temperature=temperature)
        return {"day": day, "hour": hour, "minute": minute}

    # ═══════════════════════════════════════════════════════════
    # Factions (#12)
    # ═══════════════════════════════════════════════════════════

    def add_faction(self, session_id: str, name: str, description: str = "",
                    reputation: int = 0, attitude: str = "neutral") -> int:
        return self.db_manager.get_db(session_id).add_faction(session_id, name, description, reputation, attitude)

    def get_factions(self, session_id: str) -> list:
        return self.db_manager.get_db(session_id).get_factions(session_id)

    def change_reputation(self, session_id: str, faction_id: int, delta: int):
        self.db_manager.get_db(session_id).update_faction_reputation(session_id, faction_id, delta)

    # ═══════════════════════════════════════════════════════════
    # Resources (#20)
    # ═══════════════════════════════════════════════════════════

    def set_resource(self, session_id: str, character_id: str, resource_name: str,
                     current: int, maximum: int, short_rest: bool = False, long_rest: bool = True):
        self.db_manager.get_db(session_id).set_resource(session_id, character_id, resource_name, current, maximum, short_rest, long_rest)

    def get_resources(self, session_id: str, character_id: str) -> list:
        return self.db_manager.get_db(session_id).get_resources(session_id, character_id)

    def use_resource(self, session_id: str, character_id: str, resource_name: str, amount: int = 1):
        self.db_manager.get_db(session_id).update_resource(session_id, character_id, resource_name, -amount)

    def recover_resource(self, session_id: str, character_id: str, resource_name: str, amount: int = 1):
        self.db_manager.get_db(session_id).update_resource(session_id, character_id, resource_name, amount)

    # ═══════════════════════════════════════════════════════════
    # Roll Mode (#6)
    # ═══════════════════════════════════════════════════════════

    def get_roll_mode(self, session_id: str) -> str:
        return self.db_manager.get_db(session_id).get_roll_mode(session_id)

    def set_roll_mode(self, session_id: str, mode: str):
        self.db_manager.get_db(session_id).set_roll_mode(session_id, mode)

    # ═══════════════════════════════════════════════════════════
    # NPC Memory (#21)
    # ═══════════════════════════════════════════════════════════

    def add_npc(self, session_id: str, npc_name: str, personality: str = "",
                facts: str = "", relationships: str = ""):
        self.db_manager.get_db(session_id).add_npc(session_id, npc_name, personality, facts, relationships)

    def get_npc(self, session_id: str, npc_name: str):
        return self.db_manager.get_db(session_id).get_npc(session_id, npc_name)

    def update_npc_facts(self, session_id: str, npc_name: str, new_facts: str):
        self.db_manager.get_db(session_id).update_npc_facts(session_id, npc_name, new_facts)

    # ═══════════════════════════════════════════════════════════
    # World Events (#22)
    # ═══════════════════════════════════════════════════════════

    def add_world_event(self, session_id: str, event_type: str, description: str) -> int:
        return self.db_manager.get_db(session_id).add_world_event(session_id, event_type, description)

    def get_world_events(self, session_id: str, unresolved_only: bool = False) -> list:
        return self.db_manager.get_db(session_id).get_world_events(session_id, unresolved_only)

    def resolve_world_event(self, event_id: int):
        self.db_manager.get_db("").resolve_world_event(event_id)

    # ═══════════════════════════════════════════════════════════
    # Location (#16)
    # ═══════════════════════════════════════════════════════════

    def set_location(self, session_id: str, character_id: str, name: str, description: str = ""):
        self.db_manager.get_db(session_id).set_location(session_id, character_id, name, description)

    def get_location(self, session_id: str, character_id: str):
        return self.db_manager.get_db(session_id).get_location(session_id, character_id)

    # ═══════════════════════════════════════════════════════════
    # SRD (#14)
    # ═══════════════════════════════════════════════════════════

    async def srd_lookup(self, query: str) -> str:
        # SRD cache is global-ish but per-session DB; we use a dummy session for cache
        # Actually SRD cache should probably be global, but for now we pick first available DB
        # or create a dedicated cache. For simplicity, we'll use a fixed "_srd" session DB.
        # Better: keep SRD in the first active session or a dedicated global DB.
        # For this refactor, SRD queries are rare; we'll use the first active session's DB.
        active = self.db_manager.get_all_active_sessions()
        if active:
            db = self.db_manager.get_db(active[0].id)
        else:
            db = self.db_manager.get_db("_srd")
        cached = db.get_srd_cache(query)
        if cached:
            return f"📖 [Кэш] {cached}"

        result = await self.dm.srd_lookup(query)
        db.set_srd_cache(query, result)
        return result

    def generate_loot_by_cr(self, cr: float, loot_type: str = "individual") -> str:
        """Generate loot based on D&D 5e tables by CR"""
        import random
        # Loot tables are global-ish; use first available DB
        active = self.db_manager.get_all_active_sessions()
        if active:
            db = self.db_manager.get_db(active[0].id)
        else:
            db = self.db_manager.get_db("_srd")
        table = db.get_loot_table(cr, loot_type)
        if table:
            import json
            entries = json.loads(table.entries)
            results = []
            for entry in entries:
                if random.random() * 100 <= entry.get("chance", 100):
                    qty = entry.get("qty", "1")
                    results.append(f"{qty} × {entry['name']}")
            return ", ".join(results) if results else "Ничего ценного"

        if cr <= 4:
            cp = random.randint(1, 6) * 10
            sp = random.randint(1, 4) * 10
            gp = random.randint(1, 6) if random.random() > 0.5 else 0
            return f"{cp} см, {sp} смм, {gp} зм"
        elif cr <= 10:
            gp = random.randint(2, 6) * 10
            pp = random.randint(1, 6) * 10
            return f"{gp} зм, {pp} пм"
        else:
            gp = random.randint(4, 6) * 100
            pp = random.randint(2, 6) * 100
            return f"{gp} зм, {pp} пм"

    # ═══════════════════════════════════════════════════════════
    # Encounters (#13)
    # ═══════════════════════════════════════════════════════════

    async def generate_encounter(self, session_id: str, terrain: str = "") -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)
        party_level = sum(c.level for c in chars) // max(len(chars), 1)
        party_size = len(chars)
        context = db.get_session(session_id).current_scene if db.get_session(session_id) else ""

        raw_text = await self.dm.generate_encounter(context, party_level, party_size, terrain)

        # Parallel DB-Bot + Renderer
        db_task = self.dm.process_with_db_bot(raw_text, context=f"Encounter terrain: {terrain}")
        render_task = self.dm.process_renderer(raw_text)
        game_actions, html_text = await asyncio.gather(db_task, render_task)

        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            logger.info(f"Encounter: applied {applied} DB actions")

        return {"text": raw_text, "html": html_text}

    # ═══════════════════════════════════════════════════════════
    # Weather (#11)
    # ═══════════════════════════════════════════════════════════

    async def generate_weather(self, session_id: str) -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        gt = db.get_game_time(session_id)
        terrain = ""
        loc = db.get_all_locations(session_id)
        if loc:
            terrain = loc[0].location_name

        raw_text = await self.dm.generate_weather(gt.season, terrain, gt.weather)

        # Parallel DB-Bot + Renderer
        db_task = self.dm.process_with_db_bot(raw_text, context=f"Weather in {terrain}")
        render_task = self.dm.process_renderer(raw_text)
        game_actions, html_text = await asyncio.gather(db_task, render_task)

        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            logger.info(f"Weather: applied {applied} DB actions")

        weather_types = ["ясно", "дождь", "ливень", "туман", "метель", "снег", "жара", "пыль", "град", "ураган", "ясная"]
        new_weather = gt.weather
        for w in weather_types:
            if w.lower() in raw_text.lower():
                new_weather = w
                break

        temps = ["жарко", "тепло", "прохладно", "холодно", "мороз"]
        new_temp = gt.temperature
        for t in temps:
            if t.lower() in raw_text.lower():
                new_temp = t
                break

        db.update_game_time(session_id, weather=new_weather, temperature=new_temp)
        return {"text": raw_text, "html": html_text}

    # ═══════════════════════════════════════════════════════════
    # World Pregeneration (#15)
    # ═══════════════════════════════════════════════════════════

    async def generate_world(self, session_id: str, theme: str = "dark fantasy") -> dict:
        """Generate world + apply DB. Returns dict with text and html (raw text from Master)."""
        db = self.db_manager.get_db(session_id)
        # Step 1: Master writes world narrative
        raw_text = await self.dm.generate_world_seed(theme)

        # Step 2: DB-Bot only
        game_actions = await self.dm.process_with_db_bot(raw_text, context=f"Session: {session_id}")

        # Step 3: Apply DB actions
        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            if errors:
                logger.warning(f"World gen DB errors: {errors}")
            logger.info(f"World gen: applied {applied} DB actions")

        # VALIDATION: check if world was actually created in DB
        locations = db.get_locations(session_id)
        npcs = db.get_npcs(session_id)

        if not locations:
            logger.warning(f"World gen: no locations created! Creating fallback starter location.")
            db.create_location(Location(
                id=str(uuid.uuid4())[:8], session_id=session_id,
                name="Стартовая таверна", description="Грязная таверна на краю мира.",
                type="tavern", danger_level=1,
            ))
            locations = db.get_locations(session_id)

        # Auto-bind all characters to starter location
        if locations:
            starter = locations[0]
            chars = db.get_session_characters(session_id)
            for char in chars:
                db.set_location(session_id, char.id, starter.name, starter.description)
                logger.info(f"Auto-set {char.name} location to {starter.name}")

        session = db.get_session(session_id)
        if session:
            session.current_scene = raw_text[:500]
            db.update_session(session)

        return {"text": raw_text, "html": raw_text}

    async def generate_living_world_event(self, session_id: str) -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        context = session.summary if session and session.summary else session.current_scene if session else ""
        factions = db.get_factions(session_id)
        if factions:
            context += "\n\nФракции: " + ", ".join(f.name for f in factions)

        raw_text = await self.dm.generate_world_event(context)

        if raw_text:
            # Parallel DB-Bot + Renderer
            db_task = self.dm.process_with_db_bot(raw_text, context=f"Living world event. Factions: {[f.name for f in factions]}")
            render_task = self.dm.process_renderer(raw_text)
            game_actions, html_text = await asyncio.gather(db_task, render_task)

            if game_actions:
                applied, errors = self._apply_game_actions(session_id, game_actions)
                logger.info(f"Living event: applied {applied} DB actions")

            import re
            title_match = re.search(r'\*\*Событие\*\*:\s*(.+)', raw_text)
            title = title_match.group(1).strip() if title_match else "Событие в мире"
            db.add_world_event(session_id, "living_world", f"{title}\n{raw_text}")

        return {"text": raw_text, "html": html_text}
