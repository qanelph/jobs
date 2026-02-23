"""
SessionManager — управление сессиями Claude для разных пользователей.

Архитектура:
- Клиент создаётся на каждый запрос и уничтожается после
- session_id сохраняется в файл для resume
- Входящие во время запроса подмешиваются через follow-up
"""

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from loguru import logger

from src.config import settings, get_owner_display_name, get_owner_link
from src.mcp_manager.config import get_mcp_config
from src.plugin_manager.config import get_plugin_config


QUERY_TIMEOUT_SECONDS = 7200  # 2 часа


class UserSession:
    """
    Сессия Claude для конкретного пользователя.

    Клиент создаётся на каждый запрос:
    1. connect() → query() → receive_response() → drain_incoming() → disconnect()
    2. session_id сохраняется для resume следующего запроса
    3. Входящие во время запроса подмешиваются через follow-up в тот же клиент
    """

    def __init__(
        self,
        telegram_id: int,
        session_dir: Path,
        system_prompt: str,
        is_owner: bool = False,
        allowed_tools: list[str] | None = None,
        session_key: str | None = None,
    ) -> None:
        self.telegram_id = telegram_id
        self.is_owner = is_owner
        self._system_prompt = system_prompt
        self._allowed_tools_override = allowed_tools
        key = session_key or str(telegram_id)
        self._session_file = session_dir / f"{key}.session"
        self._incoming_file = session_dir / f"{key}.incoming"
        self._session_id: str | None = self._load_session_id()
        self._incoming: list[str] = self._load_incoming()
        self._is_querying: bool = False
        self._client: ClaudeSDKClient | None = None
        self._query_lock: asyncio.Lock = asyncio.Lock()

        from src.tools import create_tools_server
        self._tools_server = create_tools_server()

    def _load_session_id(self) -> str | None:
        """Загружает session_id из файла."""
        if self._session_file.exists():
            session_id = self._session_file.read_text().strip()
            if session_id:
                logger.debug(f"Loaded session [{self.telegram_id}]: {session_id[:8]}...")
                return session_id
        return None

    def _save_session_id(self, session_id: str) -> None:
        """Сохраняет session_id в файл."""
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(session_id)
        self._session_id = session_id
        logger.debug(f"Saved session [{self.telegram_id}]: {session_id[:8]}...")

    def _load_incoming(self) -> list[str]:
        """Загружает буфер входящих из файла."""
        if self._incoming_file.exists():
            try:
                data = json.loads(self._incoming_file.read_text())
                if isinstance(data, list):
                    logger.debug(f"Loaded {len(data)} incoming messages [{self.telegram_id}]")
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load incoming [{self.telegram_id}]: {e}")
        return []

    def _save_incoming(self) -> None:
        """Сохраняет буфер входящих в файл."""
        self._incoming_file.parent.mkdir(parents=True, exist_ok=True)
        self._incoming_file.write_text(json.dumps(self._incoming, ensure_ascii=False))

    def _clear_incoming_file(self) -> None:
        """Удаляет файл буфера."""
        if self._incoming_file.exists():
            self._incoming_file.unlink()

    async def _get_task_context(self) -> str:
        """Возвращает контекст активных задач для external users."""
        if self.is_owner:
            return ""

        from src.users.repository import get_users_repository
        from src.users.prompts import format_task_context

        repo = get_users_repository()
        tasks = await repo.list_tasks(assignee_id=self.telegram_id, include_done=False)
        return format_task_context(tasks)

    def receive_incoming(self, text: str) -> None:
        """Добавляет входящее сообщение от другой сессии (персистентно)."""
        self._incoming.append(text[:2000])
        self._save_incoming()

    def _consume_incoming(self) -> str:
        """Забирает входящие сообщения и очищает буфер (включая файл)."""
        if not self._incoming:
            return ""

        lines = ["[Входящие сообщения:]"]
        for msg in self._incoming:
            lines.append(msg)
        lines.append("[Конец входящих]\n")

        self._incoming.clear()
        self._clear_incoming_file()
        return "\n".join(lines)

    def _build_options(self) -> ClaudeAgentOptions:
        """Создаёт опции для клиента."""
        env = os.environ.copy()
        if settings.http_proxy:
            env["HTTP_PROXY"] = settings.http_proxy
            env["HTTPS_PROXY"] = settings.http_proxy

        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        mcp_servers = {"jobs": self._tools_server}

        if self.is_owner:
            mcp_config = get_mcp_config()
            external_servers = mcp_config.to_mcp_json()
            mcp_servers.update(external_servers)

            mcp_servers["browser"] = {
                "command": "playwright-cdp-wrapper",
                "args": [
                    settings.browser_cdp_url,
                    "--timeout-action", "5000",
                    "--timeout-navigation", "15000",
                    "--ignore-https-errors",
                ],
                "env": {
                    "NO_PROXY": "browser,localhost,127.0.0.1",
                    "HTTP_PROXY": "",
                    "HTTPS_PROXY": "",
                },
            }

        if self._allowed_tools_override is not None:
            allowed_tools = self._allowed_tools_override
        else:
            from src.tools import get_owner_allowed_tools, EXTERNAL_ALLOWED_TOOLS
            allowed_tools = get_owner_allowed_tools() if self.is_owner else EXTERNAL_ALLOWED_TOOLS

        permission_mode = "bypassPermissions" if self.is_owner else "default"

        plugins = []
        if self.is_owner:
            plugin_config = get_plugin_config()
            plugins = plugin_config.to_sdk_format()

        options = ClaudeAgentOptions(
            model=settings.claude_model,
            cwd=Path(settings.workspace_dir),
            permission_mode=permission_mode,
            env=env,
            mcp_servers=mcp_servers,
            allowed_tools=allowed_tools,
            system_prompt=self._system_prompt,
            setting_sources=["project"],
            plugins=plugins,
            max_buffer_size=100 * 1024 * 1024,
        )

        if self._session_id:
            options.resume = self._session_id

        return options

    async def _create_client(self) -> ClaudeSDKClient:
        """Создаёт и подключает новый клиент."""
        options = self._build_options()
        client = ClaudeSDKClient(options=options)
        await client.connect()
        logger.debug(f"Client connected [{self.telegram_id}]")
        return client

    async def _destroy_client(self, client: ClaudeSDKClient) -> None:
        """Отключает и уничтожает клиент."""
        try:
            await client.disconnect()
            logger.debug(f"Client disconnected [{self.telegram_id}]")
        except Exception:
            pass

    async def query(self, prompt: str) -> str:
        """Отправляет запрос и возвращает ответ."""
        task_context = await self._get_task_context()
        incoming = self._consume_incoming()

        parts = []
        if task_context:
            parts.append(task_context)
        if incoming:
            parts.append(incoming)
        parts.append(prompt)
        full_prompt = "\n".join(parts)

        text_parts: list[str] = []

        async with self._query_lock:
            self._is_querying = True
            client = await self._create_client()
            self._client = client

            try:
                async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
                    await client.query(full_prompt)
                    interrupted = False

                    async for message in client.receive_response():
                        if message is None:
                            continue
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    text_parts.append(block.text)
                        elif isinstance(message, ResultMessage):
                            if message.session_id:
                                self._save_session_id(message.session_id)

                        if not interrupted and self._incoming:
                            await client.interrupt()
                            interrupted = True
                            logger.debug(f"Interrupted response [{self.telegram_id}]")

                    # Follow-up для входящих, накопившихся во время запроса
                    while self._incoming:
                        incoming_buf = self._consume_incoming()
                        follow_up = (
                            f"{incoming_buf}[Продолжай с учётом новых сообщений. "
                            "Выполни необходимые действия автоматически.]"
                        )
                        await client.query(follow_up)
                        interrupted = False

                        async for message in client.receive_response():
                            if message is None:
                                continue
                            if isinstance(message, AssistantMessage):
                                for block in message.content:
                                    if isinstance(block, TextBlock):
                                        text_parts.append(block.text)
                            elif isinstance(message, ResultMessage):
                                if message.session_id:
                                    self._save_session_id(message.session_id)

                            if not interrupted and self._incoming:
                                await client.interrupt()
                                interrupted = True
                                logger.debug(f"Interrupted follow-up [{self.telegram_id}]")

            except TimeoutError:
                logger.error(f"Query timeout [{self.telegram_id}]")
                return "Ошибка: таймаут запроса"

            except Exception as e:
                logger.error(f"Query error [{self.telegram_id}]: {type(e).__name__}: {e}")
                return f"Ошибка: {e}"

            finally:
                self._is_querying = False
                self._client = None
                await self._destroy_client(client)

        return text_parts[-1] if text_parts else "Нет ответа"

    @staticmethod
    def _format_tool_display(block: ToolUseBlock) -> str:
        """Формирует display-имя тула для стрима."""
        if block.name == "Skill" and block.input.get("skill"):
            return f"Skill:{block.input['skill']}"
        if block.name == "Bash" and block.input.get("command"):
            return f"Bash:{block.input['command']}"
        return block.name

    async def query_stream(self, prompt: str) -> AsyncIterator[tuple[str | None, str | None, bool]]:
        """
        Стримит ответ.

        Yields:
            (text, tool_name, is_final)
        """
        task_context = await self._get_task_context()
        incoming = self._consume_incoming()

        parts = []
        if task_context:
            parts.append(task_context)
        if incoming:
            parts.append(incoming)
        parts.append(prompt)
        full_prompt = "\n".join(parts)

        text_buffer: list[str] = []

        await self._query_lock.acquire()
        self._is_querying = True
        client = await self._create_client()
        self._client = client

        try:
            async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
                await client.query(full_prompt)
                interrupted = False

                async for message in client.receive_response():
                    if message is None:
                        continue
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_buffer.append(block.text)
                                yield (block.text, None, False)
                            elif isinstance(block, ToolUseBlock):
                                tool_display = self._format_tool_display(block)
                                yield (None, tool_display, False)

                    elif isinstance(message, ResultMessage):
                        if message.session_id:
                            self._save_session_id(message.session_id)

                    if not interrupted and self._incoming:
                        await client.interrupt()
                        interrupted = True
                        logger.debug(f"Interrupted response [{self.telegram_id}]")

                # Follow-up для входящих, накопившихся во время запроса
                while self._incoming:
                    incoming_buf = self._consume_incoming()
                    follow_up = (
                        f"{incoming_buf}[Продолжай с учётом новых сообщений. "
                        "Выполни необходимые действия автоматически.]"
                    )
                    logger.debug(f"Follow-up query [{self.telegram_id}]")
                    await client.query(follow_up)
                    interrupted = False

                    async for message in client.receive_response():
                        if message is None:
                            continue
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    text_buffer.append(block.text)
                                    yield (block.text, None, False)
                                elif isinstance(block, ToolUseBlock):
                                    tool_display = self._format_tool_display(block)
                                    yield (None, tool_display, False)

                        elif isinstance(message, ResultMessage):
                            if message.session_id:
                                self._save_session_id(message.session_id)

                        if not interrupted and self._incoming:
                            await client.interrupt()
                            interrupted = True
                            logger.debug(f"Interrupted follow-up [{self.telegram_id}]")

                yield (text_buffer[-1] if text_buffer else None, None, True)

        except TimeoutError:
            logger.error(f"Query timeout [{self.telegram_id}]")
            yield ("Ошибка: таймаут запроса", None, True)
            return

        except Exception as e:
            logger.error(f"Query error [{self.telegram_id}]: {type(e).__name__}: {e}")
            yield (f"Ошибка: {e}", None, True)
            return

        finally:
            self._is_querying = False
            self._client = None
            await self._destroy_client(client)
            self._query_lock.release()

    async def destroy(self) -> None:
        """Уничтожает сессию полностью."""
        if self._client:
            await self._destroy_client(self._client)
            self._client = None
        if self._session_file.exists():
            self._session_file.unlink()
        self._clear_incoming_file()
        logger.debug(f"Session destroyed [{self.telegram_id}]")

    def reset(self) -> None:
        """Сбрасывает сессию (sync версия для /clear)."""
        self._session_id = None
        self._incoming.clear()
        self._clear_incoming_file()
        self._client = None
        if self._session_file.exists():
            self._session_file.unlink()
        logger.info(f"Session reset [{self.telegram_id}]")


class SessionManager:
    """Менеджер сессий — создаёт и хранит сессии по telegram_id + channel."""

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, UserSession] = {}
        self._task_sessions: dict[str, UserSession] = {}
        self._ephemeral_counter: int = 0

        self._owner_prompt: str | None = None
        self._external_prompt_template: str | None = None

    @staticmethod
    def _make_key(telegram_id: int, channel: str | None = None) -> str:
        """Составной ключ сессии: 'bot:123' для бота, '123' для Telethon (backward-compat)."""
        if channel and channel != "telethon":
            return f"{channel}:{telegram_id}"
        return str(telegram_id)

    def _get_owner_prompt(self) -> str:
        if self._owner_prompt is None:
            from src.users.prompts import OWNER_SYSTEM_PROMPT
            self._owner_prompt = OWNER_SYSTEM_PROMPT
        return self._owner_prompt

    def _get_external_prompt(self, telegram_id: int, user_display_name: str) -> str:
        if self._external_prompt_template is None:
            from src.users.prompts import EXTERNAL_USER_PROMPT_TEMPLATE
            self._external_prompt_template = EXTERNAL_USER_PROMPT_TEMPLATE

        owner_link = get_owner_link()
        if owner_link:
            contact_info = f"Ссылка на владельца: {owner_link}"
        else:
            contact_info = "Прямой контакт недоступен, только через бота."

        return self._external_prompt_template.format(
            telegram_id=telegram_id,
            username=user_display_name,
            owner_name=get_owner_display_name(),
            owner_telegram_id=settings.primary_owner_id,
            owner_contact_info=contact_info,
            task_context="",  # task_context добавляется в prompt, не в system_prompt
        )

    def get_session(
        self,
        telegram_id: int,
        user_display_name: str | None = None,
        channel: str | None = None,
    ) -> UserSession:
        key = self._make_key(telegram_id, channel)
        if key in self._sessions:
            return self._sessions[key]

        is_owner = settings.is_owner(telegram_id)

        if is_owner:
            system_prompt = self._get_owner_prompt()
        else:
            display_name = user_display_name or str(telegram_id)
            system_prompt = self._get_external_prompt(telegram_id, display_name)

        if channel == "bot":
            from src.users.prompts import BOT_FORMATTING_SUFFIX
            system_prompt += BOT_FORMATTING_SUFFIX

        session = UserSession(
            telegram_id=telegram_id,
            session_dir=self._session_dir,
            system_prompt=system_prompt,
            is_owner=is_owner,
            session_key=key,
        )

        self._sessions[key] = session
        logger.info(f"Created session for {key} (owner={is_owner})")

        return session

    def get_owner_session(self) -> UserSession:
        return self.get_session(settings.primary_owner_id)

    def get_user_sessions(self, telegram_id: int) -> list[UserSession]:
        """Все активные сессии пользователя (по всем транспортам)."""
        suffix = str(telegram_id)
        return [
            s for key, s in self._sessions.items()
            if key == suffix or key.endswith(f":{suffix}")
        ]

    def create_background_session(self) -> UserSession:
        """Создаёт одноразовую сессию с owner tools для scheduler/triggers."""
        self._ephemeral_counter += 1
        key = -(100 + self._ephemeral_counter)
        logger.debug(f"Created ephemeral background session [{key}]")
        return UserSession(
            telegram_id=key,
            session_dir=self._session_dir,
            system_prompt=self._get_owner_prompt(),
            is_owner=True,
        )

    def create_heartbeat_session(self) -> UserSession:
        """Создаёт одноразовую сессию для heartbeat."""
        self._ephemeral_counter += 1
        key = -(100 + self._ephemeral_counter)
        from src.users.prompts import HEARTBEAT_SYSTEM_PROMPT
        from src.tools import HEARTBEAT_ALLOWED_TOOLS
        logger.debug(f"Created ephemeral heartbeat session [{key}]")
        return UserSession(
            telegram_id=key,
            session_dir=self._session_dir,
            system_prompt=HEARTBEAT_SYSTEM_PROMPT,
            is_owner=False,
            allowed_tools=HEARTBEAT_ALLOWED_TOOLS,
        )

    def create_task_session(self, task_id: str) -> UserSession:
        """Создаёт persistent сессию для задачи с owner tools."""
        session = UserSession(
            telegram_id=0,
            session_dir=self._session_dir,
            system_prompt=self._get_owner_prompt(),
            is_owner=True,
            session_key=f"task_{task_id}",
        )
        self._task_sessions[task_id] = session
        logger.info(f"Created task session for [{task_id}]")
        return session

    def get_task_session(self, task_id: str, session_id: str | None = None) -> UserSession | None:
        """Получает или восстанавливает task session."""
        if task_id in self._task_sessions:
            return self._task_sessions[task_id]
        if session_id:
            session = self.create_task_session(task_id)
            session._session_id = session_id
            session._save_session_id(session_id)
            return session
        return None

    @staticmethod
    def _make_group_key(chat_id: int, channel: str) -> str:
        """Ключ групповой сессии: 'group:bot:-1001234'."""
        return f"group:{channel}:{chat_id}"

    def get_group_session(
        self,
        chat_id: int,
        chat_title: str,
        channel: str,
    ) -> UserSession:
        """Возвращает (или создаёт) сессию для группового чата."""
        key = self._make_group_key(chat_id, channel)
        if key in self._sessions:
            return self._sessions[key]

        from src.users.prompts import GROUP_SYSTEM_PROMPT_TEMPLATE, BOT_FORMATTING_SUFFIX
        from src.telegram.group_log import get_log_path

        system_prompt = GROUP_SYSTEM_PROMPT_TEMPLATE.format(
            chat_title=chat_title,
            chat_id=chat_id,
            owner_ids=settings.tg_owner_ids,
            timezone=str(settings.get_timezone()),
            log_path=get_log_path(chat_id),
        )

        if channel == "bot":
            system_prompt += BOT_FORMATTING_SUFFIX

        session = UserSession(
            telegram_id=0,
            session_dir=self._session_dir,
            system_prompt=system_prompt,
            is_owner=True,
            session_key=key,
        )
        self._sessions[key] = session
        logger.info(f"Created group session for {key} ({chat_title})")
        return session

    async def reset_group_session(self, chat_id: int, channel: str) -> None:
        """Сбрасывает групповую сессию."""
        key = self._make_group_key(chat_id, channel)
        if key in self._sessions:
            session = self._sessions[key]
            await session.destroy()
            del self._sessions[key]

    async def reset_session(self, telegram_id: int, channel: str | None = None) -> None:
        key = self._make_key(telegram_id, channel)
        if key in self._sessions:
            session = self._sessions[key]
            await session.destroy()
            del self._sessions[key]

    async def reset_all(self) -> None:
        for session in self._sessions.values():
            await session.destroy()
        self._sessions.clear()
        logger.info("All sessions reset")


# Singleton
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(settings.sessions_dir)
    return _session_manager
