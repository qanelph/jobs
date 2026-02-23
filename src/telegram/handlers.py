"""
Telegram Handlers — обработка входящих сообщений.

Поддерживает multi-session архитектуру:
- Owner (tg_user_id) — полный доступ
- External users — ограниченный доступ с отдельными сессиями

Работает с любым Transport (Telethon / Bot).
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings, set_owner_info
from src.users import get_session_manager, get_users_repository
from src.users.tools import set_telegram_sender, set_context_sender, set_buffer_sender, set_task_executor
from src.triggers.executor import TriggerExecutor
from src.media import transcribe_audio, save_media, MAX_MEDIA_SIZE
from src.updater import Updater
from src.telegram.transport import Transport, TransportMode, IncomingMessage

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

    def __init__(self, transport: Transport, msg: IncomingMessage, is_premium: bool) -> None:
        self._transport = transport
        self._msg = msg
        self._is_premium = is_premium
        self._status_msg_id: int | None = None
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
        if self._status_msg_id:
            try:
                await self._transport.delete_message(self._msg.chat_id, self._status_msg_id)
            except Exception:
                pass
            self._status_msg_id = None

    async def _update(self) -> None:
        text, entities = self._render()
        if self._status_msg_id is None:
            self._status_msg_id = await self._transport.reply_with_entities(
                self._msg, text, entities,
            )
        else:
            try:
                await self._transport.edit_message(
                    self._msg.chat_id, self._status_msg_id, text, entities,
                )
            except Exception:
                pass

    def _render(self) -> tuple[str, list | None]:
        icon = "\u23f3" if self._is_premium else "\U0001fa9b"
        text = f"{icon} {self._active}"
        if self._done:
            text += f"\n\n\u2611\ufe0f {self._done}"

        entities = None
        if self._is_premium and self._transport.mode == TransportMode.TELETHON:
            from telethon.tl.types import MessageEntityCustomEmoji
            entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=LOADING_EMOJI_ID)]
        return text, entities


class TelegramHandlers:
    """Обработчики сообщений Telegram."""

    def __init__(self, primary_transport: Transport, executor: TriggerExecutor | None = None) -> None:
        self._primary = primary_transport
        self._is_premium: dict[TransportMode, bool] = {}
        self._updater = Updater()
        self._reply_targets: dict[str, IncomingMessage] = {}  # session_key → latest msg (для follow-up)

        # Настраиваем sender'ы для user tools
        set_telegram_sender(self._send_message)
        set_context_sender(self._inject_to_context)
        set_buffer_sender(self._buffer_to_context)
        if executor:
            set_task_executor(executor.execute)

    def register(self, transport: Transport) -> None:
        """Регистрирует обработчики на транспорт. Можно вызывать для нескольких."""
        transport.on_message(self._on_message)
        logger.info(f"Registered handler on {transport.mode.value} (owner: {settings.tg_user_id})")

    async def on_startup(self) -> None:
        """Вызывается после подключения. Проверяет pending update message."""
        pending = self._updater.load_pending_message()
        if not pending:
            return
        try:
            current = await self._updater._check()
            version = current.get("current", "")[:7]
            await self._primary.edit_message(
                pending["chat_id"],
                pending["message_id"],
                f"\u2705 Обновлено ({version})",
            )
            logger.info(f"Update confirmed: {version}")
        except Exception as e:
            logger.warning(f"Could not edit update message: {e}")

    async def _send_loading(self, msg: IncomingMessage, text: str) -> int:
        """Отправляет сообщение с loading-emoji (custom для premium Telethon)."""
        is_premium = await self._check_premium(msg.transport)
        icon = "\u23f3"
        entities = None
        if is_premium and msg.transport.mode == TransportMode.TELETHON:
            from telethon.tl.types import MessageEntityCustomEmoji
            entities = [MessageEntityCustomEmoji(offset=0, length=1, document_id=LOADING_EMOJI_ID)]
        return await msg.transport.reply_with_entities(msg, f"{icon} {text}", entities)

    async def _send_message(self, user_id: int, text: str) -> None:
        """Отправляет сообщение пользователю (для user tools)."""
        logger.info(f"_send_message: user_id={user_id}, text={text[:60]}...")
        await self._primary.send_message(user_id, text)

        session_manager = get_session_manager()

        # Буферизуем во ВСЕ сессии получателя (Telethon + Bot)
        sessions = session_manager.get_user_sessions(user_id)
        if not sessions:
            # Создаём дефолтную сессию если нет ни одной
            sessions = [session_manager.get_session(user_id)]
        for s in sessions:
            s.receive_incoming(text)
        logger.info(f"Buffered for [{user_id}] in {len(sessions)} session(s)")

        # Если получатель — owner и ни одна сессия не занята, запускаем автономный query
        if user_id == settings.tg_user_id:
            any_querying = any(s._is_querying for s in sessions)
            logger.info(f"Owner is recipient, any_querying={any_querying}")
            if not any_querying:
                logger.info("Triggering autonomous query for owner")
                asyncio.create_task(self._process_incoming(user_id))

    async def _inject_to_context(self, user_id: int, text: str) -> None:
        """Инжектит сообщение в контекст сессии + триггерит autonomous query."""
        session_manager = get_session_manager()
        sessions = session_manager.get_user_sessions(user_id)
        if not sessions:
            sessions = [session_manager.get_session(user_id)]
        for s in sessions:
            s.receive_incoming(text)
        logger.info(f"Injected to context [{user_id}] in {len(sessions)} session(s)")

        if user_id == settings.tg_user_id:
            if not any(s._is_querying for s in sessions):
                asyncio.create_task(self._process_incoming(user_id))

    async def _buffer_to_context(self, user_id: int, text: str) -> None:
        """Тихая буферизация в контекст без autonomous query trigger."""
        session_manager = get_session_manager()
        sessions = session_manager.get_user_sessions(user_id)
        if not sessions:
            sessions = [session_manager.get_session(user_id)]
        for s in sessions:
            s.receive_incoming(text)
        logger.info(f"Buffered to context [{user_id}] in {len(sessions)} session(s)")

    async def _process_incoming(self, user_id: int) -> None:
        """Автономный query для обработки входящих сообщений."""
        logger.info(f"_process_incoming started for [{user_id}]")
        try:
            session_manager = get_session_manager()
            session = session_manager.get_session(user_id)

            if not session._incoming:
                logger.warning(f"_process_incoming: buffer empty for [{user_id}]")
                return

            messages = session._incoming.copy()
            session._incoming.clear()
            session._clear_incoming_file()
            logger.info(f"_process_incoming: captured {len(messages)} messages")

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
                await self._primary.send_message(user_id, response[:MAX_TG_LENGTH])
            else:
                logger.info("Owner autonomous query: no actionable response")
        except Exception as e:
            logger.error(f"Incoming processing error [{user_id}]: {e}")

    async def _on_message(self, msg: IncomingMessage) -> None:
        """Обрабатывает входящее сообщение (только private chats)."""
        # Пропускаем каналы и группы — ими занимаются trigger subscriptions
        if msg.is_channel or msg.is_group:
            return

        if not msg.sender_id:
            return

        user_id = msg.sender_id
        is_owner = user_id == settings.tg_user_id
        transport = msg.transport
        channel = transport.mode.value  # "telethon" или "bot"
        session_key = f"{channel}:{user_id}"

        # /help — список команд
        if msg.text and msg.text.strip().lower() == "/help":
            help_text = (
                "`/stop` — прервать текущий запрос\n"
                "`/clear` — сбросить сессию\n"
                "`/usage` — лимиты API\n"
                "`/update` — обновить бота до последней версии"
            )
            await transport.reply(msg, help_text)
            return

        # /clear — сброс сессии (только текущий транспорт)
        if msg.text and msg.text.strip().lower() == "/clear":
            session_manager = get_session_manager()
            await session_manager.reset_session(user_id, channel=channel)
            await transport.reply(msg, "Сессия сброшена.")
            return

        # /stop — прервать текущий запрос
        if msg.text and msg.text.strip().lower() == "/stop":
            if not is_owner:
                return
            session_manager = get_session_manager()
            session = session_manager.get_session(user_id, channel=channel)
            if session._is_querying and session._client:
                await session._client.interrupt()
                await transport.reply(msg, "Остановлено.")
            else:
                await transport.reply(msg, "Нечего останавливать.")
            return

        # /update — обновление из git (только owner)
        if msg.text and msg.text.strip().lower() == "/update":
            if not is_owner:
                return
            result = await self._updater.handle()
            if isinstance(result, dict) and result.get("loading"):
                status_id = await self._send_loading(msg, "Устанавливаю обновление...")
                self._updater.save_loading_message(msg.chat_id, status_id)
            else:
                await transport.reply(msg, result)
            return

        # /usage — показать usage аккаунта (только owner)
        if msg.text and msg.text.strip().lower() == "/usage":
            if not is_owner:
                return
            await transport.reply(msg, await self._fetch_usage())
            return

        # Обработка разных типов сообщений
        prompt, media_context = await self._extract_content(msg)

        if not prompt and not media_context:
            return

        if media_context:
            prompt = f"{media_context}\n\n{prompt}" if prompt else media_context

        logger.info(f"[{'owner' if is_owner else user_id}] Received: {prompt[:100]}...")

        # Обновляем инфо owner'а
        if is_owner:
            set_owner_info(user_id, msg.sender_first_name, msg.sender_username, msg.sender_phone)
        else:
            repo = get_users_repository()
            await repo.upsert_user(
                telegram_id=user_id,
                username=msg.sender_username,
                first_name=msg.sender_first_name,
                last_name=msg.sender_last_name,
                phone=msg.sender_phone,
            )
            if await repo.is_user_banned(user_id):
                logger.info(f"[{user_id}] Banned user, ignoring")
                return

        # Добавляем время и оборачиваем в системные теги
        now = datetime.now(tz=settings.get_timezone())
        time_meta = now.strftime("%d.%m.%Y %H:%M")
        prompt = _sanitize_tags(prompt)
        prompt = f"[{time_meta}]\n<message-body>\n{prompt}\n</message-body>"

        # Отмечаем как прочитанное
        await transport.mark_read(msg.chat_id, msg.message_id)

        # Включаем typing
        await transport.set_typing(msg.chat_id, typing=True)

        # Получаем сессию для этого пользователя + транспорта
        session_manager = get_session_manager()
        user_display_name = msg.sender_first_name or msg.sender_username or str(user_id)
        session = session_manager.get_session(user_id, user_display_name, channel=channel)

        # Если сессия уже обрабатывает запрос — буферизуем в incoming
        if session._is_querying:
            session.receive_incoming(prompt)
            self._reply_targets[session_key] = msg
            logger.info(f"[{'owner' if is_owner else user_id}] Buffered (session busy), queue: {len(session._incoming)}")
            return

        last_typing = asyncio.get_event_loop().time()
        status = StatusTracker(transport, msg, await self._check_premium(transport))

        try:
            async for text, tool_name, is_final in session.query_stream(prompt):
                # Перепривязка к новому сообщению при follow-up
                new_msg = self._reply_targets.pop(session_key, None)
                if new_msg:
                    await status.delete()
                    msg = new_msg
                    status = StatusTracker(new_msg.transport, new_msg, await self._check_premium(new_msg.transport))

                now_time = asyncio.get_event_loop().time()
                if now_time - last_typing > TYPING_REFRESH_INTERVAL:
                    await transport.set_typing(msg.chat_id, typing=True)
                    last_typing = now_time

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
                        await transport.reply(msg, final_text)

        except Exception as e:
            logger.error(f"Error: {e}")
            await status.delete()
            await transport.reply(msg, f"Ошибка: {e}")

        finally:
            await transport.set_typing(msg.chat_id, typing=False)
            await status.delete()

    async def _check_premium(self, transport: Transport) -> bool:
        """Проверяет наличие premium у аккаунта (с кешированием per-transport)."""
        mode = transport.mode
        if mode not in self._is_premium:
            try:
                me = await transport.get_me()
                self._is_premium[mode] = bool(me.get("is_premium", False))
            except Exception:
                self._is_premium[mode] = False
        return self._is_premium[mode]

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
        return "\u2593" * filled + "\u2591" * (5 - filled)

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

    async def _extract_content(self, msg: IncomingMessage) -> tuple[str, str | None]:
        """
        Извлекает контент из сообщения.

        Returns:
            (text, media_context) — текст и контекст медиа (путь к файлу или транскрипция)
        """
        text = msg.text or ""
        media_context = None
        transport = msg.transport

        # Голосовое сообщение
        if msg.has_voice:
            try:
                voice_data = await transport.download_media(msg)
                if voice_data:
                    result = await transcribe_audio(voice_data)
                    media_context = f"[Голосовое сообщение]: {result.text}"
                    logger.info(f"Voice transcribed: {result.text[:50]}...")
            except Exception as e:
                logger.error(f"Voice transcription failed: {e}")
                media_context = f"[Голосовое сообщение — ошибка транскрипции: {e}]"

        # Фото
        elif msg.has_photo:
            try:
                photo_data = await transport.download_media(msg)
                if photo_data:
                    path = await save_media(photo_data, "photo.jpg", subfolder="photos")
                    media_context = f"[Фото сохранено: {path}]"
            except Exception as e:
                logger.error(f"Photo save failed: {e}")

        # Документ
        elif msg.has_document:
            try:
                if msg.document_size and msg.document_size > MAX_MEDIA_SIZE:
                    size_mb = msg.document_size // 1024 // 1024
                    media_context = f"[Файл слишком большой: {size_mb} MB, макс {MAX_MEDIA_SIZE // 1024 // 1024} MB]"
                else:
                    filename = msg.document_name or "document"
                    doc_data = await transport.download_media(msg)
                    if doc_data:
                        path = await save_media(doc_data, filename, subfolder="documents")
                        media_context = f"[Файл сохранён: {path}]"
            except Exception as e:
                logger.error(f"Document save failed: {e}")

        return text, media_context
