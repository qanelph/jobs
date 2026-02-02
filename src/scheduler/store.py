from datetime import datetime
from dataclasses import dataclass
from typing import Any

import aiosqlite
from loguru import logger

from src.config import settings


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    scheduled_at: datetime
    status: str = "pending"
    result: str | None = None
    created_at: datetime | None = None


class SchedulerStore:
    """Хранилище запланированных задач в SQLite."""

    def __init__(self):
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Возвращает соединение с БД."""
        if self._db is None:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(settings.db_path)
            self._db.row_factory = aiosqlite.Row

            # Включаем WAL и создаём таблицу
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    scheduled_at DATETIME NOT NULL,
                    status TEXT DEFAULT 'pending',
                    result TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(status)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON scheduled_tasks(scheduled_at)"
            )
            await self._db.commit()
            logger.info("Scheduler store initialized")

        return self._db

    async def add_task(
        self,
        task_id: str,
        prompt: str,
        scheduled_at: datetime,
    ) -> None:
        """Добавляет задачу в хранилище."""
        db = await self._get_db()
        await db.execute(
            """
            INSERT INTO scheduled_tasks (id, prompt, scheduled_at)
            VALUES (?, ?, ?)
            """,
            (task_id, prompt, scheduled_at.isoformat()),
        )
        await db.commit()

    async def get_pending_tasks(self) -> list[dict[str, Any]]:
        """Возвращает список ожидающих задач."""
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT id, prompt, scheduled_at, status, created_at
            FROM scheduled_tasks
            WHERE status = 'pending'
            ORDER BY scheduled_at
            """
        )
        rows = await cursor.fetchall()

        tasks = []
        for row in rows:
            tasks.append({
                "id": row["id"],
                "prompt": row["prompt"],
                "scheduled_at": datetime.fromisoformat(row["scheduled_at"]),
                "status": row["status"],
            })
        return tasks

    async def get_due_tasks(self) -> list[dict[str, Any]]:
        """Возвращает задачи, которые пора выполнить."""
        db = await self._get_db()
        now = datetime.now().isoformat()

        cursor = await db.execute(
            """
            SELECT id, prompt, scheduled_at
            FROM scheduled_tasks
            WHERE status = 'pending' AND scheduled_at <= ?
            ORDER BY scheduled_at
            """,
            (now,),
        )
        rows = await cursor.fetchall()

        tasks = []
        for row in rows:
            tasks.append({
                "id": row["id"],
                "prompt": row["prompt"],
                "scheduled_at": datetime.fromisoformat(row["scheduled_at"]),
            })
        return tasks

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
    ) -> None:
        """Обновляет статус задачи."""
        db = await self._get_db()
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET status = ?, result = ?
            WHERE id = ?
            """,
            (status, result, task_id),
        )
        await db.commit()

    async def cancel_task(self, task_id: str) -> bool:
        """Отменяет задачу. Возвращает True если задача найдена."""
        db = await self._get_db()
        cursor = await db.execute(
            """
            UPDATE scheduled_tasks
            SET status = 'cancelled'
            WHERE id = ? AND status = 'pending'
            """,
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        """Закрывает соединение."""
        if self._db:
            await self._db.close()
            self._db = None


# Singleton
scheduler_store = SchedulerStore()
