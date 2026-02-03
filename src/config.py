from datetime import datetime, timezone as tz
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_system_timezone() -> str:
    """Определяет системный timezone."""
    try:
        # Пробуем получить из системы
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz and hasattr(local_tz, 'key'):
            return local_tz.key
        # Fallback - смотрим offset и угадываем
        offset = datetime.now().astimezone().utcoffset()
        if offset:
            hours = offset.total_seconds() / 3600
            if hours == 3:
                return "Europe/Moscow"
    except Exception:
        pass
    return "Europe/Moscow"  # Default


class Settings(BaseSettings):
    """Конфигурация приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Telegram
    tg_api_id: int
    tg_api_hash: str
    tg_user_id: int

    # Claude (API key опционален при OAuth)
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-4-5-20251101"  # Opus 4.5
    http_proxy: str = "http://PbxzTVqF:NjR4RB3u@45.199.204.185:62176"

    # OpenAI (для Whisper транскрипции)
    openai_api_key: str | None = None

    # Timezone (auto = определить из системы, или явно: Europe/Moscow, UTC, etc.)
    timezone: str = "auto"

    def get_timezone(self) -> ZoneInfo:
        """Возвращает timezone для работы с временем."""
        tz_name = self.timezone if self.timezone != "auto" else _detect_system_timezone()
        return ZoneInfo(tz_name)

    # Heartbeat
    heartbeat_interval_minutes: int = 30  # 0 = отключен

    # Paths
    data_dir: Path = Path("/data")
    workspace_dir: Path = Path("/workspace")

    @property
    def session_path(self) -> Path:
        return self.data_dir / "telethon.session"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def sessions_dir(self) -> Path:
        """Директория для Claude сессий пользователей."""
        return self.data_dir / "sessions"

    @property
    def uploads_dir(self) -> Path:
        return self.workspace_dir / "uploads"


settings = Settings()


# Runtime: информация о владельце (заполняется при старте из Telethon)
_owner_display_name: str = "владелец"
_owner_link: str = ""


def set_owner_info(
    telegram_id: int,
    first_name: str | None,
    username: str | None = None,
) -> None:
    """Устанавливает информацию о владельце из Telethon."""
    global _owner_display_name, _owner_link

    _owner_display_name = first_name or username or "владелец"

    if username:
        _owner_link = f"t.me/{username}"
    else:
        _owner_link = ""


def get_owner_display_name() -> str:
    """Возвращает имя владельца (для промптов)."""
    return _owner_display_name


def get_owner_link() -> str:
    """Возвращает ссылку на владельца (t.me/username)."""
    return _owner_link
