#!/bin/bash
# 部署 Stop / SessionStart 钩子 + 记忆层到 ~/.claude
# 幂等：可重复运行；不会覆盖已积累的记忆文件，不会清掉 settings.json 已有键。
set -e
PROJECT="/mnt/d/AI/claudecode-telegram-main"
HOOKS="$HOME/.claude/hooks"
MEM="$HOME/.claude/memory"
mkdir -p "$HOOKS"

# ── 1) 复制钩子脚本 + 共享记忆库 ──────────────────────────────────────────────
cp "$PROJECT/hooks/send-to-telegram.py" "$HOOKS/send-to-telegram.py"
cp "$PROJECT/hooks/load-memory.py"      "$HOOKS/load-memory.py"
cp "$PROJECT/hooks/memory_lib.py"       "$HOOKS/memory_lib.py"
chmod +x "$HOOKS"/*.py
echo "[1] hooks copied to $HOOKS"

# ── 2) 初始化各 Bot 记忆目录（仅当文件缺失时写种子，避免覆盖已积累记忆）────────
for bot in main stock feishu; do mkdir -p "$MEM/$bot"; done

[ -f "$MEM/main/MEMORY.md" ] || cat > "$MEM/main/MEMORY.md" <<'EOF'
# 长期记忆 · 主控Bot

> 关于用户的稳定事实、偏好、长期目标。每次会话注入，跨模型共享。

- 偏好：回复保持简短，使用中文（代码注释可英文，附中文解释）。
EOF

[ -f "$MEM/main/PROJECTS.md" ] || cat > "$MEM/main/PROJECTS.md" <<'EOF'
# 进行中的项目 · 主控Bot

## Telegram ↔ Claude Code 桥接系统
- 路径：/mnt/d/AI/claudecode-telegram-main
- 简介：通过 Telegram 远程操控 Claude Code；多模型切换、微信/飞书转发、股票监控、Web 控制面板。
- 核心：bridge.py（webhook→tmux 注入）、hooks/send-to-telegram.py（Stop 钩子回传）、dashboard.py、anthropic_proxy.py。
EOF

[ -f "$MEM/main/PENDING.md" ] || cat > "$MEM/main/PENDING.md" <<'EOF'
# 待办与承诺 · 主控Bot

> 格式：- [ ] (截止/提醒时间) 事项
EOF

[ -f "$MEM/main/RECENT.md" ] || : > "$MEM/main/RECENT.md"

[ -f "$MEM/stock/PROJECTS.md" ] || cat > "$MEM/stock/PROJECTS.md" <<'EOF'
# 进行中的项目 · 股票Bot

## 股票监控工作区
- 路径：/mnt/d/cao_stock
- 简介：自选股扫描、价格监控、交易信号、同花顺公式工具。
EOF
[ -f "$MEM/stock/RECENT.md" ] || : > "$MEM/stock/RECENT.md"
echo "[2] memory dirs seeded under $MEM"

# ── 3) 合并 settings.json（保留已有键，补齐 Stop + SessionStart）──────────────
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
s = {}
if os.path.exists(p):
    try: s = json.load(open(p))
    except Exception: s = {}
hooks = s.setdefault("hooks", {})
def ensure(event, script):
    arr = hooks.setdefault(event, [])
    for grp in arr:
        for h in grp.get("hooks", []):
            if h.get("command", "").endswith(script):
                return
    cmd = "python3 " + os.path.expanduser("~/.claude/hooks/" + script)
    arr.append({"matcher": "", "hooks": [{"type": "command", "command": cmd}]})
ensure("Stop", "send-to-telegram.py")
ensure("SessionStart", "load-memory.py")
json.dump(s, open(p, "w"), ensure_ascii=False, indent=2)
print("[3] settings.json merged:", p)
PY

echo "Done. 钩子与记忆层已部署。"
