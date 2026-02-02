"""
Scheduler Tool — планирование задач на выполнение позже.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import uuid

import aiosqlite
from claude_agent_sdk import tool
from loguru import logger

from src.config import settings


# =============================================================================
# Storage
# =============================================================================


@dataclass
class ScheduledTask:
    """Запланированная задача."""

    id: str
    prompt: str
    scheduled_at: datetime
    status: str = "pending"
    result: str | None = None


class SchedulerStorage:
    """SQLite хранилище для запланированных задач."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Возвращает соединение с БД (lazy init)."""
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._init_schema()
        return self._db

    async def _init_schema(self) -> None:
        """Создаёт таблицу если не существует."""
        db = self._db
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                result TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status)"
        )
        await db.commit()

    async def add(
        self,
        task_id: str,
        prompt: str,
        scheduled_at: datetime,
    ) -> None:
        """Добавляет задачу."""
        db = await self._get_db()
        await db.execute(
            "INSERT INTO scheduled_tasks (id, prompt, scheduled_at) VALUES (?, ?, ?)",
            (task_id, prompt, scheduled_at.isoformat()),
        )
        await db.commit()

    async def get_pending(self) -> list[ScheduledTask]:
        """Возвращает список ожидающих задач."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, prompt, scheduled_at, status FROM scheduled_tasks WHERE status = 'pending' ORDER BY scheduled_at"
        )
        rows = await cursor.fetchall()
        return [
            ScheduledTask(
                id=row["id"],
                prompt=row["prompt"],
                scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
                status=row["status"],
            )
            for row in rows
        ]

    async def get_due(self) -> list[ScheduledTask]:
        """Возвращает задачи, которые пора выполнить."""
        db = await self._get_db()
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "SELECT id, prompt, scheduled_at FROM scheduled_tasks WHERE status = 'pending' AND scheduled_at <= ?",
            (now,),
        )
        rows = await cursor.fetchall()
        return [
            ScheduledTask(
                id=row["id"],
                prompt=row["prompt"],
                scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            )
            for row in rows
        ]

    async def set_status(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
    ) -> None:
        """Обновляет статус задачи."""
        db = await self._get_db()
        await db.execute(
            "UPDATE scheduled_tasks SET status = ?, result = ? WHERE id = ?",
            (status, result, task_id),
        )
        await db.commit()

    async def cancel(self, task_id: str) -> bool:
        """Отменяет задачу. Возвращает True если найдена."""
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        """Закрывает соединение."""
        if self._db:
            await self._db.close()
            self._db = None


# Singleton storage
_storage: SchedulerStorage | None = None


def get_storage() -> SchedulerStorage:
    """Возвращает storage (singleton)."""
    global _storage
    if _storage is None:
        _storage = SchedulerStorage(str(settings.db_path))
    return _storage


# =============================================================================
# Tools
# =============================================================================


@tool(
    "schedule_task",
    "Schedule a task to be executed later. Use when user asks to remind, schedule, or do something at a specific time.",
    {
        "prompt": str,
        "delay_minutes": int,
        "at_time": str,
        "at_date": str,
    },
)
async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
    """Планирует задачу на выполнение позже."""
    prompt: str | None = args.get("prompt")
    delay_minutes: int | None = args.get("delay_minutes")
    at_time: str | None = args.get("at_time")
    at_date: str | None = args.get("at_date")

    if not prompt:
        return _error("Не указан prompt для задачи")

    # Вычисляем время
    now = datetime.now()

    if delay_minutes:
        scheduled_at = now + timedelta(minutes=delay_minutes)
    elif at_time:
        try:
            hour, minute = map(int, at_time.split(":"))
            scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled_at <= now:
                scheduled_at += timedelta(days=1)
        except ValueError:
            return _error(f"Неверный формат времени '{at_time}', ожидается HH:MM")
    else:
        return _error("Укажи delay_minutes или at_time")

    if at_date:
        try:
            year, month, day = map(int, at_date.split("-"))
            scheduled_at = scheduled_at.replace(year=year, month=month, day=day)
        except ValueError:
            return _error(f"Неверный формат даты '{at_date}', ожидается YYYY-MM-DD")

    task_id = str(uuid.uuid4())[:8]
    storage = get_storage()
    await storage.add(task_id=task_id, prompt=prompt, scheduled_at=scheduled_at)

    time_str = scheduled_at.strftime("%d.%m.%Y %H:%M")
    logger.info(f"Scheduled task {task_id}: '{prompt[:50]}' at {time_str}")

    return _text(f"✅ Задача запланирована на {time_str}\nID: {task_id}\nЗадача: {prompt}")


@tool(
    "list_scheduled_tasks",
    "List all pending scheduled tasks",
    {},
)
async def list_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Показывает список запланированных задач."""
    storage = get_storage()
    tasks = await storage.get_pending()

    if not tasks:
        return _text("Нет запланированных задач")

    lines = ["📋 Запланированные задачи:\n"]
    for task in tasks:
        time_str = task.scheduled_at.strftime("%d.%m.%Y %H:%M")
        lines.append(f"• [{task.id}] {time_str}: {task.prompt[:50]}...")

    return _text("\n".join(lines))


@tool(
    "cancel_scheduled_task",
    "Cancel a scheduled task by its ID",
    {"task_id": str},
)
async def cancel_scheduled_task(args: dict[str, Any]) -> dict[str, Any]:
    """Отменяет запланированную задачу."""
    task_id: str | None = args.get("task_id")

    if not task_id:
        return _error("Не указан task_id")

    storage = get_storage()
    success = await storage.cancel(task_id)

    if success:
        return _text(f"✅ Задача {task_id} отменена")
    return _error(f"Задача {task_id} не найдена")


# Экспортируем список tools
SCHEDULER_TOOLS = [schedule_task, list_scheduled_tasks, cancel_scheduled_task]


# =============================================================================
# Scheduler Runner
# =============================================================================


class SchedulerRunner:
    """Запускает запланированные задачи в фоне."""

    def __init__(self, on_task_due) -> None:
        self._on_task_due = on_task_due
        self._running = False
        self._task = None

    async def start(self) -> None:
        """Запускает проверку задач."""
        if self._running:
            return
        self._running = True
        self._task = __import__("asyncio").create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Останавливает scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except __import__("asyncio").CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _loop(self) -> None:
        """Основной цикл."""
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            await __import__("asyncio").sleep(30)

    async def _check(self) -> None:
        """Проверяет и выполняет готовые задачи."""
        storage = get_storage()
        tasks = await storage.get_due()

        for task in tasks:
            logger.info(f"Executing task {task.id}: {task.prompt[:50]}")
            await storage.set_status(task.id, "running")

            try:
                await self._on_task_due(task.id, task.prompt)
                await storage.set_status(task.id, "completed")
            except Exception as e:
                logger.error(f"Task {task.id} failed: {e}")
                await storage.set_status(task.id, "failed", str(e))


# =============================================================================
# Helpers
# =============================================================================


def _text(text: str) -> dict[str, Any]:
    """Формирует успешный ответ."""
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    """Формирует ответ с ошибкой."""
    return {"content": [{"type": "text", "text": f"❌ {text}"}], "is_error": True}
