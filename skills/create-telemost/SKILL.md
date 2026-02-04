---
name: create-telemost
description: Use this skill to create a Yandex Telemost video meeting link. Triggers: "создай телемост", "создай ссылку на встречу", "create telemost link", "сделай видеовстречу". Also used automatically by schedule-meeting skill 5 minutes before a meeting.
tools: Read, Bash
---

# Create Telemost

## Algorithm

# Create Telemost Link

Создание ссылки на видеовстречу в Яндекс Телемост. Браузер уже авторизован под аккаунтом Яндекс.

## Algorithm

### 1. Open Telemost
```
browser_navigate("https://telemost.yandex.ru")
```

### 2. Take Snapshot
```
browser_snapshot()
```
Найди кнопку "Создать видеовстречу".

### 3. Click Create
Нажми кнопку "Создать видеовстречу".

### 4. Handle Popups
После создания могут появиться диалоги:
- "Включить микрофон не удалось" — нажми "Понятно"
- "Включить видео не удалось" — нажми "Понятно"

Закрывай их по очереди. Может потребоваться несколько snapshot + click.

### 5. Extract Link
Ссылка на встречу будет в URL страницы в формате:
`https://telemost.yandex.ru/j/XXXXXXXXXXXXX`

Также можно найти в snapshot номер встречи.

### 6. Return Result
Верни ссылку пользователю. Если в контексте есть участники — отправь им ссылку через `tg_send_message` или `send_to_user`.

## Important Notes
- НЕ пытайся включать камеру/микрофон — на сервере их нет
- Если Яндекс просит авторизацию — сообщи владельцу, сессия истекла
- Ссылка действительна сразу после создания
