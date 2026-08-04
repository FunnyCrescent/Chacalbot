"""CombatCoordinatorMixin — initiative combat, encounters, action groups."""
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


class CombatCoordinatorMixin:
    """CombatCoordinatorMixin — initiative combat, encounters, action groups."""

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


    async def _combat_starter_async(self, session_id: str, participant_names: List[str], reason: str, pc_initiatives: Dict = None) -> str:
        """Async wrapper so ai_client.py's Callable[..., Awaitable[str]] type holds —
        start_combat_for_participants itself is plain sync DB work, no I/O to await.
        pc_initiatives: dict of {character_name: {natural: int, total: int}} from player_roll_requester."""
        return self.start_combat_for_participants(session_id, participant_names, reason, pc_initiatives=pc_initiatives)


    async def _combat_ender_async(self, session_id: str, reason: str) -> str:
        """Async wrapper for end_initiative_combat."""
        return self.end_initiative_combat(session_id, reason)


    def start_combat_for_participants(self, session_id: str, participant_names: List[str], reason: str = "", pc_initiatives: Dict = None) -> str:
        """Called ONLY from the Master's start_combat tool call (see ai_client.py
        COMBAT_TOOLS) — combat begins from narrative (Дн.), never from an admin command.

        V10: PC initiatives come from pc_initiatives dict (rolled by players via
        request_player_roll with inline buttons). NPC initiatives are still rolled
        server-side with random.randint. If a PC has no pre-rolled initiative (timeout),
        falls back to server-side roll.

        V10b: Creates CombatEncounter + Combatant entries in the DB so that the
        per-turn combat loop (_start_combat_turn_loop in bot.py) can read them
        via get_current_initiative_turn / advance_initiative_turn. Does NOT call
        start_action_collection — the per-turn loop handles queue state."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Error: no active session."
        if session.combat_active:
            return "Combat is already active — do not call start_combat again this fight."

        pc_initiatives = pc_initiatives or {}
        chars_by_name = {c.name.lower(): c for c in db.get_session_characters(session_id) if c.is_alive}

        # ── Create CombatEncounter in DB ──
        encounter_id = uuid.uuid4().hex[:12]
        encounter = CombatEncounter(
            id=encounter_id, session_id=session_id, reason=reason,
            created_at=datetime.utcnow().isoformat(),
        )
        db.create_combat_encounter(encounter)

        initiative_list = []
        results = []
        sort_order = 0

        for name in participant_names:
            combatant_id = uuid.uuid4().hex[:12]
            char = chars_by_name.get(name.lower())

            if char:
                stats = json.loads(char.stats) if char.stats else {}
                dex_mod = (stats.get("dexterity", 10) - 10) // 2

                # V10: Use player-rolled initiative if available, else server roll
                pre_rolled = None
                for key, val in pc_initiatives.items():
                    if key.lower() == name.lower() or name.lower() in key.lower() or key.lower() in name.lower():
                        pre_rolled = val
                        break

                if pre_rolled:
                    roll_nat = pre_rolled.get("natural", random.randint(1, 20))
                    roll_total = roll_nat + dex_mod
                    results.append(f"{char.name}: d20={roll_nat}{dex_mod:+d} = {roll_total} (player rolled)")
                else:
                    # Timeout or no player roll — server fallback
                    roll_nat = random.randint(1, 20)
                    roll_total = roll_nat + dex_mod
                    results.append(f"{char.name}: d20={roll_nat}{dex_mod:+d} = {roll_total} (server fallback)")

                # ── Create Combatant in DB ──
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=char.name, entity_type="pc", player_id=char.player_id,
                    initiative=roll_total, natural_roll=roll_nat, dex_mod=dex_mod,
                    hp=char.hp, max_hp=char.max_hp, ac=char.ac,
                    sort_order=sort_order,
                )
                db.add_combatant(combatant)

                initiative_list.append({
                    "name": char.name, "player_id": char.player_id,
                    "initiative": roll_total, "natural": roll_nat, "dex_mod": dex_mod,
                })
            else:
                # NPC/monster — server roll, hidden
                roll_nat = random.randint(1, 20)
                roll_total = roll_nat
                results.append(f"{name}: d20 = {roll_nat} (NPC, hidden)")

                # ── Create Combatant in DB ──
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="npc", player_id=0,
                    initiative=roll_total, natural_roll=roll_nat, dex_mod=0,
                    sort_order=sort_order,
                )
                db.add_combatant(combatant)

                initiative_list.append({
                    "name": name, "player_id": 0,
                    "initiative": roll_total, "natural": roll_nat, "dex_mod": 0,
                })

            sort_order += 1

        if not initiative_list:
            return "Error: no valid participants supplied to start_combat."

        initiative_list.sort(key=lambda x: x["initiative"], reverse=True)

        # ── Update session state ──
        session.combat_active = True
        session.initiative_order = json.dumps(initiative_list)
        session.current_turn_index = 0
        session.round_number = 1
        db.update_session(session)

        db.add_history(HistoryEntry(
            session_id=session_id, author="DM",
            content=f"Бой начался ({reason}). Порядок: " + ", ".join(f"{e['name']} {e['initiative']}" for e in initiative_list),
            entry_type="combat",
        ))

        # V10b: Do NOT call start_action_collection here.
        # The per-turn orchestrator (_start_combat_turn_loop) manages queue state.

        order_str = "; ".join(results)
        return f"Combat started. Initiative rolled: {order_str}. First to act: {initiative_list[0]['name']}."


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

    # ═══════════════════════════════════════════════════════════
    # INITIATIVE-BASED COMBAT (dechrauymladd)
    # ═══════════════════════════════════════════════════════════

    def start_initiative_combat(self, session_id: str, participants: list, reason: str) -> str:
        """Start initiative-based combat. Called by Master's dechrauymladd tool."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Error: session not found"
        
        encounter_id = uuid.uuid4().hex[:12]
        encounter = CombatEncounter(
            id=encounter_id, session_id=session_id, reason=reason,
            created_at=datetime.utcnow().isoformat()
        )
        db.create_combat_encounter(encounter)
        
        players = db.get_players(session_id)
        player_map = {p.display_name: p for p in players}
        characters = db.get_session_characters(session_id)
        char_map = {c.name: c for c in characters}
        
        initiative_results = []
        sort_order = 0
        
        for name in participants:
            combatant_id = uuid.uuid4().hex[:12]
            if name in player_map:
                player = player_map[name]
                char = char_map.get(name)
                stats = json.loads(char.stats) if char and char.stats else {}
                dex = stats.get("dexterity", 10)
                dex_mod = (dex - 10) // 2
                roll = random.randint(1, 20)
                initiative = roll + dex_mod
                hp = char.hp if char else 0
                max_hp = char.max_hp if char else 0
                ac = char.ac if char else 10
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="pc", player_id=player.user_id,
                    initiative=initiative, natural_roll=roll, dex_mod=dex_mod,
                    hp=hp, max_hp=max_hp, ac=ac, sort_order=sort_order,
                )
            else:
                roll = random.randint(1, 20)
                initiative = roll
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="npc", player_id=0,
                    initiative=initiative, natural_roll=roll, dex_mod=0,
                    sort_order=sort_order,
                )
            
            db.add_combatant(combatant)
            initiative_results.append(f"{'👤' if combatant.entity_type == 'pc' else '👹'} {name}: {roll}{'+' + str(dex_mod) if combatant.entity_type == 'pc' else ''} = **{initiative}**")
            sort_order += 1
        
        session.combat_active = True
        # Use the sorted initiative order (not insertion order) for session.initiative_order
        sorted_order = db.get_initiative_order(encounter_id)
        session.initiative_order = json.dumps([{"name": e["name"], "player_id": e["player_id"]} for e in sorted_order])
        session.current_turn_index = 0
        session.round_number = 1
        db.update_session(session)
        
        db.add_history(HistoryEntry(
            session_id=session_id, entry_type="gm_secret",
            content=f"COMBAT STARTED: {reason}\n" + "\n".join(initiative_results),
            author="SYSTEM"
        ))
        
        order = db.get_initiative_order(encounter_id)
        order_text = "\n".join(f"{i+1}. {e['name']} ({e['initiative']})" for i, e in enumerate(order))
        return f"COMBAT! Reason: {reason}\n\nInitiative order:\n{order_text}"


    def end_initiative_combat(self, session_id: str, reason: str) -> str:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return "No active combat"
        db.end_combat_encounter(encounter.id)
        session = db.get_session(session_id)
        if session:
            session.combat_active = False
            session.initiative_order = "[]"
            session.current_turn_index = 0
            session.round_number = 0
            db.update_session(session)
        db.add_history(HistoryEntry(
            session_id=session_id, entry_type="narrative",
            content=f"Combat ended: {reason}", author="SYSTEM"
        ))
        # ── DUAL NARRATIVE: merge non-combat queue back into normal flow ──
        # Process any remaining non-combat actions, then clear the queue
        remaining = self.get_non_combat_queue(session_id)
        if remaining:
            logger.info(f"[dual-narrative] {len(remaining)} non-combat actions pending at combat end — will be resolved in next normal round")
            # Don't clear — they'll be picked up in the normal round
        self.clear_non_combat_queue(session_id)
        self.start_action_collection(session_id)
        return f"Combat ended: {reason}. Normal mode restored."


    def get_current_initiative_turn(self, session_id: str) -> Optional[Dict]:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return None
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return None
        idx = session.current_turn_index % len(order)
        return order[idx]


    def get_next_initiative_turns(self, session_id: str, count: int = 3) -> List[Dict]:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return []
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return []
        result = []
        for i in range(1, count + 1):
            idx = (session.current_turn_index + i) % len(order)
            result.append(order[idx])
        return result


    def advance_initiative_turn(self, session_id: str) -> Dict:
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not session or not encounter:
            return {}
        order = db.get_initiative_order(encounter.id)
        if not order:
            return {}
        new_idx = session.current_turn_index + 1
        if new_idx >= len(order):
            new_idx = 0
            session.round_number += 1
            db.increment_combat_round(encounter.id)
        session.current_turn_index = new_idx
        db.update_session(session)
        return order[new_idx]


    def get_combat_action_group(self, session_id: str) -> Dict:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return {"mode": "normal", "waiting_for": [], "npc_turn": False, "blocked_players": []}
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return {"mode": "normal", "waiting_for": [], "npc_turn": False, "blocked_players": []}
        idx = session.current_turn_index % len(order)
        current = order[idx]
        next_entries = []
        look_idx = (idx + 1) % len(order)
        while look_idx != idx:
            next_entries.append(order[look_idx])
            look_idx = (look_idx + 1) % len(order)
            if len(next_entries) >= len(order):
                break
        result = {
            "current": current,
            "next_up": next_entries[:3] if next_entries else [],
            "waiting_for": [],
            "npc_turn": current["entity_type"] != "pc",
            "blocked_players": [],
            "all_order": order,
        }
        if current["entity_type"] != "pc":
            npc_streak = [current]
            for entry in next_entries:
                if entry["entity_type"] != "pc":
                    npc_streak.append(entry)
                else:
                    break
            result["npc_streak"] = npc_streak
        else:
            pc_streak = [current]
            for entry in next_entries:
                if entry["entity_type"] == "pc":
                    pc_streak.append(entry)
                else:
                    break
            result["waiting_for"] = [p["player_id"] for p in pc_streak if p.get("player_id", 0) > 0]
            if len(pc_streak) > 1:
                result["simultaneous_players"] = pc_streak
        all_pcs = [e for e in order if e["entity_type"] == "pc" and e.get("player_id", 0) > 0]
        waiting_ids = set(result.get("waiting_for", []))
        result["blocked_players"] = [p["player_id"] for p in all_pcs if p["player_id"] not in waiting_ids]
        return result

    # ── V10b: Per-turn combat helpers ──


    def get_combat_context(self, session_id: str) -> str:
        """Build full combat state text for per-turn master context."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter or not session:
            return ""

        order = db.get_initiative_order(encounter.id)
        if not order:
            return ""

        chars = db.get_session_characters(session_id)
        char_map = {c.name.lower(): c for c in chars}

        lines = [f"--- БОЙ: Раунд {session.round_number} ---"]
        lines.append("Порядок инициативы:")
        for i, e in enumerate(order):
            marker = " >>> " if i == session.current_turn_index else "     "
            status = ""
            if e["entity_type"] == "pc":
                ch = char_map.get(e["name"].lower())
                if ch:
                    status = f" | HP {ch.hp}/{ch.max_hp} | KB {ch.ac}"
            lines.append(f"  {marker}{i+1}. {e['name']} (ini {e['initiative']}){status}")

        cur = order[session.current_turn_index % len(order)]
        lines.append(f"\nТЕКУЩИЙ ХОД: {cur['name']} ({cur['entity_type'].upper()})")
        return "\n".join(lines)


    def start_combat_turn_collection(self, session_id: str) -> Dict:
        group = self.get_combat_action_group(session_id)
        if group.get("npc_turn"):
            return group
        waiting_for = group.get("waiting_for", [])
        if waiting_for:
            db = self.db_manager.get_db(session_id)
            state = QueueState(
                session_id=session_id,
                waiting_for=json.dumps(waiting_for),
                collected_actions="{}",
                is_resolving=False,
            )
            db.set_queue_state(state)
        return group


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


