#!/usr/bin/env python3
"""共享记忆层 (memory layer) — 跨模型、按 Bot 隔离的长期记忆。

设计要点：
- 记忆存放在文件里 (~/.claude/memory/<bot>/)，与具体模型无关。
  无论切到 Claude / DeepSeek / GLM …，启动的都是同一个 `claude` CLI，
  SessionStart 钩子会把同一份记忆注入 → 切模型记忆一致。
- 按 Bot 隔离 (main / stock / feishu)，路由依据工作目录 cwd；
  env AKASHIC_MEM 可强制指定，优先级最高。
- 被 load-memory.py(读/注入) 和 send-to-telegram.py(写/更新) 共用。
"""
import os, time, re

MEM_ROOT = os.path.expanduser("~/.claude/memory")

# RECENT.md 滚动窗口：磁盘最多保留多少条；注入上下文时只取最近多少条
RECENT_MAX_ENTRIES = 20      # disk retention
RECENT_INJECT_TAIL = 8       # how many recent entries to inject per session


def resolve_bot(cwd="", transcript_path=""):
    """根据工作目录 / transcript 路径判定属于哪个 Bot 的记忆。
    env AKASHIC_MEM 优先（精确覆盖）。"""
    env = os.environ.get("AKASHIC_MEM")
    if env:
        return env.strip()
    hay = f"{cwd} {transcript_path}".lower()
    # cao_stock → 路径转写后可能是 cao-stock，两种都匹配
    if "cao_stock" in hay or "cao-stock" in hay:
        return "stock"
    if "feishu" in hay:
        return "feishu"
    return "main"


def mem_dir(bot):
    d = os.path.join(MEM_ROOT, bot)
    os.makedirs(d, exist_ok=True)
    return d


def _read(bot, name):
    p = os.path.join(mem_dir(bot), name)
    if os.path.exists(p):
        try:
            return open(p, encoding="utf-8").read()
        except Exception:
            return ""
    return ""


def _clip(s, n):
    """压成单行并截断，避免记忆文件被长文撑爆。"""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def _recent_entries(bot):
    """把 RECENT.md 拆成 entry 列表（每条以 '### ' 开头）。"""
    raw = _read(bot, "RECENT.md")
    parts = re.split(r'(?=^### )', raw, flags=re.M)
    return [p for p in parts if p.strip()]


def build_context(bot):
    """组装注入给 Claude 的记忆上下文；无任何记忆时返回空串。"""
    mem      = _read(bot, "MEMORY.md").strip()
    projects = _read(bot, "PROJECTS.md").strip()
    pending  = _read(bot, "PENDING.md").strip()
    recent   = "".join(_recent_entries(bot)[-RECENT_INJECT_TAIL:]).strip()

    parts = []
    if mem:      parts.append("## 关于用户（长期记忆）\n" + mem)
    if projects: parts.append("## 进行中的项目\n" + projects)
    if pending:  parts.append("## 待办与承诺\n" + pending)
    if recent:   parts.append("## 最近对话摘要\n" + recent)
    if not parts:
        return ""

    header = ("【记忆】以下是你与该用户长期积累的记忆，跨模型共享且持续更新。"
              "回答时自然地利用这些信息，不要机械复述，也不要因为切换了模型就遗忘。\n\n")
    return header + "\n\n".join(parts)


def append_recent(bot, user_text, assistant_text):
    """把一轮对话压缩成一条摘要，追加进 RECENT.md（滚动窗口）。"""
    ts = time.strftime("%Y-%m-%d %H:%M")
    u = _clip(user_text, 300)
    a = _clip(assistant_text, 500)
    if not (u or a):
        return
    entry = f"### {ts}\n- 用户: {u}\n- 我: {a}\n\n"

    entries = _recent_entries(bot)
    entries.append(entry)
    entries = entries[-RECENT_MAX_ENTRIES:]

    p = os.path.join(mem_dir(bot), "RECENT.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("".join(entries))


# ── 归并 Optimizer 用的读写 helper ───────────────────────────────────────────

def read_doc(bot, name):
    """读某个记忆文件原文（MEMORY.md / PROJECTS.md / PENDING.md / RECENT.md）。"""
    return _read(bot, name)


def write_doc(bot, name, content):
    """整文件覆盖写某个记忆文件（归并 Optimizer 用）。"""
    p = os.path.join(mem_dir(bot), name)
    with open(p, "w", encoding="utf-8") as f:
        f.write((content or "").rstrip() + "\n")


def recent_count(bot):
    return len(_recent_entries(bot))


def split_recent(bot, keep=None):
    """把 RECENT 拆成 (待归并的旧条目文本, 保留的最近条目数)。
    旧条目 = 注入窗口之外的那些；保留最近 keep 条不动。"""
    keep = keep or RECENT_INJECT_TAIL
    entries = _recent_entries(bot)
    if len(entries) <= keep:
        return "", len(entries)
    return "".join(entries[:-keep]), keep


def trim_recent_to_tail(bot, keep=None):
    """归并完成后，把 RECENT 修剪到只剩最近 keep 条。"""
    keep = keep or RECENT_INJECT_TAIL
    entries = _recent_entries(bot)[-keep:]
    p = os.path.join(mem_dir(bot), "RECENT.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("".join(entries))
