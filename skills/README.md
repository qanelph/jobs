# Jobs Skills

Расширения функциональности через skills.

## Архитектура Cross-Session Communication

### Проблема

Когда owner просит: "напиши @user и договорись о встрече"
- Owner session вызывает `send_to_user()` → сообщение уходит в Telegram
- Когда @user отвечает → ответ попадает в сессию @user, НЕ owner'а
- Owner session не видит ответы

### Решение: Conversation Tasks

```
┌─────────────────┐                    ┌─────────────────┐
│  Owner Session  │                    │   User Session  │
│                 │                    │                 │
│  "договорись    │                    │                 │
│   о встрече"    │                    │                 │
└────────┬────────┘                    └────────┬────────┘
         │                                      │
         │ 1. create_conversation_task()        │
         │    (сохраняем контекст в БД)         │
         ▼                                      │
┌─────────────────┐                             │
│ ConversationTask│                             │
│ - owner_id      │                             │
│ - user_id       │                             │
│ - type: meeting │                             │
│ - context: {}   │                             │
│ - status        │                             │
└────────┬────────┘                             │
         │                                      │
         │ 2. send_to_user_session()            │
         │    (инжектим контекст в сессию)      │
         ▼                                      ▼
         ┌──────────────────────────────────────┐
         │         User получает сообщение      │
         │  + system context о задаче owner'а   │
         └──────────────────────────────────────┘
                           │
                           │ 3. User отвечает
                           ▼
         ┌──────────────────────────────────────┐
         │   User Session видит контекст:       │
         │   "Owner просит согласовать встречу  │
         │    на завтра 12-20 МСК"              │
         │                                      │
         │   → Собирает инфо от user            │
         │   → Обновляет ConversationTask       │
         │   → Уведомляет owner                 │
         └──────────────────────────────────────┘
```

### Модели

```python
@dataclass
class ConversationTask:
    id: str
    owner_id: int           # Кто создал
    user_id: int            # С кем общаемся
    task_type: str          # meeting, question, task_assignment
    context: dict           # Контекст для user session
    status: str             # pending, in_progress, completed, cancelled
    result: dict | None     # Результат согласования
    created_at: datetime
    updated_at: datetime
```

### Новые Tools

**Для Owner:**
- `start_conversation(user, type, context)` — начать согласование
- `get_conversation_status(task_id)` — статус согласования

**Для User Session (автоматически):**
- При старте сессии проверяем активные ConversationTask
- Инжектим контекст в system prompt
- `update_conversation(result)` — обновить результат
- `notify_owner(message)` — уведомить владельца

## Skills

### schedule-meeting
Согласование встречи с участником.

## TODO

- [ ] Модель ConversationTask
- [ ] Tools для cross-session
- [ ] Интеграция в session_manager
- [ ] Skill schedule-meeting
