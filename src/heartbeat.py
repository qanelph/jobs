"""
Heartbeat — периодическая проверка с проактивными уведомлениями.

Каждые N минут агент "просыпается" и решает:
- Есть ли что-то важное для пользователя?
- Если да → пишет в Telegram
- Если нет → молчит (HEARTBEAT_OK)
"""

import asyncio
from typing import Callable, Awaitable

from loguru import logger

from src.config import settings
from src.session import get_session
from src.prompts import HEARTBEAT_PROMPT


# Маркер что всё ок, не нужно писать пользователю
HEARTBEAT_OK_MARKER = "HEARTBEAT_OK"

# Интервал по умолчанию (минуты)
DEFAULT_INTERVAL_MINUTES = 30


class HeartbeatRunner:
    """
    Периодический heartbeat для проактивных уведомлений.

    Каждые interval минут:
    1. Отправляет агенту HEARTBEAT_PROMPT
    2. Если ответ НЕ содержит HEARTBEAT_OK — отправляет пользователю
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

        prompt = HEARTBEAT_PROMPT.format(interval=self._interval // 60)

        session = get_session()
        response = await session.query(prompt)

        content = response.content.strip()

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

    async def trigger_now(self) -> None:
        """Запускает проверку немедленно (для тестирования)."""
        await self._check()
