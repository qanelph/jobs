"""
TelegramChannelTrigger — триггер на новые посты в Telegram канале/группе.

Требует Telethon-транспорт (Bot API не поддерживает подписку на каналы).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import events
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import Channel
from loguru import logger

from src.telegram.transport import TransportMode
from src.triggers.executor import TriggerExecutor
from src.triggers.models import TriggerEvent

if TYPE_CHECKING:
    from src.telegram.transport import Transport
    from src.telegram.telethon_transport import TelethonTransport


class TelegramChannelTrigger:
    """Триггер на новые посты в Telegram канале/группе (Telethon only)."""

    def __init__(
        self,
        executor: TriggerExecutor,
        transport: "Transport",
        config: dict,
        prompt: str,
    ) -> None:
        if transport.mode != TransportMode.TELETHON:
            raise ValueError("TelegramChannelTrigger требует Telethon-транспорт")

        self._executor = executor
        # Получаем Telethon-клиент из transport
        telethon_transport: TelethonTransport = transport  # type: ignore[assignment]
        self._client = telethon_transport.client
        self._channel: str = config["channel"]
        self._prompt = prompt
        self._handler = None
        self._event_filter = None

    async def start(self) -> None:
        entity = await self._client.get_entity(self._channel)

        # Подписываемся на канал, если ещё не подписаны
        if isinstance(entity, Channel) and not entity.left:
            logger.debug(f"Already joined {self._channel}")
        elif isinstance(entity, Channel):
            logger.info(f"Joining channel {self._channel}...")
            await self._client(JoinChannelRequest(entity))
            # Перечитываем entity после join
            entity = await self._client.get_entity(self._channel)
            logger.info(f"Joined {self._channel}")

        self._event_filter = events.NewMessage(chats=[entity])
        self._client.add_event_handler(self._on_new_post, self._event_filter)
        self._handler = self._on_new_post
        logger.info(f"TelegramChannelTrigger started: {self._channel}")

    async def stop(self) -> None:
        if self._handler:
            self._client.remove_event_handler(self._handler)
            self._handler = None
        logger.info(f"TelegramChannelTrigger stopped: {self._channel}")

    async def _on_new_post(self, event: events.NewMessage.Event) -> None:
        post = event.message
        sender = await event.get_sender()
        sender_name = getattr(sender, "first_name", "") or self._channel

        full_prompt = (
            f"Новый пост в {self._channel} от {sender_name}:\n\n"
            f"{post.text or '[медиа без текста]'}\n\n"
            f"Инструкция: {self._prompt}"
        )

        trigger_event = TriggerEvent(
            source=f"tg_channel:{self._channel}",
            prompt=full_prompt,
            context={"channel": self._channel, "message_id": post.id},
        )

        try:
            await self._executor.execute(trigger_event)
        except Exception as e:
            logger.error(f"TelegramChannelTrigger error ({self._channel}): {e}")
