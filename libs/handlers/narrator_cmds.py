"""
libs.handlers.narrator_cmds — auto-split from libs/bot_handlers.py.

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

from libs.ai_client import DMEngine, MASTER_PROMPT, MASTER_TOOLS, MODER_AI_PROMPT, OpenAIClient, get_prompt
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
    MODER_AI_ENABLED,
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
from libs.handlers.engine import _auto_resolve_npcs_then_pc
from libs.handlers.translations import _character_knows_language
from libs.handlers.engine import _db_busy_guard
from libs.handlers.translations import _get_lang_translations
from libs.handlers.utils import _keep_typing
from libs.handlers.engine import _resolve_and_send
from libs.handlers.engine import _resolve_non_combat_round
from libs.handlers.engine import _resolve_pc_combat_turn
from libs.handlers.engine import _track_round_message
from libs.handlers.engine import _track_pending_combat_message
from libs.handlers.engine import _track_round_messages_with_fallback
from libs.handlers.utils import get_session
from libs.handlers.utils import md_to_html
from libs.handlers.utils import send_safe
from libs.handlers.utils import world_gen_guard, has_any_round_history
from libs.handlers.utils import capture_thread_id
from libs.handlers.utils import get_billing_plugin
from libs.ai import usage_ledger


# ─────────────────────────────────────────────────────────────────
# Code
# ─────────────────────────────────────────────────────────────────

async def ask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Out-of-turn question — does NOT go through queue, does NOT advance game"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    # ИТЕРАЦИЯ 10 (Раздел 6): usage вопроса тоже атрибутируется сессии.
    usage_ledger.bind_session(session.id)
    if not ctx.args:
        await send_safe(update, "Использование: `/gofyn Как выглядит этот NPC?`")
        return

    question = " ".join(ctx.args)
    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    # BUG 3 FIX: до/во время генерации мира вопросы не принимаются.
    if await world_gen_guard(update, session):
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update, stop_typing)) if update.effective_chat else None

    try:
        # ModerAI filter: check if the question is appropriate before answering
        if MODER_AI_ENABLED:
            try:
                filter_response = await dm_engine.moder_ai.chat(
                    [{"role": "user", "content": question}],
                    system_prompt=get_prompt("moder_ai", session.id),
                )
                filter_text = filter_response["choices"][0]["message"].get("content", "").strip()
                if filter_text.startswith("DENY"):
                    reason = filter_text[4:].strip()[:100]
                    await send_safe(update, f"🚫 Вопрос отклонён: {reason or 'не игровой вопрос'}")
                    return
            except Exception as e:
                logger.warning(f"ModerAI filter error (allowing question through): {e}")

        history = db.get_history(session.id, limit=30)
        history_msgs = [{"role": "user" if h.author != "DM" else "assistant", "content": h.content} for h in history]

        # Build character context: backstory + active quests + known NPCs. Before this,
        # /ask had no way to answer a question like "what's my character's backstory"
        # because that information was never in the last-30-lines narrative window.
        char = db.get_character_by_player(user.id, session.id)
        context_parts = []
        if char:
            # BUG 5 FIX: раньше в контекст /gofyn попадали только имя/раса/класс и
            # предыстория — нейросеть НЕ ЗНАЛА HP, КД, характеристики, инвентарь,
            # золото и состояния персонажа и отвечала «я не знаю, что у тебя в
            # инвентаре». Теперь отдаём ПОЛНЫЙ актуальный лист из БД.
            context_parts.append(f"Персонаж: {char.name}, {char.race} {char.class_name}, уровень {char.level}.")
            if char.backstory:
                context_parts.append(f"Предыстория: {char.backstory[:800]}")

            # Полная актуальная сводка прогрессии из БД (HP/AC/статы/владения/умения/заклинания)
            try:
                progression = db.get_character_progression_summary(char.id)
                if progression:
                    context_parts.append("Полный лист персонажа (актуальное состояние):\n" + progression)
            except Exception as e:
                logger.warning(f"[gofyn] progression summary failed: {e}")
                # Фолбэк — минимум из модели Character
                try:
                    stats = json.loads(char.stats) if char.stats else {}
                    stat_line = ", ".join(f"{k.upper()[:3]}={v}" for k, v in stats.items())
                    context_parts.append(f"Характеристики: {stat_line}. HP: {char.hp}/{char.max_hp}, AC: {char.ac}.")
                except Exception:
                    pass

            # Инвентарь и деньги
            try:
                inv = sessions.get_inventory(session.id, char.id)
                if inv:
                    context_parts.append("Инвентарь: " + ", ".join(
                        f"{i['item']} x{i['qty']}" if i.get("qty", 1) > 1 else i["item"] for i in inv))
                gold = sessions.get_gold(session.id, char.id)
                gold_str = ", ".join(f"{v}{k}" for k, v in gold.items() if v)
                if gold_str:
                    context_parts.append(f"Кошель: {gold_str}.")
            except Exception as e:
                logger.warning(f"[gofyn] inventory/gold fetch failed: {e}")

            # Состояния и локация
            try:
                conds = sessions.get_character_conditions(session.id, char.id)
                if conds:
                    context_parts.append("Активные состояния: " + ", ".join(cc.condition for cc in conds))
                loc = sessions.get_location(session.id, char.id)
                if loc and loc.location_name:
                    context_parts.append(f"Локация: {loc.location_name}.")
            except Exception as e:
                logger.warning(f"[gofyn] conditions/location fetch failed: {e}")

            # Текущий раунд — чтобы вопросы «что я делал N раундов назад» имели опору
            try:
                current_round = sessions.get_current_round(session.id)
                if current_round:
                    context_parts.append(f"Сейчас идёт раунд {current_round} игры (история помечена метками [Раунд N]).")
            except Exception:
                pass

            quests = sessions.get_quests(session.id, assignee_id=char.id)
            if quests:
                context_parts.append("Активные квесты персонажа: " + "; ".join(
                    f"{q.title} ({q.description[:100]})" if q.description else q.title for q in quests
                ))
            known_npcs = sessions.get_known_npcs_for_character(session.id, char.id)
            if known_npcs:
                context_parts.append("Известные NPC: " + "; ".join(
                    (f"{n['npc_name']} ({n['attitude']}, знает: {n['known_facts'][:100]})"
                     if n['known_facts'] else f"{n['npc_name']} ({n['attitude']})")
                    for n in known_npcs
                ))
        character_context = "\n".join(context_parts)

        answer = await dm_engine.answer_question(history_msgs, question, player.display_name, character_context, session_id=session.id)

        db.add_history(HistoryEntry(session_id=session.id, author=player.display_name, content=f"[ВОПРОС] {question}", entry_type="ask"))
        db.add_history(HistoryEntry(session_id=session.id, author="DM", content=f"[ОТВЕТ ДМ] {answer}", entry_type="ask"))

        await send_safe(update, f"❓ **{player.display_name}:** {question}\n\n🎭 **ДМ:** {answer}")
    except Exception as e:
        logger.error(f"Ask error: {e}")
        await send_safe(update, f"❌ Ошибка: {e}")
    finally:
        if typing_task:
            stop_typing.set()
            typing_task.cancel()


async def dbask_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Appeal to DB-Bot to fix database state. Players can correct AI mistakes.

    D1 fix: previously made ONE chat() call and read choice.content directly — but when
    the model's first response is only tool_calls (content is empty/null, which is normal
    behavior for most providers), the reply got cut off with no explanation text at all.
    D2 fix: previously gave the DB-Bot only a snapshot of current DB state, with NO game
    history — so it had no way to actually verify a player's claim ("ДМ же писал, что
    я нашёл меч") and tended to reject appeals out of pure ignorance. Now both fixes are
    handled by DMEngine.process_dbask(), which loops until a real text answer comes back
    AND is given the last 15 history entries as context.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    if not session:
        await send_safe(update, "Нет сессии.")
        return
    # ИТЕРАЦИЯ 10 (Раздел 6): usage апелляции атрибутируется сессии.
    usage_ledger.bind_session(session.id)
    if await _db_busy_guard(update, session):
        return
    if not ctx.args:
        await send_safe(update, "Использование: `/dbgofyn у меня 15 зм, а не 10` или `/dbgofyn убери состояние отравления`")
        return

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "Сначала `/ymuno`")
        return

    # BUG 3 FIX: апелляции к БД до/во время генерации мира не имеют смысла.
    if await world_gen_guard(update, session):
        return

    # ИТЕРАЦИЯ 10 (Раздел 3): апелляция без единого хода — отказ.
    # world_gen_guard пропускает сразу после генерации мира, но между «мир
    # создан» и «первый ход игрока» есть окно, когда истории ходов ещё нет
    # вообще — DB-боту не на что опираться (не замена guard'а, а дополнительная
    # более узкая проверка СТРОГО после него).
    if not has_any_round_history(session.id):
        await send_safe(update,
            "🔒 **Апелляция пока невозможна.** В сессии ещё не было ни одного хода — "
            "DB-боту не на что опираться.\n\n"
            "Напиши `Дн. твоё действие` и сделай хотя бы один ход — после этого апелляции заработают."
        )
        return

    appeal = " ".join(ctx.args)
    char = db.get_character_by_player(user.id, session.id)
    char_name = char.name if char else player.display_name

    if update.effective_chat:
        await update.effective_chat.send_action(action="typing")

    try:
        # Build current DB state for context (all currencies, not just gp — H3)
        chars = db.get_session_characters(session.id)
        state_lines = ["Текущее состояние БД:"]
        for c in chars:
            gold = sessions.get_gold(session.id, c.id)
            inv = sessions.get_inventory(session.id, c.id)
            loc = sessions.get_location(session.id, c.id)
            conds = sessions.get_character_conditions(session.id, c.id)
            cond_str = ", ".join(cc.condition for cc in conds) if conds else "нет"
            inv_str = ", ".join(i["item"] for i in inv) if inv else "пусто"
            gold_str = ", ".join(f"{v}{k}" for k, v in gold.items() if v) or "0gp"
            state_lines.append(
                f"{c.name}: HP={c.hp}/{c.max_hp}, AC={c.ac}, Валюта=[{gold_str}], Loc={loc.location_name if loc else '?'}, Conds=[{cond_str}], Inv=[{inv_str}]"
            )
        state_text = "\n".join(state_lines)

        # D2: recent narrative history so the DB-Bot can actually check the claim
        recent_history = db.get_history(session.id, limit=15)
        history_text = "\n".join(f"[{h.author}]: {h.content}" for h in recent_history) or "(история пуста)"

        result = await dm_engine.process_dbask(appeal, char_name, state_text, history_text, session.id)
        answer = result.get("answer", "DB-Bot не ответил.")
        game_actions = result.get("game_actions", [])

        applied = 0
        if game_actions:
            applied, errors = sessions._apply_game_actions(session.id, game_actions)
            if errors:
                logger.warning(f"dbask apply errors: {errors}")

        db.add_history(HistoryEntry(
            session_id=session.id,
            author=player.display_name,
            content=f"[DB-ASK] {appeal}",
            entry_type="ooc",
        ))
        db.add_history(HistoryEntry(
            session_id=session.id,
            author="DB-Bot",
            content=f"[DB-ASK] {answer}",
            entry_type="ooc",
        ))

        applied_note = f"\n\n✅ Применено изменений: {applied}" if applied else ""
        await send_safe(update, f"🗃️ **Апелляция {char_name}:** {appeal}\n\n🤖 **DB-Bot:** {answer}{applied_note}")
    except Exception as e:
        logger.error(f"DB-ask error: {e}")
        await send_safe(update, f"❌ Ошибка DB-Bot: {e}")


async def cyfieithu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set translation language. Must be sent in DM to bot."""
    if update.effective_chat.type != "private":
        await update.message.reply_text("⚠️ Эту команду нужно отправить в личные сообщения бота.\nПерешлите её мне в ЛС.")
        return
    
    user_id = update.effective_user.id
    args = update.message.text.split(maxsplit=1)
    
    # Find player's active session
    active_sessions = db_manager.get_all_active_sessions()
    target_session = None
    for s in active_sessions:
        db = db_manager.get_db(s.id)
        players = db.get_players(s.id)
        if any(p.user_id == user_id for p in players):
            target_session = s
            break
    
    if not target_session:
        await update.message.reply_text("❌ Ты не участвуешь ни в одной активной сессии.")
        return
    
    if len(args) < 2 or not args[1].strip():
        # Show current language setting
        db = db_manager.get_db(target_session.id)
        lang = db.get_player_language(target_session.id, user_id)
        if lang and lang.enabled:
            await update.message.reply_text(f"🌐 Перевод включён: <b>{lang.language}</b>\nДля отключения: /cyfieithu off", parse_mode="HTML")
        else:
            await update.message.reply_text("🌐 Перевод отключён.\nИспользуй: /cyfieithu <язык>\nПример: /cyfieithu Spanish")
        return
    
    language = args[1].strip()
    if language.lower() in ("off", "выкл", "0"):
        db = db_manager.get_db(target_session.id)
        db.set_player_language(target_session.id, user_id, "", False)
        await update.message.reply_text("🌐 Перевод отключён.")
        return
    
    db = db_manager.get_db(target_session.id)
    db.set_player_language(target_session.id, user_id, language, True)
    # ИТЕРАЦИЯ 12: было без parse_mode — «**язык**» показывался звёздочками.
    await update.message.reply_text(
        md_to_html(
            f"🌐 Перевод на **{language}** включён! ✅\n"
            "Нарратив будет приходить в ЛС через несколько секунд после отправки в группу."
        ),
        parse_mode="HTML",
    )


async def _process_dn_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action_text: str):
    """Core action handler — called by both 'Дн.' text and /dn command."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    session = get_session(chat_id)
    if not session:
        await send_safe(update, "⚠️ Нет активной сессии в этом чате.")
        return

    # ИТЕРАЦИЯ 10 (Раздел 6): атрибуция usage раунда этой сессии. Все задачи,
    # созданные ниже через asyncio.create_task (боевой цикл, DB-Bot background,
    # NPC-резолвы, переводчик), наследуют этот контекст — сумма usage со ВСЕХ
    # ролей ляжет на сессию и будет списана в конце раунда (settle_round в
    # _run_db_bot_background). Без unbind — контекст задачи умирает вместе с
    # задачей хендлера (PTB создаёт задачу на каждый апдейт).
    usage_ledger.bind_session(session.id)

    # BUG #2: capture the forum/topic thread id from this Дн. message so async
    # background sends (Master narrative, DB-Bot phase, combat loop) can find the
    # right topic later.
    capture_thread_id(update, session.id)

    db = db_manager.get_db(session.id)
    player = db.get_player(user.id, session.id)
    if not player:
        await send_safe(update, "⚠️ Ты не в игре. Сначала `/ymuno`")
        return

    # ИТЕРАЦИЯ 10 (Разделы 6-7): токен-гейт перед обработкой хода.
    # В commercial-режиме: ЛС закрыта для не-тестеров; в платных чатах
    # проверяется баланс плательщика (по billing_mode сессии). Тестеры
    # проходят всегда. Плагина нет / COMMERCIAL_MODE=false → проверка
    # пропускается (бесплатная игра, прежнее поведение).
    billing = get_billing_plugin(ctx)
    if billing is not None:
        allowed, gate_msg = await billing.check_game_allowed(update, ctx, session=session)
        if not allowed:
            await send_safe(update, gate_msg)
            return

    # BUG 3 FIX: до или во время генерации мира ходить нельзя — только после начала игры.
    if await world_gen_guard(update, session):
        return

    # ── V10: Combat turn blocking ──
    # If combat is active with initiative, only the current turn's player(s) may Дн.
    # EXCEPTION: DUAL NARRATIVE — non-combat players can act freely via their own queue
    #
    # БОЙ-FIX v3: решение «в бою ли мы» принимается по СВЕЖЕМУ состоянию из БД
    # (session-объект в памяти может быть устаревшим — например, defensive-check
    # только что аварийно завершил рассинхронизированный бой, или Мастер параллельно
    # вызвал diweddymladd).
    fresh_session = db.get_session(session.id)
    combat_on = bool(fresh_session and fresh_session.combat_active
                     and COMBAT_INITIATIVE_ENABLED)

    if combat_on:
        # ── DUAL NARRATIVE: non-combat players go to their own queue ──
        if DUAL_NARRATIVE_ENABLED and sessions.is_non_combat_player(session.id, user.id):
            count = sessions.submit_non_combat_action(session.id, user.id, action_text)
            char = db.get_character_by_player(user.id, session.id)
            char_name = char.name if char else player.display_name
            # BUG 2 FIX: было auto_delete=15 — сообщение исчезало по таймеру ВО ВРЕМЯ
            # генерации нарратива. Теперь tracked: удалится ПОСЛЕ нарратива
            # (_resolve_non_combat_round → _delete_round_messages).
            # БОЙ-FIX: если Мастер прямо сейчас резолвит боевой ход/нарратив,
            # действие просто копится в очереди и разрешится следующим пакетом.
            _sent = await send_safe(update,
                f"✅ {char_name}: {action_text}\n"
                f"📖 Бой идёт отдельно — Мастер разрешит твой ход, не дожидаясь его конца.\n"
                f"⚔️ Если твоё действие вмешается в бой — Мастер добавит тебя в инициативу.",
                source="player",
            )
            if _sent:
                await _track_round_messages_with_fallback(
                    session.id, _sent, chat_id, user.id,
                    bot=(ctx.bot if ctx else None), message_type="bot_confirm")
            # If there are 1+ non-combat actions queued, resolve them
            if count >= 1:
                bot_obj = ctx.bot if ctx else None
                if bot_obj:
                    asyncio.create_task(_resolve_non_combat_round(session.id, chat_id, bot_obj, update, ctx))
            return

        combat_group = sessions.get_combat_action_group(session.id)

        # ── БОЙ-FIX v3: боевой состояние могло только что измениться ──
        # defensive-check в get_combat_action_group аварийно завершает бой при
        # рассинхроне (combat_active=True, но encounter потерян), а Мастер мог
        # параллельно вызвать diweddymladd. Перечитываем и честно откатываемся
        # в обычный режим вместо боевых проверок по устаревшим данным.
        fresh_session = db.get_session(session.id)
        combat_on = bool(fresh_session and fresh_session.combat_active
                         and combat_group.get("mode") != "normal")
        if not combat_on:
            if session.combat_active and sessions.pop_combat_desync_flag(session.id):
                await send_safe(update,
                    "⚠️ Боевое состояние было повреждено (данные о бое потеряны) — "
                    "бой аварийно завершён. Продолжаем в обычном режиме: ходите `Дн.` как обычно.")
        else:
            # ── FIX: Check NPC turn FIRST, before blocking ──
            # When it's an NPC's turn, ALL PCs are in blocked_players.
            # If we check blocked first, we send "Не твой ход" and return,
            # never reaching the auto-resolve path. NPC just stands AFK.
            if combat_group.get("npc_turn"):
                current = combat_group.get("current", {})
                current_name = current.get("name", "?")
                # Check if the player's character is in the initiative order at all
                all_order = combat_group.get("all_order", [])
                player_in_combat = any(e.get("player_id") == user.id for e in all_order if e.get("entity_type") == "pc")
                if not player_in_combat:
                    await send_safe(update,
                        f"⚔️ Сейчас ход NPC ({current_name}). Ты не в бою."
                    )
                    return
                # ── БОЙ-FIX: во время хода NPC игроки в бою ЖДУТ ──
                # Раньше каждое такое сообщение порождало свой auto-resolver:
                # два игрока → два параллельных резолва одного и того же хода
                # стражника (два вызова Мастера, два GM_SECRET, два расходящихся
                # нарратива). Теперь просто честно говорим, что идёт ход NPC,
                # и ждём; боевой цикл сам разрешит ход стражника.
                await send_safe(update,
                    f"⚔️ Ход {current_name}. Игроки в бою не могут действовать — ожидайте."
                )
                # Оживляем боевой цикл ТОЛЬКО если он умер (после рестарта/ошибки).
                # Если жив — сам разрулит NPC-ходы; single-flight guard внутри
                # _auto_resolve_npcs_then_pc не даст плодить дубликаты.
                bot_obj = ctx.bot if ctx else None
                if bot_obj and not _active_combat_loops.get(session.id):
                    asyncio.create_task(_auto_resolve_npcs_then_pc(session.id, user.id, chat_id, bot_obj))
                return

            # ── Now check if player is blocked (another PC's turn) ──
            blocked = combat_group.get("blocked_players", [])
            if user.id in blocked:
                current = combat_group.get("current", {})
                current_name = current.get("name", "?")
                await send_safe(update,
                    f"⚔️ Бой идёт! Не твой ход. Ждём: {current_name}"
                )
                return

            # ── БОЙ-FIX v2: очередь во время боя ОБЯЗАНА совпадать с боевой группой ──
            # Устаревшая all-players очередь (созданная до/в момент старта боя)
            # навсегда ждала non-combat игроков — классический дедлок из реального
            # лога («Ждём: Храфна Морвен, Рими», где Рими заперт в non-combat lane).
            # Пересобираем её под текущего PC, если она разошлась с инициативой.
            sessions.sync_combat_queue(session.id)

    char = db.get_character_by_player(user.id, session.id)
    char_name = char.name if char else player.display_name
    telegram_nick = player.username or player.display_name

    queue = db.get_queue_state(session.id)

    if not queue:
        # V10b: In per-turn combat, no queue exists yet (loop hasn't set one up).
        # Set up single-player collection for the current turn's PC, not all players.
        # БОЙ-FIX v3: состояние боя перечитываем СВЕЖИМ из БД — in-memory session
        # мог устареть (аварийное завершение рассинхрона и т.п.).
        fresh_session = db.get_session(session.id)
        if fresh_session and fresh_session.combat_active and COMBAT_INITIATIVE_ENABLED:
            combat_group = sessions.get_combat_action_group(session.id)
            if combat_group.get("npc_turn"):
                # БОЙ-FIX: как и в основной ветке — во время хода NPC игроки
                # в бою ждут; никаких параллельных авто-резолверов на каждое
                # сообщение. Цикл оживляем только если он умер.
                current = combat_group.get("current", {})
                current_name = current.get("name", "?")
                await send_safe(update,
                    f"⚔️ Ход {current_name}. Игроки в бою не могут действовать — ожидайте."
                )
                bot_obj = ctx.bot if ctx else None
                if bot_obj and not _active_combat_loops.get(session.id):
                    asyncio.create_task(_auto_resolve_npcs_then_pc(session.id, user.id, chat_id, bot_obj))
                return
            current = combat_group.get("current", {})
            if current.get("player_id") != user.id:
                await send_safe(update,
                    f"⚔️ Не твой ход. Ждём: {current.get('name', '?')}"
                )
                return
            # Correct player, no queue yet — set single-player collection
            sessions.start_single_player_collection(session.id, user.id)
            queue = db.get_queue_state(session.id)
        else:
            sessions.start_action_collection(session.id)
            queue = db.get_queue_state(session.id)

    if queue and queue.is_resolving:
        await send_safe(update, "⏳ Разрешаю предыдущий раунд... подожди.")
        return

    if queue:
        waiting_for = json.loads(queue.waiting_for)
        collected = json.loads(queue.collected_actions)

        if str(user.id) in collected:
            await send_safe(update, "⏳ Ты уже сходил в этом раунде. Жди следующего. (`/diddymu` — отменить своё действие)")
            return

        if user.id not in waiting_for:
            # BUG #11 FIX: In per-turn combat, the queue only contains the CURRENT
            # player. If this user is not in waiting_for, it means it's NOT their
            # turn — DO NOT reset the queue to ALL players (the previous behavior).
            # Resetting was the root cause of the bug scenario: Eira's queue had
            # only [Eira's id], СЛИЗЬ's action reset the queue to all players, and
            # the round went off the rails — the bot ended up re-prompting Eira
            # instead of advancing to СЛИЗЬ after Eira acted.
            # БОЙ-FIX v3: боевое состояние перечитываем СВЕЖИМ из БД — in-memory
            # session мог устареть (аварийное завершение рассинхрона и т.п.).
            fresh_session = db.get_session(session.id)
            if fresh_session and fresh_session.combat_active and COMBAT_INITIATIVE_ENABLED:
                # Strict initiative: this isn't your turn. Tell the user who's up.
                combat_group = sessions.get_combat_action_group(session.id)
                current = combat_group.get("current", {})
                current_name = current.get("name", "?")
                await send_safe(update,
                    f"⚔️ Не твой ход. Ждём: {current_name}"
                )
                return
            # Non-combat: keep the legacy reset-to-all-players behavior.
            sessions.start_action_collection(session.id)
            queue = db.get_queue_state(session.id)
            waiting_for = json.loads(queue.waiting_for)

        async with sessions.get_round_lock(session.id):
            is_complete, msg = sessions.submit_action(session.id, user.id, action_text)
            defer = is_complete and sessions.is_db_busy(session.id)
            if defer:
                sessions.mark_pending_resolve(session.id, chat_id, ctx)

        # Track player's "Дн." message for anti-spam deletion after narrative.
        # In combat this is handled by the bunker-bot-style _pending_combat_messages
        # flow (see engine.py _track_pending_combat_message + _cleanup_pending_combat_messages).
        if ANTISPAM_ENABLED and session:
            await _track_round_message(session.id, update.message.message_id,
                                        chat_id, user.id, "dn")

        # BUG #12 v2: bunker-bot pattern — in combat, the "✅ ... сходил" technical
        # confirmation is tracked via the in-memory _pending_combat_messages dict
        # and deleted by _cleanup_pending_combat_messages AFTER the narrative is
        # sent (inside _send_combat_turn_result). This GUARANTEES the technical
        # message vanishes only after the player sees the narrative — no race
        # condition with auto_delete timers.
        #
        # BUG 2 FIX: вне боя auto_delete=15 больше НЕ используется — все
        # тех-сообщения tracked в БД (bot_confirm) и удаляются СТРОГО после
        # нарратива (_resolve_and_send → _delete_round_messages).

        if is_complete:
            # БОЙ-FIX v3: боевое состояние перечитываем СВЕЖИМ из БД — in-memory
            # session мог устареть (аварийное завершение рассинхрона, конец боя
            # от Мастера и т.п.), и ход нельзя отправлять в боевой резолвер,
            # который без encounter молча выходит (раунд зависал навсегда).
            fresh_session = db.get_session(session.id)
            combat_still_on = bool(fresh_session and fresh_session.combat_active
                                   and COMBAT_INITIATIVE_ENABLED)
            if defer:
                # DB-Bot is busy — defer resolution. The technical message should
                # stay visible until the deferred resolve eventually fires, so
                # don't auto-delete it. The next round's narrative will trigger
                # _delete_round_messages which cleans up the player's "Дн." message
                # (tracked above) and any "bot_confirm" tracked round messages.
                # BUG 2 FIX: tracked как bot_confirm — удаление ПОСЛЕ нарратива.
                _sent = await send_safe(update,
                    f"✅ {char_name} (@{telegram_nick}) сходил!\n"
                    f"⏳ Все на месте, но мир ещё обновляется после прошлого раунда — "
                    f"разрешение начнётся автоматически, как только это закончится.",
                    source="player",
                )
                if _sent:
                    await _track_round_messages_with_fallback(
                        session.id, _sent, chat_id, user.id,
                        bot=(ctx.bot if ctx else None), message_type="bot_confirm")
            elif combat_still_on:
                # Per-turn combat: track the "✅ ... ⚔️ Разрешаю ход..." message
                # for deletion AFTER the narrative arrives. This is the bunker-bot
                # pattern — no auto_delete timer, deletion happens inside
                # _send_combat_turn_result via _cleanup_pending_combat_messages.
                sent_ids = await send_safe(update,
                    f"✅ {char_name} (@{telegram_nick}): {action_text}\n⚔️ Разрешаю ход...",
                    source="player",
                )
                if sent_ids:
                    _track_pending_combat_message(session.id, sent_ids, chat_id)
                bot_obj = ctx.bot if ctx else None
                if bot_obj:
                    asyncio.create_task(_resolve_pc_combat_turn(session.id, action_text, chat_id, bot_obj))
            else:
                # Non-combat round.
                # BUG 2 FIX: было auto_delete=15 (таймер срабатывал ВО ВРЕМЯ 30-60 сек
                # генерации — сообщение исчезало ДО нарратива). Теперь tracked:
                # _resolve_and_send удалит его ПОСЛЕ нарратива.
                sent_ids = await send_safe(update,
                    f"✅ {char_name} (@{telegram_nick}) сходил!\n"
                    f"🎲 Все на месте — нейросеть разрешает...",
                    source="player",
                )
                if sent_ids:
                    await _track_round_messages_with_fallback(
                        session.id, sent_ids, chat_id, user.id,
                        bot=(ctx.bot if ctx else None), message_type="bot_confirm")
                await _resolve_and_send(session.id, update, ctx)
        else:
            # Round not complete yet — show "Ждём: ..." with remaining players.
            # BUG 2 FIX: было auto_delete=15. Теперь tracked как bot_confirm:
            # удалится ПОСЛЕ нарратива этого раунда (когда все сходят), а не по таймеру.
            pending = sessions.get_pending_players(session.id)
            sent_ids = await send_safe(update,
                f"✅ {char_name} (@{telegram_nick}): {action_text}\n"
                f"⏳ Ждём: {', '.join(pending)}",
                source="player",
            )
            if sent_ids:
                await _track_round_messages_with_fallback(
                    session.id, sent_ids, chat_id, user.id,
                    bot=(ctx.bot if ctx else None), message_type="bot_confirm")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_chat or not update.message or not update.message.text:
        return
    text = (update.message.text or "").strip()

    if not text:
        return

    # /dn command — same as Дн. but works as a command (no admin rights needed)
    if text.startswith("/dn"):
        action_text = text[3:].strip()
        if not action_text:
            await send_safe(update, "После `/dn` напиши действие. Пример: `/dn Я атакую гоблина мечом!`")
            return
        await _process_dn_action(update, ctx, action_text)
        return

    session = get_session(update.effective_chat.id)
    if not session:
        return

    # BUG #2: capture the forum/topic thread id from this message so async background
    # sends (DB-Bot phase, combat loop, deferred resolves) can find the right topic
    # later when the original Update is gone.
    capture_thread_id(update, session.id)

    db = db_manager.get_db(session.id)
    player = db.get_player(update.effective_user.id, session.id)
    if not player:
        return

    if not (text.startswith("Дн.") or text.startswith("Dn.")):
        return

    action_text = text[3:].strip()
    if not action_text:
        await send_safe(update, "После `Дн.` или `Dn.` напиши действие. Пример: `Дн. Я атакую гоблина мечом!`")
        return

    await _process_dn_action(update, ctx, action_text)


async def _resolve_lang_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle language translation button — shows alert with translation or 'Не для тебя'."""
    query = update.callback_query
    user = query.from_user
    if not update.effective_chat:
        await query.answer("❌ Ошибка чата.", show_alert=True)
        return

    chat_id = update.effective_chat.id
    data = query.data  # "lang:язык:char"

    # Parse callback data
    parts = data.split(":", 2)
    if len(parts) < 2:
        await query.answer("❌", show_alert=True)
        return

    lang_name = parts[1]
    translations = _get_lang_translations(chat_id)

    # Find the matching translation
    original_text = ""
    for lang, text, _ in translations:
        if lang.lower() == lang_name.lower():
            original_text = text
            break

    if not original_text:
        await query.answer("❌ Перевод устарел.", show_alert=True)
        return

    # Find player's character and check if they know this language
    session = get_session(chat_id)
    if session:
        db = db_manager.get_db(session.id)
        char = db.get_character_by_player(user.id, session.id)
        if char and _character_knows_language(char.languages, lang_name):
            # Player knows the language — show translation
            await query.answer(
                f"🗣️ [{lang_name}]\n\n{original_text}",
                show_alert=True
            )
            return

    # Player doesn't know the language or has no character
    await query.answer("🔒 Не для тебя — ты не понимаешь этот язык.", show_alert=True)
