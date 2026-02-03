"""
Models — модели данных для внешних пользователей и задач.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Any
import json


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


@dataclass
class ConversationTask:
    """
    Задача согласования между owner и user.

    Позволяет owner'у делегировать общение с user'ом,
    при этом user session получает контекст задачи.
    """

    id: str
    owner_id: int                 # Кто создал задачу
    user_id: int                  # С кем общаемся
    task_type: Literal["meeting", "question", "custom"] = "custom"
    title: str = ""               # Краткое описание для user
    context: dict = field(default_factory=dict)  # Контекст для user session
    status: Literal["pending", "in_progress", "completed", "cancelled"] = "pending"
    result: dict | None = None    # Результат согласования
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def context_json(self) -> str:
        """Сериализует context в JSON."""
        return json.dumps(self.context, ensure_ascii=False)

    def result_json(self) -> str | None:
        """Сериализует result в JSON."""
        return json.dumps(self.result, ensure_ascii=False) if self.result else None

    @staticmethod
    def from_row(row: dict) -> "ConversationTask":
        """Создаёт из строки БД."""
        return ConversationTask(
            id=row["id"],
            owner_id=row["owner_id"],
            user_id=row["user_id"],
            task_type=row["task_type"],
            title=row["title"],
            context=json.loads(row["context"]) if row["context"] else {},
            status=row["status"],
            result=json.loads(row["result"]) if row["result"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
