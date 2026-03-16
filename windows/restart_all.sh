#!/bin/bash
source /etc/claude_env.sh

# Kill old processes
pkill -f "python3 bridge.py" 2>/dev/null
pkill -f "lt --port" 2>/dev/null
sleep 1

# Start tmux Claude if not running
tmux has-session -t claude 2>/dev/null || tmux new-session -d -s claude "claude --dangerously-skip-permissions"
echo "[1] tmux claude: $(tmux ls 2>&1 | grep claude)"

# Start bridge
cd /mnt/d/AI/claudecode-telegram-main
nohup python3 bridge.py > /tmp/bridge.log 2>&1 &
sleep 2
echo "[2] bridge: $(cat /tmp/bridge.log)"
echo "    port: $(ss -tlnp | grep 8080 || echo NOT_LISTENING)"

# Start localtunnel
nohup lt --port 8080 > /tmp/lt.log 2>&1 &
sleep 8
LT_URL=$(grep -o 'https://[^ ]*' /tmp/lt.log | head -1)
echo "[3] tunnel: $LT_URL"

# Register webhook
if [ -n "$LT_URL" ]; then
    RESULT=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" -d "url=$LT_URL")
    echo "[4] webhook: $RESULT"
else
    echo "[4] webhook: SKIPPED (no tunnel URL)"
fi

echo ""
echo "=== ALL DONE ==="
