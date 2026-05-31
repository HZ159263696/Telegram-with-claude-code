#!/usr/bin/env python3
"""SessionStart 钩子 — 把跨模型共享的长期记忆注入 Claude Code 上下文。

每次 Claude Code 启动 / 恢复 / relaunch（含切换模型）都会触发本钩子，
因此无论底层是哪个模型，开口前都先读入同一份记忆 → 切模型记忆一致。
输出 hookSpecificOutput.additionalContext，由 Claude Code 注入会话上下文。
"""
import sys, os, json

# memory_lib.py 与本文件同目录 (~/.claude/hooks)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import memory_lib
except Exception:
    sys.exit(0)   # 记忆模块缺失时静默跳过，绝不阻塞会话


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    cwd        = data.get("cwd", "") or os.getcwd()
    transcript = data.get("transcript_path", "")
    bot = memory_lib.resolve_bot(cwd, transcript)

    ctx = memory_lib.build_context(bot)
    if not ctx:
        sys.exit(0)   # 该 Bot 暂无记忆，不注入

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
