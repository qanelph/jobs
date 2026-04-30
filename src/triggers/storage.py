"""
TriggerStorage — SQLite хранилище подписок.

Отдельный файл triggers.sqlite (избегает WAL lock race с UsersRepository).
"""

import asyncio
import json
import uuid
from datetime import datetime

import aiosqlite
from loguru import logger

from src.triggers.models import TriggerSubscription


class TriggerStorage:
    """Хранилище подписок на триггеры в SQLite."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db

        async with self._db_lock:
            if self._db is None:
                db = await aiosqlite.connect(self._db_path)
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("PRAGMA journal_mode=WAL")
                    self._db = db
                    await self._init_schema()
                except Exception:
                    self._db = None
                    await db.close()
                    raise
        return self._db

    async def _init_schema(self) -> None:
        db = self._db
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trigger_subscriptions (
                id TEXT PRIMARY KEY,
                trigger_type TEXT NOT NULL,
                config TEXT DEFAULT '{}',
                prompt TEXT NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                recipient_ids TEXT
            )
        """)
        # Идемпотентная миграция для существующих БД.
        try:
            await db.execute("ALTER TABLE trigger_subscriptions ADD COLUMN recipient_ids TEXT")
        except aiosqlite.OperationalError:
            pass  # column already exists
        await db.commit()
        logger.debug("TriggerStorage schema initialized")

    async def list_active(self) -> list[TriggerSubscription]:
        """Возвращает все активные подписки."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM trigger_subscriptions WHERE active = 1"
        )
        rows = await cursor.fetchall()
        return [self._row_to_sub(row) for row in rows]

    async def list_all(self) -> list[TriggerSubscription]:
        """Возвращает все подписки (включая неактивные)."""
        db = await self._get_db()
        cursor = await db.execute("SELECT * FROM trigger_subscriptions")
        rows = await cursor.fetchall()
        return [self._row_to_sub(row) for row in rows]

    async def create(
        self,
        trigger_type: str,
        config: dict,
        prompt: str,
        recipient_ids: list[int] | None = None,
    ) -> TriggerSubscription:
        """Создаёт новую подписку."""
        sub_id = uuid.uuid4().hex[:8]
        db = await self._get_db()
        recipients_json = (
            json.dumps(recipient_ids) if recipient_ids is not None else None
        )
        await db.execute(
            "INSERT INTO trigger_subscriptions (id, trigger_type, config, prompt, recipient_ids) "
            "VALUES (?, ?, ?, ?, ?)",
            (sub_id, trigger_type, json.dumps(config, ensure_ascii=False), prompt, recipients_json),
        )
        await db.commit()

        return TriggerSubscription(
            id=sub_id,
            trigger_type=trigger_type,
            config=config,
            prompt=prompt,
            recipient_ids=recipient_ids,
        )

    async def delete(self, subscription_id: str) -> bool:
        """Удаляет подписку."""
        db = await self._get_db()
        cursor = await db.execute(
            "DELETE FROM trigger_subscriptions WHERE id = ?",
            (subscription_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def set_active(self, subscription_id: str, active: bool) -> bool:
        """Включает/выключает подписку."""
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE trigger_subscriptions SET active = ? WHERE id = ?",
            (1 if active else 0, subscription_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    def _row_to_sub(self, row: aiosqlite.Row) -> TriggerSubscription:
        config_raw = row["config"]
        config = json.loads(config_raw) if config_raw else {}

        created_str = row["created_at"]
        created_at = (
            datetime.fromisoformat(created_str) if created_str else datetime.now()
        )

        recipient_raw = row["recipient_ids"] if "recipient_ids" in row.keys() else None
        recipient_ids = json.loads(recipient_raw) if recipient_raw else None

        return TriggerSubscription(
            id=row["id"],
            trigger_type=row["trigger_type"],
            config=config,
            prompt=row["prompt"],
            active=bool(row["active"]),
            created_at=created_at,
            recipient_ids=recipient_ids,
        )

    async def close(self) -> None:
        """Закрывает соединение с БД."""
        if self._db:
            await self._db.close()
            self._db = None
