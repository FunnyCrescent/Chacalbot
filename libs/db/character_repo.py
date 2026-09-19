"""CharacterRepoMixin — characters, sheets, hp/conditions/rests, gold, inventory, goals, quests, resources, effects."""
import json
import logging
import sqlite3
import os
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from .models import (
    Session, Player, Character, HistoryEntry, QueueState, CharacterSheet,
    HpLog, ConditionEntry, RestEntry, GoldEntry, QuestEntry, GameTime,
    FactionEntry, FactionRelation, WorldEvent, SrdCache, LocationBinding,
    CharacterResources, NpcMemory, SrdMonster, SrdItem, SrdSpell, Location,
    LocationPath, WorldNpc, NpcRelation, LoreArticle, MarketPrice,
    EconomicEvent, ActiveEffect, Timer, LootTable, DbJournalEntry,
    MemoryEntry, LocationRelation, CombatEncounter, Combatant, PlayerLanguage,
    SettingEntry, RoundMessageTracker, PendingLevelUp,
)

logger = logging.getLogger(__name__)


class CharacterRepoMixin:
    """CharacterRepoMixin — characters, sheets, hp/conditions/rests, gold, inventory, goals, quests, resources, effects."""

    def save_character(self, character: Character) -> Character:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO characters
                   (id, session_id, player_id, name, race, class_name, level,
                    hp, max_hp, ac, stats, proficiencies, inventory, spells,
                    features, backstory, death_saves_success, death_saves_failure,
                    is_alive, conditions, languages, xp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (character.id, character.session_id, character.player_id,
                 character.name, character.race, character.class_name,
                 character.level, character.hp, character.max_hp, character.ac,
                 character.stats, character.proficiencies, character.inventory,
                 character.spells, character.features, character.backstory,
                 character.death_saves_success, character.death_saves_failure,
                 int(character.is_alive), character.conditions, character.languages,
                 max(0, int(getattr(character, "xp", 0) or 0)))
            )
        self.add_journal_entry(character.session_id, "INSERT", "characters", character.id,
                               f"Character {character.name} saved")
        return character


    def get_character(self, character_id: str) -> Optional[Character]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE id = ?", (character_id,)
            ).fetchone()
            if row:
                return self._row_to_character(row)
            return None


    def get_character_by_player(self, player_id: int, session_id: str) -> Optional[Character]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM characters WHERE player_id = ? AND session_id = ?",
                (player_id, session_id)
            ).fetchone()
            if row:
                return self._row_to_character(row)
            return None


    def get_session_characters(self, session_id: str) -> List[Character]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM characters WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [self._row_to_character(r) for r in rows]


    def update_character_hp(self, character_id: str, hp: int):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET hp = ? WHERE id = ?",
                (hp, character_id)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"HP changed to {hp}")


    def update_character_death_saves(self, character_id: str, successes: int, failures: int):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET death_saves_success = ?, death_saves_failure = ? WHERE id = ?",
                (successes, failures, character_id)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"Death saves: {successes}S / {failures}F")


    def kill_character(self, character_id: str):
        char = self.get_character(character_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE characters SET is_alive = 0, hp = 0 WHERE id = ?",
                (character_id,)
            )
        if char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"Character {char.name} died")

    # ═══════════════════════════════════════════════════════════
    # Character Progression — the DB is the source of truth, not the
    # uploaded sheet file. Levels, ability scores, features and
    # proficiencies can all change mid-campaign.
    # ═══════════════════════════════════════════════════════════

    VALID_ABILITIES = ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")


    def set_character_level(self, character_id: str, level: int, new_max_hp: Optional[int] = None):
        """Level up (or set) a character's level. Optionally bump max HP (current HP increases by the same delta)."""
        char = self.get_character(character_id)
        if not char:
            return
        level = max(1, min(20, level))
        with self._connect() as conn:
            if new_max_hp is not None:
                hp_delta = max(0, new_max_hp - char.max_hp)
                new_hp = char.hp + hp_delta
                conn.execute(
                    "UPDATE characters SET level = ?, max_hp = ?, hp = ? WHERE id = ?",
                    (level, new_max_hp, new_hp, character_id)
                )
            else:
                conn.execute("UPDATE characters SET level = ? WHERE id = ?", (level, character_id))
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                               f"{char.name} level -> {level}" + (f", max HP -> {new_max_hp}" if new_max_hp is not None else ""))

    # ═══════════════════════════════════════════════════════════
    # XP (ИТЕРАЦИЯ 15) — скрытая характеристика; видят только
    # Мастер-нейросеть и DB-бот. Авто-уровневание по порогам
    # libs.xp_system.LEVEL_XP_THRESHOLDS.
    # ═══════════════════════════════════════════════════════════

    def grant_character_xp(self, character_id: str, amount: int, source: str = "") -> Optional[Dict]:
        """Начислить XP персонажу и АВТОМАТИЧЕСКИ поднять уровень, если накопленный
        XP достиг порога из раздела 1 ТЗ («Повышение уровня происходит сразу»).

        Возвращает словарь:
          {"character_name", "xp_added", "xp_total", "old_level", "new_level",
           "leveled_up", "next_threshold", "source"}
        или None, если персонаж не найден. Отрицательные суммы клампятся так,
        что итоговый XP не опускается ниже 0.
        """
        from libs.xp_system import level_for_xp, next_level_threshold

        char = self.get_character(character_id)
        if not char:
            return None
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        old_xp = max(0, int(getattr(char, "xp", 0) or 0))
        new_xp = max(0, old_xp + amount)
        new_level = level_for_xp(new_xp)
        old_level = char.level
        leveled_up = new_level > old_level

        with self._connect() as conn:
            if leveled_up:
                conn.execute(
                    "UPDATE characters SET xp = ?, level = ? WHERE id = ?",
                    (new_xp, new_level, character_id)
                )
            else:
                conn.execute(
                    "UPDATE characters SET xp = ? WHERE id = ?",
                    (new_xp, character_id)
                )

        journal = f"{char.name}: {'+' if amount >= 0 else ''}{amount} XP ({source or 'AI'}) -> {new_xp} XP"
        if leveled_up:
            journal += f"; УРОВЕНЬ {old_level} -> {new_level} (авто по XP)"
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id, journal)

        return {
            "character_name": char.name,
            "xp_added": amount,
            "xp_total": new_xp,
            "old_level": old_level,
            "new_level": new_level,
            "leveled_up": leveled_up,
            "next_threshold": next_level_threshold(new_level),
            "source": source or "AI",
        }

    def set_character_xp(self, character_id: str, xp: int) -> Optional[Dict]:
        """Жёстко выставить XP (коррекции/тесты). Уровень синхронизируется по порогам."""
        from libs.xp_system import level_for_xp, next_level_threshold

        char = self.get_character(character_id)
        if not char:
            return None
        try:
            xp = max(0, int(xp))
        except (TypeError, ValueError):
            xp = 0
        new_level = level_for_xp(xp)
        old_level = char.level
        with self._connect() as conn:
            if new_level != old_level:
                conn.execute(
                    "UPDATE characters SET xp = ?, level = ? WHERE id = ?",
                    (xp, new_level, character_id)
                )
            else:
                conn.execute("UPDATE characters SET xp = ? WHERE id = ?", (xp, character_id))
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                               f"{char.name}: XP -> {xp}" + (f"; УРОВЕНЬ {old_level} -> {new_level}" if new_level != old_level else ""))
        return {
            "character_name": char.name,
            "xp_total": xp,
            "old_level": old_level,
            "new_level": new_level,
            "leveled_up": new_level > old_level,
            "next_threshold": next_level_threshold(new_level),
            "source": "set",
        }


    # ═══════════════════════════════════════════════════════════
    # Pending level-ups (ИТЕРАЦИЯ 16) — неоформленные повышения
    # ═══════════════════════════════════════════════════════════

    def add_pending_level_up(self, record: PendingLevelUp) -> PendingLevelUp:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO pending_level_ups
                   (id, session_id, character_id, character_name, from_level,
                    to_level, brief, status, created_at, done_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), CURRENT_TIMESTAMP), ?)""",
                (record.id, record.session_id, record.character_id, record.character_name,
                 record.from_level, record.to_level, record.brief, record.status,
                 record.created_at or "", record.done_at or "")
            )
        self.add_journal_entry(record.session_id, "INSERT", "pending_level_ups", record.id,
                               f"{record.character_name}: level {record.from_level}->{record.to_level} awaits paperwork")
        return record

    def get_pending_level_ups(self, session_id: str, status: str = "pending") -> List[PendingLevelUp]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_level_ups WHERE session_id = ? AND status = ? ORDER BY created_at",
                (session_id, status)
            ).fetchall()
            return [self._row_to_pending_level_up(r) for r in rows]

    def complete_pending_level_ups_for_character(self, character_id: str) -> int:
        """Закрыть ВСЕ pending-повышения персонажа (инструмент complete_level_up). Вернуть сколько закрыто."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pending_level_ups SET status = 'done', done_at = CURRENT_TIMESTAMP "
                "WHERE character_id = ? AND status = 'pending'",
                (character_id,)
            )
            count = cur.rowcount or 0
        if count:
            char = self.get_character(character_id)
            if char:
                self.add_journal_entry(char.session_id, "UPDATE", "pending_level_ups", character_id,
                                       f"{char.name}: level-up paperwork completed ({count})")
        return count

    def _row_to_pending_level_up(self, row) -> PendingLevelUp:
        return PendingLevelUp(
            id=row["id"], session_id=row["session_id"], character_id=row["character_id"],
            character_name=row["character_name"], from_level=row["from_level"] or 1,
            to_level=row["to_level"] or 1, brief=row["brief"] or "",
            status=row["status"] or "pending", created_at=row["created_at"] or "",
            done_at=row["done_at"] or "",
        )

    def pending_level_up_block(self, session_id: str) -> str:
        """Скрытый блок для контекста Мастера: неоформленные повышения уровня.

        Пустая строка — напоминать не о чем. Бриф уже был отправлен через
        GM_SECRET-историю; здесь — ПОВТОР, пока Мастер не закроет повышение.
        """
        pendings = self.get_pending_level_ups(session_id, status="pending")
        if not pendings:
            return ""
        lines = ["\n⬆️ НЕОФОРМЛЕННЫЕ ПОВЫШЕНИЯ УРОВНЯ (оформи в этом раунде, если сцена позволяет):"]
        for p in pendings[:5]:
            brief_short = (p.brief or "").strip()
            lines.append(brief_short)
        lines.append(
            "Выборы передай строками «УРОВЕНЬ+: ...» ВНУТРИ СВОДКИ; когда всё передано — "
            "строка «УРОВЕНЬ+ ГОТОВО: <имя>». Это напоминание исчезнет только после ГОТОВО."
        )
        return "\n".join(lines)


    def set_character_ability_score(self, character_id: str, ability: str, value: int):
        """Update a single ability score (STR/DEX/CON/INT/WIS/CHA) inside the stats JSON blob."""
        ability = ability.lower()
        if ability not in self.VALID_ABILITIES:
            return
        char = self.get_character(character_id)
        if not char:
            return
        value = max(1, min(30, value))
        try:
            stats = json.loads(char.stats) if char.stats else {}
        except Exception:
            stats = {}
        stats[ability] = value
        with self._connect() as conn:
            conn.execute("UPDATE characters SET stats = ? WHERE id = ?", (json.dumps(stats), character_id))
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                               f"{char.name} {ability} -> {value}")


    def set_character_ac(self, character_id: str, ac: int):
        char = self.get_character(character_id)
        if not char:
            return
        with self._connect() as conn:
            conn.execute("UPDATE characters SET ac = ? WHERE id = ?", (ac, character_id))
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id, f"{char.name} AC -> {ac}")


    def set_character_max_hp(self, character_id: str, max_hp: int, adjust_current: bool = True):
        char = self.get_character(character_id)
        if not char:
            return
        with self._connect() as conn:
            if adjust_current:
                delta = max_hp - char.max_hp
                new_hp = max(0, min(max_hp, char.hp + delta))
                conn.execute("UPDATE characters SET max_hp = ?, hp = ? WHERE id = ?", (max_hp, new_hp, character_id))
            else:
                conn.execute("UPDATE characters SET max_hp = ? WHERE id = ?", (max_hp, character_id))
        self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id, f"{char.name} max HP -> {max_hp}")


    def _append_to_json_list_field(self, character_id: str, field: str, value: str) -> bool:
        """Shared helper: append a string to a JSON-list column (features/proficiencies/spells/inventory) if not already present."""
        if field not in ("features", "proficiencies", "spells"):
            raise ValueError(f"Unsupported list field: {field}")
        char = self.get_character(character_id)
        if not char:
            return False
        raw = getattr(char, field, "") or "[]"
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        if value not in items:
            items.append(value)
        with self._connect() as conn:
            conn.execute(f"UPDATE characters SET {field} = ? WHERE id = ?", (json.dumps(items, ensure_ascii=False), character_id))
        return True


    def add_character_feature(self, character_id: str, feature: str):
        char = self.get_character(character_id)
        if self._append_to_json_list_field(character_id, "features", feature) and char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"{char.name} gained feature: {feature}")


    def add_character_proficiency(self, character_id: str, proficiency: str):
        char = self.get_character(character_id)
        if self._append_to_json_list_field(character_id, "proficiencies", proficiency) and char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"{char.name} gained proficiency: {proficiency}")


    def add_character_spell(self, character_id: str, spell: str):
        char = self.get_character(character_id)
        if self._append_to_json_list_field(character_id, "spells", spell) and char:
            self.add_journal_entry(char.session_id, "UPDATE", "characters", character_id,
                                   f"{char.name} learned spell: {spell}")


    def get_character_progression_summary(self, character_id: str) -> str:
        """Compact, human-readable snapshot of CURRENT (possibly leveled-up) character
        state, meant to replace the static uploaded-sheet text in the Master's prompt."""
        char = self.get_character(character_id)
        if not char:
            return ""
        try:
            stats = json.loads(char.stats) if char.stats else {}
        except Exception:
            stats = {}
        try:
            profs = json.loads(char.proficiencies) if char.proficiencies else []
        except Exception:
            profs = []
        try:
            feats = json.loads(char.features) if char.features else []
        except Exception:
            feats = []
        try:
            spells = json.loads(char.spells) if char.spells else []
        except Exception:
            spells = []

        def mod(stat):
            return (stats.get(stat, 10) - 10) // 2

        prof_bonus = 2 + (char.level - 1) // 4  # 5e progression: +2 at 1-4, +3 at 5-8, ...
        try:
            from libs.xp_system import next_level_threshold as _nxt
            _next_thr = _nxt(char.level)
        except Exception:
            _next_thr = None
        xp_total = max(0, int(getattr(char, "xp", 0) or 0))
        xp_line = f"XP: {xp_total}"
        if xp_total > 0:
            # ИТЕРАЦИЯ 15: XP — скрытая характеристика. Эта сводка идёт ТОЛЬКО в
            # контекст Мастера/LLM-бросков — игроки её не видят.
            if _next_thr is not None:
                xp_line += f" (до уровня {char.level + 1}: {_next_thr} XP)"
            else:
                xp_line += " (максимальный уровень)"
        lines = [
            f"{char.name} — {char.race} {char.class_name}, уровень {char.level}",
            f"HP {char.hp}/{char.max_hp} | AC {char.ac} | Бонус мастерства +{prof_bonus}",
            xp_line,
            f"СИЛ {stats.get('strength',10)}({mod('strength'):+d}) ЛОВ {stats.get('dexterity',10)}({mod('dexterity'):+d}) "
            f"ТЕЛ {stats.get('constitution',10)}({mod('constitution'):+d}) ИНТ {stats.get('intelligence',10)}({mod('intelligence'):+d}) "
            f"МУД {stats.get('wisdom',10)}({mod('wisdom'):+d}) ХАР {stats.get('charisma',10)}({mod('charisma'):+d})",
        ]
        if profs:
            lines.append("Владения: " + ", ".join(profs))
        if feats:
            lines.append("Черты/умения: " + ", ".join(feats))
        if spells:
            lines.append("Заклинания: " + ", ".join(spells))
        if not char.is_alive:
            lines.append("💀 МЁРТВ")
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # Character Sheets (full text)
    # ═══════════════════════════════════════════════════════════


    def save_character_sheet(self, session_id: str, player_id: int, sheet_text: str, file_name: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO character_sheets
                   (session_id, player_id, sheet_text, file_name)
                   VALUES (?, ?, ?, ?)""",
                (session_id, player_id, sheet_text, file_name)
            )
        self.add_journal_entry(session_id, "INSERT", "character_sheets", f"{player_id}",
                               f"Sheet uploaded by player {player_id}")


    def get_character_sheet(self, session_id: str, player_id: int) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sheet_text FROM character_sheets WHERE session_id = ? AND player_id = ?",
                (session_id, player_id)
            ).fetchone()
            return row["sheet_text"] if row else None


    def get_all_character_sheets(self, session_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sheet_text FROM character_sheets WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [r["sheet_text"] for r in rows if r["sheet_text"]]


    def remove_character_sheet(self, session_id: str, player_id: int):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM character_sheets WHERE session_id = ? AND player_id = ?",
                (session_id, player_id)
            )

    # ═══════════════════════════════════════════════════════════
    # History
    # ═══════════════════════════════════════════════════════════


    @staticmethod

    def _row_to_character(row: sqlite3.Row) -> Character:
        return Character(
            id=row["id"],
            session_id=row["session_id"],
            player_id=row["player_id"],
            name=row["name"],
            race=row["race"] or "",
            class_name=row["class_name"] or "",
            level=row["level"],
            hp=row["hp"],
            max_hp=row["max_hp"],
            ac=row["ac"],
            stats=row["stats"] or "{}",
            proficiencies=row["proficiencies"] or "[]",
            inventory=row["inventory"] or "[]",
            spells=row["spells"] or "[]",
            features=row["features"] or "[]",
            backstory=row["backstory"] or "",
            death_saves_success=row["death_saves_success"],
            death_saves_failure=row["death_saves_failure"],
            is_alive=bool(row["is_alive"]),
            conditions=row["conditions"] or "[]",
            languages=row["languages"] or "[]",
            xp=(row["xp"] if "xp" in row.keys() else 0) or 0,
        )

    def add_hp_log(self, session_id: str, character_id: str, character_name: str,
                   old_hp: int, new_hp: int, source: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO hp_log (session_id, character_id, character_name, old_hp, new_hp, change, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, old_hp, new_hp, new_hp - old_hp, source)
            )
        self.add_journal_entry(session_id, "INSERT", "hp_log", character_id,
                               f"{character_name}: {old_hp} -> {new_hp} HP ({source})")


    def get_hp_log(self, session_id: str, character_id: str = None, limit: int = 20) -> List[HpLog]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM hp_log WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, character_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM hp_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit)
                ).fetchall()
            return [HpLog(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], old_hp=r["old_hp"], new_hp=r["new_hp"],
                change=r["change"], source=r["source"] or "", created_at=r["created_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Conditions (#3)
    # ═══════════════════════════════════════════════════════════


    def add_condition(self, session_id: str, character_id: str, character_name: str,
                      condition: str, source: str = "", duration: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO conditions (session_id, character_id, character_name, condition, source, duration)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, condition.lower(), source, duration)
            )
        self.add_journal_entry(session_id, "INSERT", "conditions", character_id,
                               f"{character_name} gained {condition} ({source})")


    def remove_condition(self, session_id: str, character_id: str, condition: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM conditions WHERE session_id = ? AND character_id = ? AND condition = ?",
                (session_id, character_id, condition.lower())
            )
        self.add_journal_entry(session_id, "DELETE", "conditions", character_id,
                               f"Removed {condition}")


    def get_conditions(self, session_id: str, character_id: str = None) -> List[ConditionEntry]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM conditions WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC",
                    (session_id, character_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conditions WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,)
                ).fetchall()
            return [ConditionEntry(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], condition=r["condition"], source=r["source"] or "",
                duration=r["duration"] or "", created_at=r["created_at"] or "", expires_at=r["expires_at"] or ""
            ) for r in rows]


    def remove_all_conditions(self, session_id: str, character_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM conditions WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            )
        self.add_journal_entry(session_id, "DELETE", "conditions", character_id, "All conditions cleared")

    # ═══════════════════════════════════════════════════════════
    # Rest (#4)
    # ═══════════════════════════════════════════════════════════


    def add_rest(self, session_id: str, character_id: str, character_name: str,
                 rest_type: str, hp_restored: int, hit_dice_used: int, abilities_recovered: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO rest_log (session_id, character_id, character_name, rest_type, hp_restored, hit_dice_used, abilities_recovered)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, rest_type, hp_restored, hit_dice_used, abilities_recovered)
            )
        self.add_journal_entry(session_id, "INSERT", "rest_log", character_id,
                               f"{character_name}: {rest_type} rest, +{hp_restored} HP")


    def get_rest_history(self, session_id: str, character_id: str = None, limit: int = 10) -> List[RestEntry]:
        with self._connect() as conn:
            if character_id:
                rows = conn.execute(
                    "SELECT * FROM rest_log WHERE session_id = ? AND character_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, character_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rest_log WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit)
                ).fetchall()
            return [RestEntry(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                character_name=r["character_name"], rest_type=r["rest_type"],
                hp_restored=r["hp_restored"], hit_dice_used=r["hit_dice_used"],
                abilities_recovered=r["abilities_recovered"] or "", created_at=r["created_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Gold & Inventory (#9)
    # ═══════════════════════════════════════════════════════════


    def add_gold_transaction(self, session_id: str, character_id: str, character_name: str,
                             cp: int = 0, sp: int = 0, ep: int = 0, gp: int = 0, pp: int = 0, reason: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO gold_log (session_id, character_id, character_name, delta_cp, delta_sp, delta_ep, delta_gp, delta_pp, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, cp, sp, ep, gp, pp, reason)
            )
        self.add_journal_entry(session_id, "INSERT", "gold_log", character_id,
                               f"{character_name}: {gp:+d}gp ({reason})")


    def get_gold_balance(self, session_id: str, character_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(delta_cp),0) as cp, COALESCE(SUM(delta_sp),0) as sp,
                          COALESCE(SUM(delta_ep),0) as ep, COALESCE(SUM(delta_gp),0) as gp,
                          COALESCE(SUM(delta_pp),0) as pp
                   FROM gold_log WHERE session_id = ? AND character_id = ?""",
                (session_id, character_id)
            ).fetchone()
            return {"cp": row["cp"], "sp": row["sp"], "ep": row["ep"], "gp": row["gp"], "pp": row["pp"]}


    def add_inventory_item(self, session_id: str, character_id: str, character_name: str,
                           item_name: str, quantity: int = 1, description: str = ""):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, quantity FROM inventory WHERE session_id = ? AND character_id = ? AND LOWER(item_name) = LOWER(?)",
                (session_id, character_id, item_name)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (existing["quantity"] + quantity, existing["id"])
                )
            else:
                conn.execute(
                    """INSERT INTO inventory (session_id, character_id, character_name, item_name, quantity, description)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, character_id, character_name, item_name, quantity, description)
                )
        self.add_journal_entry(session_id, "INSERT", "inventory", character_id,
                               f"{character_name} gained {item_name} x{quantity}")


    def remove_inventory_item(self, session_id: str, character_id: str, item_name: str, quantity: int = 1):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, quantity FROM inventory WHERE session_id = ? AND character_id = ? AND LOWER(item_name) = LOWER(?)",
                (session_id, character_id, item_name)
            ).fetchone()
            if existing:
                new_qty = existing["quantity"] - quantity
                if new_qty <= 0:
                    conn.execute("DELETE FROM inventory WHERE id = ?", (existing["id"],))
                else:
                    conn.execute(
                        "UPDATE inventory SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_qty, existing["id"])
                    )
        self.add_journal_entry(session_id, "DELETE", "inventory", character_id,
                               f"Removed {item_name} x{quantity}")


    def get_inventory(self, session_id: str, character_id: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inventory WHERE session_id = ? AND character_id = ? ORDER BY item_name",
                (session_id, character_id)
            ).fetchall()
            return [{"item": r["item_name"], "qty": r["quantity"], "desc": r["description"] or ""} for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Character Goals — distinct from quests. See character_goals table comment.
    # ═══════════════════════════════════════════════════════════


    def add_character_goal(self, session_id: str, character_id: str, character_name: str,
                           title: str, source: str = "session", status: str = "active") -> int:
        """Dedup by (character_id, title similarity) so restating the same goal across
        several turns doesn't pile up duplicates — a rough substring check, good enough
        since goals are short human-authored phrases, not free-form paragraphs."""
        title_norm = (title or "").strip()
        if not title_norm:
            return 0
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, title FROM character_goals WHERE session_id = ? AND character_id = ? AND status = 'active'",
                (session_id, character_id)
            ).fetchall()
            for row in existing:
                a, b = row["title"].lower(), title_norm.lower()
                if a in b or b in a:
                    return row["id"]  # already tracked, don't duplicate
            cursor = conn.execute(
                """INSERT INTO character_goals (session_id, character_id, character_name, title, source, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, character_id, character_name, title_norm, source, status)
            )
            goal_id = cursor.lastrowid
        self.add_journal_entry(session_id, "INSERT", "character_goals", str(goal_id),
                               f"{character_name}: цель '{title_norm}' ({source})")
        return goal_id


    def get_character_goals(self, session_id: str, character_id: str = None, status: str = None) -> List[dict]:
        with self._connect() as conn:
            query = "SELECT * FROM character_goals WHERE session_id = ?"
            params = [session_id]
            if character_id:
                query += " AND character_id = ?"
                params.append(character_id)
            if status:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [{
                "id": r["id"], "character_name": r["character_name"], "title": r["title"],
                "status": r["status"], "source": r["source"], "created_at": r["created_at"] or "",
            } for r in rows]


    def update_character_goal_status(self, goal_id: int, status: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE character_goals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, goal_id)
            )
        self.add_journal_entry("", "UPDATE", "character_goals", str(goal_id), f"Goal status -> {status}")

    # ═══════════════════════════════════════════════════════════
    # Quests (#10, #17)
    # ═══════════════════════════════════════════════════════════


    def add_quest(self, session_id: str, title: str, description: str = "",
                  assignee_id: str = "", assignee_name: str = "", status: str = "active"):
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO quests (session_id, assignee_id, assignee_name, title, description, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, assignee_id, assignee_name, title, description, status)
            )
            quest_id = cursor.lastrowid
        self.add_journal_entry(session_id, "INSERT", "quests", str(quest_id), f"Quest added: {title}")
        return quest_id


    def update_quest(self, quest_id: int, status: str = None, title: str = None, description: str = None):
        with self._connect() as conn:
            if status:
                conn.execute("UPDATE quests SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (status, quest_id))
            if title:
                conn.execute("UPDATE quests SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (title, quest_id))
            if description:
                conn.execute("UPDATE quests SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (description, quest_id))
        self.add_journal_entry("", "UPDATE", "quests", str(quest_id), f"Quest #{quest_id} updated")


    def get_quests(self, session_id: str, status: str = None, assignee_id: str = None) -> List[QuestEntry]:
        with self._connect() as conn:
            query = "SELECT * FROM quests WHERE session_id = ?"
            params = [session_id]
            if status:
                query += " AND status = ?"
                params.append(status)
            if assignee_id:
                query += " AND assignee_id = ?"
                params.append(assignee_id)
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [QuestEntry(
                id=r["id"], session_id=r["session_id"], assignee_id=r["assignee_id"] or "",
                assignee_name=r["assignee_name"] or "", title=r["title"],
                description=r["description"] or "", status=r["status"] or "active",
                created_at=r["created_at"] or "", updated_at=r["updated_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Game Time & Weather (#11)
    # ═══════════════════════════════════════════════════════════


    def set_location(self, session_id: str, character_id: str, location_name: str, location_description: str = ""):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO location_bindings
                   (session_id, character_id, location_name, location_description, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (session_id, character_id, location_name, location_description)
            )
        self.add_journal_entry(session_id, "UPDATE", "location_bindings", character_id,
                               f"Location: {location_name}")


    def get_location(self, session_id: str, character_id: str) -> Optional[LocationBinding]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM location_bindings WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            ).fetchone()
            if row:
                return LocationBinding(
                    session_id=row["session_id"], character_id=row["character_id"],
                    location_name=row["location_name"] or "", location_description=row["location_description"] or "",
                    updated_at=row["updated_at"] or ""
                )
            return None


    def get_all_locations(self, session_id: str) -> List[LocationBinding]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM location_bindings WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            return [LocationBinding(
                session_id=r["session_id"], character_id=r["character_id"],
                location_name=r["location_name"] or "", location_description=r["location_description"] or "",
                updated_at=r["updated_at"] or ""
            ) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # Character Resources (#20)
    # ═══════════════════════════════════════════════════════════


    def set_resource(self, session_id: str, character_id: str, resource_name: str,
                     current: int, maximum: int, short_rest: bool = False, long_rest: bool = True):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO character_resources
                   (session_id, character_id, resource_name, current, maximum, short_rest_recover, long_rest_recover, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (session_id, character_id, resource_name.lower(), current, maximum,
                 int(short_rest), int(long_rest))
            )
        self.add_journal_entry(session_id, "INSERT", "character_resources", character_id,
                               f"Resource {resource_name}: {current}/{maximum}")


    def get_resources(self, session_id: str, character_id: str) -> List[CharacterResources]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM character_resources WHERE session_id = ? AND character_id = ?",
                (session_id, character_id)
            ).fetchall()
            return [CharacterResources(
                id=r["id"], session_id=r["session_id"], character_id=r["character_id"],
                resource_name=r["resource_name"], current=r["current"], maximum=r["maximum"],
                short_rest_recover=bool(r["short_rest_recover"]), long_rest_recover=bool(r["long_rest_recover"]),
                updated_at=r["updated_at"] or ""
            ) for r in rows]


    def update_resource(self, session_id: str, character_id: str, resource_name: str, delta: int):
        with self._connect() as conn:
            conn.execute(
                """UPDATE character_resources SET current = MAX(0, MIN(maximum, current + ?)),
                   updated_at = CURRENT_TIMESTAMP
                   WHERE session_id = ? AND character_id = ? AND resource_name = ?""",
                (delta, session_id, character_id, resource_name.lower())
            )
        self.add_journal_entry(session_id, "UPDATE", "character_resources", character_id,
                               f"Resource {resource_name} {delta:+d}")


    def reset_resources(self, session_id: str, character_id: str, rest_type: str):
        with self._connect() as conn:
            if rest_type == "short":
                conn.execute(
                    """UPDATE character_resources SET current = maximum
                       WHERE session_id = ? AND character_id = ? AND short_rest_recover = 1""",
                    (session_id, character_id)
                )
            elif rest_type == "long":
                conn.execute(
                    """UPDATE character_resources SET current = maximum
                       WHERE session_id = ? AND character_id = ? AND long_rest_recover = 1""",
                    (session_id, character_id)
                )
        self.add_journal_entry(session_id, "UPDATE", "character_resources", character_id,
                               f"Resources reset after {rest_type} rest")

    # ═══════════════════════════════════════════════════════════
    # NPC Memory (#21)
    # ═══════════════════════════════════════════════════════════


    def add_effect(self, effect: ActiveEffect) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO active_effects
                   (session_id, entity_type, entity_id, name, effect_type, source, duration_type, remaining, mechanics, is_removable)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (effect.session_id, effect.entity_type, effect.entity_id, effect.name, effect.effect_type, effect.source, effect.duration_type, effect.remaining, effect.mechanics, int(effect.is_removable))
            )
            effect_id = cursor.lastrowid
        self.add_journal_entry(effect.session_id, "INSERT", "active_effects", str(effect_id), f"Effect: {effect.name} on {effect.entity_id}")
        return effect_id


    def get_effects(self, session_id: str, entity_type: str = None, entity_id: str = None) -> List[ActiveEffect]:
        with self._connect() as conn:
            query = "SELECT * FROM active_effects WHERE session_id = ?"
            params = [session_id]
            if entity_type:
                query += " AND entity_type = ?"
                params.append(entity_type)
            if entity_id:
                query += " AND entity_id = ?"
                params.append(entity_id)
            rows = conn.execute(query, params).fetchall()
            return [ActiveEffect(id=r["id"], session_id=r["session_id"], entity_type=r["entity_type"], entity_id=r["entity_id"], name=r["name"], effect_type=r["effect_type"], source=r["source"], duration_type=r["duration_type"], remaining=r["remaining"], mechanics=r["mechanics"], is_removable=bool(r["is_removable"]), created_at=r["created_at"] or "") for r in rows]


    def remove_effect(self, effect_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM active_effects WHERE id = ?", (effect_id,))


