#!/bin/bash
# Run inside WSL: starts tmux+Claude and bridge server
#
# ⚠️ 后台进程启动约定（务必遵守）：每个 `&` 后台服务都必须写成
#       nohup CMD > /tmp/xxx.log 2>&1 </dev/null &
#       disown
#    三件套缺一不可：</dev/null 断开 stdin、>log 2>&1 断开输出、disown 移出作业表。
#    否则子进程会继承并占住父 shell 的管道 fd，导致依赖它的持久 shell（如 Claude Code
#    的 Bash 工具）read 永不返回而"零返回/卡死"。新增后台服务请照此模板。

source /etc/claude_env.sh 2>/dev/null
source ~/.profile 2>/dev/null

# Codex CLI 以当前 WSL 用户安装时默认位于 ~/.local/bin。
if [ -x "$HOME/.local/bin/codex" ]; then
    export CODEX_EXECUTABLE="${CODEX_EXECUTABLE:-$HOME/.local/bin/codex}"
fi

TOKEN="${TELEGRAM_BOT_TOKEN}"
STOCK_TOKEN="${STOCK_BOT_TOKEN}"   # 股票Bot token（可选）
PROJECT="/mnt/d/AI/claudecode-telegram-main"
FEISHU_STOCK_CONFIG="$PROJECT/.env.feishu_stock"
PORT=9999

if [ -z "$TOKEN" ]; then
    echo "ERROR: TELEGRAM_BOT_TOKEN not set"
    exit 1
fi

# Always deploy the exact hook revision that this bridge release uses.  The
# Stop hooks execute from ~/.claude/hooks, while bridge.py invokes the project
# copy directly; allowing the two copies to drift caused fixes to work for one
# route but not another.  Deployment is idempotent and preserves user memory.
if [ -x "$PROJECT/windows/deploy_hook.sh" ]; then
    echo "[hook] Deploying current reply hook..."
    bash "$PROJECT/windows/deploy_hook.sh" || {
        echo "ERROR: reply-hook deployment failed; refusing to start an inconsistent bridge."
        exit 1
    }
fi

# Kill any previous bridge instance and free the port
pkill -f "python3 bridge.py" 2>/dev/null; sleep 0.5
fuser -k ${PORT}/tcp 2>/dev/null; sleep 0.3

# 清除上次 session 的通知标记，本次启动允许发一次
rm -f /tmp/dashboard_notified_session.flag

# If already running inside tmux, just do the work directly
if [ -n "$TMUX" ]; then
    # ── running inside tmux pane ──────────────────────────────────────

    # 非 Claude 模型借用独立 Claude CLI 别名，由本地兼容代理路由到真实模型。
    proxy_cli_model() {
        case "$1" in
            deepseek-chat|deepseek-v4-flash)   echo "claude-3-5-sonnet-20241022" ;;
            deepseek-reasoner|deepseek-v4-pro) echo "claude-3-opus-20240229" ;;
            glm-4-plus)                        echo "claude-3-sonnet-20240229" ;;
            glm-4-flash)                       echo "claude-3-haiku-20240307" ;;
            qwen-max)                          echo "claude-3-5-sonnet-latest" ;;
            qwen-plus)                         echo "claude-3-opus-latest" ;;
            *)                                 echo "$1" ;;
        esac
    }

    claude_cmd_for_model() {
        local model="$1"
        if [[ "$model" == "codex-subscription" || "$model" == gpt-5.6-* ]]; then
            # Codex 订阅模式由 bridge 按消息执行 `codex exec`；保留空 tmux pane 供状态/中断命令使用。
            printf 'echo "Codex subscription mode: bridge runs codex exec per message"; exec bash'
        elif [[ "$model" == claude-* ]]; then
            printf 'claude --dangerously-skip-permissions --model %s' "$model"
        else
            printf 'env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy NO_PROXY="*" no_proxy="*" ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL=http://localhost:4001 claude --dangerously-skip-permissions --model %s' "$(proxy_cli_model "$model")"
        fi
    }

    # 0. Start LiteLLM proxy
    echo "[0] Starting LiteLLM proxy on port 4000..."
    pkill -f "litellm" 2>/dev/null; sleep 0.5
    LITELLM_CONFIG="$PROJECT/litellm_config.yaml"
    if command -v litellm &>/dev/null; then
        nohup litellm --config "$LITELLM_CONFIG" --port 4000 > /tmp/litellm.log 2>&1 </dev/null &
        disown
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
        nohup env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy \
            NO_PROXY="*" no_proxy="*" python3 "$PROXY_SCRIPT" > /tmp/anthropic_proxy.log 2>&1 </dev/null &
        disown
        sleep 1
        echo "    Anthropic proxy started (log: /tmp/anthropic_proxy.log)"
    else
        echo "    WARNING: anthropic_proxy.py not found, non-Claude models won't work"
    fi

    # 1. Start Claude Code in a separate tmux session named 'claude'
    #    bridge.py looks for session 'claude' via: tmux has-session -t claude
    echo "[1] Starting tmux session 'claude' (主控Bot)..."
    tmux kill-session -t claude 2>/dev/null || true

    # Read saved model preference (default: claude-fable-5)
    MODEL_FILE="$HOME/.claude/telegram_model"
    SAVED_MODEL="claude-fable-5"
    if [ -f "$MODEL_FILE" ]; then
        SAVED_MODEL=$(cat "$MODEL_FILE" | tr -d '[:space:]')
        [ -z "$SAVED_MODEL" ] && SAVED_MODEL="claude-fable-5"
    fi

    tmux new-session -d -s claude -c "$PROJECT" "$(claude_cmd_for_model "$SAVED_MODEL")"
    echo "    tmux session 'claude' started (model: $SAVED_MODEL)."

    # 1.5 Start stock Claude Code session (独立股票工作区)
    echo "[1.5] Starting tmux session 'claude_stock' (股票Bot)..."
    tmux kill-session -t claude_stock 2>/dev/null || true
    STOCK_MODEL_FILE="$HOME/.claude/telegram_model_stock"
    STOCK_MODEL="claude-sonnet-5"
    if [ -f "$STOCK_MODEL_FILE" ]; then
        STOCK_MODEL=$(cat "$STOCK_MODEL_FILE" | tr -d '[:space:]')
        [ -z "$STOCK_MODEL" ] && STOCK_MODEL="claude-sonnet-5"
    fi
    tmux new-session -d -s claude_stock -c /mnt/d/cao_stock \
        "$(claude_cmd_for_model "$STOCK_MODEL")"
    echo "    tmux session 'claude_stock' started in /mnt/d/cao_stock (model: $STOCK_MODEL)"

    # 1.6 Start Feishu Claude Code session (飞书Bot 独立工作区)
    echo "[1.6] Starting tmux session 'claude_feishu' (飞书Bot)..."
    tmux kill-session -t claude_feishu 2>/dev/null || true
    FEISHU_MODEL_FILE="$HOME/.claude/telegram_model_feishu"
    FEISHU_MODEL="claude-sonnet-5"
    if [ -f "$FEISHU_MODEL_FILE" ]; then
        FEISHU_MODEL=$(cat "$FEISHU_MODEL_FILE" | tr -d '[:space:]')
        [ -z "$FEISHU_MODEL" ] && FEISHU_MODEL="claude-sonnet-5"
    fi
    mkdir -p /mnt/d/AI/feishu_workspace
    # 部署飞书发文件助手（Claude 在 session 内调用，把文件发回飞书群）
    cp "$PROJECT/hooks/feishu_send_file.py" "$HOME/.claude/hooks/feishu_send_file.py" 2>/dev/null || true
    tmux new-session -d -s claude_feishu -c /mnt/d/AI/feishu_workspace \
        "$(claude_cmd_for_model "$FEISHU_MODEL")"
    echo "    tmux session 'claude_feishu' started in /mnt/d/AI/feishu_workspace (model: $FEISHU_MODEL)"

    # 1.7 Start Feishu stock/red Bot.  It has an independent reply route/session,
    # but AKASHIC_MEM=stock deliberately shares the Telegram stock Bot's memory.
    if [ -f "$FEISHU_STOCK_CONFIG" ]; then
        echo "[1.7] Starting tmux session 'claude_feishu_stock' (飞书红Bot)..."
        tmux kill-session -t claude_feishu_stock 2>/dev/null || true
        mkdir -p /mnt/d/AI/feishu_stock_workspace
        tmux new-session -d -s claude_feishu_stock -c /mnt/d/AI/feishu_stock_workspace \
            "AKASHIC_MEM=stock FEISHU_CONFIG_FILE='$FEISHU_STOCK_CONFIG' FEISHU_CHAT_ID_FILE='$HOME/.claude/feishu_chat_id_stock' $(claude_cmd_for_model "$STOCK_MODEL")"
        echo "    tmux session 'claude_feishu_stock' started with shared stock memory (model: $STOCK_MODEL)"
    else
        echo "    WARNING: $FEISHU_STOCK_CONFIG missing; 飞书红Bot disabled"
    fi

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
                # webhook 注册统一由 bridge.py 负责（带 secret_token 校验）。
                # 此处禁止再裸 setWebhook：不带 secret 的注册会清掉 Telegram 侧 secret，
                # 导致 bridge 对所有进站请求回 401，主控Bot 全聋（2026-06-11 事故）。
                # ngrok 用固定域名，URL 不变，断线重连后也无需重新注册。
            else
                echo "    WARNING: Could not get tunnel URL, retrying in 5s..."
                kill $LT_PID 2>/dev/null
            fi

            # Wait for lt to exit (crash), then auto-restart / 等 lt 断开后重连
            wait $LT_PID 2>/dev/null
            echo "    Bridge tunnel crashed, reconnecting in 5s..."
            sleep 5
        done
    ) > /tmp/tunnel_mgr.log 2>&1 </dev/null &
    BRIDGE_TUNNEL_PID=$!
    disown
    echo "    Bridge tunnel manager started (pid: $BRIDGE_TUNNEL_PID, log: /tmp/tunnel_mgr.log)"

    # 3. Start dashboard (web UI on port 8888)
    echo "[4] Starting dashboard on port 8888..."
    pkill -f "dashboard.py" 2>/dev/null; sleep 0.3
    fuser -k 8888/tcp 2>/dev/null; sleep 0.3
    nohup env TELEGRAM_BOT_TOKEN="$TOKEN" STOCK_BOT_TOKEN="$STOCK_TOKEN" DASHBOARD_PORT=8888 python3 "$PROJECT/dashboard.py" > /tmp/dashboard.log 2>&1 </dev/null &
    disown
    echo "    Dashboard started (log: /tmp/dashboard.log)"

    # Dashboard 公网入口现在由 bridge.py 反向代理（同一个 ngrok 固定域名）
    # 旧实现：用 cloudflared trycloudflare 给 dashboard 拉一条独立隧道，
    # 但 trycloudflare 域名每次重启都变，手机 app 配置经常失效，已经废弃。
    pkill -f "cloudflared tunnel --url http://localhost:8888" 2>/dev/null
    rm -f /tmp/dashboard_url.txt /tmp/dashboard_notified_session.flag /tmp/lt_dashboard.log

    # 5. Start Feishu bridge (long-connection WebSocket)
    echo "[6] Starting Feishu bridge..."
    pkill -f "feishu_bridge.py" 2>/dev/null; sleep 0.3
    nohup python3 "$PROJECT/feishu_bridge.py" > /tmp/feishu_bridge.log 2>&1 </dev/null &
    disown
    echo "    Feishu bridge started (log: /tmp/feishu_bridge.log)"
    if [ -f "$FEISHU_STOCK_CONFIG" ]; then
        nohup env FEISHU_CONFIG_FILE="$FEISHU_STOCK_CONFIG" \
            FEISHU_BOT_KEY=feishu_stock FEISHU_MEMORY_BOT=stock \
            FEISHU_TMUX_SESSION=claude_feishu_stock \
            FEISHU_WORK_DIR=/mnt/d/AI/feishu_stock_workspace \
            FEISHU_MODEL_FILE="$HOME/.claude/telegram_model_stock" \
            FEISHU_THINKING_FILE="$HOME/.claude/telegram_thinking_stock" \
            FEISHU_PENDING_FILE="$HOME/.claude/telegram_pending_feishu_stock" \
            FEISHU_CHAT_ID_FILE="$HOME/.claude/feishu_chat_id_stock" \
            FEISHU_MSG_ID_FILE="$HOME/.claude/feishu_reply_message_id_stock" \
            python3 "$PROJECT/feishu_bridge.py" > /tmp/feishu_stock_bridge.log 2>&1 </dev/null &
        FEISHU_STOCK_PID=$!
        echo "$FEISHU_STOCK_PID" > /tmp/feishu_stock_bridge.pid
        disown
        echo "    Feishu stock/red bridge started (log: /tmp/feishu_stock_bridge.log)"
    fi

    # 5.5 Start Proactive Brain (主动大脑：定时用 codex exec / ChatGPT 订阅判断是否主动找用户)
    echo "[6.5] Starting Proactive Brain..."
    pkill -f "proactive.py" 2>/dev/null; sleep 0.3
    nohup python3 "$PROJECT/proactive.py" > /tmp/proactive.out 2>&1 </dev/null &
    disown
    echo "    Proactive brain started (log: /tmp/proactive.log)"

    # 4. Bridge is managed by dashboard (auto-start on dashboard launch)
    echo "[5] Bridge managed by dashboard on port $PORT."
    echo "    Open http://localhost:8888 to control the bridge."
    # Keep session alive.
    # 注意：上面所有后台进程都已 disown 移出作业表，`wait` 会立即返回，
    # 这里用 sleep infinity 让 tmux pane 保持存活（服务已独立运行，与本 pane 解耦）。
    sleep infinity

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
