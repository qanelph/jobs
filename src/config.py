from datetime import datetime, timezone as tz
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_system_timezone() -> str:
    """Определяет системный timezone."""
    import os

    # Сначала смотрим явную переменную TZ
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env

    try:
        # Пробуем получить из системы
        local_tz = datetime.now().astimezone().tzinfo
        if local_tz:
            # Проверяем offset - если 0, это скорее всего UTC в Docker
            offset = datetime.now().astimezone().utcoffset()
            if offset and offset.total_seconds() == 0:
                # UTC в Docker - возвращаем Moscow как default
                return "Europe/Moscow"
            if hasattr(local_tz, 'key'):
                return local_tz.key
    except Exception:
        pass

    return "Europe/Moscow"  # Default для России


class Settings(BaseSettings):
    """Конфигурация приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Telegram
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_bot_token: str = ""
    tg_user_id: int

    @model_validator(mode="after")
    def _check_at_least_one_transport(self) -> "Settings":
        has_telethon = bool(self.tg_api_id and self.tg_api_hash)
        has_bot = bool(self.tg_bot_token)
        if not has_telethon and not has_bot:
            raise ValueError(
                "Хотя бы один Telegram-транспорт должен быть настроен: "
                "TG_API_ID + TG_API_HASH (Telethon) или TG_BOT_TOKEN (Bot)"
            )
        return self

    # Claude (API key опционален при OAuth)
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-4-6"
    http_proxy: str | None = None

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

    # Browser (CDP via HAProxy)
    browser_cdp_url: str = "http://browser:9223"

    # Paths
    data_dir: Path = Path("/data")
    workspace_dir: Path = Path("/workspace")
    claude_dir: Path = Path("/home/jobs/.claude")  # Claude Code config dir

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

    @property
    def skills_dir(self) -> Path:
        """Директория со skills."""
        return self.workspace_dir / "skills"


settings = Settings()


# Runtime: информация о владельце (заполняется при старте из Telethon)
_owner_display_name: str = "владелец"
_owner_link: str = ""
_owner_username: str | None = None
_owner_phone: str | None = None


def set_owner_info(
    telegram_id: int,
    first_name: str | None,
    username: str | None = None,
    phone: str | None = None,
) -> None:
    """Устанавливает информацию о владельце из Telethon."""
    global _owner_display_name, _owner_link, _owner_username, _owner_phone

    _owner_display_name = first_name or username or "владелец"
    _owner_username = username
    _owner_phone = phone

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


def get_owner_username() -> str | None:
    """Возвращает username владельца."""
    return _owner_username


def get_owner_phone() -> str | None:
    """Возвращает телефон владельца."""
    return _owner_phone
