"""
TelethonTransport — обёртка Telethon TelegramClient для Transport Protocol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction

from src.telegram.transport import (
    Transport,
    TransportMode,
    IncomingMessage,
    MessageCallback,
)


class TelethonTransport:
    """Transport на базе Telethon (userbot)."""

    mode = TransportMode.TELETHON

    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @property
    def client(self) -> TelegramClient:
        """Прямой доступ к Telethon-клиенту (для tools и triggers)."""
        return self._client

    async def start(self) -> None:
        pass  # Клиент уже подключён из main.py

    async def stop(self) -> None:
        await self._client.disconnect()

    async def send_message(self, chat_id: int, text: str) -> int:
        result = await self._client.send_message(chat_id, text)
        return result.id

    async def reply(self, msg: IncomingMessage, text: str) -> int:
        result = await msg.raw.reply(text)
        return result.id

    async def reply_with_entities(
        self, msg: IncomingMessage, text: str, entities: list | None,
    ) -> int:
        result = await msg.raw.reply(text, formatting_entities=entities)
        return result.id

    async def edit_message(
        self, chat_id: int, msg_id: int, text: str, entities: list | None = None,
    ) -> None:
        await self._client.edit_message(chat_id, msg_id, text, formatting_entities=entities)

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        await self._client.delete_messages(chat_id, msg_id)

    async def set_typing(self, chat_id: int, typing: bool) -> None:
        try:
            action = SendMessageTypingAction() if typing else SendMessageCancelAction()
            entity = await self._client.get_input_entity(chat_id)
            await self._client(SetTypingRequest(peer=entity, action=action))
        except Exception as e:
            logger.debug(f"Typing status error: {e}")

    async def mark_read(self, chat_id: int, msg_id: int) -> None:
        entity = await self._client.get_input_entity(chat_id)
        await self._client.send_read_acknowledge(entity, max_id=msg_id)

    async def download_media(self, msg: IncomingMessage) -> bytes | None:
        raw = msg.raw.message if hasattr(msg.raw, "message") else msg.raw
        if hasattr(raw, "voice") and raw.voice:
            return await self._client.download_media(raw.voice, bytes)
        if hasattr(raw, "photo") and raw.photo:
            return await self._client.download_media(raw.photo, bytes)
        if hasattr(raw, "document") and raw.document:
            return await self._client.download_media(raw.document, bytes)
        return None

    async def send_file(self, chat_id: int, path: Path, caption: str = "") -> int:
        result = await self._client.send_file(chat_id, path, caption=caption)
        return result.id

    async def get_me(self) -> dict:
        me = await self._client.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "username": me.username,
            "is_premium": bool(getattr(me, "premium", False)),
        }

    def on_message(self, callback: MessageCallback) -> None:
        self._client.add_event_handler(
            self._make_handler(callback),
            events.NewMessage(incoming=True),
        )

    def _make_handler(self, callback: MessageCallback):
        async def _handler(event: events.NewMessage.Event) -> None:
            message = event.message
            sender = await event.get_sender()
            if not sender:
                return

            # Определяем тип документа
            doc_name: str | None = None
            doc_size: int | None = None
            if message.document:
                doc_size = message.document.size
                for attr in message.document.attributes:
                    if hasattr(attr, "file_name"):
                        doc_name = attr.file_name
                        break

            incoming = IncomingMessage(
                message_id=message.id,
                chat_id=event.chat_id,
                sender_id=sender.id,
                sender_first_name=sender.first_name if hasattr(sender, "first_name") else None,
                sender_last_name=sender.last_name if hasattr(sender, "last_name") else None,
                sender_username=sender.username if hasattr(sender, "username") else None,
                sender_phone=sender.phone if hasattr(sender, "phone") else None,
                text=message.text or None,
                is_private=event.is_private,
                is_channel=event.is_channel and not event.is_group,
                is_group=event.is_group,
                has_voice=bool(message.voice),
                has_photo=bool(message.photo),
                has_document=bool(message.document),
                document_name=doc_name,
                document_size=doc_size,
                raw=event,
                transport=self,
            )
            await callback(incoming)

        return _handler

    async def run_forever(self) -> None:
        await self._client.run_until_disconnected()
