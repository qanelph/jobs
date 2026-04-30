"""Динамический резолв алиасов моделей Claude через Anthropic /v1/models.

Раз в час фетчим список моделей, фильтруем по серии (haiku/sonnet/opus),
берём самую свежую по created_at. Если запрос упал — используем последний
успешный кэш или хардкод-фолбэк.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 3600  # 1 час
MODELS_API_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"

# Серия → префикс id для фильтрации.
_FAMILY_PREFIXES: dict[str, str] = {
    "haiku": "claude-haiku-",
    "sonnet": "claude-sonnet-",
    "opus": "claude-opus-",
}

# Текущий кэш {alias → полное имя модели}. Пустой пока первый fetch не отработал.
_latest_by_family: dict[str, str] = {}


def get_latest_model(alias: str) -> str | None:
    """Возвращает самую свежую модель серии или None если кэш пуст."""
    return _latest_by_family.get(alias.lower())


async def _build_auth_headers() -> dict[str, str] | None:
    """Заголовки для Anthropic API: api_key или OAuth bearer.

    Сначала пробуем env ANTHROPIC_API_KEY (если задан) — самый надёжный.
    Потом OAuth токен из ~/.claude/.credentials.json.
    None — если нет ни того, ни другого."""
    if settings.anthropic_api_key:
        return {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    creds_path = Path(settings.claude_dir) / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        data = json.loads(creds_path.read_text())
    except Exception:
        return None
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": OAUTH_BETA,
    }


def _select_latest(models: list[dict[str, Any]]) -> dict[str, str]:
    """Из списка моделей выбираем по самой свежей в каждой семье.

    `created_at` — ISO8601, лексикографическая сортировка корректна.
    Если у модели нет created_at — fallback на id (тоже отсортируется
    по дате если она в id вида ...20251029).
    """
    out: dict[str, str] = {}
    for family, prefix in _FAMILY_PREFIXES.items():
        candidates = [m for m in models if isinstance(m.get("id"), str) and m["id"].startswith(prefix)]
        if not candidates:
            continue
        candidates.sort(
            key=lambda m: (m.get("created_at") or "", m["id"]),
            reverse=True,
        )
        out[family] = candidates[0]["id"]
    return out


async def refresh_once() -> bool:
    """Один цикл fetch'а. Возвращает True если кэш обновился, False иначе."""
    headers = await _build_auth_headers()
    if not headers:
        logger.debug("anthropic_models: нет credentials — пропускаем refresh")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(MODELS_API_URL, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
        logger.warning("anthropic_models fetch failed: %s", exc)
        return False

    if r.status_code in (401, 403):
        logger.warning(
            "anthropic_models: токен невалиден (status=%s) — переподключи OAuth/обнови ANTHROPIC_API_KEY",
            r.status_code,
        )
        return False
    if r.status_code != 200:
        logger.warning(
            "anthropic_models non-2xx: status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return False

    try:
        payload = r.json()
    except ValueError:
        logger.warning("anthropic_models: ответ не JSON")
        return False

    models = payload.get("data") or []
    if not isinstance(models, list):
        return False

    latest = _select_latest(models)
    if not latest:
        logger.warning("anthropic_models: ни одного matching family в ответе")
        return False

    _latest_by_family.clear()
    _latest_by_family.update(latest)
    logger.info("anthropic_models updated: %s", latest)
    return True


async def refresh_loop() -> None:
    """Фоновый цикл моделей.

    На старте сначала пытается заполнить кэш быстро (с короткими интервалами),
    чтобы не висеть час на хардкод-фолбэке если был транзиентный сбой DNS/сети.
    После первого успеха переходит на 1 час между попытками.
    """
    backoff_seq = [60, 300]  # 1 мин, потом 5 мин до первого успеха
    succeeded = False
    while True:
        try:
            ok = await refresh_once()
        except Exception:
            logger.exception("anthropic_models refresh_loop error")
            ok = False

        if ok:
            succeeded = True

        if succeeded:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        else:
            delay = backoff_seq.pop(0) if backoff_seq else REFRESH_INTERVAL_SECONDS
            await asyncio.sleep(delay)
