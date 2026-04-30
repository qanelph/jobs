"""Добавляет колонку `recipient_ids` в `trigger_subscriptions` (triggers.sqlite)."""

import sqlite3
from pathlib import Path

import aiosqlite


async def apply(data_dir: Path) -> None:
    db_path = data_dir / "triggers.sqlite"
    db = await aiosqlite.connect(str(db_path))
    try:
        try:
            await db.execute("ALTER TABLE trigger_subscriptions ADD COLUMN recipient_ids TEXT")
            await db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    finally:
        await db.close()
