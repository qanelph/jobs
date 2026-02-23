"""
Group Log — запись сообщений из групповых чатов с ротацией.

Формат: [HH:MM] Имя (@user): текст
Ротация: если файл > 1MB, обрезается до ~500KB (с конца).
"""

from datetime import datetime
from pathlib import Path

from src.config import settings

LOG_DIR = settings.workspace_dir / "group_logs"
MAX_LOG_SIZE = 1_000_000  # 1 MB
TRIM_TO_SIZE = 500_000  # 500 KB


def get_log_path(chat_id: int) -> Path:
    """Возвращает путь к лог-файлу группы."""
    return LOG_DIR / f"{chat_id}.log"


def append_message(
    chat_id: int,
    sender_name: str,
    username: str | None,
    text: str,
    *,
    tz: datetime | None = None,
) -> None:
    """Дописывает сообщение в лог группы."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    now = tz or datetime.now(tz=settings.get_timezone())
    time_str = now.strftime("%H:%M")
    user_str = f" (@{username})" if username else ""
    line = f"[{time_str}] {sender_name}{user_str}: {text}\n"

    log_path = get_log_path(chat_id)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)

    rotate_if_needed(chat_id)


def rotate_if_needed(chat_id: int) -> None:
    """Если лог > MAX_LOG_SIZE, обрезает до TRIM_TO_SIZE (оставляет конец)."""
    log_path = get_log_path(chat_id)
    if not log_path.exists():
        return

    size = log_path.stat().st_size
    if size <= MAX_LOG_SIZE:
        return

    data = log_path.read_bytes()
    # Ищем первый перевод строки после точки отсечения
    cut_pos = len(data) - TRIM_TO_SIZE
    newline_pos = data.find(b"\n", cut_pos)
    if newline_pos == -1:
        newline_pos = cut_pos

    trimmed = data[newline_pos + 1 :]
    log_path.write_bytes(b"[...log trimmed...]\n" + trimmed)
