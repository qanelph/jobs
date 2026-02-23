"""
Jobs HTTP API — конфигурация агента.

Порт 8080, авторизация: Bearer {JWT_SECRET_KEY} (shared secret с оркестратором).
"""

import asyncio
import hmac
import os
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.config import (
    MUTABLE_FIELDS,
    apply_overrides,
    get_current_overrides,
    save_overrides,
    settings,
)

# Поля, содержащие секреты — маскируются в GET /config
_SECRET_FIELDS: frozenset[str] = frozenset({
    "tg_api_hash",
    "tg_bot_token",
    "anthropic_api_key",
    "openai_api_key",
    "http_proxy",
})


def _resolve_field_type(field_name: str) -> str:
    """Определить строковый тип поля Settings для API-ответа."""
    if field_name in _SECRET_FIELDS:
        return "secret"
    annotation = settings.model_fields[field_name].annotation
    # Unwrap Optional/Union (str | None → str)
    origin = get_origin(annotation)
    if origin is Union or isinstance(annotation, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        annotation = args[0] if args else annotation
    if annotation is int:
        return "int"
    if annotation is str:
        return "str"
    if annotation is Path or annotation is type(Path()):
        return "path"
    if get_origin(annotation) is list:
        inner = get_args(annotation)
        inner_name = inner[0].__name__ if inner else "str"
        return f"list[{inner_name}]"
    return "str"


def _get_jwt_secret() -> str:
    """JWT_SECRET_KEY из env var (передаётся оркестратором при спавне)."""
    return os.environ.get("JWT_SECRET_KEY", "")


def _verify_secret(authorization: str) -> None:
    """Проверка Bearer-токена — constant-time сравнение (защита от timing-attack)."""
    secret = _get_jwt_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="JWT_SECRET_KEY not configured")
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _mask(value: str | None) -> str:
    """Маскировать секрет: 4 символа слева + звёздочки + 4 символа справа."""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


class PatchConfig(BaseModel):
    """Partial update мутабельных полей."""

    claude_model: str | None = None
    timezone: str | None = None
    http_proxy: str | None = None
    openai_api_key: str | None = None


# Гарантируем синхронность PatchConfig и MUTABLE_FIELDS — упадёт при импорте если разойдутся
assert set(PatchConfig.model_fields.keys()) == MUTABLE_FIELDS, (
    f"PatchConfig fields {set(PatchConfig.model_fields)} != MUTABLE_FIELDS {MUTABLE_FIELDS}"
)

# Lock для атомарности read-modify-write overrides
_config_lock = asyncio.Lock()


def _build_config_response() -> dict[str, Any]:
    """Построить ответ GET /config с маскировкой секретов и флагом mutable."""
    result: dict[str, Any] = {}

    for field_name in settings.model_fields:
        value = getattr(settings, field_name)
        mutable = field_name in MUTABLE_FIELDS

        if field_name in _SECRET_FIELDS and isinstance(value, str):
            value = _mask(value)

        if isinstance(value, Path):
            value = str(value)

        result[field_name] = {
            "value": value,
            "mutable": mutable,
            "type": _resolve_field_type(field_name),
        }

    return result


def create_app() -> FastAPI:
    """Создать FastAPI-приложение для конфиг-API."""
    app = FastAPI(title="Jobs Agent API", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config")
    async def get_config(
        authorization: str = Header(...),
    ) -> dict[str, Any]:
        _verify_secret(authorization)
        return _build_config_response()

    @app.patch("/config")
    async def patch_config(
        body: PatchConfig,
        authorization: str = Header(...),
    ) -> dict[str, Any]:
        _verify_secret(authorization)

        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        async with _config_lock:
            apply_overrides(updates)
            current = get_current_overrides()
            current.update(updates)
            save_overrides(current)

        return _build_config_response()

    return app
