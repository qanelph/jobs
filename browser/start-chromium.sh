#!/bin/bash
# Chromium с CDP (Chrome DevTools Protocol)

# Ждём Xvfb
sleep 2

# Устанавливаем окружение
export HOME=/home/browser

# Удаляем lock файлы от предыдущих запусков
rm -f /home/browser/.config/chromium/SingletonLock
rm -f /home/browser/.config/chromium/SingletonSocket
rm -f /home/browser/.config/chromium/SingletonCookie
export XDG_CONFIG_HOME=/home/browser/.config
export XDG_CACHE_HOME=/home/browser/.cache
export XDG_DATA_HOME=/home/browser/.local/share

# Создаём необходимые директории
mkdir -p /home/browser/.cache/dconf
mkdir -p /home/browser/.pki/nssdb

PROXY_FLAG=""
if [ -n "$HTTP_PROXY" ]; then
    # Chromium ходит через локальный tinyproxy, который форвардит на upstream с авторизацией
    PROXY_FLAG="--proxy-server=http://127.0.0.1:8888"
fi

exec chromium \
    $PROXY_FLAG \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --disable-software-rasterizer \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --remote-allow-origins=* \
    --user-data-dir=/home/browser/.config/chromium \
    --window-size=1920,1080 \
    --start-maximized \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --disable-features=TranslateUI \
    --disable-ipc-flooding-protection \
    --disable-breakpad \
    --disable-component-update \
    --disable-domain-reliability \
    --disable-sync \
    --no-first-run \
    --no-default-browser-check \
    --enable-features=NetworkService,NetworkServiceInProcess \
    about:blank
