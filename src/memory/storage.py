"""
Memory Storage — файловое хранилище памяти.

Три уровня:
1. MEMORY.md — долгосрочная (предпочтения, решения, факты)
2. memory/YYYY-MM-DD.md — дневные логи (append-only)
3. sessions/YYYY-MM-DD-slug.md — транскрипты диалогов
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re

from loguru import logger

from src.config import settings


@dataclass
class MemoryEntry:
    """Запись из памяти."""
    content: str
    file_path: Path
    line_start: int = 1
    line_end: int | None = None


class MemoryStorage:
    """Файловое хранилище памяти."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._memory_dir = workspace / "memory"
        self._sessions_dir = workspace / "sessions"
        self._init_structure()

    def _init_structure(self) -> None:
        """Создаёт структуру директорий."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        # Создаём MEMORY.md если нет
        memory_file = self._workspace / "MEMORY.md"
        if not memory_file.exists():
            memory_file.write_text(
                "# Long-term Memory\n\n"
                "<!-- Важные факты, предпочтения, решения -->\n\n"
            )
            logger.info(f"Created {memory_file}")

        # Создаём HEARTBEAT.md если нет
        heartbeat_file = self._workspace / "HEARTBEAT.md"
        if not heartbeat_file.exists():
            heartbeat_file.write_text(
                "# Heartbeat Checklist\n\n"
                "При каждом heartbeat проверяй:\n\n"
                "- [ ] Есть ли срочные задачи в scheduled_tasks?\n"
                "- [ ] Есть ли что-то важное в дневном логе?\n"
                "- [ ] Нужно ли напомнить пользователю о чём-то?\n\n"
                "Если ничего важного — отвечай: HEARTBEAT_OK\n"
            )
            logger.info(f"Created {heartbeat_file}")

    # =========================================================================
    # Long-term Memory (MEMORY.md)
    # =========================================================================

    @property
    def memory_file(self) -> Path:
        return self._workspace / "MEMORY.md"

    def read_memory(self) -> str:
        """Читает долгосрочную память."""
        if self.memory_file.exists():
            return self.memory_file.read_text()
        return ""

    def append_to_memory(self, content: str) -> None:
        """Добавляет запись в долгосрочную память."""
        current = self.read_memory()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        new_content = f"{current.rstrip()}\n\n## {timestamp}\n\n{content}\n"
        self.memory_file.write_text(new_content)
        logger.info(f"Appended to MEMORY.md: {content[:50]}...")

    # =========================================================================
    # Daily Logs (memory/YYYY-MM-DD.md)
    # =========================================================================

    def _daily_log_path(self, date: datetime | None = None) -> Path:
        """Путь к дневному логу."""
        if date is None:
            date = datetime.now()
        return self._memory_dir / f"{date.strftime('%Y-%m-%d')}.md"

    def read_daily_log(self, date: datetime | None = None) -> str:
        """Читает дневной лог."""
        path = self._daily_log_path(date)
        if path.exists():
            return path.read_text()
        return ""

    def append_to_daily_log(self, content: str) -> None:
        """Добавляет запись в дневной лог."""
        path = self._daily_log_path()
        timestamp = datetime.now().strftime("%H:%M")

        if path.exists():
            current = path.read_text()
            new_content = f"{current.rstrip()}\n\n### {timestamp}\n\n{content}\n"
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")
            new_content = f"# Daily Log: {date_str}\n\n### {timestamp}\n\n{content}\n"

        path.write_text(new_content)
        logger.debug(f"Appended to daily log: {content[:50]}...")

    def get_recent_context(self, days: int = 2) -> str:
        """
        Возвращает контекст за последние N дней.

        По умолчанию: сегодня + вчера (как в OpenClaw).
        """
        parts = []

        for i in range(days):
            date = datetime.now() - timedelta(days=i)
            content = self.read_daily_log(date)
            if content:
                parts.append(content)

        return "\n\n---\n\n".join(parts)

    # =========================================================================
    # Session Transcripts (sessions/YYYY-MM-DD-slug.md)
    # =========================================================================

    def _session_path(self, slug: str) -> Path:
        """Путь к транскрипту сессии."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        safe_slug = re.sub(r'[^\w\-]', '-', slug.lower())[:30]
        return self._sessions_dir / f"{date_str}-{safe_slug}.md"

    def save_session(self, slug: str, content: str) -> Path:
        """Сохраняет транскрипт сессии."""
        path = self._session_path(slug)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        full_content = f"# Session: {slug}\n\n**Date**: {timestamp}\n\n---\n\n{content}\n"
        path.write_text(full_content)
        logger.info(f"Saved session: {path.name}")
        return path

    def append_to_session(self, slug: str, role: str, content: str) -> None:
        """Добавляет сообщение в транскрипт сессии."""
        path = self._session_path(slug)
        timestamp = datetime.now().strftime("%H:%M")

        if path.exists():
            current = path.read_text()
        else:
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            current = f"# Session: {slug}\n\n**Date**: {date_str}\n\n---\n\n"

        entry = f"**[{timestamp}] {role}**: {content}\n\n"
        path.write_text(current + entry)

    # =========================================================================
    # File Operations (для tools)
    # =========================================================================

    def read_file(self, relative_path: str) -> str | None:
        """Читает файл из workspace."""
        path = self._workspace / relative_path
        if path.exists() and path.is_file():
            return path.read_text()
        return None

    def list_memory_files(self) -> list[Path]:
        """Список всех файлов памяти для индексирования."""
        files = []

        # MEMORY.md
        if self.memory_file.exists():
            files.append(self.memory_file)

        # Daily logs
        files.extend(sorted(self._memory_dir.glob("*.md")))

        # Sessions
        files.extend(sorted(self._sessions_dir.glob("*.md")))

        return files


# Singleton
_storage: MemoryStorage | None = None


def get_storage() -> MemoryStorage:
    """Возвращает глобальный storage."""
    global _storage
    if _storage is None:
        _storage = MemoryStorage(settings.workspace_dir)
    return _storage
