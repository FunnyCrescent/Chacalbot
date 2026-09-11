"""CharacterServiceMixin — HP/conditions/rests/gold/inventory/quests/resources (thin DB wrappers)."""
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


class CharacterServiceMixin:
    """CharacterServiceMixin — HP/conditions/rests/gold/inventory/quests/resources (thin DB wrappers)."""

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


    def get_character_goals(self, session_id: str, character_id: str = None, status: str = None) -> List[dict]:
        return self.db_manager.get_db(session_id).get_character_goals(session_id, character_id, status)


    def add_character_goal(self, session_id: str, character_id: str, character_name: str,
                           title: str, source: str = "session") -> int:
        return self.db_manager.get_db(session_id).add_character_goal(session_id, character_id, character_name, title, source)

    # ═══════════════════════════════════════════════════════════
    # World Events (#22)
    # ═══════════════════════════════════════════════════════════


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


