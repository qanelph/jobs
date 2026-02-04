"""
Telegram Handlers — обработка входящих сообщений.

Поддерживает multi-session архитектуру:
- Owner (tg_user_id) — полный доступ
- External users — ограниченный доступ с отдельными сессиями
"""

import asyncio
from datetime import datetime
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction
from telegraph import Telegraph
from loguru import logger

from src.config import settings, set_owner_info
from src.users import get_session_manager, get_users_repository
from src.users.tools import set_telegram_sender
from src.media import transcribe_audio, save_media, MAX_MEDIA_SIZE

MAX_TG_LENGTH = 4000
TYPING_REFRESH_INTERVAL = 3.0


class TelegramHandlers:
    """Обработчики сообщений Telegram."""

    def __init__(self, client: TelegramClient) -> None:
        self._client = client
        self._telegraph = Telegraph()
        self._telegraph_ready = False

        # Настраиваем sender для user tools
        set_telegram_sender(self._send_message)

    def register(self) -> None:
        """Регистрирует обработчики событий."""
        # Принимаем сообщения от всех пользователей (не только owner)
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(incoming=True),
        )
        logger.info(f"Registered handler for all users (owner: {settings.tg_user_id})")

    async def _send_message(self, user_id: int, text: str) -> None:
        """Отправляет сообщение пользователю (для user tools)."""
        await self._client.send_message(user_id, text)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Обрабатывает входящее сообщение (только private chats)."""
        # Пропускаем каналы и группы — ими занимаются trigger subscriptions
        if event.is_channel or event.is_group:
            return

        message = event.message
        sender = await event.get_sender()

        if not sender:
            return

        user_id = sender.id
        is_owner = user_id == settings.tg_user_id

        # /clear — сброс сессии
        if message.text and message.text.strip().lower() == "/clear":
            session_manager = get_session_manager()
            await session_manager.reset_session(user_id)
            await event.reply("Сессия сброшена.")
            return

        # Обработка разных типов сообщений
        prompt, media_context = await self._extract_content(message)

        if not prompt and not media_context:
            return

        # Если есть медиа-контекст — добавляем к промпту
        if media_context:
            prompt = f"{media_context}\n\n{prompt}" if prompt else media_context

        logger.info(f"[{'owner' if is_owner else user_id}] Received: {prompt[:100]}...")

        # Обновляем инфо owner'а из реальных данных Telegram
        if is_owner:
            set_owner_info(user_id, sender.first_name, sender.username)
        else:
            # Для external users сохраняем в БД
            repo = get_users_repository()
            await repo.upsert_user(
                telegram_id=user_id,
                username=sender.username,
                first_name=sender.first_name,
                last_name=sender.last_name,
                phone=sender.phone if hasattr(sender, 'phone') else None,
            )

            # Проверяем бан
            if await repo.is_user_banned(user_id):
                logger.info(f"[{user_id}] Banned user, ignoring")
                return

        # Добавляем метаданные юзера и время к промпту
        now = datetime.now(tz=settings.get_timezone())
        time_meta = now.strftime("%d.%m.%Y %H:%M")
        user_meta = self._format_user_meta(sender)
        prompt = f"[{time_meta}] {user_meta}\n\n{prompt}"

        input_chat = await event.get_input_chat()

        # Отмечаем как прочитанное
        await self._client.send_read_acknowledge(input_chat, message)

        # Включаем typing
        await self._set_typing(input_chat, typing=True)

        # Получаем сессию для этого пользователя
        session_manager = get_session_manager()
        user_display_name = sender.first_name or sender.username or str(user_id)
        session = session_manager.get_session(user_id, user_display_name)

        # Skills подхватываются автоматически через SDK (setting_sources=["project"])

        last_typing = asyncio.get_event_loop().time()
        has_sent_anything = False
        tool_msg = None  # Сообщение со статусом tool

        try:
            async for text, tool_name, is_final in session.query_stream(prompt):
                # Поддерживаем typing
                now = asyncio.get_event_loop().time()
                if now - last_typing > TYPING_REFRESH_INTERVAL:
                    await self._set_typing(input_chat, typing=True)
                    last_typing = now

                # Tool call — показываем статус
                if tool_name:
                    tool_display = self._format_tool(tool_name)
                    if tool_msg is None:
                        tool_msg = await event.reply(tool_display)
                    else:
                        await self._safe_edit(tool_msg, tool_display)
                    continue

                # Промежуточный текст — отправляем отдельным сообщением
                if text and not is_final:
                    text_clean = text.strip()
                    if text_clean:
                        # Удаляем сообщение о tool если было
                        if tool_msg:
                            await self._safe_delete(tool_msg)
                            tool_msg = None
                        await event.reply(self._prepare_response(prompt, text_clean))
                        has_sent_anything = True
                        # Восстанавливаем typing после отправки
                        await self._set_typing(input_chat, typing=True)
                        last_typing = asyncio.get_event_loop().time()

                # Финальный ответ — только если ничего не отправляли
                elif is_final and text and not has_sent_anything:
                    final_text = text.strip()
                    if final_text:
                        if tool_msg:
                            await self._safe_delete(tool_msg)
                            tool_msg = None
                        await event.reply(self._prepare_response(prompt, final_text))

        except Exception as e:
            logger.error(f"Error: {e}")
            await event.reply(f"Ошибка: {e}")

        finally:
            await self._set_typing(input_chat, typing=False)
            # Удаляем tool message если остался
            if tool_msg:
                await self._safe_delete(tool_msg)

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

    async def _safe_delete(self, message: Any) -> None:
        """Безопасно удаляет сообщение."""
        try:
            await message.delete()
        except Exception:
            pass

    def _format_user_meta(self, sender: Any) -> str:
        """Форматирует метаданные пользователя для добавления к промпту."""
        parts = [f"id: {sender.id}"]

        if sender.username:
            parts.append(f"@{sender.username}")

        name = sender.first_name or ""
        if sender.last_name:
            name = f"{name} {sender.last_name}".strip()
        if name:
            parts.append(name)

        if hasattr(sender, 'phone') and sender.phone:
            parts.append(f"tel: {sender.phone}")

        return f"[{' | '.join(parts)}]"

    def _format_tool(self, tool_name: str) -> str:
        """Форматирует название инструмента в читаемый вид."""
        # Skill:name → "⚡ Skill Name..."
        if tool_name.startswith("Skill:"):
            skill_name = tool_name.split(":", 1)[1]
            display = skill_name.replace("-", " ").replace("_", " ").title()
            return f"Skill: {display}..."

        # Убираем префиксы mcp__jobs__ и mcp__*__
        clean_name = tool_name
        if clean_name.startswith("mcp__"):
            parts = clean_name.split("__")
            clean_name = parts[-1] if len(parts) > 1 else clean_name

        tools_display = {
            # Файловые операции
            "Read": "Читаю файл...",
            "Write": "Записываю файл...",
            "Edit": "Редактирую...",
            "Glob": "Ищу файлы...",
            "Grep": "Ищу в коде...",
            # Системные
            "Bash": "Выполняю команду...",
            "Task": "Запускаю агента...",
            # Веб
            "WebFetch": "Загружаю страницу...",
            "WebSearch": "Ищу в интернете...",
            # Scheduler
            "schedule_task": "Планирую задачу...",
            "cancel_task": "Отменяю задачу...",
            # Triggers
            "subscribe_trigger": "Подписываюсь...",
            "unsubscribe_trigger": "Отписываюсь...",
            "list_triggers": "Подписки...",
            # Memory
            "memory_search": "Ищу в памяти...",
            "memory_read": "Читаю память...",
            "memory_append": "Сохраняю в память...",
            "memory_log": "Пишу в лог...",
            "memory_context": "Загружаю контекст...",
            # MCP Manager
            "mcp_search": "Ищу интеграцию...",
            "mcp_install": "Устанавливаю...",
            "mcp_list": "Список интеграций...",
            # User tools
            "create_task": "Создаю задачу...",
            "list_tasks": "Смотрю задачи...",
            "send_to_user": "Отправляю сообщение...",
            "resolve_user": "Ищу пользователя...",
            "list_users": "Список пользователей...",
            "ban_user": "Баню пользователя...",
            "unban_user": "Разбаниваю...",
            # External user tools
            "get_my_tasks": "Мои задачи...",
            "update_task": "Обновляю задачу...",
            "send_summary_to_owner": "Отправляю сводку...",
            "ban_violator": "Баню нарушителя...",
            # Telegram tools
            "tg_send_message": "Отправляю сообщение...",
            "tg_send_media": "Отправляю медиа...",
            "tg_forward_message": "Пересылаю...",
            "tg_send_comment": "Пишу комментарий...",
            "tg_get_participants": "Список участников...",
            "tg_read_channel": "Читаю канал...",
            "tg_read_comments": "Читаю комменты...",
            "tg_read_chat": "Читаю чат...",
            "tg_search_messages": "Ищу сообщения...",
            "tg_get_user_info": "Смотрю профиль...",
            "tg_get_dialogs": "Список чатов...",
            "tg_download_media": "Скачиваю медиа...",
            # Browser tools (Playwright MCP)
            "browser_navigate": "Открываю страницу...",
            "browser_navigate_back": "Назад...",
            "browser_snapshot": "Читаю страницу...",
            "browser_click": "Кликаю...",
            "browser_type": "Ввожу текст...",
            "browser_fill_form": "Заполняю поле...",
            "browser_select_option": "Выбираю...",
            "browser_hover": "Навожу курсор...",
            "browser_drag": "Перетаскиваю...",
            "browser_press_key": "Нажимаю клавишу...",
            "browser_take_screenshot": "Делаю скриншот...",
            "browser_evaluate": "Выполняю JS...",
            "browser_wait_for": "Жду...",
            "browser_console_messages": "Читаю консоль...",
            "browser_tabs": "Вкладки...",
            "browser_handle_dialog": "Обрабатываю диалог...",
            "browser_file_upload": "Загружаю файл...",
            "browser_close": "Закрываю браузер...",
        }

        return tools_display.get(clean_name, "Работаю...")

    async def _extract_content(self, message: Any) -> tuple[str, str | None]:
        """
        Извлекает контент из сообщения.

        Returns:
            (text, media_context) — текст и контекст медиа (путь к файлу или транскрипция)
        """
        text = message.text or ""
        media_context = None

        # Голосовое сообщение
        if message.voice:
            try:
                voice_data = await self._client.download_media(message.voice, bytes)
                result = await transcribe_audio(voice_data)
                media_context = f"[Голосовое сообщение]: {result.text}"
                logger.info(f"Voice transcribed: {result.text[:50]}...")
            except Exception as e:
                logger.error(f"Voice transcription failed: {e}")
                media_context = f"[Голосовое сообщение — ошибка транскрипции: {e}]"

        # Фото
        elif message.photo:
            try:
                photo_data = await self._client.download_media(message.photo, bytes)
                path = await save_media(photo_data, "photo.jpg", subfolder="photos")
                media_context = f"[Фото сохранено: {path}]"
            except Exception as e:
                logger.error(f"Photo save failed: {e}")

        # Документ (включая видео, аудио файлы)
        elif message.document:
            try:
                doc = message.document
                if doc.size and doc.size > MAX_MEDIA_SIZE:
                    size_mb = doc.size // 1024 // 1024
                    media_context = f"[Файл слишком большой: {size_mb} MB, макс {MAX_MEDIA_SIZE // 1024 // 1024} MB]"
                else:
                    # Получаем имя файла из атрибутов
                    filename = "document"
                    for attr in doc.attributes:
                        if hasattr(attr, "file_name"):
                            filename = attr.file_name
                            break

                    doc_data = await self._client.download_media(doc, bytes)
                    path = await save_media(doc_data, filename, subfolder="documents")
                    media_context = f"[Файл сохранён: {path}]"
            except Exception as e:
                logger.error(f"Document save failed: {e}")

        return text, media_context

    def _prepare_response(self, prompt: str, content: str) -> str:
        """Подготавливает ответ (Telegraph для длинных)."""
        if not content:
            return "Нет ответа"

        if len(content) <= MAX_TG_LENGTH:
            return content

        url = self._publish_telegraph(prompt, content)
        return url

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
