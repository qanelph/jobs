from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    http_proxy: str = "http://PbxzTVqF:NjR4RB3u@45.199.204.185:62176"

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
    def claude_session_path(self) -> Path:
        return self.data_dir / "claude_session_id"


settings = Settings()
