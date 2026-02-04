"""
Users Module — управление внешними пользователями и их задачами.
"""

from .models import ExternalUser, Task
from .repository import UsersRepository, get_users_repository
from .session_manager import SessionManager, get_session_manager

__all__ = [
    "ExternalUser",
    "Task",
    "UsersRepository",
    "get_users_repository",
    "SessionManager",
    "get_session_manager",
]
