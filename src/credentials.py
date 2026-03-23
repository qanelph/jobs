"""Управление Claude OAuth credentials — pull от оркестратора."""

import json
import os

import httpx
from loguru import logger

from src.config import settings


async def pull_credentials() -> bool:
    """Запрашивает свежие credentials у оркестратора.

    Orchestrator автоматически рефрешит токен если он истёк.
    Возвращает True если credentials успешно обновлены.
    """
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
        logger.warning("Credentials pull: no credentials on orchestrator")
        return False

    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps(credentials, indent=2))
    logger.info("Credentials refreshed from orchestrator")
    return True
