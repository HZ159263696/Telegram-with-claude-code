#!/bin/bash
# Run inside WSL: starts tmux+Claude and bridge server

source /etc/claude_env.sh 2>/dev/null
source ~/.profile 2>/dev/null

TOKEN="${TELEGRAM_BOT_TOKEN}"
STOCK_TOKEN="${STOCK_BOT_TOKEN}"   # 股票Bot token（可选）
PROJECT="/mnt/d/AI/claudecode-telegram-main"
PORT=9999

if [ -z "$TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

# Kill any previous bridge instance and free the port
pkill -f "python3 bridge.py" 2>/dev/null; sleep 0.5
fuser -k ${PORT}/tcp 2>/dev/null; sleep 0.3

# 清除上次 session 的通知标记，本次启动允许发一次
rm -f /tmp/dashboard_notified_session.flag

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
    echo "[1] Starting tmux session 'claude' (主控Bot)..."
    tmux kill-session -t claude 2>/dev/null || true

    # Read saved model preference (default: claude-opus-4-7)
    MODEL_FILE="$HOME/.claude/telegram_model"
    SAVED_MODEL="claude-opus-4-7"
    if [ -f "$MODEL_FILE" ]; then
        SAVED_MODEL=$(cat "$MODEL_FILE" | tr -d '[:space:]')
        [ -z "$SAVED_MODEL" ] && SAVED_MODEL="claude-opus-4-7"
    fi

    # Determine if model needs LiteLLM proxy
    CLAUDE_MODELS="claude-opus-4-7 claude-sonnet-4-6 claude-haiku-4-5-20251001"
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

    # 1.5 Start stock Claude Code session (独立股票工作区)
    echo "[1.5] Starting tmux session 'claude_stock' (股票Bot)..."
    tmux kill-session -t claude_stock 2>/dev/null || true
    STOCK_MODEL_FILE="$HOME/.claude/telegram_model_stock"
    STOCK_MODEL="claude-sonnet-4-6"
    if [ -f "$STOCK_MODEL_FILE" ]; then
        STOCK_MODEL=$(cat "$STOCK_MODEL_FILE" | tr -d '[:space:]')
        [ -z "$STOCK_MODEL" ] && STOCK_MODEL="claude-sonnet-4-6"
    fi
    tmux new-session -d -s claude_stock -c /mnt/d/cao_stock \
        "claude --dangerously-skip-permissions --model $STOCK_MODEL"
    echo "    tmux session 'claude_stock' started in /mnt/d/cao_stock (model: $STOCK_MODEL)"

    # 1.6 Start Feishu Claude Code session (飞书Bot 独立工作区)
    echo "[1.6] Starting tmux session 'claude_feishu' (飞书Bot)..."
    tmux kill-session -t claude_feishu 2>/dev/null || true
    FEISHU_MODEL_FILE="$HOME/.claude/telegram_model_feishu"
    FEISHU_MODEL="claude-sonnet-4-6"
    if [ -f "$FEISHU_MODEL_FILE" ]; then
        FEISHU_MODEL=$(cat "$FEISHU_MODEL_FILE" | tr -d '[:space:]')
        [ -z "$FEISHU_MODEL" ] && FEISHU_MODEL="claude-sonnet-4-6"
    fi
    mkdir -p /mnt/d/AI/feishu_workspace
    tmux new-session -d -s claude_feishu -c /mnt/d/AI/feishu_workspace \
        "claude --dangerously-skip-permissions --model $FEISHU_MODEL"
    echo "    tmux session 'claude_feishu' started in /mnt/d/AI/feishu_workspace (model: $FEISHU_MODEL)"

    # 2. Start ngrok tunnel with auto-reconnect / 启动 ngrok 隧道（自动重连）
    # 用 ngrok 而不是 cloudflared/localtunnel: Telegram DNS 对 trycloudflare/loca.lt 不稳定，会拒绝解析
    echo "[2] Starting tunnel via ngrok (with auto-reconnect)..."
    pkill -f "ngrok http $PORT" 2>/dev/null; sleep 0.5

    (
        while true; do
            SLOG=/tmp/lt_bridge.log
            > "$SLOG"
            # ngrok 免费版不支持代理，启动前 unset 所有 proxy 环境变量
            env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
                ngrok http $PORT --domain=mantis-erasure-shown.ngrok-free.dev --log stdout --log-format=logfmt > "$SLOG" 2>&1 &
            LT_PID=$!

            # Wait for URL / 等待 URL 出现（通过本地 API 4040 拿，避免 stdout 缓冲）
            URL=""
            for i in $(seq 1 30); do
                URL=$(curl -s --noproxy '*' http://127.0.0.1:4040/api/tunnels 2>/dev/null \
                    | grep -oE '"public_url":"https://[^"]+"' | head -1 | sed 's/"public_url":"//;s/"$//')
                [ -n "$URL" ] && break
                sleep 1
            done

            if [ -n "$URL" ]; then
                echo "    Public URL: $URL"
                echo "    Registering Telegram webhook..."
                # cloudflared trycloudflare 子域名 DNS 需要几秒到几十秒传播，所以失败重试
                for attempt in 1 2 3 4 5 6 7 8 9 10; do
                    RESULT=$(curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
                        -d "url=$URL" -d "drop_pending_updates=true" 2>/dev/null)
                    if echo "$RESULT" | grep -q '"ok":true'; then
                        echo "    Webhook registered OK (attempt $attempt)"
                        break
                    fi
                    echo "    Webhook attempt $attempt failed: $RESULT"
                    sleep 6
                done
            else
                echo "    WARNING: Could not get tunnel URL, retrying in 5s..."
                kill $LT_PID 2>/dev/null
            fi

            # Wait for lt to exit (crash), then auto-restart / 等 lt 断开后重连
            wait $LT_PID 2>/dev/null
            echo "    Bridge tunnel crashed, reconnecting in 5s..."
            sleep 5
        done
    ) &
    BRIDGE_TUNNEL_PID=$!
    echo "    Bridge tunnel manager started (pid: $BRIDGE_TUNNEL_PID)"

    # 3. Start dashboard (web UI on port 8888)
    echo "[4] Starting dashboard on port 8888..."
    pkill -f "dashboard.py" 2>/dev/null; sleep 0.3
    fuser -k 8888/tcp 2>/dev/null; sleep 0.3
    nohup env TELEGRAM_BOT_TOKEN="$TOKEN" STOCK_BOT_TOKEN="$STOCK_TOKEN" DASHBOARD_PORT=8888 python3 "$PROJECT/dashboard.py" > /tmp/dashboard.log 2>&1 &
    echo "    Dashboard started (log: /tmp/dashboard.log)"

    # Dashboard 公网入口现在由 bridge.py 反向代理（同一个 ngrok 固定域名）
    # 旧实现：用 cloudflared trycloudflare 给 dashboard 拉一条独立隧道，
    # 但 trycloudflare 域名每次重启都变，手机 app 配置经常失效，已经废弃。
    pkill -f "cloudflared tunnel --url http://localhost:8888" 2>/dev/null
    rm -f /tmp/dashboard_url.txt /tmp/dashboard_notified_session.flag /tmp/lt_dashboard.log

    # 5. Start Feishu bridge (long-connection WebSocket)
    echo "[6] Starting Feishu bridge..."
    pkill -f "feishu_bridge.py" 2>/dev/null; sleep 0.3
    nohup python3 "$PROJECT/feishu_bridge.py" > /tmp/feishu_bridge.log 2>&1 &
    echo "    Feishu bridge started (log: /tmp/feishu_bridge.log)"

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
