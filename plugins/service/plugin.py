"""
service — сервисные команды: баланс агрегатора (/cydbwysedd), персональные
бинды (/rhwymo, /datgysylltu), нормализация регистра команд и подсказка на
неизвестные команды.

Загружается ПОСЛЕДНИМ (зависит от всех остальных функциональных плагинов),
чтобы:
  - group-0 unknown_command_hint срабатывал только когда ни одна команда не
    совпала;
  - get_registered_commands() видел полный список команд.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from core.plugin import Plugin
from libs.handlers.service_cmds import (
    balance_cmd,
    bind_cmd,
    unbind_cmd,
    bind_resolver,
    command_case_normalizer,
    unknown_command_hint,
    get_registered_commands,
)

logger = logging.getLogger(__name__)

# Синее меню команд в Telegram (set_my_commands раньше вообще не вызывался —
# меню было пустым, из-за чего команды казались «не присоединёнными»).
_MENU_COMMANDS = [
    BotCommand("help", "Полная справка по всем командам (Full help)"),
    BotCommand("dechrau", "Краткая шпаргалка"),
    BotCommand("newydd", "Создать сессию"),
    BotCommand("ymuno", "Присоединиться к сессии"),
    BotCommand("cymeriad", "Загрузить/выбрать персонажа"),
    BotCommand("creu", "Создать персонажа пошагово"),
    BotCommand("dndcychwyn", "Начать игру (генерация мира)"),
    BotCommand("rholio", "Бросок навыка/спасброска"),
    BotCommand("gofyn", "Задать вопрос нейросети"),
    BotCommand("sgipio", "Пропустить свой ход (или @ник — чужой, админ)"),
    BotCommand("diddymu", "Отменить своё действие"),
    BotCommand("taflen", "Лист персонажа"),
    BotCommand("iechyd", "Показать HP"),
    BotCommand("aur", "Показать золото"),
    BotCommand("eiddo", "Показать инвентарь"),
    BotCommand("cwest", "Активные квесты"),
    BotCommand("statws", "Статус сессии"),
    BotCommand("cydbwysedd", "Баланс LLM-агрегатора"),
    BotCommand("rhwymo", "Свой псевдоним команды (в ЛС)"),
    BotCommand("datgysylltu", "Удалить свой псевдоним (в ЛС)"),
    BotCommand("cymorth", "Полная справка (валлийский аналог /help)"),
]


class ServicePlugin(Plugin):
    name = "service"
    version = "0.1.0"
    description = "Balance check / personal command binds / case-insensitive commands / unknown-cmd hint"
    author = "bugfix batch"
    # Загружаем последним: unknown_command_hint должен встать В КОНЦЕ группы 0.
    depends_on = [
        "persistence", "session-core", "ai-engine",
        "lobby-session", "character", "character-state", "world-state",
        "dice", "settings-catalog", "srd-reference", "translator", "admin",
    ]

    async def setup(self, app: Application, ctx) -> None:
        # ── Group -2: регистр команд не важен (/CYMERIAD == /cymeriad) ──
        app.add_handler(MessageHandler(filters.COMMAND, command_case_normalizer), group=-2)
        # ── Group -1: персональные бинды (/character → /cymeriad) ──
        app.add_handler(MessageHandler(filters.COMMAND, bind_resolver), group=-1)

        # ── Group 0: команды ──
        app.add_handler(CommandHandler("cydbwysedd", balance_cmd))
        app.add_handler(CommandHandler("rhwymo", bind_cmd))
        app.add_handler(CommandHandler("datgysylltu", unbind_cmd))

        # ── Group 0, ПОСЛЕДНИМ: подсказка на неизвестные команды ──
        app.add_handler(MessageHandler(filters.COMMAND, unknown_command_hint))

        # ── Синее меню команд ──
        try:
            await app.bot.set_my_commands(_MENU_COMMANDS)
            logger.info("service: set_my_commands — %d commands published", len(_MENU_COMMANDS))
        except Exception as e:
            logger.warning("service: set_my_commands failed: %s", e)

        total = len(get_registered_commands(app))
        logger.info("service: /cydbwysedd /rhwymo /datgysylltu + case-normalizer + bind resolver + unknown hint registered (total known commands: %d)", total)
