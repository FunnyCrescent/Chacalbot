"""libs.handlers.downtime_cmds — downtime activity commands.

/downtime — start/end downtime, view options
/dtstatus — show current downtime activity progress
/dtchoose — pick a downtime activity and start it
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
)

from libs.db import (
    Database, DatabaseManager, Session, Character,
)
from libs.ai.downtime_engine import (
    DowntimeEngine, DOWNTIME_ACTIVITIES, LIFESTYLE_EARNINGS,
)
from libs.handlers._state import (
    db_manager, sessions,
)
from libs.handlers.engine import _db_busy_guard
from libs.handlers.utils import get_session, send_safe

logger = logging.getLogger(__name__)

# ─── In-memory downtime state (per session) ───
# {session_id: {character_id: {activity_type, start_day, duration_days, details, progress}}}
_active_downtime: Dict[str, Dict[str, dict]] = {}


def _get_downtime_engine(db=None) -> DowntimeEngine:
    """Create a DowntimeEngine with optional DB reference."""
    return DowntimeEngine(db=db)


def _get_character_for_player(session_id: str, player_id: int) -> Optional[Character]:
    """Get a character for a player in a session."""
    db = db_manager.get_db(session_id)
    return db.get_character_by_player(player_id, session_id)


# ─────────────────────────────────────────────────────────────────
# /downtime — view downtime options or end active downtime
# ─────────────────────────────────────────────────────────────────

async def downtime_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/downtime — View available downtime activities.
    /downtime end — End current downtime and apply results.
    /downtime status — Same as /dtstatus."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    # Sub-command handling
    if ctx.args:
        sub = ctx.args[0].lower()
        if sub in ("end", "конец", "завершить"):
            await _end_downtime(update, session, user.id)
            return
        if sub in ("status", "статус"):
            await _show_downtime_status(update, session, user.id)
            return

    # Show available downtime options
    db = db_manager.get_db(session.id)
    char = _get_character_for_player(session.id, user.id)
    if not char:
        await send_safe(update, "❌ У вас нет персонажа в этой сессии.")
        return

    # Check if already in downtime
    session_dt = _active_downtime.get(session.id, {})
    char_dt = session_dt.get(char.id)
    if char_dt:
        activity_name = DOWNTIME_ACTIVITIES.get(char_dt["activity_type"], {}).get("name_ru", char_dt["activity_type"])
        progress = char_dt.get("progress", 0) * 100
        await send_safe(update,
            f"⏳ **Даунтайм активен:** {activity_name}\n"
            f"День {char_dt.get('current_day', 0)} из {char_dt.get('duration_days', '?')}\n"
            f"Прогресс: {progress:.0f}%\n\n"
            f"Используйте /downtime end для завершения."
        )
        return

    # Show options
    lines = ["📋 **Доступные занятия даунтайма:**", ""]
    for i, (activity_type, activity) in enumerate(DOWNTIME_ACTIVITIES.items(), 1):
        req_str = "; ".join(activity["requirements"][:2])
        if len(activity["requirements"]) > 2:
            req_str += "..."
        lines.append(f"{i}. **{activity['name_ru']}** ({activity_type})")
        lines.append(f"   {activity['description']}")
        lines.append(f"   Требования: {req_str}")
        lines.append(f"   Влияние на мир: {activity['world_impact']}")
        lines.append("")

    lines.append("Используйте /dtchoose <тип> <дни> [детали] для выбора.")
    lines.append("Пример: /dtchoose training 30 skill=Stealth teacher=Goblin")
    lines.append("Пример: /dtchoose working 14 lifestyle=comfortable")

    await send_safe(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────
# /dtchoose — pick a downtime activity
# ─────────────────────────────────────────────────────────────────

async def dtchoose_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dtchoose <activity_type> <days> [key=value ...]
    Start a downtime activity."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if await _db_busy_guard(update, session):
        return

    if not ctx.args or len(ctx.args) < 2:
        await send_safe(update,
            "❌ Использование: /dtchoose <тип> <дни> [ключ=значение ...]\n\n"
            "Типы: " + ", ".join(DOWNTIME_ACTIVITIES.keys()) + "\n\n"
            "Примеры:\n"
            "  /dtchoose training 30 skill=Stealth teacher=Гоблин\n"
            "  /dtchoose crafting 14 item=Longsword item_cost_gp=15\n"
            "  /dtchoose working 7 lifestyle=comfortable\n"
            "  /dtchoose carousing 10 lifestyle=wealthy\n"
            "  /dtchoose research 14 topic=Древние_руины\n"
            "  /dtchoose religious_service 10 deity=Пелор temple_faction_id=3\n"
            "  /dtchoose recuperating 3 diseases=Пёс_чума"
        )
        return

    activity_type = ctx.args[0].lower()
    try:
        duration_days = int(ctx.args[1])
    except ValueError:
        await send_safe(update, "❌ Количество дней должно быть числом.")
        return

    # Parse key=value details
    details = {}
    for arg in ctx.args[2:]:
        if "=" in arg:
            key, value = arg.split("=", 1)
            # Try to parse as number
            try:
                if "." in value:
                    details[key] = float(value)
                else:
                    details[key] = int(value)
            except ValueError:
                details[key] = value

    # Validate activity type
    if activity_type not in DOWNTIME_ACTIVITIES:
        await send_safe(update,
            f"❌ Неизвестный тип: {activity_type}\n"
            f"Доступные: {', '.join(DOWNTIME_ACTIVITIES.keys())}"
        )
        return

    # Get character
    char = _get_character_for_player(session.id, user.id)
    if not char:
        await send_safe(update, "❌ У вас нет персонажа в этой сессии.")
        return

    # Check if already in downtime
    session_dt = _active_downtime.setdefault(session.id, {})
    if char.id in session_dt:
        current = session_dt[char.id]
        current_name = DOWNTIME_ACTIVITIES.get(current["activity_type"], {}).get("name_ru", current["activity_type"])
        await send_safe(update,
            f"❌ Вы уже в даунтайме: {current_name}. "
            f"Сначала завершите текущее (/downtime end)."
        )
        return

    # Process the downtime activity
    db = db_manager.get_db(session.id)
    engine = _get_downtime_engine(db=db)
    result = engine.process_downtime_activity(
        session_id=session.id,
        character_id=char.id,
        activity_type=activity_type,
        duration_days=duration_days,
        details=details,
    )

    if not result.success:
        await send_safe(update, f"❌ {result.narrative}")
        return

    # Store in active downtime state
    session_dt[char.id] = {
        "activity_type": activity_type,
        "duration_days": duration_days,
        "current_day": duration_days,  # Processed all at once
        "details": details,
        "progress": result.progress,
        "result": {
            "gold_earned": result.gold_earned,
            "gold_spent": result.gold_spent,
            "character_changes": result.character_changes,
        },
    }

    # Send result
    activity_name = DOWNTIME_ACTIVITIES[activity_type]["name_ru"]
    response_lines = [
        f"✅ **{activity_name}** — {duration_days} дней",
        "",
        result.narrative,
    ]
    if result.world_changes:
        response_lines.append("")
        response_lines.append("🌍 **Изменения мира:**")
        for wc in result.world_changes:
            response_lines.append(f"  • {wc['description']}")

    await send_safe(update, "\n".join(response_lines))


# ─────────────────────────────────────────────────────────────────
# /dtstatus — show current downtime status
# ─────────────────────────────────────────────────────────────────

async def dtstatus_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/dtstatus — Show your current downtime activity status."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    await _show_downtime_status(update, session, user.id)


async def _show_downtime_status(update: Update, session, player_id: int):
    """Internal: show downtime status for a player."""
    char = _get_character_for_player(session.id, player_id)
    if not char:
        await send_safe(update, "❌ У вас нет персонажа в этой сессии.")
        return

    session_dt = _active_downtime.get(session.id, {})
    char_dt = session_dt.get(char.id)

    if not char_dt:
        await send_safe(update,
            "📋 У вас нет активного даунтайма.\n"
            "Используйте /downtime для просмотра доступных занятий."
        )
        return

    activity_type = char_dt["activity_type"]
    activity = DOWNTIME_ACTIVITIES.get(activity_type, {})
    activity_name = activity.get("name_ru", activity_type)
    progress = char_dt.get("progress", 0) * 100
    duration = char_dt.get("duration_days", 0)
    current_day = char_dt.get("current_day", 0)
    details = char_dt.get("details", {})

    lines = [
        f"⏳ **Даунтайм: {activity_name}**",
        f"Персонаж: {char.name}",
        f"День: {current_day} из {duration}",
        f"Прогресс: {progress:.0f}%",
    ]

    if details:
        lines.append("")
        lines.append("**Параметры:**")
        for key, value in details.items():
            lines.append(f"  • {key}: {value}")

    # Show stored results
    stored_result = char_dt.get("result", {})
    if stored_result:
        lines.append("")
        if stored_result.get("gold_earned", 0) > 0:
            lines.append(f"💰 Заработано: {stored_result['gold_earned']:.1f} gp")
        if stored_result.get("gold_spent", 0) > 0:
            lines.append(f"💸 Потрачено: {stored_result['gold_spent']:.1f} gp")

        char_changes = stored_result.get("character_changes", {})
        if char_changes:
            lines.append("")
            lines.append("**Изменения персонажа:**")
            for key, value in char_changes.items():
                if isinstance(value, bool):
                    lines.append(f"  • {key}: {'✅' if value else '❌'}")
                elif isinstance(value, float):
                    lines.append(f"  • {key}: {value:.2f}")
                else:
                    lines.append(f"  • {key}: {value}")

    lines.append("")
    lines.append("Используйте /downtime end для завершения и применения результатов.")

    await send_safe(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────
# End downtime
# ─────────────────────────────────────────────────────────────────

async def _end_downtime(update: Update, session, player_id: int):
    """End active downtime and apply final results."""
    char = _get_character_for_player(session.id, player_id)
    if not char:
        await send_safe(update, "❌ У вас нет персонажа в этой сессии.")
        return

    session_dt = _active_downtime.get(session.id, {})
    char_dt = session_dt.get(char.id)

    if not char_dt:
        await send_safe(update, "❌ У вас нет активного даунтайма.")
        return

    activity_type = char_dt["activity_type"]
    activity = DOWNTIME_ACTIVITIES.get(activity_type, {})
    activity_name = activity.get("name_ru", activity_type)
    duration = char_dt.get("duration_days", 0)

    # Remove from active downtime
    del session_dt[char.id]
    if not session_dt:
        _active_downtime.pop(session.id, None)

    # Generate world delta narrative
    db = db_manager.get_db(session.id)
    engine = _get_downtime_engine(db=db)
    world_delta = engine.generate_world_delta_narrative(session.id, duration)

    # Build response
    lines = [
        f"✅ **Даунтайм завершён: {activity_name}** ({duration} дней)",
        "",
        "Персонаж возвращается к активной игре.",
    ]

    # Show character changes from stored result
    stored_result = char_dt.get("result", {})
    char_changes = stored_result.get("character_changes", {})

    if char_changes:
        lines.append("")
        lines.append("**Результаты:**")
        for key, value in char_changes.items():
            if key == "world_events_created":
                continue
            if isinstance(value, bool):
                lines.append(f"  • {key}: {'✅' if value else '❌'}")
            elif isinstance(value, float):
                lines.append(f"  • {key}: {value:.2f}")
            elif isinstance(value, list):
                if value:
                    lines.append(f"  • {key}: {', '.join(str(v) for v in value)}")
            else:
                lines.append(f"  • {key}: {value}")

    # Show gold changes
    gold_earned = stored_result.get("gold_earned", 0)
    gold_spent = stored_result.get("gold_spent", 0)
    if gold_earned > 0 or gold_spent > 0:
        lines.append("")
        lines.append("**Золото:**")
        if gold_earned > 0:
            lines.append(f"  💰 Заработано: +{gold_earned:.1f} gp")
        if gold_spent > 0:
            lines.append(f"  💸 Потрачено: -{gold_spent:.1f} gp")
        net = gold_earned - gold_spent
        lines.append(f"  Итого: {net:+.1f} gp")

    # World delta
    if world_delta:
        lines.append("")
        lines.append("🌍 **Что изменилось в мире:**")
        lines.append(world_delta)

    await send_safe(update, "\n".join(lines))
