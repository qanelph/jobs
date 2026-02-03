"""
Models — модели данных для внешних пользователей и задач.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class ExternalUser:
    """Внешний пользователь Telegram."""

    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    notes: str = ""
    first_contact: datetime = field(default_factory=datetime.now)
    last_contact: datetime = field(default_factory=datetime.now)
    warnings_count: int = 0
    is_banned: bool = False

    @property
    def display_name(self) -> str:
        """Отображаемое имя пользователя."""
        if self.first_name:
            parts = [self.first_name]
            if self.last_name:
                parts.append(self.last_name)
            return " ".join(parts)
        if self.username:
            return f"@{self.username}"
        return str(self.telegram_id)

    @property
    def mention(self) -> str:
        """Упоминание для отправки сообщений."""
        if self.username:
            return f"@{self.username}"
        return self.display_name


@dataclass
class UserTask:
    """Задача, назначенная пользователю."""

    id: str
    assignee_id: int
    description: str
    deadline: datetime | None = None
    status: Literal["pending", "accepted", "completed", "overdue"] = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    created_by: int | None = None  # telegram_id того, кто создал

    @property
    def is_overdue(self) -> bool:
        """Просрочена ли задача."""
        if self.deadline and self.status not in ("completed",):
            return datetime.now() > self.deadline
        return False
