"""
Updater client — двухшаговое обновление через /update команду.

Первый вызов: проверяет наличие обновлений и показывает коммиты.
Второй вызов (в течение 60 сек): запускает обновление.
После рестарта: редактирует loading-сообщение на "Обновлено".
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from loguru import logger

UPDATER_URL = "http://updater:9100"
PENDING_TIMEOUT = 60  # секунд на подтверждение
AUTO_CHECK_INTERVAL = 3600  # 1 час
UPDATE_STATE_FILE = Path("/data/update.json")


@dataclass
class Updater:
    _pending_at: float = field(default=0, init=False)

    async def handle(self) -> str | dict:
        """
        Вызывается при /update.

        Возвращает str (обычный ответ) или dict с ключом "loading"
        для отправки сообщения с custom emoji.
        """
        if self._pending_at and time.monotonic() - self._pending_at < PENDING_TIMEOUT:
            self._pending_at = 0
            asyncio.create_task(self._trigger_update())
            return {"loading": True}

        self._pending_at = 0
        info = await self._check()

        if "error" in info:
            return f"\u274c Ошибка: {info['error']}"

        if not info["commits"]:
            return f"\u2705 Последняя версия ({info['current'][:7]})"

        self._pending_at = time.monotonic()
        lines = [f"- [{c['hash'][:7]}] {c['message']}" for c in info["commits"]]
        return (
            "\U0001f918 Доступно обновление\n\n"
            + "\n".join(lines)
            + "\n\nЧтобы установить, напишите /update ещё раз."
        )

    async def check_for_notification(self) -> str | None:
        """Проверяет обновления для автоуведомления. Возвращает текст или None."""
        info = await self._check()
        if "error" in info or not info["commits"]:
            return None
        lines = [f"- [{c['hash'][:7]}] {c['message']}" for c in info["commits"]]
        return (
            "\U0001f918 Доступно обновление\n\n"
            + "\n".join(lines)
            + "\n\nЧтобы установить, напишите /update."
        )

    @staticmethod
    def save_loading_message(chat_id: int, message_id: int) -> None:
        """Сохраняет ID loading-сообщения для редактирования после рестарта."""
        UPDATE_STATE_FILE.write_text(json.dumps({
            "chat_id": chat_id,
            "message_id": message_id,
        }))

    @staticmethod
    def load_pending_message() -> dict | None:
        """Читает сохранённый ID loading-сообщения и удаляет файл."""
        if not UPDATE_STATE_FILE.exists():
            return None
        data = json.loads(UPDATE_STATE_FILE.read_text())
        UPDATE_STATE_FILE.unlink()
        return data

    async def _check(self) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{UPDATER_URL}/check") as resp:
                return await resp.json()

    async def _trigger_update(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{UPDATER_URL}/update") as resp:
                    data = await resp.json()
                    if "error" in data:
                        logger.error(f"Update failed: {data['error']}")
        except Exception as e:
            logger.error(f"Update request failed: {e}")
