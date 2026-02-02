import getpass
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from loguru import logger

from src.telegram.client import save_session_string


def safe_input(prompt: str) -> str:
    """Безопасный ввод с обработкой кодировки."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    value = sys.stdin.readline().strip()
    # Убираем возможные surrogate characters
    return value.encode("utf-8", errors="ignore").decode("utf-8")


async def interactive_auth(client: TelegramClient) -> bool:
    """
    Интерактивная авторизация при первом запуске.
    Запрашивает телефон и код через stdin.

    Returns:
        True если авторизация успешна, False если уже авторизован.
    """
    await client.connect()

    if await client.is_user_authorized():
        logger.info("Уже авторизован в Telegram")
        return False

    phone = safe_input("Введи номер телефона (+7...): ")
    await client.send_code_request(phone)

    code = safe_input("Введи код из Telegram: ")

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = safe_input("Введи 2FA пароль: ")
        await client.sign_in(password=password)

    # Сохраняем сессию
    session_string = client.session.save()
    save_session_string(session_string)

    me = await client.get_me()
    logger.info(f"Авторизация успешна! Logged in as {me.first_name} (ID: {me.id})")

    return True
