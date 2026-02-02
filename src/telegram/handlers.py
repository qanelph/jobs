"""
Telegram Handlers — обработка входящих сообщений.
"""

import asyncio
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction
from telegraph import Telegraph
from loguru import logger

from src.config import settings
from src.session import get_session

MAX_TG_LENGTH = 4000
TYPING_REFRESH_INTERVAL = 3.0


class TelegramHandlers:
    """Обработчики сообщений Telegram."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._telegraph = Telegraph()
        self._telegraph_ready = False

    def register(self) -> None:
        """Регистрирует обработчики событий."""
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(from_users=[settings.tg_user_id]),
        )
        logger.info(f"Registered handler for user {settings.tg_user_id}")

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Обрабатывает входящее сообщение."""
        message = event.message
        prompt = message.text

        if not prompt:
            return

        logger.info(f"Received: {prompt[:100]}...")

        input_chat = await event.get_input_chat()

        # Отмечаем как прочитанное
        await self._client.send_read_acknowledge(input_chat, message)

        # Включаем typing
        await self._set_typing(input_chat, typing=True)

        session = get_session()
        status_msg = None
        last_typing = asyncio.get_event_loop().time()
        final_content = ""

        try:
            async for update in session.query_stream(prompt):
                # Поддерживаем typing
                now = asyncio.get_event_loop().time()
                if now - last_typing > TYPING_REFRESH_INTERVAL:
                    await self._set_typing(input_chat, typing=True)
                    last_typing = now

                if update.tool_name:
                    tool_display = self._format_tool(update.tool_name)
                    if status_msg is None:
                        status_msg = await event.reply(f"🔧 {tool_display}...")
                    else:
                        await self._safe_edit(status_msg, f"🔧 {tool_display}...")

                elif update.is_final:
                    final_content = update.text or ""

        except Exception as e:
            logger.error(f"Error: {e}")
            final_content = f"❌ Ошибка: {e}"

        finally:
            await self._set_typing(input_chat, typing=False)

        # Отправляем результат
        response_text = self._prepare_response(prompt, final_content)

        if status_msg:
            await self._safe_edit(status_msg, response_text)
        else:
            await event.reply(response_text)

    async def _set_typing(self, chat: Any, typing: bool) -> None:
        """Устанавливает статус typing."""
        try:
            action = SendMessageTypingAction() if typing else SendMessageCancelAction()
            await self._client(SetTypingRequest(peer=chat, action=action))
        except Exception as e:
            logger.debug(f"Typing status error: {e}")

    async def _safe_edit(self, message: Any, text: str) -> None:
        """Безопасно редактирует сообщение."""
        try:
            await message.edit(text)
        except Exception:
            pass

    def _format_tool(self, tool_name: str) -> str:
        """Форматирует название инструмента."""
        icons = {
            "Read": "📖 Читаю",
            "Write": "✍️ Пишу",
            "Edit": "✏️ Редактирую",
            "Bash": "💻 Выполняю",
            "Glob": "🔍 Ищу файлы",
            "Grep": "🔎 Ищу в файлах",
            "WebFetch": "🌐 Загружаю",
            "WebSearch": "🔍 Ищу в сети",
            "Task": "🤖 Агент",
            "schedule_task": "📅 Планирую",
            "list_scheduled_tasks": "📋 Список задач",
            "cancel_scheduled_task": "❌ Отмена задачи",
        }
        return icons.get(tool_name, f"⚙️ {tool_name}")

    def _prepare_response(self, prompt: str, content: str) -> str:
        """Подготавливает ответ (Telegraph для длинных)."""
        if not content:
            return "🤷 Нет ответа"

        if len(content) <= MAX_TG_LENGTH:
            return content

        url = self._publish_telegraph(prompt, content)
        return f"📄 {url}"

    def _publish_telegraph(self, title: str, content: str) -> str:
        """Публикует в Telegraph."""
        if not self._telegraph_ready:
            self._telegraph.create_account(short_name="JobsBot")
            self._telegraph_ready = True

        short_title = title[:50] + "..." if len(title) > 50 else title
        safe = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        page = self._telegraph.create_page(
            title=short_title,
            html_content=f"<pre>{safe}</pre>",
        )
        return page["url"]
