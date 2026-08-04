"""
ConversationHandler для /creu — пошаговое создание персонажа D&D 5e.

Только в ЛС. HTML форматирование. LLM-генерация кнопок на лету.
Ноль слэш-команд кроме /creu — отмена и пропуск через inline-кнопки.
Жизненный цикл: книга → раса → подраса → класс → подкласс →
уровень → пол → бросок статов → предыстория → мировоззрение → детали → .md → удаление из песочницы.
"""

import io
import json
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ConversationHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from . import dice, llm, storage, formatter

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# FSM States
# ═══════════════════════════════════════════════════════════════
(
    WAITING_SOURCE,
    WAITING_RACE,
    WAITING_SUBRACE,
    WAITING_CLASS,
    WAITING_SUBCLASS,
    WAITING_LEVEL,
    WAITING_GENDER,
    WAITING_STATS,
    WAITING_BACKGROUND,
    WAITING_ALIGNMENT,
    WAITING_DETAILS,
) = range(11)

# ═══════════════════════════════════════════════════════════════
# Callback data константы
# ═══════════════════════════════════════════════════════════════
CUSTOM_BTN = "__custom__"
SKIP_BTN = "__skip__"
ACCEPT_BTN = "__accept__"
REROLL_BTN = "__reroll__"
CANCEL_BTN = "__cancel__"

# ═══════════════════════════════════════════════════════════════
# Hardcoded данные (не меняются)
# ═══════════════════════════════════════════════════════════════

SOURCE_BOOKS = [
    "Player's Handbook",
    "Volo's Guide to Monsters",
    "Mordenkainen's Tome of Foes",
    "Tasha's Cauldron of Everything",
    "Xanathar's Guide to Everything",
    "Monsters of the Multiverse",
    "Fizban's Treasury of Dragons",
]

ALIGNMENTS = [
    "LG", "NG", "CG",
    "LN", "N",  "CN",
    "LE", "NE", "CE",
]

ALIGNMENT_NAMES = {
    "LG": "Законно-добрый", "NG": "Нейтрально-добрый", "CG": "Хаотично-добрый",
    "LN": "Законно-нейтральный", "N": "Нейтральный", "CN": "Хаотично-нейтральный",
    "LE": "Законно-злой", "NE": "Нейтрально-злой", "CE": "Хаотично-злой",
}


# ═══════════════════════════════════════════════════════════════
# Утилиты клавиатур
# ═══════════════════════════════════════════════════════════════

def _build_keyboard(
    options: list[str],
    *,
    allow_custom: bool = True,
    allow_skip: bool = False,
    show_cancel: bool = True,
) -> InlineKeyboardMarkup:
    """Строит inline-кнопки из списка опций."""
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for opt in options:
        row.append(InlineKeyboardButton(text=opt, callback_data=opt))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Нижняя панель: Свой вариант | Пропустить | Отмена
    bottom: list[InlineKeyboardButton] = []
    if allow_custom:
        bottom.append(InlineKeyboardButton(text="\u270f\ufe0f Свой вариант", callback_data=CUSTOM_BTN))
    if allow_skip:
        bottom.append(InlineKeyboardButton(text="\u23ed Пропустить", callback_data=SKIP_BTN))
    if show_cancel and bottom:
        bottom.append(InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN))
    elif show_cancel:
        bottom.append(InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN))
    if bottom:
        buttons.append(bottom)

    return InlineKeyboardMarkup(buttons)


def _build_alignment_keyboard() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for code in ALIGNMENTS:
        label = f"{code} — {ALIGNMENT_NAMES[code]}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=code)])
    buttons.append([InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN)])
    return InlineKeyboardMarkup(buttons)


def _stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="\u2705 Принять", callback_data=ACCEPT_BTN),
            InlineKeyboardButton(text="\U0001f504 Перебросить", callback_data=REROLL_BTN),
            InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN),
        ]
    ])


def _data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("creu_data", {})


def _set_step(context: ContextTypes.DEFAULT_TYPE, step: int) -> None:
    context.user_data["creu_step"] = step


def _html_bold(text: str) -> str:
    safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f"<b>{safe}</b>"


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN)]])


# ═══════════════════════════════════════════════════════════════
# Глобальный обработчик спец-кнопок (cancel/skip)
# Вызывается из каждого callback-хендлера перед основной логикой
# ═══════════════════════════════════════════════════════════════

async def _handle_special(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> int | None:
    """Обрабатывает cancel/skip. Возвращает state или None если это не спец-кнопка."""
    if data == CANCEL_BTN:
        query = update.callback_query
        await query.answer("Отменено")
        context.user_data.pop("creu_data", None)
        context.user_data.pop("creu_step", None)
        await query.edit_message_text(
            "\u274c Создание персонажа отменено.", parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == SKIP_BTN:
        query = update.callback_query
        await query.answer("Пропущено")
        step = context.user_data.get("creu_step", -1)
        d = _data(context)

        if step == WAITING_SUBRACE:
            d["subrace"] = ""
            return await _ask_class(update, context, msg=query.message)

        if step == WAITING_SUBCLASS:
            d["subclass"] = ""
            await query.edit_message_text("Укажи пол персонажа:", parse_mode="HTML")
            _set_step(context, WAITING_GENDER)
            return WAITING_GENDER

        if step == WAITING_DETAILS:
            d["details"] = ""
            return await _do_generate(update, context)

        # В остальных шагах skip не работает
        await query.answer(text="Этот шаг нельзя пропустить", show_alert=True)
        return step

    return None  # Не спец-кнопка


# ═══════════════════════════════════════════════════════════════
# Вспомогательные «переходные» функции
# ═══════════════════════════════════════════════════════════════

async def _ask_race(update: Update, context: ContextTypes.DEFAULT_TYPE, msg=None):
    d = _data(context)
    book = d.get("source_book", "Player's Handbook")
    send = msg or await update.effective_chat.send_message(
        f"\U0001f4d6 Загружаю расы из {_html_bold(book)}...",
        parse_mode="HTML",
    )
    races = await llm.get_races(book)
    if races:
        kb = _build_keyboard(races)
        await send.edit_text(
            f"Расы из {_html_bold(book)}. Выбери расу или введи свою:",
            reply_markup=kb, parse_mode="HTML",
        )
    else:
        await send.edit_text(
            "Не удалось загрузить список рас. Напиши расу текстом:",
            reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
    _set_step(context, WAITING_RACE)
    return WAITING_RACE


async def _ask_subrace(update: Update, context: ContextTypes.DEFAULT_TYPE, msg=None):
    d = _data(context)
    race = d.get("race", "")
    send = msg or await update.effective_chat.send_message(
        f"Проверяю подрасы для {_html_bold(race)}...",
        parse_mode="HTML",
    )
    subraces = await llm.has_subraces(race)
    if subraces:
        kb = _build_keyboard(subraces, allow_skip=True)
        await send.edit_text(
            f"Выбери подрасу для {_html_bold(race)}:",
            reply_markup=kb, parse_mode="HTML",
        )
        _set_step(context, WAITING_SUBRACE)
        return WAITING_SUBRACE
    else:
        await send.edit_text(
            f"У расы {race} нет подрас. Переходим к классу.", parse_mode="HTML",
        )
        return await _ask_class(update, context, msg=send)


async def _ask_class(update: Update, context: ContextTypes.DEFAULT_TYPE, msg=None):
    send = msg or await update.effective_chat.send_message(
        "\U0001f3ae Загружаю классы...", parse_mode="HTML",
    )
    classes = await llm.get_classes()
    if classes:
        kb = _build_keyboard(classes)
        await send.edit_text(
            "Выбери класс или введи свой:", reply_markup=kb, parse_mode="HTML",
        )
    else:
        await send.edit_text(
            "Не удалось загрузить список классов. Напиши класс текстом:",
            reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
    _set_step(context, WAITING_CLASS)
    return WAITING_CLASS


async def _ask_subclass(update: Update, context: ContextTypes.DEFAULT_TYPE, msg=None):
    d = _data(context)
    cls = d.get("class_", "")
    level = d.get("level", 1)
    send = msg or await update.effective_chat.send_message(
        f"\U0001f50d Загружаю подклассы для {_html_bold(cls)}...",
        parse_mode="HTML",
    )
    subclasses = await llm.get_subclasses(cls)
    if subclasses:
        kb = _build_keyboard(subclasses, allow_skip=True)
        await send.edit_text(
            f"Подкласс для {_html_bold(cls)} ({level} ур.). Выбери:",
            reply_markup=kb, parse_mode="HTML",
        )
        _set_step(context, WAITING_SUBCLASS)
        return WAITING_SUBCLASS
    else:
        await send.edit_text(
            "Не удалось загрузить подклассы. Напиши текстом:",
            reply_markup=_build_keyboard([], allow_skip=True, allow_custom=False),
            parse_mode="HTML",
        )
        _set_step(context, WAITING_SUBCLASS)
        return WAITING_SUBCLASS


async def _ask_background(update: Update, context: ContextTypes.DEFAULT_TYPE, msg=None):
    send = msg or await update.effective_chat.send_message(
        "\U0001f4dc Загружаю предыстории...", parse_mode="HTML",
    )
    bgs = await llm.get_backgrounds()
    if bgs:
        kb = _build_keyboard(bgs)
        await send.edit_text("Выбери предысторию:", reply_markup=kb, parse_mode="HTML")
    else:
        await send.edit_text(
            "Напиши предысторию текстом:",
            reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
    _set_step(context, WAITING_BACKGROUND)
    return WAITING_BACKGROUND


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def creu_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик /creu — только в ЛС."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "\u274c Создание персонажа доступно только в личных сообщениях.\n"
            "Напиши мне в ЛС, чтобы начать."
        )
        return ConversationHandler.END

    context.user_data["creu_data"] = {}
    context.user_data["creu_step"] = WAITING_SOURCE

    kb = _build_keyboard(SOURCE_BOOKS, allow_skip=False)
    await update.message.reply_text(
        "\u2694\ufe0f <b>Создание персонажа D&D 5e</b>\n\n"
        "Из какой книги берём расу?",
        reply_markup=kb,
        parse_mode="HTML",
    )
    return WAITING_SOURCE


# ═══════════════════════════════════════════════════════════════
# STATE HANDLERS
# ═══════════════════════════════════════════════════════════════

# ── 1. Источник ──────────────────────────────────────────────
async def on_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    if query.data == CUSTOM_BTN:
        await query.edit_message_text(
            "Напиши название книги:", reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
        return WAITING_SOURCE

    _data(context)["source_book"] = query.data
    return await _ask_race(update, context, msg=query.message)


async def on_source_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["source_book"] = update.message.text.strip()
    return await _ask_race(update, context)


# ── 2. Раса ──────────────────────────────────────────────────
async def on_race_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    if query.data == CUSTOM_BTN:
        await query.edit_message_text(
            "Напиши расу текстом:", reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
        return WAITING_RACE

    _data(context)["race"] = query.data
    return await _ask_subrace(update, context, msg=query.message)


async def on_race_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["race"] = update.message.text.strip()
    return await _ask_subrace(update, context)


# ── 3. Подраса (пропускается если нет) ───────────────────────
async def on_subrace_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    _data(context)["subrace"] = query.data
    return await _ask_class(update, context, msg=query.message)


async def on_subrace_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["subrace"] = update.message.text.strip()
    return await _ask_class(update, context)


# ── 4. Класс ─────────────────────────────────────────────────
async def on_class_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    if query.data == CUSTOM_BTN:
        await query.edit_message_text(
            "Напиши класс текстом:", reply_markup=_cancel_keyboard(), parse_mode="HTML",
        )
        return WAITING_CLASS

    _data(context)["class_"] = query.data
    await query.edit_message_text(
        f"Класс: {_html_bold(query.data)}\n\nУкажи уровень персонажа (1-20):",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    _set_step(context, WAITING_LEVEL)
    return WAITING_LEVEL


async def on_class_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["class_"] = update.message.text.strip()
    await update.message.reply_text(
        f"Класс: {_html_bold(update.message.text.strip())}\n\nУкажи уровень персонажа (1-20):",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    _set_step(context, WAITING_LEVEL)
    return WAITING_LEVEL


# ── 5. Уровень ───────────────────────────────────────────────
async def on_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        level = int(text)
        if not 1 <= level <= 20:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "\u274c Уровень должен быть числом от 1 до 20. Попробуй ещё раз:",
            reply_markup=_cancel_keyboard(),
        )
        return WAITING_LEVEL

    _data(context)["level"] = level

    if level >= 3:
        return await _ask_subclass(update, context)
    else:
        await update.message.reply_text(
            "Укажи пол персонажа:",
            reply_markup=_cancel_keyboard(),
            parse_mode="HTML",
        )
        _set_step(context, WAITING_GENDER)
        return WAITING_GENDER


# ── 6. Подкласс (только при уровне >= 3) ─────────────────────
async def on_subclass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    if query.data == CUSTOM_BTN:
        await query.edit_message_text(
            "Напиши подкласс текстом:",
            reply_markup=_build_keyboard([], allow_skip=True, allow_custom=False),
            parse_mode="HTML",
        )
        return WAITING_SUBCLASS

    _data(context)["subclass"] = query.data
    await query.edit_message_text(
        "Укажи пол персонажа:", reply_markup=_cancel_keyboard(), parse_mode="HTML",
    )
    _set_step(context, WAITING_GENDER)
    return WAITING_GENDER


async def on_subclass_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["subclass"] = update.message.text.strip()
    await update.message.reply_text(
        "Укажи пол персонажа:",
        reply_markup=_cancel_keyboard(),
        parse_mode="HTML",
    )
    _set_step(context, WAITING_GENDER)
    return WAITING_GENDER


# ── 7. Пол ───────────────────────────────────────────────────
async def on_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gender = update.message.text.strip()
    _data(context)["gender"] = gender

    stats = dice.roll_all_stats()
    _data(context)["rolled_stats"] = stats

    stats_text = (
        f"\U0001f3b2 <b>Бросок характеристик</b> (4d6 drop lowest)\n\n"
        f"СИЛ: <b>{stats[0]}</b>  |  ЛОВ: <b>{stats[1]}</b>  |  ТЕЛ: <b>{stats[2]}</b>\n"
        f"ИНТ: <b>{stats[3]}</b>  |  МУД: <b>{stats[4]}</b>  |  ХАР: <b>{stats[5]}</b>"
    )
    await update.message.reply_text(stats_text, reply_markup=_stats_keyboard(), parse_mode="HTML")
    _set_step(context, WAITING_STATS)
    return WAITING_STATS


# ── 8. Бросок статов (принять / перебросить / отмена) ─────────
async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CANCEL_BTN:
        context.user_data.pop("creu_data", None)
        context.user_data.pop("creu_step", None)
        await query.edit_message_text("\u274c Создание персонажа отменено.", parse_mode="HTML")
        return ConversationHandler.END

    if query.data == REROLL_BTN:
        stats = dice.roll_all_stats()
        _data(context)["rolled_stats"] = stats

        stats_text = (
            f"\U0001f3b2 <b>Новый бросок</b>\n\n"
            f"СИЛ: <b>{stats[0]}</b>  |  ЛОВ: <b>{stats[1]}</b>  |  ТЕЛ: <b>{stats[2]}</b>\n"
            f"ИНТ: <b>{stats[3]}</b>  |  МУД: <b>{stats[4]}</b>  |  ХАР: <b>{stats[5]}</b>"
        )
        await query.edit_message_text(stats_text, reply_markup=_stats_keyboard(), parse_mode="HTML")
        return WAITING_STATS

    # ACCEPT
    return await _ask_background(update, context, msg=query.message)


# ── 9. Предыстория ──────────────────────────────────────────
async def on_background_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    if query.data == CUSTOM_BTN:
        await query.edit_message_text(
            "Напиши предысторию текстом:",
            reply_markup=_cancel_keyboard(),
            parse_mode="HTML",
        )
        return WAITING_BACKGROUND

    _data(context)["background"] = query.data
    await query.edit_message_text(
        "Выбери мировоззрение:",
        reply_markup=_build_alignment_keyboard(),
        parse_mode="HTML",
    )
    _set_step(context, WAITING_ALIGNMENT)
    return WAITING_ALIGNMENT


async def on_background_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["background"] = update.message.text.strip()
    await update.message.reply_text(
        "Выбери мировоззрение:",
        reply_markup=_build_alignment_keyboard(),
        parse_mode="HTML",
    )
    _set_step(context, WAITING_ALIGNMENT)
    return WAITING_ALIGNMENT


# ── 10. Мировоззрение ────────────────────────────────────────
async def on_alignment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == CANCEL_BTN:
        context.user_data.pop("creu_data", None)
        context.user_data.pop("creu_step", None)
        await query.edit_message_text("\u274c Создание персонажа отменено.", parse_mode="HTML")
        return ConversationHandler.END

    alignment = query.data
    name = ALIGNMENT_NAMES.get(alignment, alignment)
    _data(context)["alignment"] = alignment

    skip_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="\u23ed Пропустить", callback_data=SKIP_BTN)],
        [InlineKeyboardButton(text="\u274c Отмена", callback_data=CANCEL_BTN)],
    ])
    await query.edit_message_text(
        f"Мировоззрение: {_html_bold(f'{alignment} — {name}')}\n\n"
        "Хочешь добавить детали о персонаже?\n"
        "Внешность, характер, мотивация, что угодно.",
        reply_markup=skip_kb,
        parse_mode="HTML",
    )
    _set_step(context, WAITING_DETAILS)
    return WAITING_DETAILS


# ── 11. Дополнительные детали ────────────────────────────────
async def on_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    special = await _handle_special(update, context, query.data)
    if special is not None:
        return special

    # Если пришла какая-то другая кнопка (не должно быть, но на всякий)
    return WAITING_DETAILS


async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _data(context)["details"] = update.message.text.strip()
    return await _do_generate(update, context)


# ═══════════════════════════════════════════════════════════════
# ГЕНЕРАЦИЯ ПЕРСОНАЖА
# ═══════════════════════════════════════════════════════════════

async def _do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = _data(context)
    chat = update.effective_chat

    status_msg = await chat.send_message(
        "\u2699\ufe0f Генерирую персонажа... Это может занять 10-20 секунд.",
        parse_mode="HTML",
    )

    try:
        char_json = await llm.generate_character(d)

        user_id = update.effective_user.id
        name = char_json.get("name", "Безымянный")
        sandbox_id = storage.save_to_sandbox(user_id, name, json.dumps(char_json, ensure_ascii=False))

        md_content = formatter.format_character_md(char_json)

        filename = f"{name.replace(' ', '_')}_L{d.get('level', 1)}.md"
        await chat.send_document(
            document=io.BytesIO(md_content.encode("utf-8")),
            filename=filename,
            caption=(
                f"\u2705 <b>{name}</b> готов!\n"
                f"{char_json.get('race', '')} {char_json.get('class', '')} {d.get('level', 1)} ур."
            ),
            parse_mode="HTML",
        )

        storage.delete_from_sandbox(sandbox_id)

        await status_msg.edit_text(
            f"\u2705 Лист персонажа <b>{name}</b> создан и доставлен.\n"
            "\U0001f5d1\ufe0f Песочница очищена.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"[creu] Генерация упала: {e}", exc_info=True)
        await status_msg.edit_text(
            f"\u274c Ошибка при генерации персонажа.\n\n"
            f"<code>{e}</code>\n\n"
            "Попробуй /creu ещё раз.",
            parse_mode="HTML",
        )

    context.user_data.pop("creu_data", None)
    context.user_data.pop("creu_step", None)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════
# PUBLIC API — ConversationHandler
# ═══════════════════════════════════════════════════════════════

def get_handler() -> ConversationHandler:
    """Возвращает готовый ConversationHandler. Единственная команда — /creu."""
    return ConversationHandler(
        entry_points=[CommandHandler("creu", creu_start)],
        states={
            WAITING_SOURCE: [
                CallbackQueryHandler(on_source_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_source_text),
            ],
            WAITING_RACE: [
                CallbackQueryHandler(on_race_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_race_text),
            ],
            WAITING_SUBRACE: [
                CallbackQueryHandler(on_subrace_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_subrace_text),
            ],
            WAITING_CLASS: [
                CallbackQueryHandler(on_class_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_class_text),
            ],
            WAITING_LEVEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_level),
            ],
            WAITING_SUBCLASS: [
                CallbackQueryHandler(on_subclass_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_subclass_text),
            ],
            WAITING_GENDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_gender),
            ],
            WAITING_STATS: [
                CallbackQueryHandler(on_stats_callback),
            ],
            WAITING_BACKGROUND: [
                CallbackQueryHandler(on_background_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_background_text),
            ],
            WAITING_ALIGNMENT: [
                CallbackQueryHandler(on_alignment_callback),
            ],
            WAITING_DETAILS: [
                CallbackQueryHandler(on_details_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_details),
            ],
        },
        fallbacks=[],  # Ноль команд-фоллбэков
        per_chat=True,
        per_user=True,
    )
