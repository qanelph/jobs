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


class ClaudeSession:
    """
    Сессия Claude Code с сохранением контекста между сообщениями.
    Один экземпляр на пользователя.
    """

    def __init__(self):
        self._client: ClaudeSDKClient | None = None
        self._session_id: str | None = None

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


# Глобальная сессия для пользователя (singleton)
_user_session: ClaudeSession | None = None


def get_session() -> ClaudeSession:
    """Возвращает глобальную сессию Claude."""
    global _user_session
    if _user_session is None:
        _user_session = ClaudeSession()
    return _user_session
