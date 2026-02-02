import os
from dataclasses import dataclass
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

from src.config import settings
from src.scheduler.tools import create_scheduler_server


SESSION_FILE = settings.data_dir / "claude_session_id"


@dataclass
class ProgressUpdate:
    """Промежуточное обновление от Claude."""

    text: str | None = None
    tool_name: str | None = None
    is_final: bool = False


@dataclass
class ClaudeResponse:
    """Финальный ответ от Claude."""

    content: str
    cost_usd: float = 0.0
    session_id: str | None = None
    is_error: bool = False


def _load_session_id() -> str | None:
    """Загружает session_id из файла."""
    if SESSION_FILE.exists():
        session_id = SESSION_FILE.read_text().strip()
        if session_id:
            logger.info(f"Loaded session: {session_id[:8]}...")
            return session_id
    return None


def _save_session_id(session_id: str) -> None:
    """Сохраняет session_id в файл."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(session_id)
    logger.debug(f"Saved session: {session_id[:8]}...")


class ClaudeSession:
    """
    Сессия Claude Code с сохранением контекста между сообщениями.
    Один экземпляр на пользователя.
    """

    def __init__(self):
        self._session_id: str | None = _load_session_id()
        self._scheduler_server = create_scheduler_server()

    def _get_options(self) -> ClaudeAgentOptions:
        """Создаёт опции для клиента."""
        env = os.environ.copy()
        env["HTTP_PROXY"] = settings.http_proxy
        env["HTTPS_PROXY"] = settings.http_proxy

        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

        options = ClaudeAgentOptions(
            cwd=Path(settings.workspace_dir),
            permission_mode="bypassPermissions",
            env=env,
            # MCP серверы с кастомными tools
            mcp_servers={"scheduler": self._scheduler_server},
            # Разрешаем использовать scheduler tools
            allowed_tools=[
                "mcp__scheduler__schedule_task",
                "mcp__scheduler__list_scheduled_tasks",
                "mcp__scheduler__cancel_scheduled_task",
            ],
        )

        # Если есть предыдущая сессия — продолжаем её
        if self._session_id:
            options.resume = self._session_id

        return options

    async def query_stream(self, prompt: str) -> AsyncIterator[ProgressUpdate]:
        """
        Отправляет запрос и стримит промежуточные обновления.

        Yields:
            ProgressUpdate с текстом или информацией о tool use.
        """
        options = self._get_options()
        text_buffer = []

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_buffer.append(block.text)
                                yield ProgressUpdate(text=block.text)

                            elif isinstance(block, ToolUseBlock):
                                yield ProgressUpdate(tool_name=block.name)

                    elif isinstance(message, ResultMessage):
                        self._session_id = message.session_id
                        # Сохраняем сессию для персистентности
                        if self._session_id:
                            _save_session_id(self._session_id)

                        final_text = "".join(text_buffer)
                        yield ProgressUpdate(
                            text=final_text,
                            is_final=True,
                        )

        except Exception as e:
            logger.error(f"Claude SDK error: {e}")
            yield ProgressUpdate(text=f"Ошибка: {e}", is_final=True)

    async def query(self, prompt: str) -> ClaudeResponse:
        """
        Отправляет запрос и возвращает полный ответ (без стриминга).
        """
        text_parts = []
        response = ClaudeResponse(content="")

        async for update in self.query_stream(prompt):
            if update.text and update.is_final:
                response.content = update.text
            elif update.text:
                text_parts.append(update.text)

        if not response.content and text_parts:
            response.content = "".join(text_parts)

        response.session_id = self._session_id
        return response

    def reset_session(self) -> None:
        """Сбрасывает сессию (начинает новый разговор)."""
        self._session_id = None
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        logger.info("Session reset")


# Глобальная сессия для пользователя (singleton)
_user_session: ClaudeSession | None = None


def get_session() -> ClaudeSession:
    """Возвращает глобальную сессию Claude."""
    global _user_session
    if _user_session is None:
        _user_session = ClaudeSession()
    return _user_session
