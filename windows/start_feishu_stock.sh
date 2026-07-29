#!/bin/bash
# 单独启动/重启飞书太子（股票红 Bot），不影响 Telegram、Dashboard 和普通飞书 Bot。
set -e

source /etc/claude_env.sh 2>/dev/null || true
source "$HOME/.profile" 2>/dev/null || true

PROJECT="/mnt/d/AI/claudecode-telegram-main"
CONFIG="$PROJECT/.env.feishu_stock"
WORK_DIR="/mnt/d/AI/feishu_stock_workspace"
SESSION="claude_feishu_stock"
PID_FILE="/tmp/feishu_stock_bridge.pid"
LOG_FILE="/tmp/feishu_stock_bridge.log"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: 缺少本地凭据文件 $CONFIG"
    exit 1
fi

if ! python3 -c 'import lark_oapi' 2>/dev/null; then
    echo "ERROR: Python 环境缺少 lark_oapi，请先安装飞书 SDK。"
    exit 1
fi

# Stop/SessionStart Hook 负责回复路由和 stock 共享记忆；部署不会覆盖已有记忆。
bash "$PROJECT/windows/deploy_hook.sh"
cp "$PROJECT/hooks/feishu_send_file.py" "$HOME/.claude/hooks/feishu_send_file.py"

MODEL_FILE="$HOME/.claude/telegram_model_stock"
MODEL="claude-sonnet-5"
if [ -f "$MODEL_FILE" ]; then
    MODEL=$(tr -d '[:space:]' < "$MODEL_FILE")
    [ -z "$MODEL" ] && MODEL="claude-sonnet-5"
fi

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

if [[ "$MODEL" == "codex-subscription" || "$MODEL" == gpt-5.6-* ]]; then
    LAUNCH_CMD='echo "Codex subscription mode: bridge runs codex exec per message"; exec bash'
elif [[ "$MODEL" == claude-* ]]; then
    LAUNCH_CMD="claude --dangerously-skip-permissions --model $MODEL"
else
    CLI_MODEL=$(proxy_cli_model "$MODEL")
    LAUNCH_CMD="env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u all_proxy NO_PROXY='*' no_proxy='*' ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL=http://localhost:4001 claude --dangerously-skip-permissions --model $CLI_MODEL"
fi

mkdir -p "$WORK_DIR"
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -c "$WORK_DIR" \
    "AKASHIC_MEM=stock FEISHU_CONFIG_FILE='$CONFIG' FEISHU_CHAT_ID_FILE='$HOME/.claude/feishu_chat_id_stock' $LAUNCH_CMD"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(tr -cd '0-9' < "$PID_FILE")
    if [ -n "$OLD_PID" ]; then
        kill "$OLD_PID" 2>/dev/null || true
    fi
fi

nohup env FEISHU_CONFIG_FILE="$CONFIG" \
    FEISHU_BOT_KEY=feishu_stock FEISHU_MEMORY_BOT=stock \
    FEISHU_TMUX_SESSION="$SESSION" FEISHU_WORK_DIR="$WORK_DIR" \
    FEISHU_MODEL_FILE="$MODEL_FILE" \
    FEISHU_THINKING_FILE="$HOME/.claude/telegram_thinking_stock" \
    FEISHU_PENDING_FILE="$HOME/.claude/telegram_pending_feishu_stock" \
    FEISHU_CHAT_ID_FILE="$HOME/.claude/feishu_chat_id_stock" \
    FEISHU_MSG_ID_FILE="$HOME/.claude/feishu_reply_message_id_stock" \
    python3 "$PROJECT/feishu_bridge.py" > "$LOG_FILE" 2>&1 </dev/null &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
disown

sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "✅ 飞书太子股票 Bot 已启动（PID=$NEW_PID，model=$MODEL）"
    echo "日志：$LOG_FILE"
    echo "现在可在飞书私聊中直接发送 /ping 或 查看持仓，无需 @机器人。"
else
    echo "❌ 飞书太子股票 Bot 启动失败，请查看 $LOG_FILE"
    exit 1
fi
