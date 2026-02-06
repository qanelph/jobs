#!/bin/bash
# Локальный прокси-форвардер: localhost:8888 → upstream с авторизацией

if [ -z "$HTTP_PROXY" ]; then
    echo "HTTP_PROXY not set, tinyproxy disabled"
    sleep infinity
    exit 0
fi

# Парсим http://user:pass@host:port
PROXY_PARTS=$(echo "$HTTP_PROXY" | sed -E 's|https?://||')

if echo "$PROXY_PARTS" | grep -q '@'; then
    USERPASS=$(echo "$PROXY_PARTS" | cut -d'@' -f1)
    HOSTPORT=$(echo "$PROXY_PARTS" | cut -d'@' -f2)
else
    HOSTPORT="$PROXY_PARTS"
    USERPASS=""
fi

# Генерим конфиг
cat > /tmp/tinyproxy.conf << EOF
Port 8888
Listen 127.0.0.1
Timeout 600
MaxClients 100
LogLevel Error
EOF

if [ -n "$USERPASS" ]; then
    echo "Upstream http ${USERPASS}@${HOSTPORT}" >> /tmp/tinyproxy.conf
else
    echo "Upstream http ${HOSTPORT}" >> /tmp/tinyproxy.conf
fi

exec tinyproxy -d -c /tmp/tinyproxy.conf
