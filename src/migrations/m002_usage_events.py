"""Создаёт таблицу usage_events для учёта потребления токенов Claude SDK."""

from pathlib import Path

import aiosqlite


async def apply(data_dir: Path) -> None:
    db_path = data_dir / "db.sqlite"
    db = await aiosqlite.connect(str(db_path))
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                telegram_id INTEGER,
                session_id TEXT,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL,
                duration_ms INTEGER
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts)"
        )
        await db.commit()
    finally:
        await db.close()
