import json
from datetime import datetime, timezone as tz
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import TypeAdapter, field_validator, model_validator
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
    tg_user_id: int = 0  # backward-compat: single owner
    tg_owner_ids: list[int] = []  # JSON array in env: TG_OWNER_IDS=[123,456]

    @model_validator(mode="after")
    def _validate_telegram(self) -> "Settings":
        has_telethon = bool(self.tg_api_id and self.tg_api_hash)
        has_bot = bool(self.tg_bot_token)
        if not has_telethon and not has_bot:
            raise ValueError(
                "Хотя бы один Telegram-транспорт должен быть настроен: "
                "TG_API_ID + TG_API_HASH (Telethon) или TG_BOT_TOKEN (Bot)"
            )
        # backward-compat: если tg_owner_ids пуст — берём из tg_user_id
        if not self.tg_owner_ids and self.tg_user_id:
            self.tg_owner_ids = [self.tg_user_id]
        if not self.tg_owner_ids:
            raise ValueError("TG_OWNER_IDS или TG_USER_ID обязателен (Telegram ID владельца)")
        # sync tg_user_id с primary owner для backward-compat
        self.tg_user_id = self.tg_owner_ids[0]
        return self

    @property
    def primary_owner_id(self) -> int:
        """Первый owner — для heartbeat, proactive sends."""
        return self.tg_owner_ids[0]

    def is_owner(self, telegram_id: int) -> bool:
        """Проверяет, является ли пользователь одним из владельцев."""
        return telegram_id in self.tg_owner_ids

    # Custom instructions (из Jobsy UI, env: CUSTOM_INSTRUCTIONS)
    custom_instructions: str = ""

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

    @field_validator("heartbeat_interval_minutes", mode="before")
    @classmethod
    def _empty_str_to_default(cls, v: object) -> object:
        """Пустая строка из env var → дефолт."""
        if isinstance(v, str) and v.strip() == "":
            return 30
        return v

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

# Поля, которые можно менять через HTTP API без рестарта.
# heartbeat_interval_minutes исключён: HeartbeatRunner читает значение при старте,
# runtime-изменение не подхватывается без рестарта контейнера.
MUTABLE_FIELDS: frozenset[str] = frozenset({
    "claude_model",
    "timezone",
    "http_proxy",
    "openai_api_key",
    "tg_api_id",
    "tg_api_hash",
    "tg_user_id",
    "tg_owner_ids",
})

OVERRIDES_FILE = "config_overrides.json"


def _overrides_path() -> Path:
    return settings.data_dir / OVERRIDES_FILE


def get_current_overrides() -> dict[str, object]:
    """Прочитать текущий файл overrides (пустой dict если файла нет)."""
    path = _overrides_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_overrides(overrides: dict[str, object]) -> None:
    """Сохранить overrides в JSON файл."""
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_overrides(overrides: dict[str, object]) -> None:
    """Применить overrides к in-memory settings (только mutable-поля, с валидацией типов)."""
    for key, value in overrides.items():
        if key not in MUTABLE_FIELDS:
            continue
        field_info = settings.model_fields.get(key)
        if field_info is None:
            continue
        validated = TypeAdapter(field_info.annotation).validate_python(value)
        setattr(settings, key, validated)


def load_overrides() -> None:
    """Загрузить overrides из файла и применить к settings."""
    overrides = get_current_overrides()
    if overrides:
        apply_overrides(overrides)
        logger.info(f"Config overrides loaded: {list(overrides.keys())}")


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
