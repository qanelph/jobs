"""
TriggerExecutor — единая точка выполнения TriggerEvent.

Принимает событие, отправляет preview, запрашивает агента,
проверяет silent_marker, доставляет результат owner'у.
"""

import asyncio

from loguru import logger
from telethon import TelegramClient

from src.config import settings
from src.triggers.models import TriggerEvent
from src.users.session_manager import SessionManager


MAX_MESSAGE_LENGTH = 4000


class TriggerExecutor:
    """Выполняет TriggerEvent: query → deliver."""

    def __init__(self, client: TelegramClient, session_manager: SessionManager) -> None:
        self._client = client
        self._session_manager = session_manager
        self._lock = asyncio.Lock()

    async def execute(self, event: TriggerEvent) -> str | None:
        """
        Выполняет событие триггера (последовательно, через lock).

        1. Отправляет preview_message owner'у (если есть)
        2. Запрашивает агента через owner session
        3. Проверяет silent_marker — если есть, не доставляет
        4. Добавляет result_prefix, truncate, отправляет owner'у

        Returns:
            Ответ агента или None (если silent).
        """
        async with self._lock:
            return await self._execute_inner(event)

    async def _execute_inner(self, event: TriggerEvent) -> str | None:
        logger.debug(f"Executing trigger event: {event.source}")

        # Preview
        if event.preview_message and event.notify_owner:
            await self.send_to_owner(event.preview_message)

        # Query agent
        session = self._session_manager.get_owner_session()
        content = await session.query(event.prompt)
        content = content.strip()

        # Silent marker check
        if event.silent_marker and event.silent_marker in content:
            logger.debug(f"Trigger {event.source}: silent ({event.silent_marker})")
            return None

        # Prepare result
        if event.silent_marker:
            content = content.replace(event.silent_marker, "").strip()

        if not content:
            return None

        if event.result_prefix:
            content = f"{event.result_prefix}\n{content}"

        # Truncate
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[:MAX_MESSAGE_LENGTH] + "..."

        # Deliver
        if event.notify_owner:
            await self.send_to_owner(content)

        return content

    async def send_to_owner(self, text: str) -> None:
        """Отправляет сообщение owner'у."""
        await self._client.send_message(settings.tg_user_id, text)
