#!/bin/bash
# 杀掉现有 dashboard，按当前环境变量重启
source /etc/claude_env.sh 2>/dev/null
pkill -f "python3 .*dashboard.py" 2>/dev/null
sleep 1
nohup python3 /mnt/d/AI/claudecode-telegram-main/dashboard.py > /tmp/dashboard.log 2>&1 &
disown
echo "Dashboard restarted PID $!"
sleep 2
echo "Log:"
tail -10 /tmp/dashboard.log
