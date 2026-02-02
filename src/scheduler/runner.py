import asyncio
from datetime import datetime

from loguru import logger

from src.scheduler.store import scheduler_store


class SchedulerRunner:
    """Запускает запланированные задачи."""

    def __init__(self, on_task_due):
        """
        Args:
            on_task_due: Async callback(task_id, prompt) вызывается когда задача готова к выполнению.
        """
        self._on_task_due = on_task_due
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Запускает проверку задач в фоне."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Останавливает scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    async def _run_loop(self) -> None:
        """Основной цикл проверки задач."""
        while self._running:
            try:
                await self._check_tasks()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")

            # Проверяем каждые 30 секунд
            await asyncio.sleep(30)

    async def _check_tasks(self) -> None:
        """Проверяет и выполняет готовые задачи."""
        tasks = await scheduler_store.get_due_tasks()

        for task in tasks:
            task_id = task["id"]
            prompt = task["prompt"]

            logger.info(f"Executing scheduled task {task_id}: {prompt[:50]}...")

            # Помечаем как выполняющуюся
            await scheduler_store.update_task_status(task_id, "running")

            try:
                # Вызываем callback
                await self._on_task_due(task_id, prompt)
                await scheduler_store.update_task_status(task_id, "completed")
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}")
                await scheduler_store.update_task_status(task_id, "failed", str(e))
