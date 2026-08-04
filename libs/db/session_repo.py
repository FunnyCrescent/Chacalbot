"""SessionRepoMixin — session lifecycle, players, queue_state, history, roll_mode."""
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
    SettingEntry, RoundMessageTracker,
)

logger = logging.getLogger(__name__)


class SessionRepoMixin:
    """SessionRepoMixin — session lifecycle, players, queue_state, history, roll_mode."""

    def create_session(self, session: Session) -> Session:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO sessions (id, chat_id, name, creator_id, status, current_scene)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session.id, session.chat_id, session.name, session.creator_id,
                 session.status, session.current_scene)
            )
        self.add_journal_entry(session.id, "INSERT", "sessions", session.id, f"Created session {session.name}")
        return session


    def get_session(self, session_id: str) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None


    def get_session_by_chat(self, chat_id: int) -> Optional[Session]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status = 'active'",
                (chat_id,)
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None


    def get_last_ended_session_by_chat(self, chat_id: int) -> Optional[Session]:
        """For /resume — find the most recently ended session in this chat so it can be
        reactivated. Without this there was no way back into a session after /end short
        of creating a brand new one (losing the old session's id/link, though not its
        DB rows, which /end never deletes)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status = 'ended' ORDER BY updated_at DESC LIMIT 1",
                (chat_id,)
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None


    def get_all_active_sessions(self) -> List[Session]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active'"
            ).fetchall()
            return [self._row_to_session(r) for r in rows]


    def update_session(self, session: Session):
        with self._connect() as conn:
            conn.execute(
                """UPDATE sessions SET
                    name = ?, status = ?, current_scene = ?, combat_active = ?,
                    initiative_order = ?, current_turn_index = ?, round_number = ?,
                    autostart = ?, pvp_active = ?, summary = ?, summary_at_count = ?,
                    genre = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (session.name, session.status, session.current_scene,
                 int(session.combat_active), session.initiative_order,
                 session.current_turn_index, session.round_number,
                 int(session.autostart), int(session.pvp_active), session.summary,
                 session.summary_at_count, session.genre, session.id)
            )
        self.add_journal_entry(session.id, "UPDATE", "sessions", session.id, "Updated session state")


    def count_history(self, session_id: str) -> int:
        """Total history row count, uncapped — used to decide when it's actually
        time to auto-summarize (get_history(limit=N) is capped and can't tell us this)."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM history WHERE session_id = ?", (session_id,)).fetchone()
            return row["c"] if row else 0


    def end_session(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'ended', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,)
            )
        self.add_journal_entry(session_id, "UPDATE", "sessions", session_id, "Session ended")


    def reactivate_session(self, session_id: str):
        """Counterpart to end_session, for /resume. History/characters/world state were
        never deleted by /end (only /delete does that), so reactivating just flips the
        status flag back and everything else is exactly as it was left."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,)
            )
        self.add_journal_entry(session_id, "UPDATE", "sessions", session_id, "Session reactivated (/resume)")

    # ═══════════════════════════════════════════════════════════
    # Player Management
    # ═══════════════════════════════════════════════════════════


    def add_player(self, player: Player) -> Player:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (user_id, session_id, username, display_name, is_creator)
                   VALUES (?, ?, ?, ?, ?)""",
                (player.user_id, player.session_id, player.username,
                 player.display_name, int(player.is_creator))
            )
        self.add_journal_entry(player.session_id, "INSERT", "players", str(player.user_id),
                               f"Player {player.display_name} joined")
        return player


    def remove_player(self, user_id: int, session_id: str) -> bool:
        """Removes the player/character AND scrubs them from any in-progress round's
        queue_state. Before this fix, a kicked (or self-/leave) player who was still in
        waiting_for/collected_actions stayed there forever — /kick removed them from the
        players table but nothing ever removed them from the pending round, so the round
        could never complete (waiting_for never emptied) and /skip couldn't target them
        either (they were no longer in get_players()). Returns True if removing them
        just completed the round (waiting_for became empty), so callers can trigger
        resolution instead of leaving the round stuck."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM players WHERE user_id = ? AND session_id = ?",
                (user_id, session_id)
            )
            conn.execute(
                "DELETE FROM characters WHERE player_id = ? AND session_id = ?",
                (user_id, session_id)
            )
        self.add_journal_entry(session_id, "DELETE", "players", str(user_id), "Player left")
        return self._remove_from_pending_round(session_id, user_id)


    def _remove_from_pending_round(self, session_id: str, user_id: int) -> bool:
        """Scrub a user_id out of the active round's waiting_for/collected_actions.
        Returns True if doing so just emptied waiting_for (round is now complete)."""
        state = self.get_queue_state(session_id)
        if not state:
            return False
        try:
            waiting_for = json.loads(state.waiting_for)
        except Exception:
            waiting_for = []
        try:
            collected = json.loads(state.collected_actions)
        except Exception:
            collected = {}

        changed = False
        if user_id in waiting_for:
            waiting_for.remove(user_id)
            changed = True
        if str(user_id) in collected:
            del collected[str(user_id)]
            changed = True

        if changed:
            state.waiting_for = json.dumps(waiting_for)
            state.collected_actions = json.dumps(collected)
            if not waiting_for:
                state.is_resolving = True
            self.set_queue_state(state)
            self.add_journal_entry(session_id, "UPDATE", "queue_state", str(user_id),
                                   "Removed from pending round (player left/kicked) — was stuck forever before this fix")

        return changed and not waiting_for


    def get_players(self, session_id: str) -> List[Player]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM players WHERE session_id = ? ORDER BY joined_at",
                (session_id,)
            ).fetchall()
            return [self._row_to_player(r) for r in rows]


    def get_player(self, user_id: int, session_id: str) -> Optional[Player]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM players WHERE user_id = ? AND session_id = ?",
                (user_id, session_id)
            ).fetchone()
            if row:
                return self._row_to_player(row)
            return None

    # ═══════════════════════════════════════════════════════════
    # Character Management
    # ═══════════════════════════════════════════════════════════


    def add_history(self, entry: HistoryEntry) -> HistoryEntry:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO history (session_id, author, content, entry_type)
                   VALUES (?, ?, ?, ?)""",
                (entry.session_id, entry.author, entry.content, entry.entry_type)
            )
            entry.id = cursor.lastrowid
        return entry


    def get_history(self, session_id: str, limit: int = 50) -> List[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM history WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, limit)
            ).fetchall()
            return [self._row_to_history(r) for r in reversed(rows)]


    def get_history_by_type(self, session_id: str, entry_type: str, limit: int = 20) -> List[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM history WHERE session_id = ? AND entry_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session_id, entry_type, limit)
            ).fetchall()
            return [self._row_to_history(r) for r in reversed(rows)]


    def clear_history(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM history WHERE session_id = ?",
                (session_id,)
            )
        self.add_journal_entry(session_id, "DELETE", "history", "", "Cleared history")

    # ═══════════════════════════════════════════════════════════
    # Queue State
    # ═══════════════════════════════════════════════════════════


    def get_queue_state(self, session_id: str) -> Optional[QueueState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM queue_state WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            if row:
                return QueueState(
                    session_id=row["session_id"],
                    waiting_for=row["waiting_for"],
                    collected_actions=row["collected_actions"],
                    is_resolving=bool(row["is_resolving"]),
                )
            return None


    def set_queue_state(self, state: QueueState):
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO queue_state
                   (session_id, waiting_for, collected_actions, is_resolving)
                   VALUES (?, ?, ?, ?)""",
                (state.session_id, state.waiting_for,
                 state.collected_actions, int(state.is_resolving))
            )


    def clear_queue_state(self, session_id: str):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM queue_state WHERE session_id = ?",
                (session_id,)
            )

    # ═══════════════════════════════════════════════════════════
    # Row Converters
    # ═══════════════════════════════════════════════════════════

    @staticmethod

    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            chat_id=row["chat_id"],
            name=row["name"],
            creator_id=row["creator_id"],
            status=row["status"],
            current_scene=row["current_scene"] or "",
            combat_active=bool(row["combat_active"]),
            initiative_order=row["initiative_order"] or "[]",
            current_turn_index=row["current_turn_index"],
            round_number=row["round_number"],
            pvp_active=bool(row["pvp_active"]),
            autostart=bool(row["autostart"]),
            summary=row["summary"] or "",
            summary_at_count=row["summary_at_count"] if "summary_at_count" in row.keys() else 0,
            genre=row["genre"] if "genre" in row.keys() else "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @staticmethod

    def _row_to_player(row: sqlite3.Row) -> Player:
        return Player(
            user_id=row["user_id"],
            session_id=row["session_id"],
            username=row["username"] or "",
            display_name=row["display_name"],
            is_creator=bool(row["is_creator"]),
            joined_at=row["joined_at"] or "",
        )

    @staticmethod

    def _row_to_history(row: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            session_id=row["session_id"],
            author=row["author"],
            content=row["content"],
            entry_type=row["entry_type"],
            id=row["id"],
            created_at=row["created_at"] or "",
        )

    # ═══════════════════════════════════════════════════════════
    # HP Tracking (#1)
    # ═══════════════════════════════════════════════════════════


    def get_roll_mode(self, session_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT mode FROM roll_mode WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            return row["mode"] if row else "mixed"


    def set_roll_mode(self, session_id: str, mode: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO roll_mode (session_id, mode) VALUES (?, ?)",
                (session_id, mode)
            )

    # ═══════════════════════════════════════════════════════════
    # World Memory — semantic diary (embedding-based retrieval + sleep consolidation)
    # ═══════════════════════════════════════════════════════════


