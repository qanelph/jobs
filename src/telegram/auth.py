"""
Telegram Auth — интерактивная авторизация.

Поддерживает два способа:
1. QR-код — сканируешь камерой в Telegram
2. Телефон + код — классический способ
"""

import asyncio
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from loguru import logger

from src.telegram.client import save_session_string


def _safe_input(prompt: str) -> str:
    """Безопасный ввод с обработкой кодировки."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    value = sys.stdin.readline().strip()
    return value.encode("utf-8", errors="ignore").decode("utf-8")


def _print_qr(url: str) -> None:
    """Печатает QR-код в терминал."""
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


async def _handle_2fa(client: TelegramClient) -> bool:
    """Запрашивает 2FA пароль и завершает авторизацию."""
    password = _safe_input("Введи 2FA пароль: ")
    await client.sign_in(password=password)
    return True


async def _qr_auth(client: TelegramClient) -> bool:
    """Авторизация через QR-код."""
    print("\nОткрой Telegram на телефоне:")
    print("Настройки → Устройства → Подключить устройство")
    print("Наведи камеру на QR-код:\n")

    qr_login = await client.qr_login()
    _print_qr(qr_login.url)

    print("\nЖду сканирования...")

    try:
        await qr_login.wait(timeout=60)
        return True
    except asyncio.TimeoutError:
        # Пробуем ещё раз с новым QR
        print("\nQR истёк, генерирую новый...\n")
        await qr_login.recreate()
        _print_qr(qr_login.url)
        print("\nЖду сканирования...")
        try:
            await qr_login.wait(timeout=120)
            return True
        except asyncio.TimeoutError:
            print("\nТаймаут. Попробуй ввод по номеру.")
            return False
        except SessionPasswordNeededError:
            return await _handle_2fa(client)
    except SessionPasswordNeededError:
        return await _handle_2fa(client)


async def _phone_auth(client: TelegramClient) -> bool:
    """Авторизация по номеру телефона."""
    phone = _safe_input("Введи номер телефона (+7...): ")
    await client.send_code_request(phone)

    code = _safe_input("Введи код из Telegram: ")

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = _safe_input("Введи 2FA пароль: ")
        await client.sign_in(password=password)

    return True


async def interactive_auth(client: TelegramClient) -> bool:
    """
    Интерактивная авторизация через stdin.

    Returns:
        True если авторизация выполнена, False если уже авторизован.
    """
    await client.connect()

    if await client.is_user_authorized():
        logger.info("Уже авторизован в Telegram")
        return False

    method = _safe_input("Способ входа — [1] QR-код  [2] Номер телефона: ")

    if method == "2":
        await _phone_auth(client)
    else:
        success = await _qr_auth(client)
        if not success:
            await _phone_auth(client)

    session_string = client.session.save()
    save_session_string(session_string)

    me = await client.get_me()
    logger.info(f"Авторизация успешна: {me.first_name} (ID: {me.id})")

    return True
