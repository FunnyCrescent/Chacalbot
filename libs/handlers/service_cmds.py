"""
libs.handlers.service_cmds — сервисные команды (Фичи 5 и 7).

/cydbwysedd  — проверка баланса на счету LLM-агрегатора (по API).
/rhwymo      — персональный бинд команды: /rhwymo <цель> <псевдоним> (только ЛС).
/datgysylltu — удалить свой бинд: /datgysylltu <псевдоним> | /datgysylltu popeth.

Плюс инфраструктура нечувствительности к регистру (Фича 7):
  - command_case_normalizer (group -2): "/CYMERIAD" → "/cymeriad" для ЛЮБОЙ команды;
  - bind_resolver (group -1): подставляет персональные бинды пользователя;
  - unknown_command_hint (group 0, регистрируется ПОСЛЕДНИМ плагином): подсказка
    на действительно неизвестные команды.

Бинды работают ТОЛЬКО для владельца и в любом чате; СОЗДАВАТЬ их можно только
в ЛС бота. Нельзя биндить уже существующие команды бота.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import aiohttp
from telegram import Update, MessageEntity
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from libs.config_legacy import (
    OPENAI_BASE_URL, OPENAI_API_KEY, BASE_DIR,
)
from libs.db import BindsDB
from libs.db.binds_db import ALIAS_RE, RESERVED_BIND_WORDS
from libs.proxy_helper import aiohttp_session_kwargs, aiohttp_request_kwargs
from libs.handlers._state import sessions
from libs.handlers.utils import get_session, md_to_html, send_safe

logger = logging.getLogger(__name__)

BINDS_DB = BindsDB(f"{BASE_DIR}/data/binds.db")

# Команды, у которых есть валлийское имя: /help — единственная англо-валлийская пара.
HELP_ALIASES = {"help", "cymorth"}


# ══════════════════════════════════════════════════════════════════
# Реестр зарегистрированных команд
# ══════════════════════════════════════════════════════════════════

def get_registered_commands(app: Application) -> Set[str]:
    """Собирает ВСЕ имена команд из зарегистрированных CommandHandler'ов.

    ConversationHandler не имеет атрибута .commands — раскрываем его
    entry_points (иначе /creu нельзя выбрать целью бинда)."""
    cmds: Set[str] = set()

    def _collect(handler) -> None:
        commands = getattr(handler, "commands", None)
        if commands:
            cmds.update(str(c).lower() for c in commands)
            return
        # ConversationHandler и подобные — рекурсивно обходим вложенные хендлеры
        for attr in ("entry_points", "states", "fallbacks"):
            nested = getattr(handler, attr, None)
            if not nested:
                continue
            values = nested.values() if isinstance(nested, dict) else nested
            for sub in (values or []):
                if isinstance(sub, (list, tuple)):
                    for s2 in sub:
                        _collect(s2)
                else:
                    _collect(sub)

    try:
        for group_handlers in getattr(app, "handlers", {}).values():
            for handler in group_handlers:
                _collect(handler)
    except Exception as e:
        logger.warning(f"[service] get_registered_commands failed: {e}")
    return cmds


# ══════════════════════════════════════════════════════════════════
# Group -2: нечувствительность ВСЕХ команд к регистру
# ══════════════════════════════════════════════════════════════════

_CMD_TOKEN_RE = re.compile(r"^/([A-Za-z0-9_]+)(@[A-Za-z0-9_]+)?")


async def command_case_normalizer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """"/CYMERIAD" и "/Rhwymo" → "/cymeriad", "/rhwymo" (первый токен команды).

    Живёт в группе -2 и выполняется ДО всех CommandHandler'ов. Мутирует текст
    сообщения так, что регистр команды больше никогда не имеет значения."""
    try:
        message = update.message or update.edited_message
        if not message or not message.text:
            return
        text = message.text
        m = _CMD_TOKEN_RE.match(text)
        if not m:
            return
        cmd, bot_suffix = m.group(1), m.group(2) or ""
        lowered = cmd.lower()
        if cmd == lowered:
            return
        new_text = "/" + lowered + bot_suffix + text[m.end():]
        try:
            message.text = new_text
        except Exception:
            return
        # Entity BOT_COMMAND покрывает первый токен — длина не изменилась,
        # поэтому entities корректировать не нужно.
    except Exception as e:
        logger.debug(f"[service] case normalizer skipped: {e}")


# ══════════════════════════════════════════════════════════════════
# Group -1: персональные бинды
# ══════════════════════════════════════════════════════════════════

async def bind_resolver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подменяет персональный бинт пользователя на настоящую команду.

    /character (бинд владельца на /cymeriad) → message.text переписывается на
    "/cymeriad ...", после чего group 0 подхватывает настоящий CommandHandler.
    Работает во всех чатах, но резолвится СТРОГО по user_id владельца бинда."""
    try:
        message = update.message or update.edited_message
        if not message or not message.text:
            return
        user = update.effective_user
        if not user:
            return

        text = message.text
        m = _CMD_TOKEN_RE.match(text)
        if not m:
            return
        cmd = m.group(1).lower()
        bot_suffix = m.group(2) or ""

        # Реальная команда всегда имеет приоритет над биндами.
        registered = get_registered_commands(ctx.application)
        if cmd in registered:
            return

        target = BINDS_DB.get_bind(user.id, cmd)
        if not target or target == cmd:
            return

        # Защита: цель должна быть реальной командой (если команда пропала из
        # бота — бинд просто не срабатывает).
        if target not in registered:
            return

        new_token = "/" + target + bot_suffix
        new_text = new_token + text[m.end():]

        # Пишем новый текст и пересобираем entity BOT_COMMAND под новый токен.
        old_entities = list(getattr(message, "entities", None) or [])
        try:
            message.text = new_text
        except Exception:
            return
        if old_entities:
            e0 = old_entities[0]
            delta = len(new_token) - int(getattr(e0, "length", 0) or 0)
            new_entities = []
            for idx, ent in enumerate(old_entities):
                try:
                    if idx == 0:
                        new_entities.append(MessageEntity(
                            type=MessageEntity.BOT_COMMAND,
                            offset=int(getattr(e0, "offset", 0) or 0),
                            length=len(new_token),
                        ))
                    else:
                        new_entities.append(MessageEntity(
                            type=ent.type,
                            offset=int(getattr(ent, "offset", 0) or 0) + delta,
                            length=int(getattr(ent, "length", 0) or 0),
                            url=getattr(ent, "url", "") or "",
                            language_code=getattr(ent, "language_code", "") or "",
                        ))
                except Exception:
                    pass
            try:
                message.entities = tuple(new_entities)
            except Exception:
                pass

        logger.debug(f"[service] bind resolved for {user.id}: /{cmd} -> /{target}")
    except Exception as e:
        logger.debug(f"[service] bind resolver skipped: {e}")


# ══════════════════════════════════════════════════════════════════
# Group 0 (регистрируется последним плагином): неизвестные команды
# ══════════════════════════════════════════════════════════════════

async def unknown_command_hint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подсказка для реально неизвестных команд. Регистрируется ПОСЛЕДНИМ
    обработчиком в группе 0 — срабатывает, только если ни один CommandHandler
    не совпал (в т.ч. потому что бинт принадлежит другому игроку)."""
    try:
        message = update.message
        if not message or not message.text:
            return
        m = _CMD_TOKEN_RE.match(message.text)
        if not m:
            return
        await send_safe(update,
            "❓ Неизвестная команда.\n"
            "📖 Все команды: /help (или /cymorth).\n"
            "🔗 Свой псевдоним команды можно сделать в ЛС бота: `/rhwymo <команда> <псевдоним>`",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# /rhwymo — создать персональный бинд (только ЛС)
# ══════════════════════════════════════════════════════════════════

async def bind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type if update.effective_chat else "private"

    if chat_type != "private":
        # ИТЕРАЦИЯ 12: parse_mode="Markdown" (легаси) заменён на HTML —
        # Telegram-маркдаун не принимает `...`-бэктики надёжно и депрекейтед.
        await update.message.reply_text(
            md_to_html(
                "🔒 Бинды создаются только в личных сообщениях бота.\n"
                "Перешли мне эту команду в ЛС: `/rhwymo <команда> <псевдоним>`"
            ),
            parse_mode="HTML",
        )
        return

    args = [a for a in (ctx.args or []) if a.strip()]
    registered = get_registered_commands(ctx.application)

    # /rhwymo rhestr | list — показать свои бинды
    if len(args) == 1 and args[0].lower().lstrip("/") in RESERVED_BIND_WORDS:
        alias_filter = args[0].lower().lstrip("/")
        if alias_filter in ("rhestr", "list"):
            binds = BINDS_DB.list_binds(user.id)
            if not binds:
                await send_safe(update, "🔗 У тебя пока нет биндов.\nСоздать: `/rhwymo <команда> <псевдоним>`")
                return
            lines = ["🔗 **Твои бинды:**\n"]
            for alias, target, created in binds:
                lines.append(f"  `/{alias}` → `/{target}`")
            lines.append("\n❌ Удалить: `/datgysylltu <псевдоним>` · всё сразу: `/datgysylltu popeth`")
            await send_safe(update, "\n".join(lines))
            return

    # /rhwymo без аргументов — справка
    if not args:
        await send_safe(update,
            "🔗 **Персональные бинды команд**\n\n"
            "Использование: `/rhwymo <команда> <псевдоним>`\n"
            "Пример: `/rhwymo cymeriad character` — теперь `/character` работает как `/cymeriad` (только у тебя).\n\n"
            "📋 Список: `/rhwymo rhestr`\n"
            "❌ Удалить: `/datgysylltu <псевдоним>`\n\n"
            "Правила:\n"
            "• Только в ЛС бота; бинд работает только для тебя (в любом чате).\n"
            "• Нельзя занимать имя уже существующей команды бота.\n"
            "• Псевдоним: 2–32 символа, латиница/цифры/`_`, без пробелов и эмодзи.\n"
            "• Регистр не важен ни для биндов, ни для обычных команд.")
        return

    if len(args) != 2:
        await send_safe(update, "Формат: `/rhwymo <команда> <псевдоним>`. Пример: `/rhwymo cymeriad character`")
        return

    target = args[0].lstrip("/").lower()
    alias = args[1].lstrip("/").lower()

    # Валидация цели
    if target not in registered:
        await send_safe(update,
            f"❌ Команда `/{target}` не существует у бота.\n"
            f"Смотри список: /help")
        return

    # Валидация псевдонима
    if alias in registered:
        await send_safe(update, f"❌ `/{alias}` — уже зарегистрированная команда бота, её нельзя занимать.")
        return
    if alias in RESERVED_BIND_WORDS:
        await send_safe(update, f"❌ `{alias}` — служебное слово, выбери другой псевдоним.")
        return
    if not ALIAS_RE.match(alias):
        await send_safe(update,
            "❌ Псевдоним должен быть 2–32 символа: латиница, цифры, `_`. "
            "Без пробелов, эмодзи и спецсимволов.")
        return
    if alias == target:
        await send_safe(update, "❌ Псевдоним совпадает с командой — в этом нет смысла.")
        return

    # Замена существующего бинда?
    previous = BINDS_DB.get_bind(user.id, alias)

    ok, code = BINDS_DB.add_bind(user.id, alias, target)
    if not ok:
        await send_safe(update, "❌ Не удалось создать бинд (некорректные данные).")
        return
    await send_safe(update,
        f"✅ Готово: `/{alias}` → `/{target}`\n"
        f"Пиши `/{alias}` в любом чате — сработает только у тебя."
        + (f"\n(перезаписан старый бинд `/{alias}` → `/{previous}`)" if previous and previous != target else ""))


# ══════════════════════════════════════════════════════════════════
# /datgysylltu — удалить бинд
# ══════════════════════════════════════════════════════════════════

async def unbind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type if update.effective_chat else "private"

    if chat_type != "private":
        await update.message.reply_text("🔒 Управление биндами — только в ЛС бота.")
        return

    args = [a for a in (ctx.args or []) if a.strip()]
    if not args:
        binds = BINDS_DB.list_binds(user.id)
        if not binds:
            await send_safe(update, "🔗 У тебя нет биндов.")
            return
        lines = ["🔗 **Твои бинды:**\n"]
        for alias, target, _created in binds:
            lines.append(f"  `/{alias}` → `/{target}`")
        lines.append("\n❌ Удалить: `/datgysylltu <псевдоним>` · всё сразу: `/datgysylltu popeth`")
        await send_safe(update, "\n".join(lines))
        return

    what = args[0].lstrip("/").lower()
    if what in ("popeth", "all"):
        removed = BINDS_DB.remove_all_binds(user.id)
        await send_safe(update, f"🧹 Удалено биндов: {removed}.")
        return

    if BINDS_DB.remove_bind(user.id, what):
        await send_safe(update, f"✅ Бинд `/{what}` удалён.")
    else:
        await send_safe(update, f"❌ Бинда `/{what}` у тебя нет. Список: `/rhwymo rhestr`")


# ══════════════════════════════════════════════════════════════════
# /cydbwysedd — баланс LLM-агрегатора (Фича 5)
# ══════════════════════════════════════════════════════════════════

_BALANCE_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://dnd-bot.local",
    "X-Title": "D&D Dark Fantasy Bot",
}


def _fmt_usd(v) -> str:
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)


def _host(base_url: str) -> str:
    try:
        return urlparse(base_url).netloc or base_url
    except Exception:
        return base_url


async def _fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers=_BALANCE_HEADERS,
                           timeout=aiohttp.ClientTimeout(total=20),
                           **aiohttp_request_kwargs()) as resp:
        if resp.status != 200:
            return None
        try:
            return await resp.json(content_type=None)
        except Exception:
            return None


async def check_aggregator_balance() -> str:
    """Опрашивает известные эндпоинты баланса OpenAI-совместимых агрегаторов.

    Поддерживается:
      • OpenRouter          — /auth/key, /credits
      • OpenAI (legacy)     — /dashboard/billing/credit_grants, /subscription, /usage
      • one-api / new-api   — /dashboard/billing/subscription (+ /usage)
    Возвращает готовый текст или строку с ошибкой."""
    if not OPENAI_API_KEY:
        return "❌ OPENAI_API_KEY не задан в .env — проверить баланс нельзя."
    if not OPENAI_BASE_URL:
        return "❌ OPENAI_BASE_URL не задан в .env — проверить баланс нельзя."

    base = OPENAI_BASE_URL.rstrip("/")
    sess_kwargs = aiohttp_session_kwargs()
    req_kwargs = aiohttp_request_kwargs()

    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, **sess_kwargs) as http:
            # ── OpenRouter: /auth/key ──
            data = await _fetch_json(http, f"{base}/auth/key", )
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                d = data["data"]
                usage, limit = d.get("usage"), d.get("limit")
                lines = [f"💰 Баланс агрегатора ({_host(base)}):"]
                if limit is not None:
                    lines.append(f"• Использовано: {_fmt_usd(usage)} из {_fmt_usd(limit)}")
                    try:
                        lines.append(f"• Осталось: {_fmt_usd(float(limit) - float(usage or 0))}")
                    except (TypeError, ValueError):
                        pass
                else:
                    lines.append(f"• Использовано: {_fmt_usd(usage)} (лимит не задан)")
                if d.get("label"):
                    lines.append(f"• Ключ: {d['label']}")
                if d.get("is_free_tier"):
                    lines.append("• Тариф: free tier")
                return "\n".join(lines)

            # ── OpenRouter: /credits ──
            data = await _fetch_json(http, f"{base}/credits")
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                d = data["data"]
                total, used = d.get("total_credits"), d.get("total_usage")
                lines = [f"💰 Баланс агрегатора ({_host(base)}):"]
                if total is not None:
                    lines.append(f"• Всего кредитов: {_fmt_usd(total)}")
                if used is not None:
                    lines.append(f"• Использовано: {_fmt_usd(used)}")
                if total is not None and used is not None:
                    try:
                        lines.append(f"• Осталось: {_fmt_usd(float(total) - float(used))}")
                    except (TypeError, ValueError):
                        pass
                return "\n".join(lines)

            # ── OpenAI legacy / one-api: /dashboard/billing/credit_grants ──
            data = await _fetch_json(http, f"{base}/dashboard/billing/credit_grants", )
            if isinstance(data, dict) and ("total_available" in data or "total_granted" in data):
                lines = [f"💰 Баланс агрегатора ({_host(base)}):"]
                if data.get("total_granted") is not None:
                    lines.append(f"• Выдано: {_fmt_usd(data.get('total_granted'))}")
                if data.get("total_used") is not None:
                    lines.append(f"• Использовано: {_fmt_usd(data.get('total_used'))}")
                if data.get("total_available") is not None:
                    lines.append(f"• Осталось: {_fmt_usd(data.get('total_available'))}")
                return "\n".join(lines)

            # ── one-api / new-api / OpenAI: /dashboard/billing/subscription + usage ──
            sub = await _fetch_json(http, f"{base}/dashboard/billing/subscription")
            if isinstance(sub, dict) and ("hard_limit_usd" in sub or "system_hard_limit_usd" in sub):
                limit = sub.get("system_hard_limit_usd") or sub.get("hard_limit_usd")
                lines = [f"💰 Баланс агрегатора ({_host(base)}):"]
                if limit is not None:
                    lines.append(f"• Лимит: {_fmt_usd(limit)}")
                usage = await _fetch_json(http, f"{base}/dashboard/billing/usage")
                if isinstance(usage, dict) and usage.get("total_usage") is not None:
                    used = usage.get("total_usage")
                    # one-api возвращает центы, OpenAI legacy — тоже центы
                    used = used / 100.0 if isinstance(used, (int, float)) and used > 1000 else used
                    lines.append(f"• Использовано: {_fmt_usd(used)}")
                    if limit:
                        try:
                            lines.append(f"• Осталось: {_fmt_usd(float(limit) - float(used))}")
                        except (TypeError, ValueError):
                            pass
                if sub.get("access_until"):
                    lines.append(f"• Доступ до: {sub.get('access_until')}")
                return "\n".join(lines)
    except asyncio.TimeoutError:
        return "⏱️ Агрегатор не ответил вовремя — попробуй позже."
    except Exception as e:
        logger.warning(f"[cydbwysedd] balance check failed: {e}")
        return f"❌ Не удалось получить баланс: {str(e)[:200]}"

    return (
        "❌ Агрегатор не поддерживает известные мне эндпоинты баланса.\n"
        f"Базовый URL: `{OPENAI_BASE_URL}`\n"
        "Поддерживаются: OpenRouter (/auth/key, /credits), OpenAI и one-api/new-api (/dashboard/billing/*)."
    )


async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/cydbwysedd — баланс агрегатора. В группах только создатель сессии, в ЛС — все."""
    user = update.effective_user
    chat_type = update.effective_chat.type if update.effective_chat else "private"

    if chat_type != "private":
        session = get_session(update.effective_chat.id)
        if session and not sessions.is_creator(user.id, session.id):
            await send_safe(update, "❌ Баланс агрегатора может смотреть только создатель сессии (или напиши мне в ЛС).")
            return

    note = await send_safe(update, "💰 Проверяю баланс агрегатора...")
    result = await check_aggregator_balance()
    await send_safe(update, result)
