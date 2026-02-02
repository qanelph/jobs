"""
Claude Code Session — управление сессией с сохранением контекста.
"""

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
from src.tools import create_tools_server, TOOL_NAMES


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
    session_id: str | None = None
    is_error: bool = False


class ClaudeSession:
    """
    Сессия Claude Code с сохранением контекста.

    - Контекст сохраняется между сообщениями
    - Session ID персистится в файл для переживания перезапусков
    - Включает MCP tools (scheduler и др.)
    """

    def __init__(self) -> None:
        self._session_id: str | None = self._load_session_id()
        self._tools_server = create_tools_server()

    def _load_session_id(self) -> str | None:
        """Загружает session_id из файла."""
        path = settings.claude_session_path
        if path.exists():
            session_id = path.read_text().strip()
            if session_id:
                logger.info(f"Loaded session: {session_id[:8]}...")
                return session_id
        return None

    def _save_session_id(self, session_id: str) -> None:
        """Сохраняет session_id в файл."""
        path = settings.claude_session_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session_id)
        logger.debug(f"Saved session: {session_id[:8]}...")

    def _build_options(self) -> ClaudeAgentOptions:
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
            mcp_servers={"jobs": self._tools_server},
            allowed_tools=TOOL_NAMES,
        )

        if self._session_id:
            options.resume = self._session_id

        return options

    async def query_stream(self, prompt: str) -> AsyncIterator[ProgressUpdate]:
        """
        Отправляет запрос и стримит обновления.

        Yields:
            ProgressUpdate с текстом или tool name.
        """
        options = self._build_options()
        text_buffer: list[str] = []

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
                        if message.session_id:
                            self._session_id = message.session_id
                            self._save_session_id(message.session_id)

                        yield ProgressUpdate(
                            text="".join(text_buffer),
                            is_final=True,
                        )

        except Exception as e:
            logger.error(f"Claude SDK error: {e}")
            yield ProgressUpdate(text=f"Ошибка: {e}", is_final=True)

    async def query(self, prompt: str) -> ClaudeResponse:
        """Отправляет запрос и возвращает полный ответ."""
        text_parts: list[str] = []
        final_text = ""

        async for update in self.query_stream(prompt):
            if update.is_final:
                final_text = update.text or ""
            elif update.text:
                text_parts.append(update.text)

        content = final_text or "".join(text_parts)

        return ClaudeResponse(
            content=content,
            session_id=self._session_id,
        )

    def reset(self) -> None:
        """Сбрасывает сессию (новый разговор)."""
        self._session_id = None
        path = settings.claude_session_path
        if path.exists():
            path.unlink()
        logger.info("Session reset")


# Singleton
_session: ClaudeSession | None = None


def get_session() -> ClaudeSession:
    """Возвращает глобальную сессию."""
    global _session
    if _session is None:
        _session = ClaudeSession()
    return _session
