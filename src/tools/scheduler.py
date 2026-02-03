"""
Scheduler Tool — планирование задач.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable
import uuid

import aiosqlite
from claude_agent_sdk import tool
from loguru import logger

from src.config import settings

# Timezone для парсинга времени
_tz = settings.get_timezone()


# =============================================================================
# Storage
# =============================================================================


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    scheduled_at: datetime
    repeat_seconds: int | None = None
    status: str = "pending"


class SchedulerStorage:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._init_schema()
        return self._db

    async def _init_schema(self) -> None:
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                repeat_seconds INTEGER,
                status TEXT DEFAULT 'pending'
            )
        """)
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status)")
        await self._db.commit()

    async def add(
        self,
        task_id: str,
        prompt: str,
        scheduled_at: datetime,
        repeat_seconds: int | None = None,
    ) -> None:
        db = await self._get_db()
        await db.execute(
            "INSERT INTO scheduled_tasks (id, prompt, scheduled_at, repeat_seconds) VALUES (?, ?, ?, ?)",
            (task_id, prompt, scheduled_at.isoformat(), repeat_seconds),
        )
        await db.commit()

    async def get_pending(self) -> list[ScheduledTask]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, prompt, scheduled_at, repeat_seconds, status FROM scheduled_tasks WHERE status = 'pending' ORDER BY scheduled_at"
        )
        return [
            ScheduledTask(
                id=row["id"],
                prompt=row["prompt"],
                scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
                repeat_seconds=row["repeat_seconds"],
                status=row["status"],
            )
            for row in await cursor.fetchall()
        ]

    async def get_due(self) -> list[ScheduledTask]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, prompt, scheduled_at, repeat_seconds FROM scheduled_tasks WHERE status = 'pending' AND scheduled_at <= ?",
            (datetime.now().isoformat(),),
        )
        return [
            ScheduledTask(
                id=row["id"],
                prompt=row["prompt"],
                scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
                repeat_seconds=row["repeat_seconds"],
            )
            for row in await cursor.fetchall()
        ]

    async def set_status(self, task_id: str, status: str) -> None:
        db = await self._get_db()
        await db.execute("UPDATE scheduled_tasks SET status = ? WHERE id = ?", (status, task_id))
        await db.commit()

    async def reschedule(self, task_id: str, new_scheduled_at: datetime) -> None:
        """Перепланирует задачу на новое время."""
        db = await self._get_db()
        await db.execute(
            "UPDATE scheduled_tasks SET scheduled_at = ?, status = 'pending' WHERE id = ?",
            (new_scheduled_at.isoformat(), task_id),
        )
        await db.commit()

    async def cancel(self, task_id: str) -> bool:
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None


_storage: SchedulerStorage | None = None


def get_storage() -> SchedulerStorage:
    global _storage
    if _storage is None:
        _storage = SchedulerStorage(str(settings.db_path))
    return _storage


# =============================================================================
# Tools
# =============================================================================


@tool(
    "schedule_task",
    "Schedule a task. Time format: 'HH:MM' for today, 'YYYY-MM-DD HH:MM' for specific date. Repeat: '24h', '1h', '30m', or None.",
    {
        "prompt": str,
        "time": str,
        "repeat": str,
    },
)
async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
    prompt: str | None = args.get("prompt")
    time_str: str | None = args.get("time")
    repeat: str | None = args.get("repeat")

    if not prompt:
        return _error("prompt обязателен")

    if not time_str:
        return _error("time обязателен (формат: HH:MM или YYYY-MM-DD HH:MM)")

    # Парсим время
    scheduled_at = _parse_time(time_str)
    if scheduled_at is None:
        return _error(f"Неверный формат времени: {time_str}. Используй HH:MM или YYYY-MM-DD HH:MM")

    # Если время в прошлом — переносим на завтра
    now = datetime.now(_tz)
    if scheduled_at <= now:
        scheduled_at += timedelta(days=1)

    # Парсим repeat
    repeat_seconds = _parse_repeat(repeat) if repeat else None

    task_id = str(uuid.uuid4())[:8]

    await get_storage().add(
        task_id=task_id,
        prompt=prompt,
        scheduled_at=scheduled_at,
        repeat_seconds=repeat_seconds,
    )

    time_display = scheduled_at.strftime("%d.%m %H:%M")
    repeat_str = f", повтор: {repeat}" if repeat else ""
    logger.info(f"Scheduled [{task_id}]: {prompt[:40]}... at {time_display}{repeat_str}")

    return _text(f"[{task_id}] {time_display}{repeat_str}\n{prompt}")


@tool("list_scheduled_tasks", "List pending tasks", {})
async def list_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
    tasks = await get_storage().get_pending()

    if not tasks:
        return _text("Нет задач")

    lines = []
    for t in tasks:
        time_str = t.scheduled_at.strftime("%d.%m %H:%M")
        repeat = f" (каждые {t.repeat_seconds}с)" if t.repeat_seconds else ""
        lines.append(f"• [{t.id}] {time_str}{repeat}: {t.prompt[:40]}...")

    return _text("\n".join(lines))


@tool("cancel_scheduled_task", "Cancel task by ID", {"task_id": str})
async def cancel_scheduled_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = args.get("task_id")
    if not task_id:
        return _error("task_id обязателен")

    if await get_storage().cancel(task_id):
        return _text(f"[{task_id}] отменена")
    return _error(f"[{task_id}] не найдена")


SCHEDULER_TOOLS = [schedule_task, list_scheduled_tasks, cancel_scheduled_task]


# =============================================================================
# Runner
# =============================================================================


class SchedulerRunner:
    def __init__(self, on_task_due: Callable[[str, str], Awaitable[None]]) -> None:
        self._on_task_due = on_task_due
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(30)

    async def _check(self) -> None:
        storage = get_storage()
        tasks = await storage.get_due()

        for task in tasks:
            logger.info(f"Executing [{task.id}]: {task.prompt[:40]}")
            await storage.set_status(task.id, "running")

            try:
                await self._on_task_due(task.id, task.prompt)

                # Если повторяющаяся — перепланируем
                if task.repeat_seconds:
                    next_at = datetime.now() + timedelta(seconds=task.repeat_seconds)
                    await storage.reschedule(task.id, next_at)
                    logger.info(f"Rescheduled [{task.id}] to {next_at.strftime('%H:%M')}")
                else:
                    await storage.set_status(task.id, "completed")

            except Exception as e:
                logger.error(f"Task [{task.id}] failed: {e}")
                await storage.set_status(task.id, "failed")


# =============================================================================
# Helpers
# =============================================================================


def _parse_time(time_str: str) -> datetime | None:
    """
    Парсит время из строки в timezone пользователя.

    Форматы:
    - "HH:MM" — сегодня в указанное время
    - "YYYY-MM-DD HH:MM" — конкретная дата и время
    - "YYYY-MM-DDTHH:MM:SS" — ISO формат
    """
    time_str = time_str.strip()

    # ISO формат
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz)
        return dt
    except ValueError:
        pass

    # Полный формат YYYY-MM-DD HH:MM
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=_tz)
    except ValueError:
        pass

    # Только время HH:MM — сегодня
    try:
        time_part = datetime.strptime(time_str, "%H:%M")
        now = datetime.now(_tz)
        return now.replace(hour=time_part.hour, minute=time_part.minute, second=0, microsecond=0)
    except ValueError:
        pass

    return None


def _parse_repeat(repeat: str) -> int | None:
    """
    Парсит интервал повторения.

    Форматы:
    - "30m" — 30 минут
    - "1h" — 1 час
    - "24h" — 24 часа
    - "1d" — 1 день
    """
    repeat = repeat.strip().lower()

    if repeat.endswith("m"):
        try:
            return int(repeat[:-1]) * 60
        except ValueError:
            return None

    if repeat.endswith("h"):
        try:
            return int(repeat[:-1]) * 3600
        except ValueError:
            return None

    if repeat.endswith("d"):
        try:
            return int(repeat[:-1]) * 86400
        except ValueError:
            return None

    # Попробуем как число секунд (для совместимости)
    try:
        return int(repeat)
    except ValueError:
        return None


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
