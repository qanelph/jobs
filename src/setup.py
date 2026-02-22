"""
Setup — первоначальная настройка при первом запуске.
"""

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from src.config import settings
from src.telegram.client import load_session_string


# Claude хранит credentials в /home/jobs/.claude (монтируется из ./data/.claude)
# Path.home() может быть /root если запущен от root
CLAUDE_CONFIG_DIR = Path("/home/jobs/.claude")
CLAUDE_AUTH_FILES = [
    CLAUDE_CONFIG_DIR / "credentials.json",
    CLAUDE_CONFIG_DIR / ".credentials.json",
]


def is_telegram_configured() -> bool:
    """Проверяет наличие хотя бы одного Telegram-транспорта."""
    # Telethon: есть сессия
    session = load_session_string()
    has_telethon = session is not None and len(session) > 0

    # Bot: есть токен
    has_bot = bool(settings.tg_bot_token)

    return has_telethon or has_bot


def is_claude_configured() -> bool:
    """Проверяет наличие Claude credentials."""
    return any(f.exists() for f in CLAUDE_AUTH_FILES)


def _clear_all_sessions() -> None:
    """Очищает все Claude сессии (после перелогина credentials невалидны)."""
    sessions_dir = settings.sessions_dir
    if sessions_dir.exists():
        import shutil
        for f in sessions_dir.iterdir():
            if f.is_file() and f.suffix == ".session":
                f.unlink()
                logger.debug(f"Removed session: {f.name}")
        logger.info("All Claude sessions cleared")


def _setup_claude_interactive() -> bool:
    """Запускает Claude для OAuth авторизации."""
    logger.info("Запуск Claude Code для авторизации...")
    logger.info("Откроется браузер. После входа вернитесь и нажмите Ctrl+C")
    print()

    env = {
        **os.environ,
        "HOME": "/home/jobs",  # Claude Code ищет credentials в $HOME/.claude
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
        logger.info("Claude Code авторизован")
        # Сбрасываем все сессии — старые session_id теперь невалидны
        _clear_all_sessions()
        return True

    logger.warning("Credentials не найдены")
    return False


def _safe_input(prompt: str) -> str:
    """Безопасный ввод с обработкой кодировки."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    value = sys.stdin.readline().strip()
    return value.encode("utf-8", errors="ignore").decode("utf-8")


def _write_env_var(key: str, value: str) -> None:
    """Записывает или обновляет переменную в .env файле."""
    env_path = Path(".env")
    lines: list[str] = []
    found = False

    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")


async def _setup_telegram_telethon() -> bool:
    """Настраивает Telegram через Telethon (userbot)."""
    from src.telegram.client import create_client
    from src.telegram.auth import interactive_auth

    session_string = load_session_string()
    client = create_client(session_string)

    try:
        await interactive_auth(client)
        return True
    except Exception as e:
        logger.error(f"Ошибка Telegram Telethon: {e}")
        return False
    finally:
        await client.disconnect()


def _setup_telegram_bot() -> bool:
    """Настраивает Telegram через Bot API."""
    token = _safe_input("Введи токен бота (@BotFather → /newbot): ")
    if not token or ":" not in token:
        print("Некорректный токен")
        return False

    _write_env_var("TG_BOT_TOKEN", token)

    # Спрашиваем TG_USER_ID если ещё не задан
    if not settings.tg_user_id:
        user_id = _safe_input("Введи свой Telegram ID (владелец бота): ")
        if not user_id.isdigit():
            print("Некорректный ID")
            return False
        _write_env_var("TG_USER_ID", user_id)

    logger.info("Bot token сохранён")
    return True


async def _setup_telegram() -> bool:
    """Настраивает Telegram транспорт(ы)."""
    has_telethon_config = bool(settings.tg_api_id and settings.tg_api_hash)

    if has_telethon_config:
        print("Выбери способ подключения:")
        print("[1] Telethon (userbot) — полный доступ к Telegram")
        print("[2] Telegram Bot — через API токен (ограниченный доступ)")
        print("[3] Оба — и Telethon, и Bot одновременно")
        choice = _safe_input("\n> ")
    else:
        print("TG_API_ID / TG_API_HASH не заданы.")
        print("[1] Telegram Bot — через API токен")
        print("[2] Задать Telethon вручную (API ID/Hash)")
        choice = _safe_input("\n> ")

        if choice == "1":
            return _setup_telegram_bot()
        else:
            print("Задай TG_API_ID и TG_API_HASH в .env и перезапусти")
            return False

    if choice == "1":
        return await _setup_telegram_telethon()
    elif choice == "2":
        return _setup_telegram_bot()
    elif choice == "3":
        if not await _setup_telegram_telethon():
            return False
        return _setup_telegram_bot()
    else:
        print("Неизвестный выбор")
        return False


async def run_setup(force: bool = False) -> bool:
    """
    Запускает полный setup flow.

    Args:
        force: принудительный запуск (--setup), всегда показывает меню транспортов.

    Returns:
        True если настройка успешна.
    """
    print("=" * 50)
    print("Jobs Setup")
    print("=" * 50)
    print()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)

    # Telegram
    print("Шаг 1/2: Telegram")
    print("-" * 30)

    if force:
        # --setup: всегда показываем меню транспортов
        if not await _setup_telegram():
            return False
    elif is_telegram_configured():
        # Проверяем Telethon сессию если есть
        session = load_session_string()
        if session:
            from src.telegram.client import create_client
            client = create_client(session)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    logger.info(f"Telethon: {me.first_name} (ID: {me.id})")
                else:
                    if not await _setup_telegram():
                        return False
            finally:
                await client.disconnect()

        if settings.tg_bot_token:
            logger.info(f"Bot token: настроен")
    else:
        if not await _setup_telegram():
            return False

    print()

    # Claude
    print("Шаг 2/2: Claude Code")
    print("-" * 30)

    if is_claude_configured():
        logger.info("Claude Code уже настроен")
    else:
        if not _setup_claude_interactive():
            return False

    print()
    print("=" * 50)
    print("Setup завершён!")
    print("=" * 50)

    return True
