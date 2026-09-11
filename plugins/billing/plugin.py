"""
billing — плагин монетизации (ИТЕРАЦИЯ 10, Разделы 6-7).

Активируется флагом COMMERCIAL_MODE=true в .env. Если плагин удалён из
plugins/, выключен в plugins.toml, или COMMERCIAL_MODE != true — игра
БЕСПЛАТНА ВЕЗДЕ (безопасный паттерн: хендлеры достают плагин через
PluginManager.get_plugin("billing") и молча пропускают проверки, если
его нет — НИКАКИХ прямых импортов плагина из libs/handlers).

Схема доступа при COMMERCIAL_MODE=true:
  • общий чат (MAIN_CHAT_ID) — игра бесплатна, без токен-гейта;
  • ЛС — играть нельзя для всех, КРОМЕ TESTERS (у не-тестеров в ЛС
    работают только /creu+диалог, /cyfieithu, /gwneud, заглушка оплаты);
  • любой другой чат — токен-гейт с РЕАЛЬНЫМ списанием: новым игрокам
    30 000 000 токенов ОДИН РАЗ за аккаунт (глобально по user_id).
    Пополнение баланса игроком пока НЕ реализовано (осознанно отложено) —
    тестеры пополняют вручную командой /ychwanegu.

Учёт: OpenAIClient отдаёт response.usage.total_tokens в usage_ledger;
плагин подписан на него и накапливает токены по session_id. В точке
«раунд полностью завершён» (хук в engine._run_db_bot_background после
set_db_busy(False)) — settle_round(): списание по session.billing_mode
("split" — поровну на активных игроков, "creator_pays" — весь раунд на
создателя). Тестеры не гейтируются и не списываются никогда.

Требование «любой плагин удаляем без краша» выполнено архитектурно:
этот файл НИЧЕГО не патчит в libs/handlers — все точки входа проверяют
get_plugin("billing") и работают без плагина как раньше.
"""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from core.plugin import Plugin
import libs.config_legacy as _cfg
from libs.config_legacy import (
    COMMERCIAL_MODE, TOKEN_GRANT_AMOUNT, BILLING_DB_PATH,
    ADMIN_CHAT_ID, get_testers,
)
from libs.db.billing_repo import BillingRepo

logger = logging.getLogger(__name__)


class BillingPlugin(Plugin):
    name = "billing"
    version = "1.0.0"
    description = (
        "Commercial mode: DM gate for non-testers, token gate with real "
        "per-round usage charges, /newydd billing menu, /ymuno confirm, "
        "tester top-up command /ychwanegu"
    )
    author = "iteracja 10"
    depends_on = ["persistence", "session-core"]
    provides = ["billing"]

    def __init__(self):
        super().__init__()
        self._active = bool(COMMERCIAL_MODE)
        self._repo: BillingRepo | None = None
        self._testers = get_testers()
        # session_id -> pending tokens (наполняется подпиской на usage_ledger)
        self._pending: dict[str, int] = {}

    # ═══════════════════════════════════════════════════════════
    # setup
    # ═══════════════════════════════════════════════════════════

    async def setup(self, app: Application, ctx) -> None:
        if not self._active:
            logger.info(
                "billing: COMMERCIAL_MODE != true — плагин загружен, но пассивен "
                "(игра бесплатна везде; все проверки проходят насквозь)")
        self._repo = BillingRepo(BILLING_DB_PATH)

        # Раздел 6: подписка на реальный usage LLM-вызовов.
        try:
            from libs.ai import usage_ledger
            usage_ledger.subscribe(self._on_usage)
        except Exception as e:
            logger.warning(f"billing: usage_ledger subscribe failed: {e}")

        # /ychwanegu — ручное пополнение игрокам (только TESTERS).
        app.add_handler(CommandHandler("ychwanegu", self.ychwanegu_cmd))
        # billmode:split / billmode:creator_pays — меню способа оплаты /newydd.
        app.add_handler(CallbackQueryHandler(
            self.billmode_callback, pattern=r"^billmode:(split|creator_pays)$"))

        logger.info(
            "billing ready: mode=%s, testers=%d, main_chat=%s, grant=%d, db=%s",
            "ACTIVE" if self._active else "passive", len(self._testers),
            MAIN_CHAT_ID or "не задан", TOKEN_GRANT_AMOUNT, BILLING_DB_PATH)

    # ═══════════════════════════════════════════════════════════
    # Публичный API (вызывается из libs/handlers через get_plugin)
    # ═══════════════════════════════════════════════════════════

    def is_active(self) -> bool:
        """COMMERCIAL_MODE=true и плагин загружен."""
        return self._active

    def is_tester(self, user_id) -> bool:
        try:
            return int(user_id) in self._testers
        except (TypeError, ValueError):
            return False

    def get_balance(self, user_id) -> int:
        if not self._repo:
            return 0
        return self._repo.get_balance(user_id)

    def ensure_player_grant(self, user_id) -> int:
        """Стартовый грант ОДИН РАЗ (идемпотентно). Возвращает баланс."""
        if not self._repo:
            return 0
        return self._repo.ensure_grant(user_id, TOKEN_GRANT_AMOUNT)

    def is_paid_chat(self, chat) -> bool:
        """Платный чат: commercial активен, НЕ ЛС и НЕ общий чат.
        MAIN_CHAT_ID читается через модуль (cfg) — динамически, удобно в тестах."""
        if not self._active or chat is None:
            return False
        if chat.type == "private":
            return False
        main_chat = _cfg.MAIN_CHAT_ID
        if main_chat and int(chat.id) == int(main_chat):
            return False
        return True

    def resolve_payer(self, session, acting_user_id: int) -> int:
        """Чей баланс оплачивает раунд по session.billing_mode.
        "creator_pays" → создатель сессии; иначе ("split") → сам действующий."""
        mode = (getattr(session, "billing_mode", "split") or "split")
        if mode == "creator_pays":
            return int(getattr(session, "creator_id", 0) or 0)
        return int(acting_user_id or 0)

    async def check_game_allowed(self, update: Update, ctx,
                                 session=None) -> tuple[bool, str]:
        """Гейт игрового действия (Дн., /newydd, /ymuno).

        Возвращает (allowed, message). message непуст только при отказе.
        НИКОГДА не бросает исключений наружу — сбой биллинга не должен
        ломать игру (fail-open, но сбой сам логируется)."""
        try:
            return self._check_game_allowed_inner(update, session)
        except Exception as e:
            logger.error(f"billing: check_game_allowed failed (fail-open): {e}")
            return True, ""

    def _check_game_allowed_inner(self, update: Update, session) -> tuple[bool, str]:
        if not self._active:
            return True, ""
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user:
            return True, ""

        # 1) Тестеры — безлимит: токен-гейт не действует НИ В КАКОМ чате.
        if self.is_tester(user.id):
            return True, ""

        # 2) ЛС закрыта для игры для всех, кроме тестеров.
        if chat.type == "private":
            return False, (
                "🔒 **В ЛС играть нельзя.** Игра доступна в общем чате бота и в группах.\n\n"
                "В ЛС работают только: `/creu` (создание персонажа), `/cyfieithu` (перевод), "
                "`/gwneud` (скрытое действие)."
            )

        # 3) Общий чат — бесплатно.
        if not self.is_paid_chat(chat):
            return True, ""

        # 4) Платный чат: грант новичку (один раз за аккаунт) + баланс плательщика.
        self.ensure_player_grant(user.id)
        if session is not None:
            payer_id = self.resolve_payer(session, user.id)
            if payer_id and self.is_tester(payer_id):
                return True, ""  # платит тестер — безлимит
            balance = self.get_balance(payer_id or user.id)
            if balance <= 0:
                mode = (getattr(session, "billing_mode", "split") or "split")
                who = ("создателя сессии" if mode == "creator_pays"
                       else "твой личный баланс")
                return False, (
                    "💰 **Токены исчерпаны — игра остановлена.**\n\n"
                    f"Баланс: **0** ({who}, режим оплаты: "
                    f"{'платит создатель' if mode == 'creator_pays' else 'поровну между игроками'}). "
                    "Это ЖЁСТКАЯ блокировка, а не баг: сессия не продолжится, "
                    "пока баланс не пополнится.\n\n"
                    "💡 Пополнение: обратись к тестерам — они начислят токены "
                    "командой `/ychwanegu` (оплата обрабатывается тестером вне бота)."
                )
        return True, ""

    # ═══════════════════════════════════════════════════════════
    # usage accounting + settle (Раздел 6: реальное списание)
    # ═══════════════════════════════════════════════════════════

    def _on_usage(self, session_id: str, role: str, model: str, tokens: int) -> None:
        try:
            self._pending[session_id] = self._pending.get(session_id, 0) + int(tokens)
        except Exception:
            pass

    def pending_usage(self, session_id: str) -> int:
        return self._pending.get(session_id, 0)

    async def settle_round(self, session_id: str, bot_obj=None, chat_id: int = 0) -> None:
        """Списать накопленные за раунд токены по billing_mode сессии.

        Вызывается из engine._run_db_bot_background СТРОГО один раз — в точке
        «раунд полностью завершён» (после set_db_busy(False) + md-хука).
        Никогда не бросает исключений."""
        try:
            await self._settle_round_inner(session_id, bot_obj, chat_id)
        except Exception as e:
            logger.error(f"billing: settle_round({session_id}) failed: {e}")

    async def _settle_round_inner(self, session_id: str, bot_obj, chat_id) -> None:
        if not self._active or not self._repo:
            return
        tokens = self._pending.pop(session_id, 0)
        if tokens <= 0:
            return
        # Общий чат — бесплатная игра: токены копятся, но не списываются.
        main_chat = _cfg.MAIN_CHAT_ID
        if chat_id and main_chat and int(chat_id) == int(main_chat):
            return
        from libs.handlers._state import db_manager
        db = db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return
        mode = (getattr(session, "billing_mode", "split") or "split")
        note = f"раунд {getattr(session, 'round_number', 0)}, режим {mode}"

        charges: dict[int, int] = {}
        if mode == "creator_pays":
            creator = int(getattr(session, "creator_id", 0) or 0)
            if creator:
                charges[creator] = tokens
        else:
            players = db.get_players(session_id)
            n = len(players) or 1
            share = int(tokens) // n
            for p in players:
                charges[int(p.user_id)] = share

        drained = []
        for uid, cost in charges.items():
            if not uid or self.is_tester(uid):
                continue  # тестеры безлимитны — не списываем и не гейтим
            new_balance = self._repo.charge(uid, cost, session_id, note=note)
            logger.info(
                f"[billing] charge user={uid} cost={cost} balance={new_balance} "
                f"session={session_id} mode={mode}")
            if new_balance <= 0:
                drained.append(uid)

        # Честно предупреждаем чат, если у кого-то кончились токены —
        # следующий Дн. такого игрока будет заблокирован (это не баг).
        if drained and bot_obj is not None and chat_id:
            try:
                names = ", ".join(f"`{uid}`" for uid in drained)
                await bot_obj.send_message(
                    chat_id,
                    "💰 Токены исчерпаны у: " + names + ".\n"
                    "Их следующий ход будет заблокирован до пополнения — "
                    "тестеры могут начислить токены командой /ychwanegu.",
                )
            except Exception as e:
                logger.warning(f"[billing] drained-notice failed: {e}")

    # ═══════════════════════════════════════════════════════════
    # /ychwanegu — ручное пополнение тестерами (костыль до реальной оплаты)
    # ═══════════════════════════════════════════════════════════

    async def ychwanegu_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """/ychwanegu <user_id> <сумма> — только TESTERS. Оплата обрабатывается
        тестером ВНЕ бота; команда лишь начисляет токены игроку. Каждое
        пополнение логируется в ADMIN_CHAT_ID (кто, кому, сколько, когда)."""
        user = update.effective_user
        if not self._active:
            return  # команда существует только в commercial-режиме
        if not user or not self.is_tester(user.id):
            await update.effective_chat.send_message(
                "⛔ Команда доступна только тестерам.")
            return
        args = (ctx.args or [])
        if len(args) != 2 or not args[0].lstrip("-").isdigit() or not args[1].lstrip("-").isdigit():
            await update.effective_chat.send_message(
                "Использование: `/ychwanegu <user_id> <сумма>`\n"
                "Пример: `/ychwanegu 123456789 5000000`")
            return
        target_id, amount = int(args[0]), int(args[1])
        if amount <= 0:
            await update.effective_chat.send_message("⚠️ Сумма должна быть > 0.")
            return
        new_balance = self._repo.topup(
            actor_id=user.id, user_id=target_id, amount=amount,
            note=f"ручное пополнение тестером {user.id}")
        await update.effective_chat.send_message(
            f"✅ Игроку `{target_id}` начислено **{amount}** токенов.\n"
            f"Новый баланс: **{new_balance}**.")

        # Обязательный лог в админ-канал (переиспользуем телеметрический).
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_text = (f"💰 [billing] ПОПОЛНЕНИЕ: тестер {user.first_name or ''} "
                    f"(`{user.id}`) → игрок `{target_id}` +{amount} "
                    f"(баланс: {new_balance}) в {ts}")
        if ADMIN_CHAT_ID:
            try:
                await ctx.bot.send_message(ADMIN_CHAT_ID, log_text)
            except Exception as e:
                logger.warning(f"[billing] admin log failed: {e}")
        logger.info(log_text)

    # ═══════════════════════════════════════════════════════════
    # billmode callback — меню способа оплаты /newydd
    # ═══════════════════════════════════════════════════════════

    async def billmode_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._active:
            return
        user = query.from_user
        chat_id = update.effective_chat.id if update.effective_chat else 0
        from libs.handlers._state import db_manager
        session = db_manager.get_session_by_chat(chat_id)
        if not session:
            await query.edit_message_text("⚠️ Сессия не найдена.")
            return
        if user.id != session.creator_id:
            await query.answer("Только создатель сессии выбирает способ оплаты.",
                               show_alert=True)
            return
        mode = query.data.split(":", 1)[1]
        # Фиксация НАВСЕГДА (Раздел 6: менять нельзя после выбора) —
        # кнопки исчезают при первом нажатии (edit_message_text ниже).
        session.billing_mode = mode
        db = db_manager.get_db(session.id)
        db.update_session(session)
        label = ("💰 Способ оплаты: **поровну между игроками**"
                 if mode == "split"
                 else "👑 Способ оплаты: **полностью платит создатель**")
        await query.edit_message_text(
            label + "\n\n🔒 Выбор зафиксирован на всю жизнь сессии и не меняется.")
