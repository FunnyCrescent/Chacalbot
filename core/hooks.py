"""
core.hooks — Extension Points (mixins).

Плагин декларирует `provides = ["narrator.on_message"]` — extension point.
Другой плагин вызывает `ctx.hooks.register("narrator.on_message", fn)`.
Хозяин вызывает `await ctx.hooks.trigger("narrator.on_message", *args, **kwargs)`.
"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Hook = Callable[..., Awaitable[Any]]


class HookRegistry:
    """Реестр hook'ов для mixin-паттерна (single-threaded asyncio)."""

    def __init__(self):
        self._hooks: dict[str, list[tuple[str, Hook]]] = defaultdict(list)
        self._declared_points: set[str] = set()

    def declare(self, point: str, owner: str) -> None:
        self._declared_points.add(point)
        logger.debug("Extension point declared: %s (by %s)", point, owner)

    def register(self, point: str, hook: Hook, plugin_name: str = "?") -> None:
        if point not in self._declared_points:
            logger.warning(
                "Plugin %s registers hook for undeclared point '%s'. "
                "Owner plugin may be disabled or missing.",
                plugin_name, point,
            )
        self._hooks[point].append((plugin_name, hook))
        logger.debug("Hook registered: %s <- %s", point, plugin_name)

    def unregister_all(self, plugin_name: str) -> None:
        for point in list(self._hooks.keys()):
            self._hooks[point] = [
                (n, h) for (n, h) in self._hooks[point] if n != plugin_name
            ]
            if not self._hooks[point]:
                del self._hooks[point]

    def has(self, point: str) -> bool:
        return bool(self._hooks.get(point))

    def list_hooks(self, point: str) -> list[tuple[str, Hook]]:
        return list(self._hooks.get(point, []))

    async def trigger(self, point: str, *args, **kwargs) -> list[Any]:
        results: list[Any] = []
        for plugin_name, hook in self._hooks.get(point, []):
            try:
                result = await hook(*args, **kwargs)
                results.append(result)
            except Exception:
                logger.exception("Hook %s for point '%s' raised", plugin_name, point)
                results.append(None)
        return results

    async def trigger_first(self, point: str, *args, **kwargs) -> Any:
        for plugin_name, hook in self._hooks.get(point, []):
            try:
                result = await hook(*args, **kwargs)
                if result is not None:
                    return result
            except Exception:
                logger.exception("Hook %s for '%s' failed", plugin_name, point)
        return None

    async def trigger_parallel(self, point: str, *args, **kwargs) -> list[Any]:
        hooks = self._hooks.get(point, [])
        if not hooks:
            return []
        coros = []
        for plugin_name, hook in hooks:
            async def _safe_call(h=hook, n=plugin_name):
                try:
                    return await hook(*args, **kwargs)
                except Exception:
                    logger.exception("Hook %s for '%s' failed", n, point)
                    return None
            coros.append(_safe_call())
        return await asyncio.gather(*coros)

    def snapshot(self) -> dict[str, list[str]]:
        return {point: [n for (n, _) in hooks] for point, hooks in self._hooks.items()}
