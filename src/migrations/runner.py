"""
Простая миграционная система для SQLite.

Миграции — модули m001_*, m002_* в этом пакете.
Каждый экспортирует `async def apply(data_dir: Path)`.
Применённые миграции трекаются в data_dir/.migrations.json.
"""

import importlib
import json
import pkgutil
from pathlib import Path

from loguru import logger

_RECORD_FILE = ".migrations.json"


def _load_applied(data_dir: Path) -> set[str]:
    path = data_dir / _RECORD_FILE
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def _save_applied(data_dir: Path, applied: set[str]) -> None:
    path = data_dir / _RECORD_FILE
    path.write_text(json.dumps(sorted(applied), indent=2) + "\n")


async def run_migrations(data_dir: Path) -> None:
    """Обнаружить и запустить pending-миграции."""
    import src.migrations as pkg

    applied = _load_applied(data_dir)

    # Собираем модули m001_*, m002_*, ... (сортировка по имени = по порядку)
    names: list[str] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("m") and len(info.name) > 4 and info.name[1:4].isdigit():
            names.append(info.name)

    for name in sorted(names):
        if name in applied:
            continue
        mod = importlib.import_module(f"src.migrations.{name}")
        await mod.apply(data_dir)
        applied.add(name)
        _save_applied(data_dir, applied)
        logger.info(f"Migration applied: {name}")
