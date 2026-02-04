"""
SessionManager — управление сессиями Claude для разных пользователей.

Каждый пользователь получает свою изолированную сессию:
- Owner (tg_user_id) — полный доступ с owner tools
- External users — ограниченный доступ с external tools
"""

import asyncio
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


MAX_CONTEXT_MESSAGES = 10
QUERY_TIMEOUT_SECONDS = 300  # 5 минут


class UserSession:
    """
    Сессия Claude для конкретного пользователя.

    Хранит локальный буфер последних сообщений (_context),
    который подкладывается в каждый prompt — ассистент видит
    что он отправлял через tool calls даже если session resume не сработал.
    """

    def __init__(
        self,
        telegram_id: int,
        session_dir: Path,
        system_prompt: str,
        is_owner: bool = False,
        base_prompt_builder: callable = None,
    ) -> None:
        self.telegram_id = telegram_id
        self.is_owner = is_owner
        self._system_prompt = system_prompt
        self._base_prompt_builder = base_prompt_builder
        self._session_file = session_dir / f"{telegram_id}.session"
        self._session_id: str | None = self._load_session_id()
        self._context: list[tuple[str, str]] = []  # (role, text) — буфер контекста
        # Lazy import to avoid circular dependency
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

    async def _refresh_prompt_with_context(self) -> None:
        """Обновляет system_prompt с актуальным контекстом задач (для external users)."""
        if self.is_owner or not self._base_prompt_builder:
            return

        from src.users.repository import get_users_repository
        from src.users.prompts import format_task_context

        repo = get_users_repository()
        tasks = await repo.list_tasks(assignee_id=self.telegram_id, include_done=False)

        task_context = format_task_context(tasks)
        self._system_prompt = self._base_prompt_builder(task_context)

    def _save_session_id(self, session_id: str) -> None:
        """Сохраняет session_id в файл."""
        self._session_file.parent.mkdir(parents=True, exist_ok=True)
        self._session_file.write_text(session_id)
        logger.debug(f"Saved session [{self.telegram_id}]: {session_id[:8]}...")

    def add_context(self, role: str, text: str) -> None:
        """Добавляет сообщение в буфер контекста."""
        self._context.append((role, text[:1000]))
        if len(self._context) > MAX_CONTEXT_MESSAGES:
            self._context = self._context[-MAX_CONTEXT_MESSAGES:]

    def _format_context(self) -> str:
        """Форматирует буфер контекста для вставки в prompt."""
        if not self._context:
            return ""

        lines = ["[Предыдущие сообщения в этом чате:]"]
        for role, text in self._context:
            prefix = "Ты" if role == "assistant" else "Пользователь"
            lines.append(f"{prefix}: {text}")
        lines.append("[Конец контекста]\n")
        return "\n".join(lines)

    def _build_options(self, system_prompt_override: str | None = None) -> ClaudeAgentOptions:
        """Создаёт опции для клиента."""
        env = os.environ.copy()
        if settings.http_proxy:
            env["HTTP_PROXY"] = settings.http_proxy
            env["HTTPS_PROXY"] = settings.http_proxy

        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        # MCP серверы
        mcp_servers = {"jobs": self._tools_server}

        # Owner получает внешние MCP серверы + browser
        if self.is_owner:
            mcp_config = get_mcp_config()
            external_servers = mcp_config.to_mcp_json()
            mcp_servers.update(external_servers)

            mcp_servers["browser"] = {
                "command": "playwright-cdp-wrapper",
                "args": [settings.browser_cdp_url],
                "env": {"NO_PROXY": "browser,localhost,127.0.0.1"},
            }

        from src.tools import OWNER_ALLOWED_TOOLS, EXTERNAL_ALLOWED_TOOLS
        allowed_tools = OWNER_ALLOWED_TOOLS if self.is_owner else EXTERNAL_ALLOWED_TOOLS

        permission_mode = "bypassPermissions" if self.is_owner else "default"
        prompt = system_prompt_override if system_prompt_override else self._system_prompt

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
            system_prompt=prompt,
            setting_sources=["project"],
            plugins=plugins,
        )

        if self._session_id:
            options.resume = self._session_id

        return options

    async def query(self, prompt: str) -> str:
        """Отправляет запрос и возвращает ответ."""
        await self._refresh_prompt_with_context()

        # Подкладываем контекст предыдущих сообщений
        context = self._format_context()
        full_prompt = f"{context}{prompt}" if context else prompt

        options = self._build_options()
        text_parts: list[str] = []

        try:
            async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(full_prompt)

                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    text_parts.append(block.text)

                        elif isinstance(message, ResultMessage):
                            if message.session_id:
                                self._session_id = message.session_id
                                self._save_session_id(message.session_id)

        except TimeoutError:
            logger.error(f"Claude timeout [{self.telegram_id}]: {QUERY_TIMEOUT_SECONDS}s")
            return "Ошибка: таймаут запроса"

        except Exception as e:
            logger.error(f"Claude error [{self.telegram_id}]: {type(e).__name__}: {e}")
            return f"Ошибка: {e}"

        result = "".join(text_parts) or "Нет ответа"

        # Сохраняем обмен в контекст
        self.add_context("user", prompt)
        self.add_context("assistant", result[:500])

        return result

    async def query_stream(self, prompt: str) -> AsyncIterator[tuple[str | None, str | None, bool]]:
        """
        Стримит ответ.

        Yields:
            (text, tool_name, is_final)
        """
        await self._refresh_prompt_with_context()

        # Подкладываем контекст предыдущих сообщений
        context = self._format_context()
        full_prompt = f"{context}{prompt}" if context else prompt

        options = self._build_options()
        text_buffer: list[str] = []

        try:
            async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(full_prompt)

                    async for message in client.receive_response():
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    text_buffer.append(block.text)
                                    yield (block.text, None, False)
                                elif isinstance(block, ToolUseBlock):
                                    tool_display = block.name
                                    if block.name == "Skill" and block.input.get("skill"):
                                        tool_display = f"Skill:{block.input['skill']}"
                                    yield (None, tool_display, False)

                        elif isinstance(message, ResultMessage):
                            if message.session_id:
                                self._session_id = message.session_id
                                self._save_session_id(message.session_id)

                            yield ("".join(text_buffer), None, True)

        except TimeoutError:
            logger.error(f"Claude timeout [{self.telegram_id}]: {QUERY_TIMEOUT_SECONDS}s")
            yield ("Ошибка: таймаут запроса", None, True)
            return

        except Exception as e:
            logger.error(f"Claude error [{self.telegram_id}]: {type(e).__name__}: {e}")
            yield (f"Ошибка: {e}", None, True)
            return

        # Сохраняем обмен в контекст
        response = "".join(text_buffer)
        self.add_context("user", prompt)
        self.add_context("assistant", response[:500])

    def reset(self) -> None:
        """Сбрасывает сессию."""
        self._session_id = None
        self._context.clear()
        if self._session_file.exists():
            self._session_file.unlink()
        logger.info(f"Session reset [{self.telegram_id}]")


class SessionManager:
    """Менеджер сессий — создаёт и хранит сессии по telegram_id."""

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[int, UserSession] = {}

        self._owner_prompt: str | None = None
        self._external_prompt_template: str | None = None

    def _get_owner_prompt(self) -> str:
        if self._owner_prompt is None:
            from src.users.prompts import OWNER_SYSTEM_PROMPT
            self._owner_prompt = OWNER_SYSTEM_PROMPT
        return self._owner_prompt

    def _get_external_prompt(
        self,
        telegram_id: int,
        user_display_name: str,
        task_context: str = "",
    ) -> str:
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
            owner_contact_info=contact_info,
            task_context=task_context,
        )

    def get_session(self, telegram_id: int, user_display_name: str | None = None) -> UserSession:
        if telegram_id in self._sessions:
            return self._sessions[telegram_id]

        is_owner = telegram_id == settings.tg_user_id
        base_prompt_builder = None

        if is_owner:
            system_prompt = self._get_owner_prompt()
        else:
            display_name = user_display_name or str(telegram_id)
            system_prompt = self._get_external_prompt(telegram_id, display_name)
            base_prompt_builder = lambda ctx, tid=telegram_id, dn=display_name: self._get_external_prompt(tid, dn, ctx)

        session = UserSession(
            telegram_id=telegram_id,
            session_dir=self._session_dir,
            system_prompt=system_prompt,
            is_owner=is_owner,
            base_prompt_builder=base_prompt_builder,
        )

        self._sessions[telegram_id] = session
        logger.info(f"Created session for {telegram_id} (owner={is_owner})")

        return session

    def get_owner_session(self) -> UserSession:
        return self.get_session(settings.tg_user_id)

    async def reset_session(self, telegram_id: int) -> None:
        if telegram_id in self._sessions:
            self._sessions[telegram_id].reset()
            del self._sessions[telegram_id]

    async def reset_all(self) -> None:
        for session in self._sessions.values():
            session.reset()
        self._sessions.clear()
        logger.info("All sessions reset")


# Singleton
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(settings.sessions_dir)
    return _session_manager
