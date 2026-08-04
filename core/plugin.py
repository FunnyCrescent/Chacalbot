"""
core.plugin — базовый класс плагина.

Минимальный контракт:
    class Plugin:
        name: str          — уникальный идентификатор
        version: str       — SemVer
        description: str   — короткое описание
        depends_on: list[str] — имена плагинов, которые должны быть загружены раньше
        provides: list[str]   — extension points, которые этот плагин объявляет

        async def setup(self, app, ctx) -> None:
            '''Регистрирует handlers, подключается к сервисам, создаёт задачи.'''
            ...
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    from .context import PluginContext

Hook = Callable[..., Awaitable[Any]]


@dataclass
class PluginMeta:
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    extends: dict[str, list[Hook]] = field(default_factory=dict)
    author: str = ""
    license: str = ""
    url: str = ""


class Plugin:
    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    depends_on: list[str] = []
    provides: list[str] = []
    author: str = ""
    license: str = ""
    url: str = ""

    def __init__(self):
        if not self.name:
            raise TypeError(f"Plugin subclass {type(self).__name__} must set 'name'")

    async def setup(self, app: "Application", ctx: "PluginContext") -> None:
        raise NotImplementedError(
            f"Plugin {self.name} must implement async setup(app, ctx)"
        )

    def __repr__(self) -> str:
        return f"<Plugin {self.name} v{self.version}>"

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name=self.name, version=self.version, description=self.description,
            depends_on=list(self.depends_on), provides=list(self.provides),
            author=self.author, license=self.license, url=self.url,
        )
