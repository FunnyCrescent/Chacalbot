"""Unit tests for libs/creu/ — the /creu character-creation package.

dice.py  — pure rolling math (seeded);
formatter.py — JSON → .md character sheet rendering;
storage.py — sandbox SQLite persistence (tmp-path DB, Bug 4 fix surface).

handler.py (ConversationHandler) is Telegram-FSM bound — not a unit test
target; its wiring is covered by the plugin load check in integration runs.
"""
import sqlite3

import pytest

from libs.creu import dice, formatter, storage


# ═══════════════════════════════════════════════════════════════
# dice
# ═══════════════════════════════════════════════════════════════

class TestDice:

    @pytest.mark.parametrize("score,mod", [
        (8, -1), (9, -1), (10, 0), (11, 0),
        (12, 1), (16, 3), (20, 5), (3, -4), (1, -5),
    ])
    def test_modifier_floor_division(self, score, mod):
        assert dice.modifier(score) == mod

    def test_roll_d6_range(self):
        import random
        random.seed(42)
        assert all(1 <= dice.roll_d6() <= 6 for _ in range(200))

    def test_roll_4d6_drop_lowest_range(self):
        import random
        random.seed(42)
        assert all(3 <= dice.roll_4d6_drop_lowest() <= 18 for _ in range(200))

    def test_roll_all_stats_shape(self):
        import random
        random.seed(7)
        stats = dice.roll_all_stats()
        assert len(stats) == 6
        assert all(3 <= s <= 18 for s in stats)

    def test_drop_lowest_drops_exactly_one(self):
        """Deterministic formula check of the sorted[1:] logic:
        6,6,6,6 → 18; 1,2,3,4 → 2+3+4=9."""
        assert sum(sorted([6, 6, 6, 6])[1:]) == 18
        assert sum(sorted([1, 2, 3, 4])[1:]) == 9


# ═══════════════════════════════════════════════════════════════
# formatter
# ═══════════════════════════════════════════════════════════════

def full_char() -> dict:
    return {
        "name": "Бран", "race": "Гном", "subrace": "Скальный",
        "class": "Бард", "subclass": "Знаний", "level": 3,
        "background": "Артист", "alignment": "Хаотично-добрый",
        "strength": 10, "dexterity": 16, "constitution": 14,
        "intelligence": 12, "wisdom": 10, "charisma": 18,
        "hp_max": 22, "ac": 14, "speed": 25,
        "proficiency_bonus": 2,
        "skills": ["Выступление", "Обман"],
        "equipment": ["лютня"], "weapons": ["рапира"],
        "gold": 15,
        "personality_traits": "болтлив",
    }


class TestFormatter:

    def test_header_and_basics(self):
        md = formatter.format_character_md(full_char())
        assert "# Бран" in md
        assert "**Раса:** Гном (Скальный)" in md
        assert "**Класс:** Бард (Знаний), 3 уровень" in md
        assert "**Предыстория:** Артист" in md

    def test_subrace_and_subclass_parens_optional(self):
        data = full_char()
        del data["subrace"], data["subclass"]
        md = formatter.format_character_md(data)
        assert "**Раса:** Гном" in md
        assert "**Класс:** Бард, 3 уровень" in md

    def test_stat_table_with_mods(self):
        md = formatter.format_character_md(full_char())
        # DEX 16 → +3, WIS 10 → +0
        assert "16 (+3)" in md
        assert "10 (+0)" in md

    def test_derived_values(self):
        md = formatter.format_character_md(full_char())
        assert "**HP:** 22" in md
        assert "**AC:** 14" in md
        assert "**Инициатива:** +3" in md          # from DEX 16
        assert "**Бонус мастерства:** +2" in md

    def test_optional_sections_omitted_when_empty(self):
        md = formatter.format_character_md({"name": "Тихий", "class": "Воин"})
        assert "## Навыки" not in md
        assert "## Заклинания" not in md
        assert "## Снаряжение" not in md

    def test_lists_render_items(self):
        md = formatter.format_character_md(full_char())
        assert "- Выступление" in md
        assert "**Оружие:**" in md
        assert "- рапира" in md
        assert "**Золото:** 15 зм" in md

    def test_personality_section(self):
        md = formatter.format_character_md(full_char())
        assert "**Черты характера:** болтлив" in md

    def test_missing_name_fallback(self):
        md = formatter.format_character_md({})
        assert "# Безымянный" in md


# ═══════════════════════════════════════════════════════════════
# storage (sandbox DB — Bug 4 fix surface)
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def sdb(tmp_path, monkeypatch):
    """Redirect the sandbox DB to a temp file and init it fresh."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "data" / "creu_sandbox.db")
    storage.init_db()
    return storage


class TestStorage:

    def test_init_creates_db_in_target_dir(self, sdb, tmp_path):
        assert sdb.DB_PATH.parent.exists()
        assert sdb.DB_PATH.exists()

    def test_save_returns_incrementing_ids(self, sdb):
        id1 = sdb.save_to_sandbox(1, "Бран", "{}")
        id2 = sdb.save_to_sandbox(2, "Айрин", "{}")
        assert id2 == id1 + 1

    def test_rows_are_per_user_isolated(self, sdb):
        """The sandbox is keyed by user_id — deleting one user's row must
        never touch another user's character (Bug 1 family paranoia)."""
        a = sdb.save_to_sandbox(111, "Бран", '{"name": "Бран"}')
        b = sdb.save_to_sandbox(222, "Айрин", '{"name": "Айрин"}')
        assert sdb.delete_from_sandbox(a) is True
        conn = sqlite3.connect(str(sdb.DB_PATH))
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT user_id, name FROM creu_sandbox")}
        conn.close()
        assert rows == {222: "Айрин"}

    def test_delete_missing_row_returns_false(self, sdb):
        assert sdb.delete_from_sandbox(99999) is False

    def test_cleanup_removes_only_stale_undelivered(self, sdb):
        import time
        fresh = sdb.save_to_sandbox(1, "Свежий", "{}")
        old_undelivered = sdb.save_to_sandbox(2, "Завис", "{}")
        old_delivered = sdb.save_to_sandbox(3, "Доставлен", "{}")
        # Make row 2 look 20 minutes old and undelivered; row 3 delivered.
        conn = sqlite3.connect(str(sdb.DB_PATH))
        conn.execute("UPDATE creu_sandbox SET created_at = ? WHERE id = ?",
                     (time.time() - 1200, old_undelivered))
        conn.execute("UPDATE creu_sandbox SET created_at = ?, delivered = 1 WHERE id = ?",
                     (time.time() - 1200, old_delivered))
        conn.commit()
        conn.close()

        removed = sdb.cleanup_stale()

        assert removed == 1
        conn = sqlite3.connect(str(sdb.DB_PATH))
        remaining = {r[0] for r in conn.execute("SELECT id FROM creu_sandbox")}
        conn.close()
        assert remaining == {fresh, old_delivered}

    def test_cleanup_keeps_recent_undelivered(self, sdb):
        sdb.save_to_sandbox(5, "Только что", "{}")
        assert sdb.cleanup_stale() == 0
