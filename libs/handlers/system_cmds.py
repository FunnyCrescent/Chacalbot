"""
libs.handlers.system_cmds — auto-split from libs/bot_handlers.py.

DO NOT EDIT MANUALLY — regenerate via scripts/split_bot_handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import re as _re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from libs.ai_client import DMEngine, MASTER_PROMPT, MASTER_TOOLS, OpenAIClient
from libs.character_parser import CharacterParser, ParsedCharacter
from libs.creu import get_handler as get_creu_handler
from libs.creu.storage import init_db as init_creu_db, cleanup_stale as creu_cleanup_stale
from libs.config_legacy import (
    TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, OPENAI_BASE_URL, BASE_DIR, DB_PATH, LOG_PATH, CHARACTERS_DIR,
    ADMIN_CHAT_ID, PLAYER_ROLL_TIMEOUT_SECONDS, COMBAT_PC_TURN_TIMEOUT_SECONDS,
    TRANSLATOR_ENABLED, TRANSLATOR_MODEL, TRANSLATOR_TEMP, TRANSLATOR_MAX_TOKENS, TRANSLATOR_BATCH_DELAY,
    NPC_AI_ENABLED, NPC_AI_MODE, NPC_AI_MODEL, NPC_AI_TEMP, NPC_AI_MAX_TOKENS,
    ANTISPAM_ENABLED, ANTISPAM_DELETE_DELAY, ANTISPAM_DELETE_BATCH_SIZE, ANTISPAM_DELETE_BATCH_DELAY,
    ANTISPAM_DELETE_TYPES, ANTISPAM_DM_ONLY_COMMANDS,
    ADMIN_IS_GAME_MASTER, READONLY_COMMANDS,
    COMBAT_INITIATIVE_ENABLED, COMBAT_TOOL_NAME, COMBAT_END_TOOL_NAME,
    DUAL_NARRATIVE_ENABLED,
    SETTINGS_DIR, SETTINGS_DEFAULT_GENRE,
    SAVED_CHARS_DB,
)
from libs.db import (
    Database, DatabaseManager, HistoryEntry, Session, Character,
    CombatEncounter, Combatant, PlayerLanguage, SettingEntry, RoundMessageTracker,
    SavedCharsDB,
)
from libs.session_manager import SessionManager
from libs.handlers._state import (
    db_manager, dm_engine, sessions, saved_chars_db, md_logger,
    _lang_translations, _lang_round_counter,
    _active_combat_loops, _combat_recovery_sessions,
    _pending_button_rolls, _genre_selections,
    MarkdownLogger, _ChatHandle, init_state,
)

logger = logging.getLogger(__name__)

# Кросс-доменные импорты
from libs.handlers.utils import get_session
from libs.handlers.utils import send_safe
from libs.handlers.utils import send_to_admin


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_safe(update, 
        "⚔️ **D&D Dark Fantasy Bot** ⚔️\n"
        "Я — **Нейросеть-Мастер**. Тёмный, опасный и беспощадный мир.\n\n"
        "📖 Команды бота имеют уникальные названия — шпаргалка: /help\n\n"

        "🚀 **Быстрый старт**\n"
        "1️⃣ `/newydd Название` — создать сессию (new)\n"
        "2️⃣ `/ymuno` — присоединиться (join)\n"
        "3️⃣ `/creu` — создать нового персонажа пошагово в ЛС бота (кузнец)\n"
        "   ИЛИ `/cymeriad` (прикрепи .txt/.md или ответь на файл) — загрузить готовый лист\n"
        "   В ЛС бота: `/cymeriad` без файла — выбрать сохранённого персонажа\n"
        "4️⃣ `Дн. действие` или `/dn действие` — сходить\n\n"

        "🎲 **Как проходят ходы**\n"
        "• Каждый раунд — одно действие с префиксом `Дн.`\n"
        "• Нейросеть ждёт, пока сходят ВСЕ, потом разрешает разом\n"
        "• Наратив приходит сразу; мир (БД) обновляется следом, в фоне\n"
        "• `/diddymu` — отменить своё действие ДО разрешения раунда (cancel)\n"
        "• `/gofyn ВОПРОС` — спросить Нейросетьа вне очереди (ask)\n"
        "• `/rholio атлетика` — проверенный бросок навыка/спасброска (roll)\n\n"

        "⚔️ **Бой:** бой стартует нейросеть из нарратива · /sgipio — скип · создатель: /combat [имена] — начать, /gorffenymladd — завершить (combat/endcombat/skip)\n"
        "❤️ **HP и жизнь:** /iechyd /arbedmarwolaeth /cyflwr /gorffwys (hp/deathsave/condition/rest)\n"
        "💰 **Экономика:** /aur /eiddo (gold/inventory)\n"
        "📜 **Квесты:** /cwest — задачи, подтверждённые сюжетом/NPC (quest)\n"
        "🎯 **Цели:** /nodau · /nodau all — личные мотивы персонажа, не требуют подтверждения (goals)\n"
        "🌍 **Мир:** /amser /tywydd /ffactiynau /lleoliad /byd (time/weather/factions/location/world)\n"
        "🏰 **Локации:** /dinas — что мир знает о тебе · /dinas all — все локации (city)\n"
        "🎭 **NPC:** /cymeriadnc — кого встретил твой персонаж · /cymeriadnc all — все NPC мира (npc)\n"
        "🕸️ **/perthynasau** · /perthynasau all — общая карта: NPC + локации + фракции (relations)\n"
        "📊 **Партия:** /gallu (свой перс) · /gallu all (вся партия) (ability)\n"
        "🕵️ **Приватно (в ЛС бота):** /gwneud действие (do)\n\n"

        "🔧 **Админ — только техник:**\n"
        "/sgipio @ник — скип чужого хода · свой ход: /sgipio · /cicio — кикнуть\n"
        "/gorfoddatrys — сбросить зависший раунд · /gorffenymladd — завершить бой\n"
        "/clirio — очистить историю · /diwedd — завершить сессию\n"
        "/dndcychwyn — начать игру\n\n"

        "🔒 Админ НЕ может влиять на игру (HP, золото, квесты и т.д.).\n"
        "Игроки влияют на мир только через RP-действия (`Дн.`).\n"
        "Нейросеть управляет миром и разрешает все игровые изменения.\n\n"

        "📋 Используя бота, вы соглашаетесь с /telerau (terms)\n"
        "🗑️ Удалить все данные: /dileu (delete)\n\n"
        "Тени сгущаются...",
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Фича 6: полная справка — ровно ДВА сообщения, красиво разделённые.
    Часть 1: старт, персонаж, ходы, бой. Часть 2: состояние, мир, приватное,
    сервис, админ. Все команды существуют реально (справка больше не врёт про
    /ymladd и /dn). Фича 2: у справки два имени — /cymorth (валл.) и /help (англ.)."""
    part1 = (
        "⚔️ <b>D&amp;D Dark Fantasy Bot — Справка 1/2</b> ⚔️\n"
        "Старт · Персонаж · Ходы · Бой\n\n"

        "━━ 🚀 <b>Начало игры</b> ━━\n"
        "/newydd Название — создать сессию\n"
        "/ymuno — присоединиться к сессии\n"
        "/chwaraewyr — список игроков\n"
        "/cymeriad [файл] — загрузить лист персонажа (прикрепи .txt/.md или ответь на файл)\n"
        "&nbsp;&nbsp;• В ЛС: /cymeriad без файла — выбрать/удалить сохранённого персонажа\n"
        "/creu — создать персонажа пошагово в ЛС бота (кузнец)\n"
        "/taflen — полный лист персонажа\n"
        "/gallu — характеристики · /gallu all — вся партия\n"
        "/dndcychwyn [сеттинг] — СТАРТ: генерация мира (только создатель)\n"
        "/categori — выбрать готовый сеттинг/жанр\n"
        "/categori add — свой сеттинг (ответом на .md)\n"
        "/setcurrency — своя валюта (опционально)\n\n"

        "━━ 🎲 <b>Ходы</b> ━━\n"
        "Каждый раунд — одно действие с префиксом <b>Дн.</b> (или Dn.)\n"
        "Нейросеть ждёт ходов ВСЕХ и разрешает раунд одним нарративом.\n"
        "/rholio навык — ПРОВЕРЕННЫЙ бросок сервера (атлетика, спасбросок...)\n"
        "/rholio encounter [местность] — случайный энкаунтер (админ)\n"
        "/gofyn вопрос — спросить нейросеть вне очереди (знает твой лист!)\n"
        "/diddymu — отменить СВОЁ действие до разрешения раунда\n"
        "/sgipio — ⏭ пропустить СВОЙ ход (доступно каждому)\n"
        "⏳ Пока мир генерируется (/dndcychwyn), ходы заблокированы — играбельно только после старта.\n\n"

        "━━ ⚔️ <b>Бой</b> ━━\n"
        "Бой начинает нейросеть через инструмент dechrauymladd; инициатива — по очереди.\n"
        "/sgipio — пропустить СВОЙ боевой ход (инициатива уйдёт дальше)\n"
        "/sgipio @ник — пропустить ход другого игрока (только создатель)\n"
        "/gorffenymladd — завершить бой (только создатель)\n"
        "/cccg — PvP вкл/выкл (только создатель)\n\n"

        "━━ 🕵️ <b>Приватность</b> ━━\n"
        "/gwneud действие — скрытное действие (в ЛС бота; провал увидят все)\n"
        "/cyfieithu язык — перевод нарратива в ЛС · /cyfieithu off — выкл\n\n"

        "📄 <b>Продолжение — во втором сообщении (2/2): состояние, мир, сервис, админ.</b>"
    )

    part2 = (
        "⚔️ <b>D&amp;D Dark Fantasy Bot — Справка 2/2</b> ⚔️\n"
        "Персонаж · Мир · Сервис · Админ\n\n"

        "━━ ❤️ <b>Персонаж: состояние</b> ━━\n"
        "/iechyd — HP и спасброски от смерти\n"
        "/arbedmarwolaeth имя — бросок от смерти\n"
        "/cyflwr list — состояния · /cyflwr — детали\n"
        "/gorffwys short|long имя — отдых\n"
        "/adnoddau — классовые ресурсы · /adnoddau use|set|recover\n"
        "/canolbwyntio — концентрация (только просмотр; управляет нейросеть)\n"
        "/modd — режим бросков (только просмотр; управляет нейросеть)\n\n"

        "━━ 💰 <b>Экономика, квесты, цели</b> ━━\n"
        "/aur — кошель · /eiddo — инвентарь\n"
        "/cwest — квесты (подтверждённые сюжетом)\n"
        "/cwest update ID completed|failed — обновить статус\n"
        "/nodau — личные мотивы · /nodau all — вся партия\n"
        "/perthynasau — карта связей · /perthynasau all — подробно\n\n"

        "━━ 🌍 <b>Мир</b> ━━\n"
        "/amser — время · /tywydd — погода\n"
        "/lleoliad — твоя локация · /byd — статус мира\n"
        "/ffactiynau — фракции и репутация\n"
        "/dinas — что мир знает о тебе · /dinas all — все локации\n"
        "/cymeriadnc — встреченные NPC · /cymeriadnc all — все NPC\n"
        "/cymeriadnc note Имя текст — заметка об NPC\n"
        "/digwyddiad — события мира · /digwyddiad resolve ID\n"
        "/cyfeirlyfr запрос — справочник правил D&amp;D (SRD)\n"
        "#раунд — история игры помечена раундами: спрашивай «что я делал 5 раундов назад?»\n\n"

        "━━ 🔧 <b>Сервис</b> ━━\n"
        "/help — ЭТА справка (английское имя; /cymorth — валлийское)\n"
        "/dechrau — краткая шпаргалка\n"
        "/rhwymo команда псевдоним — свой алиас команды (в ЛС; работает только у тебя)\n"
        "&nbsp;&nbsp;• /rhwymo rhestr — список твоих биндов\n"
        "/datgysylltu псевдоним — удалить бинд · /datgysylltu popeth — все\n"
        "/cydbwysedd — баланс на счету LLM-агрегатора (в группах — только создатель)\n"
        "🔗 Любая команда принимает ЛЮБОЙ регистр: /CYMERIAD = /cymeriad\n\n"

        "━━ 👑 <b>Управление сессией (создатель)</b> ━━\n"
        "/statws — статус · /chwaraewyr — игроки\n"
        "/trosglwyddo @ник — передать права создателя\n"
        "/cicio @ник — исключить игрока\n"
        "/diwedd — завершить сессию (история экспортируется)\n"
        "/ailddechrau — возобновить завершённую сессию\n"
        "/clirio — очистить контекст нейросети\n"
        "/gorfoddatrys — сбросить зависший раунд\n"
        "/crynodeb — сводка кампании\n"
        "/dbgofyn запрос — апелляция к базе данных (HP/золото/состояния)\n"
        "/dileu — ПОЛНОЕ удаление данных сессии\n"
        "/telerau — условия использования\n\n"

        "━━ ⚖️ <b>Важно</b> ━━\n"
        "🔒 Админ — только техник: скип, кик, сброс раунда. На игру (HP, золото, сюжет) НЕ влияет.\n"
        "🎮 Мир меняют только RP-действия (Дн.) через нейросеть-Мастера.\n"
        "🌍 До генерации мира (/dndcychwyn) и во время неё ходить нельзя.\n\n"
        "📋 Используя бота, вы соглашаетесь с /telerau\n"
        "Тени сгущаются..."
    )

    await send_safe(update, part1, raw_html=True)
    await send_safe(update, part2, raw_html=True)


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_chat.id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    db = db_manager.get_db(session.id)
    players = sessions.get_players(session.id)
    chars = db.get_session_characters(session.id)
    lines = [
        f"📜 **{session.name}** | ID: `{session.id}`",
        f"Игроков: {len(players)} | Персонажей: {len(chars)}",
    ]
    if session.combat_active:
        lines.append(f"⚔️ Бой: Раунд {session.round_number}")
        cur = sessions.get_current_turn(session.id)
        if cur:
            lines.append(f"Ход: **{cur['name']}**")

    queue = db.get_queue_state(session.id)
    if queue:
        waiting = json.loads(queue.waiting_for)
        if waiting:
            lines.append(f"\n🎲 Ждём: {', '.join(sessions.get_pending_players(session.id))}")
        elif queue.is_resolving:
            lines.append("\n✅ Разрешаю...")

    await send_safe(update, "\n".join(lines))


async def end_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session or not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return

    db = db_manager.get_db(session.id)
    history = db.get_history(session.id, limit=1000)
    export_path = None
    if history:
        lines = [f"Session: {session.name}", f"ID: {session.id}", "", "=" * 50, ""]
        for h in history:
            lines.append(f"[{h.author}]: {h.content}")
            lines.append("")

        export_path = os.path.join(BASE_DIR, "logs", f"session_{session.id}_{session.name.replace(' ', '_')}.txt")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"[EXPORT] Session saved to {export_path}")

    sessions.end_session(session.id)
    await send_safe(update, f"🏁 **{session.name}** завершена. История экспортирована в logs/.", source="system")

    if ADMIN_CHAT_ID and ctx and export_path:
        await send_to_admin(ctx, f"📋 Session ended: {session.name} ({session.id})", export_path)


async def resume_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reactivate the most recently /end-ed session in this chat. /end never deletes
    anything (only /delete does) — it just marks the session 'ended', which meant there
    was previously no way back into it short of creating a brand new session."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    if get_session(chat_id):
        await send_safe(update, "❌ В чате уже есть активная сессия.")
        return

    ended = db_manager.get_last_ended_session_by_chat(chat_id)
    if not ended:
        await send_safe(update, "Нет завершённых сессий в этом чате, которые можно возобновить.")
        return

    if ended.creator_id != user.id:
        await send_safe(update, "❌ Возобновить сессию может только её создатель.")
        return

    session = db_manager.resume_session(ended.id)
    if not session:
        await send_safe(update, "❌ Не удалось возобновить сессию.")
        return

    await send_safe(update, 
        f"▶️ **Сессия возобновлена!**\n*{session.name}*\nID: `{session.id}`\n\n"
        f"История, персонажи и мир сохранены как были."
    )


async def clear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Clear AI context (history) — keeps characters and session"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель может очистить контекст.")
        return

    db_manager.clear_history(session.id)
    await send_safe(update, 
        "🧹 Контекст нейросети очищен!\n\n"
        "История игры удалена. Чистый лист.\n"
        "Листы персонажей и игроки сохранены.",
    )


async def forceresolve_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """DM safety valve for a round stuck after a crash mid-resolve (e.g. the
    LocationBinding hash bug that used to fire on every round with a set location, or
    any future exception during resolve_round). Unlike /clear, this does NOT wipe
    campaign history/memory/characters — it only discards the current round's pending
    queue and starts a fresh one."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только Админ может сбросить раунд.")
        return

    info = sessions.force_reset_round(session.id)
    sessions.set_db_busy(session.id, False)
    sessions.pop_pending_resolve(session.id)
    pending = sessions.get_pending_players(session.id)

    lines = ["🔧 **Раунд сброшен.** История и персонажи не тронуты."]
    if info["discarded_actions"]:
        lines.append(f"⚠️ Утеряны несохранённые действия этого раунда: {', '.join(info['discarded_actions'])}")
    if info["discarded_waiting"]:
        lines.append(f"⏳ Ранее ждали: {', '.join(info['discarded_waiting'])}")
    lines.append(f"\n📝 Новый раунд начат. Ждём: {', '.join(pending)}")
    await send_safe(update, "\n".join(lines))


async def summary_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate session summary using Granite"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    if not sessions.is_creator(user.id, session.id):
        await send_safe(update, "❌ Только создатель.")
        return

    await send_safe(update, "📋 Генерирую сводку кампании...")

    try:
        summary = await sessions.summarize_and_save(session.id)
        if summary:
            await send_safe(update, f"📋 Сводка кампании:\n\n{summary}")
        else:
            await send_safe(update, "📋 История слишком короткая для сводки.")
    except Exception as e:
        logger.error(f"Summary error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")


async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Retract your own submitted action before the round resolves — for the exact
    'oh crap, how do I undo this' moment (a player once tried to type Дн. by mistake
    and had no way back short of waiting the whole round out)."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return

    ok, msg = sessions.cancel_action(session.id, user.id)
    await send_safe(update, ("↩️ " if ok else "❌ ") + msg)


async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update error: {ctx.error}")
    try:
        if ADMIN_CHAT_ID and ctx and update and update.effective_chat:
            await send_to_admin(ctx, f"⚠️ ERROR in chat {update.effective_chat.id}: {str(ctx.error)[:500]}")
    except Exception:
        pass


async def terms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show terms of service"""
    await send_safe(update,
        "📋 **Пользовательское соглашение**\n\n"
        "1. Бот собирает полные логи сессий для исправления ошибок и улучшения нейросетьа.\n"
        "2. Данные хранятся на сервере разработчика и не передаются третьим лицам.\n"
        "3. Администратор имеет технический доступ к логам сессий.\n"
        "4. По команде `/dileu` вы можете запросить удаление всех данных сессии.\n"
        "5. Используя бота, вы даёте согласие на сбор и обработку данных.\n\n"
        "Если не согласны — не используйте бота или разверните свою копию."
    )
