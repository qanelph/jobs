"""
Telegram Handlers — обработка входящих сообщений.

Поддерживает multi-session архитектуру:
- Owner (tg_user_id) — полный доступ
- External users — ограниченный доступ с отдельными сессиями
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
from telethon import TelegramClient, events
from telethon.tl.functions.messages import SetTypingRequest
from telethon.tl.types import SendMessageTypingAction, SendMessageCancelAction, MessageEntityCustomEmoji
from loguru import logger

from src.config import settings, set_owner_info
from src.users import get_session_manager, get_users_repository
from src.users.tools import set_telegram_sender, set_context_sender, set_buffer_sender, set_task_executor
from src.triggers.executor import TriggerExecutor
from src.media import transcribe_audio, save_media, MAX_MEDIA_SIZE
from src.updater import Updater

MAX_TG_LENGTH = 4000
TYPING_REFRESH_INTERVAL = 3.0
LOADING_EMOJI_ID = 5255778087437617493
MAX_DONE_LENGTH = 200

_SYSTEM_TAGS_RE = re.compile(r'<\s*/?(?:message-body|sender-meta)\s*/?\s*>', re.IGNORECASE)


def _sanitize_tags(text: str) -> str:
    """Удаляет системные теги из пользовательского ввода."""
    return _SYSTEM_TAGS_RE.sub('', text)


class StatusTracker:
    """Управляет статусным сообщением с двумя слотами: active (тул) и done (результат)."""

    def __init__(self, event: Any, is_premium: bool) -> None:
        self._event = event
        self._is_premium = is_premium
        self._msg: Any | None = None
        self._active: str | None = None
        self._done: str | None = None

    async def set_active(self, text: str) -> None:
        """Обновляет верхний слот (текущее действие)."""
        self._active = text
        await self._update()

    async def set_done(self, text: str) -> None:
        """Обновляет нижний слот (результат предыдущего действия)."""
        self._done = text[:MAX_DONE_LENGTH] if len(text) > MAX_DONE_LENGTH else text
        if self._active:
            await self._update()

    async def delete(self) -> None:
        """Удаляет статусное сообщение."""
        if self._msg:
            try:
                await self._msg.delete()
            except Exception:
                pass
            self._msg = None

    async def _update(self) -> None:
        text, entities = self._render()
        if self._msg is None:
            self._msg = await self._event.reply(text, formatting_entities=entities)
        else:
            try:
                await self._msg.edit(text, formatting_entities=entities)
            except Exception:
                pass

    def _render(self) -> tuple[str, list | None]:
        icon = "⏳" if self._is_premium else "🪛"
        text = f"{icon} {self._active}"
        if self._done:
            text += f"\n\n☑️ {self._done}"

        entities = None
        if self._is_premium:
            entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=LOADING_EMOJI_ID)]
        return text, entities


class TelegramHandlers:
    """Обработчики сообщений Telegram."""

    def __init__(self, client: TelegramClient, executor: TriggerExecutor | None = None) -> None:
        self._client = client
        self._is_premium: bool | None = None  # Lazy-init
        self._updater = Updater()
        self._reply_targets: dict[int, Any] = {}  # user_id → latest event (для follow-up)

        # Настраиваем sender'ы для user tools
        set_telegram_sender(self._send_message)
        set_context_sender(self._inject_to_context)
        set_buffer_sender(self._buffer_to_context)
        if executor:
            set_task_executor(executor.execute)

    def register(self) -> None:
        """Регистрирует обработчики событий."""
        self._client.add_event_handler(
            self._on_message,
            events.NewMessage(incoming=True),
        )
        logger.info(f"Registered handler for all users (owner: {settings.tg_user_id})")

    async def on_startup(self) -> None:
        """Вызывается после подключения. Проверяет pending update message."""
        pending = self._updater.load_pending_message()
        if not pending:
            return
        try:
            current = await self._updater._check()
            version = current.get("current", "")[:7]
            await self._client.edit_message(
                pending["chat_id"],
                pending["message_id"],
                f"\u2705 Обновлено ({version})",
            )
            logger.info(f"Update confirmed: {version}")
        except Exception as e:
            logger.warning(f"Could not edit update message: {e}")

    async def _send_loading(self, event: Any, text: str) -> Any:
        """Отправляет сообщение с loading-emoji (custom для premium)."""
        is_premium = await self._check_premium()
        icon = "\u23f3"
        entities = None
        if is_premium:
            entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=LOADING_EMOJI_ID)]
        return await event.reply(f"{icon} {text}", formatting_entities=entities)

    async def _send_message(self, user_id: int, text: str) -> None:
        """Отправляет сообщение пользователю (для user tools)."""
        logger.info(f"_send_message: user_id={user_id}, text={text[:60]}...")
        await self._client.send_message(user_id, text)

        session_manager = get_session_manager()

        # Всегда создаём/получаем сессию получателя (get_session идемпотентен)
        recipient = session_manager.get_session(user_id)
        recipient.receive_incoming(text)
        logger.info(f"Buffered for [{user_id}], buffer size: {len(recipient._incoming)}")

        # Если получатель — owner и он не в активном запросе, запускаем автономный query
        if user_id == settings.tg_user_id:
            is_querying = recipient._is_querying
            logger.info(f"Owner is recipient, is_querying={is_querying}")
            if not is_querying:
                logger.info("Triggering autonomous query for owner")
                asyncio.create_task(self._process_incoming(user_id))

    async def _inject_to_context(self, user_id: int, text: str) -> None:
        """Инжектит сообщение в контекст сессии + триггерит autonomous query."""
        session_manager = get_session_manager()
        recipient = session_manager.get_session(user_id)
        recipient.receive_incoming(text)
        logger.info(f"Injected to context [{user_id}], buffer: {len(recipient._incoming)}")

        if user_id == settings.tg_user_id:
            if not recipient._is_querying:
                asyncio.create_task(self._process_incoming(user_id))

    async def _buffer_to_context(self, user_id: int, text: str) -> None:
        """Тихая буферизация в контекст без autonomous query trigger."""
        session_manager = get_session_manager()
        recipient = session_manager.get_session(user_id)
        recipient.receive_incoming(text)
        logger.info(f"Buffered to context [{user_id}], buffer: {len(recipient._incoming)}")

    async def _process_incoming(self, user_id: int) -> None:
        """
        Автономный query для обработки входящих сообщений.

        Защита от race condition:
        - Атомарно забираем буфер ДО query
        - Включаем сообщения явно в prompt
        - session.query() вызовет _consume_incoming(), но буфер уже пуст
        """
        logger.info(f"_process_incoming started for [{user_id}]")
        try:
            session_manager = get_session_manager()
            session = session_manager.get_session(user_id)

            # Атомарно забираем буфер — защита от race condition
            if not session._incoming:
                logger.warning(f"_process_incoming: buffer empty for [{user_id}]")
                return

            # Копируем и очищаем буфер ДО query (включая файл)
            messages = session._incoming.copy()
            session._incoming.clear()
            session._clear_incoming_file()
            logger.info(f"_process_incoming: captured {len(messages)} messages")

            # Формируем prompt с сообщениями явно
            incoming_text = "\n".join(["[Входящие сообщения:]"] + messages + ["[Конец входящих]"])
            prompt = (
                f"{incoming_text}\n\n"
                "[Входящее уведомление от другой сессии. "
                "Обработай информацию и кратко сообщи owner'у результат. "
                "Выполни необходимые действия автоматически — schedule_task, send_to_user и т.д. "
                "Не жди подтверждения от owner'а. НЕ дублируй текст уведомления дословно.]"
            )

            response = await session.query(prompt)

            if response and response != "Нет ответа":
                logger.info(f"Owner autonomous response: {response[:80]}...")
                await self._client.send_message(user_id, response[:MAX_TG_LENGTH])
            else:
                logger.info("Owner autonomous query: no actionable response")
        except Exception as e:
            logger.error(f"Incoming processing error [{user_id}]: {e}")

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

        # /help — список команд
        if message.text and message.text.strip().lower() == "/help":
            help_text = (
                "`/stop` — прервать текущий запрос\n"
                "`/clear` — сбросить сессию\n"
                "`/usage` — лимиты API\n"
                "`/update` — обновить бота до последней версии"
            )
            await event.reply(help_text)
            return

        # /clear — сброс сессии
        if message.text and message.text.strip().lower() == "/clear":
            session_manager = get_session_manager()
            await session_manager.reset_session(user_id)
            await event.reply("Сессия сброшена.")
            return

        # /stop — прервать текущий запрос (сессия сохраняется)
        if message.text and message.text.strip().lower() == "/stop":
            if not is_owner:
                return
            session_manager = get_session_manager()
            session = session_manager.get_session(user_id)
            if session._is_querying and session._client:
                await session._client.interrupt()
                await event.reply("Остановлено.")
            else:
                await event.reply("Нечего останавливать.")
            return

        # /update — обновление из git (только owner)
        if message.text and message.text.strip().lower() == "/update":
            if not is_owner:
                return
            result = await self._updater.handle()
            if isinstance(result, dict) and result.get("loading"):
                msg = await self._send_loading(event, "Устанавливаю обновление...")
                self._updater.save_loading_message(msg.chat_id, msg.id)
            else:
                await event.reply(result)
            return

        # /usage — показать usage аккаунта (только owner)
        if message.text and message.text.strip().lower() == "/usage":
            if not is_owner:
                return
            await event.reply(await self._fetch_usage())
            return

        # Обработка разных типов сообщений
        prompt, media_context = await self._extract_content(message)

        if not prompt and not media_context:
            return

        # Если есть медиа-контекст — добавляем к промпту
        if media_context:
            prompt = f"{media_context}\n\n{prompt}" if prompt else media_context

        # Обработка пересланных сообщений — добавляем метку отправителя
        if message.forward:
            prefix = "[Переслано от: скрытый профиль]"
            try:
                fwd_sender = await message.forward.get_sender()
                if fwd_sender:
                    name = getattr(fwd_sender, 'first_name', '') or ''
                    last = getattr(fwd_sender, 'last_name', '') or ''
                    uname = getattr(fwd_sender, 'username', '') or ''
                    uid = getattr(fwd_sender, 'id', '')
                    prefix = f"[Переслано от: {name} {last} (@{uname}, ID: {uid})]"
            except Exception:
                pass
            prompt = f"{prefix}\n{prompt}"

        logger.info(f"[{'owner' if is_owner else user_id}] Received: {prompt[:100]}...")

        # Обновляем инфо owner'а из реальных данных Telegram
        if is_owner:
            set_owner_info(user_id, sender.first_name, sender.username, getattr(sender, 'phone', None))
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

        # Добавляем время и оборачиваем в системные теги
        now = datetime.now(tz=settings.get_timezone())
        time_meta = now.strftime("%d.%m.%Y %H:%M")
        prompt = _sanitize_tags(prompt)
        prompt = f"[{time_meta}]\n<message-body>\n{prompt}\n</message-body>"

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

        # Если сессия уже обрабатывает запрос — буферизуем в incoming
        # Follow-up цикл в query_stream подхватит это сообщение
        if session._is_querying:
            session.receive_incoming(prompt)
            self._reply_targets[user_id] = event
            logger.info(f"[{'owner' if is_owner else user_id}] Buffered (session busy), queue: {len(session._incoming)}")
            return

        last_typing = asyncio.get_event_loop().time()
        status = StatusTracker(event, await self._check_premium())

        try:
            async for text, tool_name, is_final in session.query_stream(prompt):
                # Перепривязка к новому сообщению при follow-up
                new_event = self._reply_targets.pop(user_id, None)
                if new_event:
                    await status.delete()
                    event = new_event
                    status = StatusTracker(event, await self._check_premium())

                now = asyncio.get_event_loop().time()
                if now - last_typing > TYPING_REFRESH_INTERVAL:
                    await self._set_typing(input_chat, typing=True)
                    last_typing = now

                if tool_name:
                    await status.set_active(self._format_tool(tool_name))
                elif text and not is_final:
                    text_clean = text.strip()
                    if text_clean:
                        await status.set_done(text_clean)
                elif is_final and text:
                    final_text = text.strip()
                    if final_text:
                        await status.delete()
                        await event.reply(final_text)

        except Exception as e:
            logger.error(f"Error: {e}")
            await status.delete()
            await event.reply(f"Ошибка: {e}")

        finally:
            await self._set_typing(input_chat, typing=False)
            await status.delete()

    async def _set_typing(self, chat: Any, typing: bool) -> None:
        """Устанавливает статус typing."""
        try:
            action = SendMessageTypingAction() if typing else SendMessageCancelAction()
            await self._client(SetTypingRequest(peer=chat, action=action))
        except Exception as e:
            logger.debug(f"Typing status error: {e}")

    async def _check_premium(self) -> bool:
        """Проверяет наличие premium у аккаунта (с кешированием)."""
        if self._is_premium is None:
            try:
                me = await self._client.get_me()
                self._is_premium = bool(getattr(me, "premium", False))
            except Exception:
                self._is_premium = False
        return self._is_premium

    async def _fetch_usage(self) -> str:
        """Запрашивает usage аккаунта через OAuth API."""
        creds_file = settings.claude_dir / ".credentials.json"
        if not creds_file.exists():
            return "Credentials не найдены"

        creds = json.loads(creds_file.read_text())
        token = creds.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            return "OAuth токен не найден"

        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.0.31",
        }

        proxy = settings.http_proxy or None
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers=headers,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"Usage API error {resp.status}: {body[:200]}"
                data = await resp.json()

        windows = [
            ("five_hour", "за 5ч"),
            ("seven_day", "за 7д"),
            ("seven_day_opus", "opus 7д"),
            ("seven_day_sonnet", "sonnet 7д"),
        ]

        lines: list[str] = []
        for key, label in windows:
            info = data.get(key)
            if not info:
                continue
            pct = info.get("utilization", 0)
            bar = self._usage_bar(pct)
            reset = info.get("resets_at")
            reset_str = ""
            if reset:
                reset_at = datetime.fromisoformat(reset)
                delta = reset_at - datetime.now(timezone.utc)
                total_min = int(delta.total_seconds() / 60)
                if total_min <= 0:
                    reset_str = ", сброс сейчас"
                elif total_min < 60:
                    reset_str = f", сброс через {total_min}мин"
                elif total_min < 1440:
                    h, m = divmod(total_min, 60)
                    reset_str = f", сброс через {h}ч {m}мин" if m else f", сброс через {h}ч"
                else:
                    d, rem = divmod(total_min, 1440)
                    h = rem // 60
                    reset_str = f", сброс через {d}д {h}ч" if h else f", сброс через {d}д"
            lines.append(f"{bar} {pct:.0f}% {label}{reset_str}")

        extra = data.get("extra_usage", {})
        if extra and extra.get("is_enabled"):
            used = extra.get("used_credits") or 0
            limit = extra.get("monthly_limit") or 0
            lines.append(f"доп: ${used:.2f} / ${limit:.2f}")

        return "\n".join(lines) if lines else "Нет данных"

    @staticmethod
    def _usage_bar(pct: float) -> str:
        filled = round(pct / 100 * 5)
        return "▓" * filled + "░" * (5 - filled)

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
        """Форматирует название тула в читаемый текст."""
        if tool_name.startswith("Skill:"):
            skill_name = tool_name.split(":", 1)[1]
            display = skill_name.replace("-", " ").replace("_", " ").title()
            return f"Skill: {display}..."

        if tool_name.startswith("Bash:"):
            command = tool_name.split(":", 1)[1].strip()
            if len(command) > 120:
                command = command[:120] + "..."
            return f"Выполняю команду...\n\n{command}"

        # Убираем префиксы mcp__*__
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
            "browser_proxy": "Переключаю прокси...",
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

