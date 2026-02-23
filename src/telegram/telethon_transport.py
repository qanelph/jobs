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
        self._me_id: int = 0
        self._me_username: str = ""

    @property
    def client(self) -> TelegramClient:
        """Прямой доступ к Telethon-клиенту (для tools и triggers)."""
        return self._client

    async def start(self) -> None:
        me = await self._client.get_me()
        self._me_id = me.id
        self._me_username = (me.username or "").lower()

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

            # Reply-to
            reply_to_message_id: int | None = None
            is_reply_to_bot = False
            if message.reply_to and hasattr(message.reply_to, "reply_to_msg_id"):
                reply_to_message_id = message.reply_to.reply_to_msg_id
                # Проверяем reply к боту (только для групп, чтобы не тратить запрос)
                if event.is_group and reply_to_message_id:
                    try:
                        reply_msg = await message.get_reply_message()
                        if reply_msg and reply_msg.sender_id == self._me_id:
                            is_reply_to_bot = True
                    except Exception:
                        pass

            # Bot mention
            text = message.text or None
            is_bot_mentioned = False
            if text and self._me_username:
                is_bot_mentioned = f"@{self._me_username}" in text.lower()

            # Display name
            first_name = sender.first_name if hasattr(sender, "first_name") else None
            last_name = sender.last_name if hasattr(sender, "last_name") else None
            username = sender.username if hasattr(sender, "username") else None
            display_name = (first_name or "")
            if last_name:
                display_name = f"{display_name} {last_name}".strip()
            if not display_name:
                display_name = username or str(sender.id)

            incoming = IncomingMessage(
                message_id=message.id,
                chat_id=event.chat_id,
                sender_id=sender.id,
                sender_first_name=first_name,
                sender_last_name=last_name,
                sender_username=username,
                sender_phone=sender.phone if hasattr(sender, "phone") else None,
                text=text,
                is_private=event.is_private,
                is_channel=event.is_channel and not event.is_group,
                is_group=event.is_group,
                has_voice=bool(message.voice),
                has_photo=bool(message.photo),
                has_document=bool(message.document),
                document_name=doc_name,
                document_size=doc_size,
                reply_to_message_id=reply_to_message_id,
                is_bot_mentioned=is_bot_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                sender_display_name=display_name,
                raw=event,
                transport=self,
            )
            await callback(incoming)

        return _handler

    async def run_forever(self) -> None:
        await self._client.run_until_disconnected()
