"""
creu — модуль генерации персонажей D&D 5e для Telegram-бота "Кузнец"

Использование в bot.py:
    from creu import get_handler
    application.add_handler(get_handler())

Команда: /creu (только в ЛС)
Поток: книга → раса → подраса → класс → подкласс → уровень → пол → бросок статов → предыстория → мировоззрение → детали → генерация .md → удаление из песочницы
"""

from .handler import get_handler

__all__ = ["get_handler"]
