"""
Transport — абстракция транспорта Telegram.

Два транспорта:
- Telethon (userbot) — полный доступ к Telegram API
- Bot (aiogram) — через Bot API (ограниченный)

Оба реализуют Transport Protocol и конвертируют сообщения в IncomingMessage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable, Protocol, runtime_checkable


class TransportMode(str, Enum):
    TELETHON = "telethon"
    BOT = "bot"


@dataclass
class IncomingMessage:
    """Унифицированное входящее сообщение от любого транспорта."""

    message_id: int
    chat_id: int
    sender_id: int
    sender_first_name: str | None
    sender_last_name: str | None
    sender_username: str | None
    sender_phone: str | None  # Только Telethon
    text: str | None
    is_private: bool
    is_channel: bool
    is_group: bool
    has_voice: bool
    has_photo: bool
    has_document: bool
    document_name: str | None
    document_size: int | None
    raw: Any  # Telethon event или aiogram Message
    transport: Transport  # Транспорт-источник (для reply)
    reply_to_message_id: int | None = None
    is_bot_mentioned: bool = False
    is_reply_to_bot: bool = False
    sender_display_name: str = ""


# Callback type для on_message
MessageCallback = Callable[[IncomingMessage], Awaitable[None]]


@runtime_checkable
class Transport(Protocol):
    """Протокол транспорта Telegram."""

    mode: TransportMode

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_message(self, chat_id: int, text: str) -> int: ...
    async def reply(self, msg: IncomingMessage, text: str) -> int: ...
    async def reply_with_entities(
        self, msg: IncomingMessage, text: str, entities: list | None,
    ) -> int: ...
    async def edit_message(
        self, chat_id: int, msg_id: int, text: str, entities: list | None = None,
    ) -> None: ...
    async def delete_message(self, chat_id: int, msg_id: int) -> None: ...
    async def set_typing(self, chat_id: int, typing: bool) -> None: ...
    async def mark_read(self, chat_id: int, msg_id: int) -> None: ...
    async def download_media(self, msg: IncomingMessage) -> bytes | None: ...
    async def send_file(self, chat_id: int, path: Path, caption: str = "") -> int: ...
    async def get_me(self) -> dict: ...
    def on_message(self, callback: MessageCallback) -> None: ...
    async def run_forever(self) -> None: ...
