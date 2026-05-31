#!/bin/bash
# 生成默认配置（若不存在）+ 启动/重启主动大脑常驻进程
PROJECT=/mnt/d/AI/claudecode-telegram-main
CFG=$HOME/.claude/proactive_config.json

cd "$PROJECT" || exit 1

python3 - <<'PY'
import json, os, sys
sys.path.insert(0, "/mnt/d/AI/claudecode-telegram-main")
from proactive import default_config, BOTS
p = os.path.expanduser("~/.claude/proactive_config.json")
raw = {}
if os.path.exists(p):
    try:
        raw = json.load(open(p, encoding="utf-8"))
    except Exception:
        raw = {}
# per-bot 格式判定：顶层 key 是否为 bot 名（main/stock/feishu）
is_perbot = isinstance(raw, dict) and any(k in BOTS for k in raw)
if not is_perbot:
    if os.path.exists(p):
        os.replace(p, p + ".bak")
    json.dump(default_config(), open(p, "w"), ensure_ascii=False, indent=2)
    print("已生成/迁移到 per-bot 配置" + ("（旧文件备份为 .bak）" if raw else ""))
else:
    print("已是 per-bot 配置，保留不动")
PY
echo "--- config ---"
cat "$CFG"
echo

pkill -f "proactive.py" 2>/dev/null; sleep 0.5
nohup python3 "$PROJECT/proactive.py" > /tmp/proactive.out 2>&1 &
sleep 1
echo "--- 常驻进程 ---"
pgrep -af "proactive.py" || echo "(未启动，检查 /tmp/proactive.out)"
