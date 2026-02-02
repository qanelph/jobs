"""
Telegram Client — создание и управление Telethon клиентом.
"""

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.config import settings


def create_client(session: str | None = None) -> TelegramClient:
    """
    Создаёт TelegramClient с настройками.

    Args:
        session: String session для восстановления авторизации.

    Returns:
        Настроенный TelegramClient.
    """
    session_obj = StringSession(session) if session else StringSession()

    return TelegramClient(
        session=session_obj,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        device_model="arm64",
        system_version="23.5.0",
        app_version="1.36.0",
    )


def load_session_string() -> str | None:
    """Загружает сохранённую сессию из файла."""
    if settings.session_path.exists():
        content = settings.session_path.read_text().strip()
        return content if content else None
    return None


def save_session_string(session_string: str) -> None:
    """Сохраняет строку сессии в файл."""
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    settings.session_path.write_text(session_string)
