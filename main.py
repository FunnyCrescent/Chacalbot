"""
main.py — точка входа D&D бота с плагинной архитектурой.

Запуск:
    python main.py
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import AppConfig, Bootstrap, PluginManager


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    config = AppConfig.load(base_dir=PROJECT_ROOT)
    configure_logging(config.log_level)
    log = logging.getLogger("main")
    log.info("=== D&D Bot (plugin architecture v0.2) starting ===")
    log.info("Config: %s", config)

    try:
        config.validate_required()
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)

    manager = PluginManager(plugins_dir=config.plugins_dir, config=config)
    log.info("Discovering plugins in %s ...", config.plugins_dir)
    manager.discover()
    manager.resolve()
    log.info(manager.status_table())

    bootstrap = Bootstrap(config=config, manager=manager)
    app = bootstrap.build()

    log.info("Starting polling...")
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
