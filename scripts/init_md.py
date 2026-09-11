#!/usr/bin/env python3
"""Инициализация/статус MD-хранилища (ТЗ «консолидация MD + ready.md»).

Запуск:
    python3 scripts/init_md.py            # создать структуру + _applied, показать статус
    python3 scripts/init_md.py --status   # только статус

Делает то же, что и автоматический ensure_md_layout() при старте бота:
  - MD/{prompts,characters,settings}/
  - MD/ready.md (по умолчанию False)
  - MD/_applied/ — первичная копия источников, если кэша ещё нет
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs import md_store  # noqa: E402


def status() -> None:
    print(f"MD каталог:        {md_store.MD_DIR}")
    print(f"ready.md:          {'True' if md_store.read_ready() else 'False'}  ({md_store.READY_FILE})")
    print(f"_applied:          {md_store.APPLIED_DIR} ({'есть' if os.path.isdir(md_store.APPLIED_DIR) else 'НЕТ'})")
    for cat, src in md_store.SOURCE_DIRS.items():
        n_src = sum(len(files) for _, _, files in os.walk(src)) if os.path.isdir(src) else 0
        app = os.path.join(md_store.APPLIED_DIR, cat)
        n_app = sum(len(files) for _, _, files in os.walk(app)) if os.path.isdir(app) else 0
        print(f"  {cat:<11} источников: {n_src:<3} в _applied: {n_app}")
    print(f"Session copies:    {md_store.SESSIONS_BASE}")


if __name__ == "__main__":
    if "--status" not in sys.argv:
        md_store.ensure_md_layout()
        print("[init_md] MD-хранилище инициализировано.")
    status()
