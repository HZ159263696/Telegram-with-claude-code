#!/bin/bash
# Run inside WSL: starts tmux+Claude and bridge server

source /etc/claude_env.sh 2>/dev/null
source ~/.profile 2>/dev/null

TOKEN="${TELEGRAM_BOT_TOKEN}"
PROJECT="/mnt/d/AI/claudecode-telegram-main"
PORT=9999

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

    # 0. Start LiteLLM proxy
    echo "[0] Starting LiteLLM proxy on port 4000..."
    pkill -f "litellm" 2>/dev/null; sleep 0.5
    LITELLM_CONFIG="$PROJECT/litellm_config.yaml"
    if command -v litellm &>/dev/null; then
        nohup litellm --config "$LITELLM_CONFIG" --port 4000 > /tmp/litellm.log 2>&1 &
        sleep 2
        echo "    LiteLLM proxy started (log: /tmp/litellm.log)"
    else
        echo "    WARNING: litellm not found. Run: pip install 'litellm[proxy]'"
    fi

    # 0.5. Start Anthropic-to-OpenAI translation proxy (needed before Claude Code for non-Claude models)
    echo "[0.5] Starting Anthropic proxy on port 4001..."
    pkill -f "anthropic_proxy.py" 2>/dev/null; sleep 0.3
    fuser -k 4001/tcp 2>/dev/null; sleep 0.3
    PROXY_SCRIPT="$PROJECT/anthropic_proxy.py"
    if [ -f "$PROXY_SCRIPT" ]; then
        nohup python3 "$PROXY_SCRIPT" > /tmp/anthropic_proxy.log 2>&1 &
        sleep 1
        echo "    Anthropic proxy started (log: /tmp/anthropic_proxy.log)"
    else
        echo "    WARNING: anthropic_proxy.py not found, non-Claude models won't work"
    fi

    # 1. Start Claude Code in a separate tmux session named 'claude'
    #    bridge.py looks for session 'claude' via: tmux has-session -t claude
    echo "[1] Starting tmux session 'claude'..."
    tmux kill-session -t claude 2>/dev/null || true

    # Read saved model preference (default: claude-opus-4-6)
    MODEL_FILE="$HOME/.claude/telegram_model"
    SAVED_MODEL="claude-opus-4-6"
    if [ -f "$MODEL_FILE" ]; then
        SAVED_MODEL=$(cat "$MODEL_FILE" | tr -d '[:space:]')
        [ -z "$SAVED_MODEL" ] && SAVED_MODEL="claude-opus-4-6"
    fi

    # Determine if model needs LiteLLM proxy
    CLAUDE_MODELS="claude-opus-4-6 claude-sonnet-4-6 claude-haiku-4-5-20251001"
    IS_CLAUDE=false
    for cm in $CLAUDE_MODELS; do
        [ "$SAVED_MODEL" = "$cm" ] && IS_CLAUDE=true
    done

    if $IS_CLAUDE; then
        tmux new-session -d -s claude "claude --dangerously-skip-permissions --model $SAVED_MODEL"
    else
        # Map non-Claude models to CLI aliases for LiteLLM routing
        case "$SAVED_MODEL" in
            deepseek-chat)     CLI_MODEL="claude-3-5-sonnet-20241022" ;;
            deepseek-reasoner) CLI_MODEL="claude-3-opus-20240229" ;;
            glm-4-plus)        CLI_MODEL="claude-3-sonnet-20240229" ;;
            glm-4-flash)       CLI_MODEL="claude-3-haiku-20240307" ;;
            abab6.5s-chat)     CLI_MODEL="claude-3-5-haiku-20241022" ;;
            qwen-max)          CLI_MODEL="claude-3-5-sonnet-latest" ;;
            qwen-plus)         CLI_MODEL="claude-3-opus-latest" ;;
            *)                 CLI_MODEL="$SAVED_MODEL" ;;
        esac
        tmux new-session -d -s claude "ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL=http://localhost:4001 claude --dangerously-skip-permissions --model $CLI_MODEL"
    fi
    echo "    tmux session 'claude' started (model: $SAVED_MODEL)."

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

    # 3. Start dashboard (web UI on port 8888)
    echo "[4] Starting dashboard on port 8888..."
    pkill -f "dashboard.py" 2>/dev/null; sleep 0.3
    fuser -k 8888/tcp 2>/dev/null; sleep 0.3
    nohup env TELEGRAM_BOT_TOKEN="$TOKEN" DASHBOARD_PORT=8888 python3 "$PROJECT/dashboard.py" > /tmp/dashboard.log 2>&1 &
    echo "    Dashboard started (log: /tmp/dashboard.log)"

    # 4. Bridge is managed by dashboard (auto-start on dashboard launch)
    echo "[5] Bridge managed by dashboard on port $PORT."
    echo "    Open http://localhost:8888 to control the bridge."
    # Keep session alive
    wait

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
