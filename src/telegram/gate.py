"""
Telethon Gate — сериализация доступа к TelegramClient.

Один asyncio.Lock гарантирует, что только одна исходящая операция
выполняется одновременно. Это предотвращает рассинхронизацию
MTProto-состояния при параллельных вызовах из tools, transport и triggers.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import TelegramClient

_lock = asyncio.Lock()
_client: TelegramClient | None = None


def set_client(client: TelegramClient) -> None:
    """Устанавливает глобальный Telethon клиент."""
    global _client
    _client = client


@asynccontextmanager
async def use_client() -> AsyncIterator[TelegramClient]:
    """Acquire lock + yield client. Сериализует все исходящие операции."""
    async with _lock:
        if _client is None:
            raise RuntimeError("Telethon client not set — call gate.set_client() first")
        yield _client
