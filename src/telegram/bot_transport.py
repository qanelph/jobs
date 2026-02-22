"""
BotTransport — aiogram 3.x реализация Transport Protocol.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.enums import ChatAction
from loguru import logger

from src.telegram.transport import (
    Transport,
    TransportMode,
    IncomingMessage,
    MessageCallback,
)


class BotTransport:
    """Transport на базе aiogram Bot API."""

    mode = TransportMode.BOT

    def __init__(self, token: str) -> None:
        self._bot = Bot(token=token)
        self._dp = Dispatcher()
        self._callbacks: list[MessageCallback] = []
        self._running = False

    @property
    def bot(self) -> Bot:
        return self._bot

    async def start(self) -> None:
        pass  # Polling запускается в run_forever

    async def stop(self) -> None:
        self._running = False
        await self._dp.stop_polling()
        await self._bot.session.close()

    async def send_message(self, chat_id: int, text: str) -> int:
        try:
            result = await self._bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception:
            result = await self._bot.send_message(chat_id, text)
        return result.message_id

    async def reply(self, msg: IncomingMessage, text: str) -> int:
        try:
            result = await self._bot.send_message(
                msg.chat_id,
                text,
                reply_to_message_id=msg.message_id,
                parse_mode="Markdown",
            )
        except Exception:
            result = await self._bot.send_message(
                msg.chat_id,
                text,
                reply_to_message_id=msg.message_id,
            )
        return result.message_id

    async def reply_with_entities(
        self, msg: IncomingMessage, text: str, entities: list | None,
    ) -> int:
        # Bot API не поддерживает custom emoji entities — отправляем как обычно
        return await self.reply(msg, text)

    async def edit_message(
        self, chat_id: int, msg_id: int, text: str, entities: list | None = None,
    ) -> None:
        try:
            await self._bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown",
            )
        except Exception:
            try:
                await self._bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.debug(f"Edit message error: {e}")

    async def delete_message(self, chat_id: int, msg_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logger.debug(f"Delete message error: {e}")

    async def set_typing(self, chat_id: int, typing: bool) -> None:
        if not typing:
            return  # Bot API: typing auto-expires after 5s, no cancel
        try:
            await self._bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception as e:
            logger.debug(f"Typing action error: {e}")

    async def mark_read(self, chat_id: int, msg_id: int) -> None:
        pass  # Bot API не поддерживает mark_read

    async def download_media(self, msg: IncomingMessage) -> bytes | None:
        raw: Message = msg.raw
        file_id: str | None = None

        if raw.voice:
            file_id = raw.voice.file_id
        elif raw.photo:
            file_id = raw.photo[-1].file_id  # Максимальный размер
        elif raw.document:
            file_id = raw.document.file_id

        if not file_id:
            return None

        file = await self._bot.get_file(file_id)
        if not file.file_path:
            return None

        buf = BytesIO()
        await self._bot.download_file(file.file_path, buf)
        return buf.getvalue()

    async def send_file(self, chat_id: int, path: Path, caption: str = "") -> int:
        result = await self._bot.send_document(
            chat_id,
            FSInputFile(path),
            caption=caption or None,
        )
        return result.message_id

    async def get_me(self) -> dict:
        me = await self._bot.get_me()
        return {
            "id": me.id,
            "first_name": me.first_name,
            "username": me.username,
            "is_premium": False,  # Bot API боты не premium
        }

    def on_message(self, callback: MessageCallback) -> None:
        self._callbacks.append(callback)

        @self._dp.message()
        async def _handler(message: Message) -> None:
            if not message.from_user:
                return

            # Определяем тип документа
            doc_name: str | None = None
            doc_size: int | None = None
            if message.document:
                doc_name = message.document.file_name
                doc_size = message.document.file_size

            chat = message.chat
            is_private = chat.type == "private"
            is_channel = chat.type == "channel"
            is_group = chat.type in ("group", "supergroup")

            incoming = IncomingMessage(
                message_id=message.message_id,
                chat_id=chat.id,
                sender_id=message.from_user.id,
                sender_first_name=message.from_user.first_name,
                sender_last_name=message.from_user.last_name,
                sender_username=message.from_user.username,
                sender_phone=None,  # Bot API не даёт телефон
                text=message.text or message.caption or None,
                is_private=is_private,
                is_channel=is_channel,
                is_group=is_group,
                has_voice=bool(message.voice),
                has_photo=bool(message.photo),
                has_document=bool(message.document),
                document_name=doc_name,
                document_size=doc_size,
                raw=message,
                transport=self,
            )
            await callback(incoming)

    async def run_forever(self) -> None:
        self._running = True
        logger.info("Bot polling started")
        await self._dp.start_polling(self._bot)
