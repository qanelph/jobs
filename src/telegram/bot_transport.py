"""
BotTransport — aiogram 3.x реализация Transport Protocol.
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from loguru import logger

from src.telegram.transport import (
    Transport,
    TransportMode,
    IncomingMessage,
    MessageCallback,
)


_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_CODE_BLOCK_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\w)\*([^*\n]+?)\*(?!\w)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _escape_mdv2(text: str) -> str:
    """Экранирует спецсимволы MarkdownV2."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def _md_to_v2(text: str) -> str:
    """Конвертирует стандартный Markdown в Telegram MarkdownV2."""
    # 1. Извлекаем code blocks (внутри не экранируем)
    blocks: list[str] = []

    def _save_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        blocks.append(f"```{lang}\n{code}```")
        return f"\x00BLOCK{len(blocks) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_save_block, text)

    # 2. Извлекаем inline code
    inlines: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inlines.append(f"`{m.group(1)}`")
        return f"\x00INLINE{len(inlines) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_save_inline, text)

    # 3. Извлекаем links: [text](url)
    links: list[str] = []

    def _save_link(m: re.Match) -> str:
        link_text = _escape_mdv2(m.group(1))
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        links.append(f"[{link_text}]({url})")
        return f"\x00LINK{len(links) - 1}\x00"

    text = _LINK_RE.sub(_save_link, text)

    # 4. Bold: **text** → *text* (MarkdownV2 bold = одна звёздочка)
    bolds: list[str] = []

    def _save_bold(m: re.Match) -> str:
        content = _escape_mdv2(m.group(1))
        bolds.append(f"*{content}*")
        return f"\x00BOLD{len(bolds) - 1}\x00"

    text = _BOLD_RE.sub(_save_bold, text)

    # 5. Italic: *text* → _text_ (MarkdownV2 italic = подчёркивание)
    italics: list[str] = []

    def _save_italic(m: re.Match) -> str:
        content = _escape_mdv2(m.group(1))
        italics.append(f"_{content}_")
        return f"\x00ITALIC{len(italics) - 1}\x00"

    text = _ITALIC_RE.sub(_save_italic, text)

    # 6. Экранируем весь оставшийся текст
    text = _escape_mdv2(text)

    # 7. Восстанавливаем placeholder'ы (содержат \x00 + буквы + цифры — не экранируются)
    for i, b in enumerate(bolds):
        text = text.replace(f"\x00BOLD{i}\x00", b)
    for i, it in enumerate(italics):
        text = text.replace(f"\x00ITALIC{i}\x00", it)
    for i, lnk in enumerate(links):
        text = text.replace(f"\x00LINK{i}\x00", lnk)
    for i, il in enumerate(inlines):
        text = text.replace(f"\x00INLINE{i}\x00", il)
    for i, bl in enumerate(blocks):
        text = text.replace(f"\x00BLOCK{i}\x00", bl)

    return text


class BotTransport:
    """Transport на базе aiogram Bot API."""

    mode = TransportMode.BOT

    def __init__(self, token: str) -> None:
        self._bot = Bot(token=token)
        self._dp = Dispatcher()
        self._running = False
        self._me_id: int = 0
        self._me_username: str = ""

    @property
    def bot(self) -> Bot:
        return self._bot

    async def start(self) -> None:
        me = await self._bot.get_me()
        self._me_id = me.id
        self._me_username = (me.username or "").lower()

    async def stop(self) -> None:
        self._running = False
        await self._dp.stop_polling()
        await self._bot.session.close()

    async def send_message(self, chat_id: int, text: str) -> int:
        try:
            result = await self._bot.send_message(chat_id, _md_to_v2(text), parse_mode="MarkdownV2")
        except TelegramBadRequest:
            result = await self._bot.send_message(chat_id, text)
        return result.message_id

    async def reply(self, msg: IncomingMessage, text: str) -> int:
        try:
            result = await self._bot.send_message(
                msg.chat_id,
                _md_to_v2(text),
                reply_to_message_id=msg.message_id,
                parse_mode="MarkdownV2",
            )
        except TelegramBadRequest:
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
                _md_to_v2(text), chat_id=chat_id, message_id=msg_id, parse_mode="MarkdownV2",
            )
        except TelegramBadRequest:
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

            # Reply-to
            reply_to_message_id: int | None = None
            is_reply_to_bot = False
            if message.reply_to_message:
                reply_to_message_id = message.reply_to_message.message_id
                reply_from = message.reply_to_message.from_user
                if reply_from and reply_from.id == self._me_id:
                    is_reply_to_bot = True

            # Bot mention
            text = message.text or message.caption or None
            is_bot_mentioned = False
            if text and self._me_username:
                is_bot_mentioned = f"@{self._me_username}" in text.lower()

            # Display name
            user = message.from_user
            display_name = user.first_name or ""
            if user.last_name:
                display_name = f"{display_name} {user.last_name}".strip()
            if not display_name:
                display_name = user.username or str(user.id)

            incoming = IncomingMessage(
                message_id=message.message_id,
                chat_id=chat.id,
                sender_id=user.id,
                sender_first_name=user.first_name,
                sender_last_name=user.last_name,
                sender_username=user.username,
                sender_phone=None,  # Bot API не даёт телефон
                text=text,
                is_private=is_private,
                is_channel=is_channel,
                is_group=is_group,
                has_voice=bool(message.voice),
                has_photo=bool(message.photo),
                has_document=bool(message.document),
                document_name=doc_name,
                document_size=doc_size,
                reply_to_message_id=reply_to_message_id,
                is_bot_mentioned=is_bot_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                sender_display_name=display_name,
                raw=message,
                transport=self,
            )
            await callback(incoming)

    async def run_forever(self) -> None:
        self._running = True
        logger.info("Bot polling started")
        await self._dp.start_polling(self._bot)
