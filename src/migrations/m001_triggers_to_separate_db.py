"""Перенос trigger_subscriptions из db.sqlite в triggers.sqlite."""

from pathlib import Path

import aiosqlite


async def apply(data_dir: Path) -> None:
    old_path = data_dir / "db.sqlite"
    if not old_path.exists():
        return

    old_db = await aiosqlite.connect(str(old_path))
    old_db.row_factory = aiosqlite.Row
    try:
        cursor = await old_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trigger_subscriptions'"
        )
        if not await cursor.fetchone():
            return

        cursor = await old_db.execute("SELECT * FROM trigger_subscriptions")
        rows = await cursor.fetchall()

        if rows:
            new_db = await aiosqlite.connect(str(data_dir / "triggers.sqlite"))
            try:
                await new_db.execute("PRAGMA journal_mode=WAL")
                await new_db.execute("""
                    CREATE TABLE IF NOT EXISTS trigger_subscriptions (
                        id TEXT PRIMARY KEY,
                        trigger_type TEXT NOT NULL,
                        config TEXT DEFAULT '{}',
                        prompt TEXT NOT NULL,
                        active INTEGER DEFAULT 1,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                for row in rows:
                    await new_db.execute(
                        "INSERT OR IGNORE INTO trigger_subscriptions "
                        "(id, trigger_type, config, prompt, active, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (row["id"], row["trigger_type"], row["config"],
                         row["prompt"], row["active"], row["created_at"]),
                    )
                await new_db.commit()
            finally:
                await new_db.close()

        await old_db.execute("DROP TABLE trigger_subscriptions")
        await old_db.commit()
    finally:
        await old_db.close()
