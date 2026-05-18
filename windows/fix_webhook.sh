#!/bin/bash
# 修复 webhook：通过 tmux 启动 serveo 隧道并重新注册到 Telegram
# Fix webhook: start serveo tunnel via tmux and re-register to Telegram
set -e
SLOG=/tmp/serveo.log
PORT=${PORT:-9999}

# 清理旧的隧道（localtunnel / cloudflared / serveo）/ kill old tunnels
for pid in $(pgrep -f "lt --port $PORT" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done
for pid in $(pgrep -f "cloudflared tunnel.*:$PORT" 2>/dev/null); do kill "$pid" 2>/dev/null || true; done
tmux kill-session -t serveo 2>/dev/null || true
sleep 2

: > "$SLOG"
# tmux 里启动 ssh，保持持久会话 / start ssh inside tmux for persistence
tmux new-session -d -s serveo "ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R 80:localhost:$PORT serveo.net 2>&1 | tee $SLOG"
echo "serveo started, waiting for URL..."

URL=""
i=0
while [ "$i" -lt 30 ]; do
    sleep 1
    URL=$(sed 's/\x1b\[[0-9;]*[mK]//g' "$SLOG" 2>/dev/null | tr -d '\r' | grep -oE 'https://[a-z0-9-]+\.serveousercontent\.com' | head -1 || true)
    if [ -n "$URL" ]; then break; fi
    i=$((i+1))
done

if [ -z "$URL" ]; then
    echo "ERROR: no URL captured within 30s"
    cat "$SLOG"
    exit 1
fi
echo "URL=$URL"

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    # 从 dashboard 进程继承 / inherit from dashboard
    DASH_PID=$(pgrep -f dashboard.py | head -1)
    if [ -n "$DASH_PID" ]; then
        TELEGRAM_BOT_TOKEN=$(cat /proc/$DASH_PID/environ 2>/dev/null | tr '\0' '\n' | grep '^TELEGRAM_BOT_TOKEN=' | cut -d= -f2-)
    fi
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

echo "registering webhook..."
curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" -d "url=$URL"
echo
echo "--- webhook info ---"
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
echo
