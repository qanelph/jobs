"""
Scheduler Tool — планирование задач.

Расписание — это свойство Task (kind="scheduled").
Scheduler читает из таблицы tasks вместо отдельной scheduled_tasks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from claude_agent_sdk import tool
from loguru import logger

from src.config import settings

if TYPE_CHECKING:
    from src.triggers.executor import TriggerExecutor

# Timezone для парсинга времени
_tz = settings.get_timezone()


# =============================================================================
# Tools
# =============================================================================


@tool(
    "schedule_task",
    "Schedule a task. Time format: 'HH:MM' for today, 'YYYY-MM-DD HH:MM' for specific date. "
    "Repeat: '24h', '1h', '30m', or None. prompt is optional (defaults to title).",
    {
        "title": str,
        "prompt": str,
        "time": str,
        "repeat": str,
    },
)
async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
    title: str | None = args.get("title")
    prompt: str | None = args.get("prompt")
    time_str: str | None = args.get("time")
    repeat: str | None = args.get("repeat")

    if not title and not prompt:
        return _error("title или prompt обязателен")

    if not title:
        title = prompt[:80]

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

    # Создаём Task с kind="scheduled"
    from src.users.repository import get_users_repository
    repo = get_users_repository()

    context = {"prompt": prompt} if prompt else {}

    task = await repo.create_task(
        title=title,
        kind="scheduled",
        created_by=settings.tg_user_id,
        context=context,
        schedule_at=scheduled_at,
        schedule_repeat=repeat_seconds,
    )

    time_display = scheduled_at.strftime("%d.%m %H:%M")
    repeat_str = f", повтор: {repeat}" if repeat else ""
    logger.info(f"Scheduled [{task.id}]: {title[:40]}... at {time_display}{repeat_str}")

    return _text(f"[{task.id}] {time_display}{repeat_str}\n{title}")


@tool("cancel_task", "Cancel any task by ID", {"task_id": str})
async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
    task_id = args.get("task_id")
    if not task_id:
        return _error("task_id обязателен")

    from src.users.repository import get_users_repository
    repo = get_users_repository()

    task = await repo.get_task(task_id)
    if not task:
        return _error(f"[{task_id}] не найдена")

    if task.status in ("done", "cancelled"):
        return _error(f"[{task_id}] уже {task.status}")

    success = await repo.update_task(task_id, status="cancelled")
    if success:
        return _text(f"[{task_id}] отменена")
    return _error(f"[{task_id}] не удалось отменить")


SCHEDULER_TOOLS = [schedule_task, cancel_task]


# =============================================================================
# Runner
# =============================================================================


class SchedulerRunner:
    def __init__(self, executor: TriggerExecutor) -> None:
        self._executor = executor
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
        from src.users.repository import get_users_repository
        from src.triggers.models import TriggerEvent

        repo = get_users_repository()

        tasks = await repo.get_scheduled_due()

        for task in tasks:
            prompt = task.context.get("prompt") or task.title
            logger.info(f"Executing [{task.id}]: {prompt[:40]}")

            # Для repeating: сдвигаем schedule_at ВПЕРЁД ДО выполнения
            # (защита от двойного срабатывания)
            if task.schedule_repeat:
                next_at = datetime.now() + timedelta(seconds=task.schedule_repeat)
                await repo.update_schedule(task.id, next_at)
                logger.info(f"Rescheduled [{task.id}] to {next_at.strftime('%H:%M')}")
            else:
                # One-time: очищаем schedule_at
                await repo.update_schedule(task.id, None)

            try:
                event = TriggerEvent(
                    source="scheduler",
                    prompt=prompt,
                    context={"task_id": task.id},
                    preview_message=f"Выполняю задачу:\n{prompt}",
                    result_prefix=f"Результат [{task.id}]:",
                )
                await self._executor.execute(event)

                # One-time: ставим done после успеха
                if not task.schedule_repeat:
                    await repo.update_task(task.id, status="done")

            except Exception as e:
                logger.error(f"Task [{task.id}] failed: {e}")


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
