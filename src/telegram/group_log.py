"""
Group Log — запись сообщений из групповых чатов с ротацией.

Формат: [HH:MM] Имя (@user): текст
Ротация: если файл > 1MB, обрезается до ~500KB (с конца).
"""

import asyncio
from datetime import datetime
from pathlib import Path

from src.config import settings

LOG_DIR = settings.workspace_dir / "group_logs"
MAX_LOG_SIZE = 1_000_000  # 1 MB
TRIM_TO_SIZE = 500_000  # 500 KB

# Lock per chat_id — защита от concurrent записей и ротации
_locks: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def get_log_path(chat_id: int) -> Path:
    """Возвращает путь к лог-файлу группы."""
    return LOG_DIR / f"{chat_id}.log"


async def append_message(
    chat_id: int,
    sender_name: str,
    username: str | None,
    text: str,
    *,
    timestamp: datetime | None = None,
) -> None:
    """Дописывает сообщение в лог группы (async-safe)."""
    now = timestamp or datetime.now(tz=settings.get_timezone())
    time_str = now.strftime("%H:%M")
    user_str = f" (@{username})" if username else ""
    line = f"[{time_str}] {sender_name}{user_str}: {text}\n"

    log_path = get_log_path(chat_id)

    async with _get_lock(chat_id):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
        _rotate_if_needed(log_path)


def _rotate_if_needed(log_path: Path) -> None:
    """Если лог > MAX_LOG_SIZE, обрезает до TRIM_TO_SIZE (оставляет конец)."""
    if not log_path.exists():
        return

    size = log_path.stat().st_size
    if size <= MAX_LOG_SIZE:
        return

    data = log_path.read_bytes()
    cut_pos = len(data) - TRIM_TO_SIZE
    newline_pos = data.find(b"\n", cut_pos)
    if newline_pos == -1:
        newline_pos = cut_pos

    trimmed = data[newline_pos + 1 :]
    log_path.write_bytes(b"[...log trimmed...]\n" + trimmed)
