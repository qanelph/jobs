"""
Setup — первоначальная настройка при первом запуске.
"""

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from src.config import settings
from src.telegram.client import create_client, load_session_string
from src.telegram.auth import interactive_auth


CLAUDE_CONFIG_DIR = Path.home() / ".claude"
CLAUDE_AUTH_FILES = [
    CLAUDE_CONFIG_DIR / "credentials.json",
    CLAUDE_CONFIG_DIR / ".credentials.json",
    CLAUDE_CONFIG_DIR / "settings.json",
]


def is_telegram_configured() -> bool:
    """Проверяет наличие Telegram сессии."""
    session = load_session_string()
    return session is not None and len(session) > 0


def is_claude_configured() -> bool:
    """Проверяет наличие Claude credentials."""
    return any(f.exists() for f in CLAUDE_AUTH_FILES)


def _setup_claude_interactive() -> bool:
    """Запускает Claude для OAuth авторизации."""
    logger.info("Запуск Claude Code для авторизации...")
    logger.info("Откроется браузер. После входа вернитесь и нажмите Ctrl+C")
    print()

    env = {
        **os.environ,
        "HTTP_PROXY": settings.http_proxy,
        "HTTPS_PROXY": settings.http_proxy,
    }

    subprocess.run(
        ["claude"],
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    if is_claude_configured():
        logger.info("✅ Claude Code авторизован")
        return True

    logger.warning("❌ Credentials не найдены")
    return False


async def _setup_telegram() -> bool:
    """Настраивает Telegram."""
    session_string = load_session_string()
    client = create_client(session_string)

    try:
        await interactive_auth(client)
        return True
    except Exception as e:
        logger.error(f"Ошибка Telegram: {e}")
        return False
    finally:
        await client.disconnect()


async def run_setup() -> bool:
    """
    Запускает полный setup flow.

    Returns:
        True если настройка успешна.
    """
    print("=" * 50)
    print("🚀 Jobs Setup")
    print("=" * 50)
    print()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)

    # Telegram
    print("📱 Шаг 1/2: Telegram")
    print("-" * 30)

    if is_telegram_configured():
        session = load_session_string()
        client = create_client(session)
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                logger.info(f"Telegram: {me.first_name} (ID: {me.id})")
            else:
                if not await _setup_telegram():
                    return False
        finally:
            await client.disconnect()
    else:
        if not await _setup_telegram():
            return False

    print()

    # Claude
    print("🤖 Шаг 2/2: Claude Code")
    print("-" * 30)

    if is_claude_configured():
        logger.info("Claude Code уже настроен")
    else:
        if not _setup_claude_interactive():
            return False

    print()
    print("=" * 50)
    print("✅ Setup завершён!")
    print("=" * 50)

    return True
