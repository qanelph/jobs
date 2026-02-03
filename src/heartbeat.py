"""
Heartbeat — периодическая проверка с проактивными уведомлениями.

Каждые N минут агент "просыпается" и решает:
- Есть ли что-то важное для пользователя?
- Есть ли просроченные задачи?
- Если да → пишет в Telegram
- Если нет → молчит (HEARTBEAT_OK)
"""

import asyncio
from datetime import datetime
from typing import Callable, Awaitable, Any

from loguru import logger

from src.config import settings
from src.users.prompts import HEARTBEAT_PROMPT


# Маркер что всё ок, не нужно писать пользователю
HEARTBEAT_OK_MARKER = "HEARTBEAT_OK"

# Интервал по умолчанию (минуты)
DEFAULT_INTERVAL_MINUTES = 30

# Глобальная ссылка на Telegram клиент
_telegram_client: Any = None


def set_heartbeat_client(client: Any) -> None:
    """Устанавливает Telegram клиент для отправки напоминаний."""
    global _telegram_client
    _telegram_client = client


class HeartbeatRunner:
    """
    Периодический heartbeat для проактивных уведомлений.

    Каждые interval минут:
    1. Проверяет просроченные задачи пользователей
    2. Отправляет агенту HEARTBEAT_PROMPT
    3. Если ответ НЕ содержит HEARTBEAT_OK — отправляет пользователю
    """

    def __init__(
        self,
        on_alert: Callable[[str], Awaitable[None]],
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> None:
        """
        Args:
            on_alert: Callback для отправки уведомления пользователю
            interval_minutes: Интервал между проверками
        """
        self._on_alert = on_alert
        self._interval = interval_minutes * 60  # в секунды
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Запускает heartbeat loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Heartbeat started (interval: {self._interval // 60} min)")

    async def stop(self) -> None:
        """Останавливает heartbeat."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat stopped")

    async def _loop(self) -> None:
        """Основной цикл."""
        # Первый heartbeat через interval (не сразу)
        await asyncio.sleep(self._interval)

        while self._running:
            try:
                await self._check()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

            await asyncio.sleep(self._interval)

    async def _check(self) -> None:
        """Выполняет проверку."""
        logger.debug("Heartbeat check started")

        # Проверяем просроченные задачи пользователей
        await self._check_user_tasks()

        # Формируем промпт с информацией о задачах
        prompt = await self._build_heartbeat_prompt()

        # Используем сессию owner'а
        from src.users import get_session_manager
        session_manager = get_session_manager()
        session = session_manager.get_owner_session()

        content = await session.query(prompt)
        content = content.strip()

        # Проверяем маркер
        if HEARTBEAT_OK_MARKER in content:
            logger.debug("Heartbeat: all OK, no alert needed")
            return

        # Есть что сказать — отправляем пользователю
        logger.info(f"Heartbeat alert: {content[:100]}...")

        # Убираем маркер если он частично присутствует
        alert_text = content.replace(HEARTBEAT_OK_MARKER, "").strip()

        if alert_text:
            await self._on_alert(f"💡 {alert_text}")

    async def _check_user_tasks(self) -> None:
        """Проверяет просроченные задачи и напоминает пользователям."""
        from src.users import get_users_repository, get_session_manager
        from src.config import settings

        repo = get_users_repository()

        # Получаем просроченные задачи
        overdue = await repo.get_overdue_tasks()
        if not overdue:
            return

        logger.info(f"Found {len(overdue)} overdue tasks")

        # Группируем по assignee
        by_user: dict[int, list] = {}
        for task in overdue:
            if task.assignee_id not in by_user:
                by_user[task.assignee_id] = []
            by_user[task.assignee_id].append(task)

        # Отправляем напоминания пользователям
        client = _telegram_client
        if not client:
            logger.warning("Telegram client not available for reminders")
            return

        for user_id, tasks in by_user.items():
            # Не напоминаем owner'у через этот механизм
            if user_id == settings.tg_user_id:
                continue

            user = await repo.get_user(user_id)
            user_name = user.display_name if user else str(user_id)

            # Формируем напоминание
            task_lines = []
            for task in tasks[:3]:  # Максимум 3 задачи в напоминании
                days = (datetime.now() - task.deadline).days if task.deadline else 0
                task_lines.append(f"• {task.description[:50]} (просрочено {days} дн.)")

            reminder = "Напоминание о просроченных задачах:\n\n" + "\n".join(task_lines)
            if len(tasks) > 3:
                reminder += f"\n\n...и ещё {len(tasks) - 3} задач(и)"

            try:
                await client.send_message(user_id, reminder)
                logger.info(f"Sent reminder to {user_name}: {len(tasks)} overdue tasks")
            except Exception as e:
                logger.error(f"Failed to send reminder to {user_name}: {e}")

    async def _build_heartbeat_prompt(self) -> str:
        """Формирует промпт для heartbeat с информацией о задачах."""
        from src.users import get_users_repository

        base_prompt = HEARTBEAT_PROMPT.format(interval=self._interval // 60)

        # Добавляем информацию о просроченных задачах
        repo = get_users_repository()
        overdue = await repo.get_overdue_tasks()
        upcoming = await repo.get_upcoming_tasks(hours=24)

        task_info = []

        if overdue:
            task_info.append(f"\n## Просроченные задачи ({len(overdue)})")
            for task in overdue[:5]:  # Ограничиваем
                user = await repo.get_user(task.assignee_id)
                user_name = user.display_name if user else str(task.assignee_id)
                days = (asyncio.get_event_loop().time() - task.deadline.timestamp()) / 86400 if task.deadline else 0
                task_info.append(f"- [{task.id}] {user_name}: {task.description[:40]} (просрочено)")

        if upcoming:
            task_info.append(f"\n## Задачи на сегодня ({len(upcoming)})")
            for task in upcoming[:5]:
                user = await repo.get_user(task.assignee_id)
                user_name = user.display_name if user else str(task.assignee_id)
                time_str = task.deadline.strftime("%H:%M") if task.deadline else "—"
                task_info.append(f"- [{task.id}] {user_name}: {task.description[:40]} (дедлайн {time_str})")

        if task_info:
            return base_prompt + "\n" + "\n".join(task_info)

        return base_prompt

    async def trigger_now(self) -> None:
        """Запускает проверку немедленно (для тестирования)."""
        await self._check()
