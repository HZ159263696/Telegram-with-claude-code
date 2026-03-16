#!/bin/bash
# Live test: send a message to Claude and watch hook behavior
source /etc/claude_env.sh

echo "=== Creating pending file ==="
echo $(date +%s) > ~/.claude/telegram_pending
ls -la ~/.claude/telegram_pending

echo "=== Sending 'hello' to Claude via tmux ==="
tmux send-keys -t claude -l "hello"
tmux send-keys -t claude Enter

echo "=== Waiting 10s for Claude to respond ==="
sleep 10

echo "=== Claude screen ==="
tmux capture-pane -t claude -p | tail -5

echo "=== Pending file after response ==="
ls -la ~/.claude/telegram_pending 2>&1

echo "=== Check if hook was called (add debug log) ==="
cat /tmp/hook_debug.log 2>/dev/null || echo "no hook debug log"
