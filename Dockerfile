FROM python:3.12-slim

# Системные зависимости
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Установка Node.js (для Claude Code CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Установка Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Создаём non-root пользователя (Claude Code не работает под root)
RUN useradd -m -s /bin/bash jobs

# Python зависимости
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Копируем код
COPY src/ /app/src/

# Создаём рабочие директории с правами для пользователя
RUN mkdir -p /workspace /data /home/jobs/.claude && \
    chown -R jobs:jobs /workspace /data /home/jobs/.claude /app

# Переключаемся на non-root пользователя
USER jobs

WORKDIR /app

CMD ["python", "-m", "src.main"]
