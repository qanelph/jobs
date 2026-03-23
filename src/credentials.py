"""Управление Claude OAuth credentials — pull от оркестратора."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
from loguru import logger

from src.config import settings

_pull_lock = asyncio.Lock()


def _atomic_write(path: Path, data: str) -> None:
    """Атомарная запись файла через tmpfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None
        Path(tmp).unlink(missing_ok=True)
        raise


async def pull_credentials() -> bool:
    """Запрашивает свежие credentials у оркестратора.

    Orchestrator автоматически рефрешит токен если он истёк.
    Возвращает True если credentials успешно обновлены.
    Lock защищает от параллельных записей в один файл.
    """
    async with _pull_lock:
        return await _pull_credentials_impl()


async def _pull_credentials_impl() -> bool:
    orchestrator_url = os.environ.get("ORCHESTRATOR_URL", "")
    jwt_secret = os.environ.get("JWT_SECRET_KEY", "")
    if not orchestrator_url or not jwt_secret:
        return False

    url = f"{orchestrator_url.rstrip('/')}/claude-auth/credentials"
    headers = {"Authorization": f"Bearer {jwt_secret}"}
    creds_path = settings.claude_dir / ".credentials.json"

    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code != 200:
        logger.warning(f"Credentials pull: HTTP {resp.status_code}")
        return False

    data = resp.json()
    credentials = data.get("credentials")
    if not credentials:
        logger.info("Credentials pull: no credentials on orchestrator")
        return False

    _atomic_write(creds_path, json.dumps(credentials, indent=2))
    logger.info("Credentials refreshed from orchestrator")
    return True
