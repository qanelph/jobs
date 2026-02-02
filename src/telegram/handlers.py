import asyncio

from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction
from telegraph import Telegraph
from loguru import logger

from src.config import settings
from src.claude.runner import get_session, ProgressUpdate

MAX_TG_LENGTH = 4000  # Оставляем запас до лимита 4096
PROGRESS_UPDATE_INTERVAL = 3  # Секунд между обновлениями прогресса


class TelegramHandlers:
    """Обработчики сообщений Telegram."""

    def __init__(self, client: TelegramClient):
        self.client = client
        self.telegraph = Telegraph()
        self._telegraph_initialized = False

    def _ensure_telegraph(self) -> None:
        """Ленивая инициализация Telegraph аккаунта."""
        if not self._telegraph_initialized:
            self.telegraph.create_account(short_name="JobsBot")
            self._telegraph_initialized = True

    def register(self) -> None:
        """Регистрирует обработчики событий."""
        self.client.add_event_handler(
            self._handle_message,
            events.NewMessage(from_users=[settings.tg_user_id]),
        )
        logger.info(f"Registered message handler for user {settings.tg_user_id}")

    async def _set_typing(self, chat_id: int, typing: bool = True) -> None:
        """Устанавливает статус 'печатает'."""
        try:
            action = SendMessageTypingAction() if typing else SendMessageCancelAction()
            await self.client(SetTypingRequest(peer=chat_id, action=action))
        except Exception as e:
            logger.debug(f"Failed to set typing status: {e}")

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        """Обрабатывает входящее сообщение."""
        message = event.message
        prompt = message.text
        chat_id = message.chat_id

        if not prompt:
            return

        logger.info(f"Received message: {prompt[:100]}...")

        # Включаем статус "печатает"
        await self._set_typing(chat_id, True)

        session = get_session()
        status_msg = None
        last_progress_update = 0
        current_tool = None
        text_parts = []
        final_content = ""

        try:
            async for update in session.query_stream(prompt):
                now = asyncio.get_event_loop().time()

                # Поддерживаем статус "печатает"
                if now - last_progress_update > PROGRESS_UPDATE_INTERVAL:
                    await self._set_typing(chat_id, True)
                    last_progress_update = now

                if update.tool_name:
                    # Показываем какой инструмент используется
                    current_tool = update.tool_name
                    tool_display = self._format_tool_name(current_tool)

                    if status_msg is None:
                        status_msg = await event.reply(f"🔧 {tool_display}...")
                    else:
                        try:
                            await status_msg.edit(f"🔧 {tool_display}...")
                        except Exception:
                            pass  # Игнорируем ошибки редактирования

                elif update.text and not update.is_final:
                    text_parts.append(update.text)

                elif update.is_final:
                    final_content = update.text or "".join(text_parts)

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            final_content = f"❌ Ошибка: {e}"

        finally:
            # Выключаем статус "печатает"
            await self._set_typing(chat_id, False)

        # Отправляем финальный ответ
        if not final_content:
            final_content = "🤷 Нет ответа"

        if len(final_content) > MAX_TG_LENGTH:
            # Длинный ответ → Telegraph
            url = self._publish_to_telegraph(prompt, final_content)
            response_text = f"📄 {url}"
        else:
            response_text = final_content

        if status_msg:
            try:
                await status_msg.edit(response_text)
            except Exception:
                await event.reply(response_text)
        else:
            await event.reply(response_text)

    def _format_tool_name(self, tool_name: str) -> str:
        """Форматирует название инструмента для отображения."""
        tool_icons = {
            "Read": "📖 Читаю файл",
            "Write": "✍️ Пишу файл",
            "Edit": "✏️ Редактирую",
            "Bash": "💻 Выполняю команду",
            "Glob": "🔍 Ищу файлы",
            "Grep": "🔎 Ищу в файлах",
            "WebFetch": "🌐 Загружаю страницу",
            "WebSearch": "🔍 Ищу в интернете",
            "Task": "🤖 Запускаю агента",
        }
        return tool_icons.get(tool_name, f"⚙️ {tool_name}")

    def _publish_to_telegraph(self, title: str, content: str) -> str:
        """Публикует контент в Telegraph и возвращает URL."""
        self._ensure_telegraph()

        short_title = title[:50] + "..." if len(title) > 50 else title
        safe_content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html_content = f"<pre>{safe_content}</pre>"

        page = self.telegraph.create_page(
            title=short_title,
            html_content=html_content,
        )

        return page["url"]
