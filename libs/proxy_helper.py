"""
libs.proxy_helper — единый прокси-хелпер для Telegram + LLM.

Поддерживаемые форматы URL:
    http://host:port
    http://user:pass@host:port
    https://host:port
    socks5://host:port
    socks5://user:pass@host:port
    socks5h://host:port           (DNS через прокси — РЕКОМЕНДУЕТСЯ)
    socks4://host:port

Зависимости (ставить один раз):
    HTTP/HTTPS прокси — ничего дополнительно (httpx и aiohttp умеют из коробки).
    SOCKS для Telegram (httpx)  → pip install "httpx[socks]"
    SOCKS для LLM (aiohttp)     → pip install aiohttp-socks

Конфигурация — через .env:
    GLOBAL_PROXY=socks5h://user:pass@1.2.3.4:1080
    TELEGRAM_PROXY=             (точечно перекрывает GLOBAL_PROXY)
    OPENAI_PROXY=               (точечно перекрывает GLOBAL_PROXY)

Логика резолва effective-прокси (по убыванию приоритета):
    1. TELEGRAM_PROXY / OPENAI_PROXY  (точечно)
    2. GLOBAL_PROXY                    (глобально)
    3. ALL_PROXY / HTTPS_PROXY / HTTP_PROXY  (системные env-переменные)
       — Hiddify/v2rayN/clash обычно прописывают их сами
    4. Прямое подключение

Дополнительно для aiohttp:
    - aiohttp_session_kwargs() всегда возвращает {"trust_env": True},
      чтобы aiohttp нативно подхватывал HTTP_PROXY/HTTPS_PROXY/ALL_PROXY,
      даже если вы не задали GLOBAL_PROXY.
    - Для SOCKS5 используется ProxyConnector (trust_env бесполезен для SOCKS).

Hiddify/v2rayN на ПК (типичные порты):
    - 10808 — SOCKS5 (использовать socks5h://127.0.0.1:10808)
    - 10809 — HTTP    (использовать http://127.0.0.1:10809)
    - 12334 — SOCKS5 (некоторые сборки v2rayN)
    - 7890  — HTTP    (Clash)

Точки применения:
    - core/bootstrap.py → apply_telegram_proxy(builder)
    - libs/ai/client.py → aiohttp_session_kwargs() + aiohttp_request_kwargs()
    - libs/creu/llm.py  → то же самое
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Чтение конфигурации
# ─────────────────────────────────────────────────────────────────

def _env(name: str) -> Optional[str]:
    """Вернёт значение env-переменной или None если пусто/не задана."""
    val = os.environ.get(name, "").strip()
    return val or None


def _resolve(global_val: Optional[str], specific: Optional[str]) -> Optional[str]:
    """Specific перекрывает global. Пусто → None."""
    return specific or global_val or None


def _system_proxy() -> Optional[str]:
    """
    Системный fallback: ALL_PROXY > HTTPS_PROXY > HTTP_PROXY.

    Hiddify/v2rayN/clash при включении часто прописывают эти переменные
    на уровне процесса/системы. Если пользователь не задал GLOBAL_PROXY,
    мы автоматически подхватываем системный.
    """
    return _env("ALL_PROXY") or _env("all_proxy") or \
           _env("HTTPS_PROXY") or _env("https_proxy") or \
           _env("HTTP_PROXY") or _env("http_proxy")


def get_global_proxy() -> Optional[str]:
    """GLOBAL_PROXY или, если пусто, системный env-fallback.

    Приоритет: GLOBAL_PROXY > системные env (ALL_PROXY > HTTPS_PROXY > HTTP_PROXY).
    """
    return _env("GLOBAL_PROXY") or _system_proxy()


def get_telegram_proxy() -> Optional[str]:
    """Вернёт effective-прокси для Telegram.

    Порядок: TELEGRAM_PROXY > GLOBAL_PROXY > системный env.
    """
    return _resolve(get_global_proxy(), _env("TELEGRAM_PROXY"))


def get_openai_proxy() -> Optional[str]:
    """Вернёт effective-прокси для LLM-клиентов.

    Порядок: OPENAI_PROXY > GLOBAL_PROXY > системный env.
    """
    return _resolve(get_global_proxy(), _env("OPENAI_PROXY"))


def _is_socks(url: Optional[str]) -> bool:
    return bool(url) and url.lower().startswith(
        ("socks5://", "socks5h://", "socks4://", "socks4a://")
    )


# ─────────────────────────────────────────────────────────────────
# Telegram — python-telegram-bot v21+ (httpx под капотом)
# ─────────────────────────────────────────────────────────────────

def apply_telegram_proxy(builder):
    """
    Применить прокси к ApplicationBuilder из python-telegram-bot.

    Использование в core/bootstrap.py:
        from libs.proxy_helper import apply_telegram_proxy
        builder = apply_telegram_proxy(
            ApplicationBuilder()
            .token(token)
            .concurrent_updates(True)
            .post_init(...)
            .post_shutdown(...)
        )
        app = builder.build()
    """
    proxy = get_telegram_proxy()
    if not proxy:
        logger.info("[proxy] Telegram: прямое подключение (без прокси)")
        return builder

    try:
        from telegram.request import HTTPXRequest
    except ImportError:
        logger.error("[proxy] python-telegram-bot не установлен — прокси не применён")
        return builder

    if _is_socks(proxy):
        try:
            import socksio  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "Для SOCKS-прокси установите: pip install 'httpx[socks]'"
            )

    builder = builder.request(HTTPXRequest(proxy=proxy))
    builder = builder.get_updates_request(HTTPXRequest(proxy=proxy))
    masked = _mask_proxy(proxy)
    logger.info("[proxy] Telegram: трафик через %s", masked)
    return builder


# ─────────────────────────────────────────────────────────────────
# LLM — aiohttp (libs/ai/client.py + libs/creu/llm.py)
# ─────────────────────────────────────────────────────────────────

def aiohttp_session_kwargs() -> dict:
    """
    Kwargs для aiohttp.ClientSession(...).

    Возвращает:
      - Для SOCKS5:  {"connector": ProxyConnector(...), "trust_env": True}
      - Для HTTP:    {"trust_env": True}
        (aiohttp сам прочитает HTTP_PROXY/HTTPS_PROXY/ALL_PROXY из env)
      - Для прямого: {"trust_env": True}
        (если Hiddify прописал системные env-переменные, aiohttp их подхватит)

    trust_env=True критичен для Hiddify: когда Hiddify выставляет
    HTTP_PROXY/HTTPS_PROXY в окружение, aiohttp по умолчанию их игнорирует.

    Важно: aiohttp_socks не понимает схему socks5h:// напрямую.
    Мы конвертируем socks5h:// → socks5:// + rdns=True
    (rdns=True = DNS резолвится через прокси, что и означает суффикс 'h').
    """
    proxy = get_openai_proxy()
    kwargs = {"trust_env": True}

    if proxy and _is_socks(proxy):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            raise RuntimeError(
                "Для SOCKS-прокси установите: pip install aiohttp-socks"
            )
        # socks5h:// → socks5:// + rdns=True (DNS через прокси)
        # socks4a:// → socks4:// + rdns=True
        rdns = False
        url = proxy
        if url.lower().startswith("socks5h://"):
            url = "socks5://" + url[len("socks5h://"):]
            rdns = True
        elif url.lower().startswith("socks4a://"):
            url = "socks4://" + url[len("socks4a://"):]
            rdns = True
        try:
            connector = ProxyConnector.from_url(url, rdns=rdns)
        except TypeError:
            # Старые версии aiohttp_socks не принимают rdns в from_url
            connector = ProxyConnector.from_url(url)
        kwargs["connector"] = connector

    return kwargs


def aiohttp_request_kwargs() -> dict:
    """
    Kwargs для session.post(...) / session.get(...).

    Возвращает:
      - Для HTTP-прокси: {"proxy": "http://..."}
        (явная передача, перекрывает trust_env на уровне запроса)
      - Для SOCKS5: {} (прокси зашит в connector-е сессии)
      - Для прямого: {} (полагаемся на trust_env=True в сессии)
    """
    proxy = get_openai_proxy()
    if not proxy or _is_socks(proxy):
        return {}
    return {"proxy": proxy}


# ─────────────────────────────────────────────────────────────────
# Диагностика
# ─────────────────────────────────────────────────────────────────

def _mask_proxy(url: str) -> str:
    """Скрыть user:pass в логах. Если без пароля — вернуть как есть."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    prefix = f"{scheme}://" if scheme else ""
    return f"{prefix}{creds}@{host}"


def log_status() -> None:
    """Логировать текущую конфигурацию прокси. Вызвать из main.py после load .env."""
    tg = get_telegram_proxy()
    ai = get_openai_proxy()
    sys_proxy = _system_proxy()

    if not tg and not ai:
        logger.info("[proxy] Прокси отключён — все подключения прямые")
        if sys_proxy:
            logger.info("[proxy] (но в окружении найден системный: %s — игнорируется, "
                        "т.к. GLOBAL_PROXY пуст)", _mask_proxy(sys_proxy))
        return

    logger.info("[proxy] Telegram:    %s", _mask_proxy(tg) if tg else "(прямое)")
    logger.info("[proxy] LLM/aiohttp: %s", _mask_proxy(ai) if ai else "(прямое)")
    if sys_proxy and not _env("GLOBAL_PROXY"):
        logger.info("[proxy] Источник: системный env (Hiddify/v2rayN/clash?)")
    elif _env("GLOBAL_PROXY"):
        logger.info("[proxy] Источник: GLOBAL_PROXY из .env")
