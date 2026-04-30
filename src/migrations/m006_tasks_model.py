"""Добавляет колонку `model` в `tasks` для override модели per scheduled-task."""

import sqlite3
from pathlib import Path

import aiosqlite


async def apply(data_dir: Path) -> None:
    db_path = data_dir / "db.sqlite"
    db = await aiosqlite.connect(str(db_path))
    try:
        try:
            await db.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
            await db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    finally:
        await db.close()
