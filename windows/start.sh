#!/bin/bash
# Run inside WSL: starts tmux+Claude and bridge server

source /etc/claude_env.sh 2>/dev/null
source ~/.profile 2>/dev/null

TOKEN="${TELEGRAM_BOT_TOKEN}"
PROJECT="/mnt/d/AI/claudecode-telegram-main"
PORT=8080

if [ -z "$TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

# Kill any previous bridge instance and free the port
pkill -f "python3 bridge.py" 2>/dev/null; sleep 0.5
fuser -k ${PORT}/tcp 2>/dev/null; sleep 0.3

# If already running inside tmux, just do the work directly
if [ -n "$TMUX" ]; then
    # ── running inside tmux pane ──────────────────────────────────────

    # 1. Start Claude Code in a separate tmux session named 'claude'
    #    bridge.py looks for session 'claude' via: tmux has-session -t claude
    echo "[1] Starting tmux session 'claude'..."
    tmux kill-session -t claude 2>/dev/null || true
    tmux new-session -d -s claude "claude --dangerously-skip-permissions"
    echo "    tmux session 'claude' started."

    # 2. Start localtunnel
    echo "[2] Starting tunnel via localtunnel..."
    LTLOG=/tmp/lt_bridge.log

    if ! npx --yes lt --version &>/dev/null 2>&1; then
        echo "    Installing localtunnel..."
        npm install -g localtunnel 2>/dev/null
    fi

    npx lt --port $PORT > $LTLOG 2>&1 &
    echo "    Waiting for tunnel URL..."
    for i in $(seq 1 20); do
        URL=$(grep -o 'https://[^[:space:]]*.loca.lt' $LTLOG 2>/dev/null | head -1)
        [ -n "$URL" ] && break
        sleep 1
    done

    if [ -n "$URL" ]; then
        echo "    Public URL: $URL"
        echo "[3] Registering Telegram webhook..."
        RESULT=$(curl -s "https://api.telegram.org/bot${TOKEN}/setWebhook" \
            -d "url=$URL" 2>/dev/null)
        if echo "$RESULT" | grep -q '"ok":true'; then
            echo "    Webhook registered OK"
        else
            echo "    Webhook failed: $RESULT"
        fi
    else
        echo "    WARNING: Could not get tunnel URL."
        echo "    Try manually: npx lt --port 8080"
    fi

    # 3. Start bridge (with auto-restart loop)
    echo "[4] Starting bridge on port $PORT (auto-restart enabled)..."
    cd "$PROJECT"
    while true; do
        python3 bridge.py
        echo "    Bridge exited, restarting in 5s..."
        sleep 5
    done

else
    # ── NOT in tmux: launch a tmux session and re-run this script inside it ──

    echo "[0] Launching tmux session 'bridge'..."
    # Kill old session if any
    tmux kill-session -t bridge 2>/dev/null || true
    # Also kill old claude session
    tmux kill-session -t claude 2>/dev/null || true

    # Create new tmux session running this script again (now inside tmux)
    tmux new-session -d -s bridge -n bridge \
        "bash $0; read -p 'Exited. Press Enter...'"

    echo "    All services started in tmux session 'bridge'."
    echo "    Attach anytime: wsl -d Ubuntu -- tmux attach -t bridge"
    echo "    Done. This window can be closed."
fi
