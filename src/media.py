"""
Media processing — голосовые сообщения и файлы.
"""

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import openai
from loguru import logger

from src.config import settings

MAX_MEDIA_SIZE = 50 * 1024 * 1024  # 50 MB


@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None


def _get_openai_client() -> openai.AsyncOpenAI:
    """Создаёт OpenAI клиент с прокси."""
    http_client = None
    if settings.http_proxy:
        http_client = httpx.AsyncClient(proxy=settings.http_proxy)

    return openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        http_client=http_client,
    )


async def transcribe_audio(audio_bytes: bytes, language: str = "ru") -> TranscriptionResult:
    """
    Транскрибирует аудио через OpenAI Whisper.

    Args:
        audio_bytes: Аудио файл в бинарном формате
        language: Язык аудио (по умолчанию "ru")

    Returns:
        TranscriptionResult с текстом
    """
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    client = _get_openai_client()

    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = "voice.ogg"

    logger.info(f"Transcribing audio: {len(audio_bytes)} bytes, proxy: {bool(settings.http_proxy)}")

    result = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        language=language,
    )

    logger.info(f"Transcription: {result.text[:100]}...")

    return TranscriptionResult(
        text=result.text,
        language=language,
    )


async def save_media(data: bytes, filename: str, subfolder: str = "") -> Path:
    """
    Сохраняет медиа файл в workspace/uploads.

    Args:
        data: Бинарные данные файла
        filename: Имя файла
        subfolder: Опциональная подпапка (photos, documents, etc)

    Returns:
        Path к сохранённому файлу
    """
    if len(data) > MAX_MEDIA_SIZE:
        raise ValueError(f"Файл слишком большой: {len(data) // 1024 // 1024} MB (макс {MAX_MEDIA_SIZE // 1024 // 1024} MB)")

    # Создаём структуру директорий
    uploads_dir = settings.uploads_dir
    if subfolder:
        uploads_dir = uploads_dir / subfolder

    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Добавляем timestamp для уникальности
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
    unique_name = f"{name}_{timestamp}.{ext}" if ext else f"{name}_{timestamp}"

    file_path = uploads_dir / unique_name
    file_path.write_bytes(data)

    logger.info(f"Saved media: {file_path} ({len(data)} bytes)")

    return file_path
