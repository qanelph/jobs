FROM debian:bookworm-slim

# Системные зависимости
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-sandbox \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    supervisor \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-noto-cjk \
    dbus-x11 \
    procps \
    curl \
    haproxy \
    tinyproxy \
    pulseaudio \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Создаём пользователя
RUN useradd -m -s /bin/bash browser

# Директории
RUN mkdir -p /home/browser/.config/chromium \
    /home/browser/.cache/dconf \
    /home/browser/.pki/nssdb \
    /home/browser/.local/share \
    /var/log/supervisor \
    /tmp/.X11-unix \
    && chown -R browser:browser /home/browser

# Директория для аудио-записей
RUN mkdir -p /recordings && chown browser:browser /recordings

# Директория управления (shared volume с jobs)
RUN mkdir -p /browser-control && chmod 777 /browser-control

# Supervisor config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# HAProxy config
COPY haproxy.cfg /etc/haproxy/haproxy.cfg

# Скрипты запуска
COPY start-*.sh /usr/local/bin/
COPY cleanup-recordings.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/start-*.sh /usr/local/bin/cleanup-recordings.sh

# Порты
# 9223 - CDP proxy (socat)
# 5900 - VNC
# 6080 - noVNC web interface
EXPOSE 9223 5900 6080

# Переменные окружения
ENV DISPLAY=:99
ENV RESOLUTION=1920x1080x24

WORKDIR /home/browser

# supervisord запускается от root, программы от user browser
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
