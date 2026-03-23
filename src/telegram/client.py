"""
Telegram Client — создание и управление Telethon клиентом.
"""

from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.config import settings


def _parse_http_proxy(url: str) -> tuple | None:
    """Парсит HTTP_PROXY URL в формат Telethon: (type, addr, port, ..., username, password)."""
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        return None
    import python_socks
    return (
        python_socks.ProxyType.HTTP,
        parsed.hostname,
        parsed.port,
        True,
        parsed.username,
        parsed.password,
    )


def create_client(session: str | None = None) -> TelegramClient:
    """
    Создаёт TelegramClient с настройками.

    Args:
        session: String session для восстановления авторизации.

    Returns:
        Настроенный TelegramClient.
    """
    session_obj = StringSession(session) if session else StringSession()
    proxy = _parse_http_proxy(settings.http_proxy) if settings.http_proxy else None

    return TelegramClient(
        session=session_obj,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        device_model="arm64",
        system_version="23.5.0",
        app_version="1.36.0",
        proxy=proxy,
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
