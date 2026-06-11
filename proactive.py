#!/usr/bin/env python3
"""主动大脑 (Proactive Brain) — 让每个 bot 能"自己找用户说话"。

设计要点：
- **每个 bot 独立的大脑/记忆/配置**：main / stock / feishu 各有自己的记忆目录
  (见 hooks/memory_lib.py) 和各自的主动开关与参数；可以只给某些 bot 开主动。
- 后台定时循环。对每个"已开启主动"的 bot，每隔它自己的 interval 分钟判断一次：
  用 headless `claude -p`（**走订阅，不带 API key**）读该 bot 的记忆，判断
  "此时此刻是否值得主动联系用户"。要么 SKIP（默认、克制），要么生成一条消息发出去。
- 与对话模型解耦：无论对话 bot 切到 Claude/DeepSeek/GLM，大脑始终用 Claude 订阅判断。
- 克制优先：静默时段、两次主动最小间隔、每天上限、用户正忙(pending)时跳过。

配置文件 ~/.claude/proactive_config.json 为 **per-bot 结构**：
    {"main": {"enabled": true, "interval_min": 30, ...}, "stock": {...}, "feishu": {...}}

运行：
    python3 proactive.py                     # 后台常驻循环（start.sh 启动）
    python3 proactive.py --once  [--bot main]# 立即跑一次（遵守规则）
    python3 proactive.py --test  [--bot main]# 立即跑一次（忽略 quiet/gap/上限）
    python3 proactive.py --force [--bot main]# 跳过判断，强制发一条（演示/自检）
    python3 proactive.py --optimize [--bot main] # 立即把零散 RECENT 归并进长期记忆
"""
import os
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error

# 绕过系统代理（与 bridge.py 一致，代理会破坏到 Telegram 的 TLS）
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

# 复用已部署的共享记忆层
sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))
try:
    import memory_lib
except Exception:
    memory_lib = None

LOG          = "/tmp/proactive.log"
CONFIG_FILE  = os.path.expanduser("~/.claude/proactive_config.json")
STATE_FILE   = os.path.expanduser("~/.claude/proactive_state.json")
# 大脑的 headless claude -p 在独立 cwd 跑：避免它的 transcript 写进各 bot 交互 session
# 的 projects 目录，从而污染 bridge 轮询找的"最新 transcript"。
BRAIN_WORKDIR = os.path.expanduser("~/.claude/proactive_workdir")
os.makedirs(BRAIN_WORKDIR, exist_ok=True)

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID",     "REDACTED_FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# ── 主动支持的 Bot ────────────────────────────────────────────────────────────
# mem_bot: 记忆目录名(memory_lib)；kind: 发送渠道；chat_id_file/pending_file 同 bridge
BOTS = {
    "main": {
        "mem_bot":      "main",
        "kind":         "telegram",
        "token":        os.environ.get("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        "chat_id_file": os.path.expanduser("~/.claude/telegram_chat_id"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending"),
    },
    "stock": {
        "mem_bot":      "stock",
        "kind":         "telegram",
        "token":        os.environ.get("STOCK_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        "chat_id_file": os.path.expanduser("~/.claude/telegram_chat_id_stock"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending_stock"),
    },
    "feishu": {
        "mem_bot":      "feishu",
        "kind":         "feishu",
        "token":        "feishu",
        "chat_id_file": os.path.expanduser("~/.claude/feishu_chat_id"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending_feishu"),
    },
}

# ── 默认配置 ──────────────────────────────────────────────────────────────────
# 单个 bot 的默认参数（不含 enabled，enabled 由 DEFAULT_ENABLED 按 bot 决定）
DEFAULT_BOT = {
    "interval_min":   30,                # 多久醒来判断一次（分钟）
    "quiet_start":    23,                # 静默时段开始（含），夜里不打扰
    "quiet_end":      8,                 # 静默时段结束（之前），早 8 点前不打扰
    "min_gap_hours":  4,                 # 两次主动联系的最小间隔（小时）
    "max_per_day":    4,                 # 每天主动上限
    "min_idle_hours": 2,                 # 用户至少安静这么久才考虑主动
    "brain_model":    "claude-haiku-4-5-20251001",  # 大脑用的模型（快、省额度）
    "optimize_enabled": True,            # 是否每天把零散 RECENT 归并进长期记忆
    "optimize_model":   "",              # 归并用模型（空=用 brain_model；建议 sonnet 更稳）
}
# 每个 bot 默认是否开启主动：默认只有主控 Bot 开，其它需用户在面板手动开
DEFAULT_ENABLED = {"main": True, "stock": False, "feishu": False}

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


# ── config / state ───────────────────────────────────────────────────────────

def default_config():
    """生成 per-bot 默认配置（供首次生成 / dashboard / start 脚本用）。"""
    cfg = {}
    for name in BOTS:
        b = dict(DEFAULT_BOT)
        b["enabled"] = DEFAULT_ENABLED.get(name, False)
        cfg[name] = b
    return cfg


def load_config():
    """读原始 per-bot 配置（可能不全，由 bot_config 补默认）。"""
    if os.path.exists(CONFIG_FILE):
        try:
            raw = json.load(open(CONFIG_FILE, encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception as e:
            log(f"config 读取失败，用默认: {e}")
    return {}


def bot_config(raw, name):
    """某个 bot 的最终配置 = 默认 + enabled 默认 + 文件覆盖。"""
    b = dict(DEFAULT_BOT)
    b["enabled"] = DEFAULT_ENABLED.get(name, False)
    sub = raw.get(name) if isinstance(raw, dict) else None
    if isinstance(sub, dict):
        b.update(sub)
    return b


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"state 写入失败: {e}")


def bot_state(state, name):
    """取某个 bot 的子状态（last_sent_ts / sent_today / day / last_proactive / last_check_ts）。"""
    return state.setdefault(name, {})


# ── 辅助：最近一次用户活动时间（用 RECENT.md 末条时间戳近似）──────────────────

def last_activity_ts(mem_bot):
    if memory_lib is None:
        return 0
    try:
        entries = memory_lib._recent_entries(mem_bot)
        if not entries:
            return 0
        head = entries[-1].splitlines()[0]          # "### 2026-05-31 14:30"
        stamp = head.lstrip("#").strip()
        return int(time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M")))
    except Exception:
        return 0


def human_gap(seconds):
    if seconds <= 0:
        return "未知"
    h = seconds / 3600
    if h < 1:
        return f"约 {int(seconds // 60)} 分钟"
    if h < 24:
        return f"约 {int(h)} 小时"
    return f"约 {int(h // 24)} 天"


# ── 发送：Telegram / 飞书 ──────────────────────────────────────────────────────

def tg_send(token, chat_id, text):
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                body, {"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            return bool(resp.get("ok"))
        except urllib.error.HTTPError as e:
            log(f"tg sendMessage HTTP {e.code}: {e.read()[:200]}")
            return False
        except Exception as e:
            log(f"tg sendMessage error attempt {attempt+1}: {e}")
            time.sleep(0.8 * (attempt + 1))
    return False


def feishu_send(receive_id, text):
    """飞书主动发消息（非 reply，需 receive_id：chat_id oc_… 或 open_id ou_…）。"""
    try:
        body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            body, {"Content-Type": "application/json"})
        tok = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("tenant_access_token")
        if not tok:
            return False
        rid_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={rid_type}"
        payload = json.dumps({
            "receive_id": receive_id, "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }).encode()
        req2 = urllib.request.Request(
            url, payload,
            {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        resp = json.loads(urllib.request.urlopen(req2, timeout=15).read())
        ok = resp.get("code") == 0
        if not ok:
            log(f"feishu send fail: {resp}")
        return ok
    except Exception as e:
        log(f"feishu send error: {e}")
        return False


def send_to(b, target, text):
    if b.get("kind") == "feishu":
        return feishu_send(target, text)
    return tg_send(b["token"], target, text)


# ── 大脑：让 Claude 订阅判断要不要主动 + 生成消息 ─────────────────────────────

def build_prompt(mem_bot, now, idle_str, sent_today, last_proactive, force=False):
    weekday = WEEKDAYS[now.tm_wday]
    now_str = time.strftime("%Y-%m-%d %H:%M", now)
    mem_ctx = ""
    if memory_lib is not None:
        try:
            mem_ctx = memory_lib.build_context(mem_bot) or ""
        except Exception as e:
            log(f"build_context 失败: {e}")
    if not mem_ctx:
        mem_ctx = "（暂无长期记忆）"

    last_line = last_proactive if last_proactive else "（今天还没主动联系过）"
    situ = f"""【当前情境】
- 现在时间：{now_str}（{weekday}）
- 距离上一次和用户对话：{idle_str}
- 你今天已经主动联系过 {sent_today} 次
- 你上一次主动说的是：{last_line}

{mem_ctx}"""

    if force:
        return f"""你是用户的长期 AI 伙伴，拥有跨会话、跨模型共享的持续记忆（见下）。
现在请你**主动**给用户发一条消息：基于记忆挑一个此刻最合适、最自然的角度
（关心近况 / 跟进进行中的项目或待办 / 一个有用的小提醒 / 结合时间的问候）。

要求：
- 简短，一两句即可。
- 口吻像一个真正了解他的朋友，参考"最近对话"里你和他的说话风格、称呼。
- 用中文，不要署名，不要解释你的理由，不要使用任何工具。

{situ}

直接输出要发给用户的消息正文。"""

    return f"""你是用户的长期 AI 伙伴，此刻运行在后台。你拥有跨会话、跨模型共享的持续记忆（见下）。
现在请你判断一件事：**此时此刻，是否值得主动给用户发一条消息？**

【判断原则】
- 默认克制。绝大多数情况下应当 SKIP，不要打扰用户。
- 只有当你有具体、有价值的理由时才主动，例如：
  · 记忆里的待办/承诺到了该跟进的时刻；
  · 进行中的项目有自然的跟进点或值得分享的想法；
  · 用户很久没联系了，一句真诚的关心或一个有用的小提醒；
  · 结合当前时间的贴心举动（深夜提醒早点休息、清晨问候并带上今天该做的事）。
- 绝不重复你最近已经说过的话。
- 语气自然、简短（一两句即可），像一个真正了解他的朋友，不要客套、不要机械、不要署名。
- 只基于下面给出的信息判断，**不要使用任何工具**。
- 用中文。

{situ}

【输出要求】
- 如果不该打扰：只输出一行 SKIP（不要任何其它字符）。
- 如果该主动联系：直接输出要发给用户的消息正文，不要前缀、不要引号、不要解释你的理由。"""


def ask_brain(bcfg, prompt, model=None, timeout=150):
    """调用 headless claude -p（走订阅）。返回 stdout 文本；失败返回空串。"""
    # 关键：剔除 ANTHROPIC_API_KEY / BASE_URL，强制走订阅而非任何代理/厂商 key
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
    cmd = ["claude", "-p", prompt,
           "--model", model or bcfg.get("brain_model") or DEFAULT_BOT["brain_model"],
           "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=BRAIN_WORKDIR)
    except subprocess.TimeoutExpired:
        log("brain 超时")
        return ""
    except Exception as e:
        log(f"brain 调用失败: {e}")
        return ""
    if r.returncode != 0:
        log(f"brain 退出码 {r.returncode}: {r.stderr[:200]}")
    return (r.stdout or "").strip()


def parse_decision(out):
    """解析大脑输出：返回要发送的消息正文，或 None 表示 SKIP。"""
    if not out:
        return None
    s = out.strip().strip("`").strip()
    head = s.splitlines()[0].strip().upper().rstrip(".。!！")
    if head == "SKIP" or s.upper() == "SKIP":
        return None
    if len(s) > 1500:
        s = s[:1500] + "…"
    return s


# ── 单个 bot 的一轮判断 ───────────────────────────────────────────────────────

def tick_bot(bcfg, state, name, ignore_limits=False, force=False, respect_interval=False):
    b = BOTS.get(name)
    if not b:
        log(f"未知 bot: {name}")
        return
    if force:
        ignore_limits = True
    mem_bot = b["mem_bot"]
    now = time.localtime()
    bs = bot_state(state, name)
    today = time.strftime("%Y-%m-%d", now)

    # 跨天重置当日计数
    if bs.get("day") != today:
        bs["day"] = today
        bs["sent_today"] = 0

    # 0. 循环模式下：按该 bot 自己的 interval 控制判断频率
    if respect_interval and not ignore_limits:
        if time.time() - bs.get("last_check_ts", 0) < bcfg["interval_min"] * 60:
            return
        bs["last_check_ts"] = int(time.time())

    # 1. 必须有 chat_id（用户至少聊过一次才主动）
    if not os.path.exists(b["chat_id_file"]):
        log(f"[{name}] 无 chat_id，跳过")
        return
    target = open(b["chat_id_file"]).read().strip()
    if not target:
        log(f"[{name}] chat_id 为空，跳过")
        return

    # 2. 用户正忙（pending 存在 = 正在等回复）→ 不插话
    if os.path.exists(b["pending_file"]):
        log(f"[{name}] 用户正忙(pending)，跳过")
        return

    if not ignore_limits:
        h = now.tm_hour
        qs, qe = bcfg["quiet_start"], bcfg["quiet_end"]
        quiet = (h >= qs or h < qe) if qs > qe else (qs <= h < qe)
        if quiet:
            log(f"[{name}] 静默时段({qs}-{qe})，跳过")
            return
        if bs.get("sent_today", 0) >= bcfg["max_per_day"]:
            log(f"[{name}] 今日已达上限 {bcfg['max_per_day']}，跳过")
            return
        gap = time.time() - bs.get("last_sent_ts", 0)
        if gap < bcfg["min_gap_hours"] * 3600:
            log(f"[{name}] 距上次主动 {human_gap(gap)} < {bcfg['min_gap_hours']}h，跳过")
            return
        act = last_activity_ts(mem_bot)
        idle = time.time() - act if act else 10 ** 9
        if idle < bcfg["min_idle_hours"] * 3600:
            log(f"[{name}] 用户 {human_gap(idle)} 前刚活跃 < {bcfg['min_idle_hours']}h，跳过")
            return

    # 组装情境
    act = last_activity_ts(mem_bot)
    idle_str = human_gap(time.time() - act) if act else "未知（还没有对话记录）"
    prompt = build_prompt(mem_bot, now, idle_str,
                          bs.get("sent_today", 0), bs.get("last_proactive", ""),
                          force=force)

    log(f"[{name}] 询问大脑（model={bcfg['brain_model']}{'，force' if force else ''}）…")
    out = ask_brain(bcfg, prompt)
    if force:
        msg = (out or "").strip().strip("`").strip()
        if len(msg) > 1500:
            msg = msg[:1500] + "…"
        msg = msg or None
    else:
        msg = parse_decision(out)
    if msg is None:
        log(f"[{name}] 大脑决定 SKIP")
        return

    log(f"[{name}] 大脑决定主动: {msg[:60]}…")
    if send_to(b, target, msg):
        bs["last_sent_ts"] = int(time.time())
        bs["sent_today"] = bs.get("sent_today", 0) + 1
        bs["last_proactive"] = msg[:200]
        save_state(state)
        if memory_lib is not None:
            try:
                memory_lib.append_recent(mem_bot, "（我主动联系了用户）", msg)
            except Exception as e:
                log(f"记忆写入失败: {e}")
        log(f"[{name}] 已发送主动消息 ✓")
    else:
        log(f"[{name}] 发送失败")


# ── 记忆归并 Optimizer：把零散的旧 RECENT 提炼进长期记忆 ────────────────────

def build_optimize_prompt(old, cur_mem, cur_proj, cur_pend):
    return f"""你是用户的长期 AI 伙伴的"记忆整理员"。下面是用户较早的一批对话记录，
以及你当前的三份长期记忆文件。请把这批旧对话里**值得长期保留**的信息，增量融入记忆。

【三份文件各自的职责】
- MEMORY.md：关于用户的稳定事实、偏好、长期目标。
- PROJECTS.md：进行中的项目及其最新进展。
- PENDING.md：待办与承诺（格式：- [ ] (时间) 事项；已完成的删除或标记 [x]）。

【原则】
- 增量更新：保留原有仍然有效的内容，只把旧对话里的**新**信息融进去；绝不凭空编造。
- 去重精炼：同类信息合并成一条，丢弃琐碎的一次性闲聊。
- 保持各文件原有的标题结构与中文风格。
- 不要使用任何工具。

【较早的对话记录】
{old}

【当前 MEMORY.md】
{cur_mem or '（空）'}

【当前 PROJECTS.md】
{cur_proj or '（空）'}

【当前 PENDING.md】
{cur_pend or '（空）'}

【输出格式】严格按下面三段输出，每段给出该文件**更新后的完整内容**，不要任何额外解释：
===MEMORY===
<MEMORY.md 完整内容>
===PROJECTS===
<PROJECTS.md 完整内容>
===PENDING===
<PENDING.md 完整内容>"""


def parse_optimize(out):
    """按 ===MEMORY===/===PROJECTS===/===PENDING=== 切分大脑输出。"""
    if not out:
        return None
    import re
    parts = re.split(r"===\s*(MEMORY|PROJECTS|PENDING)\s*===", out)
    if len(parts) < 3:
        return None
    name_map = {"MEMORY": "MEMORY.md", "PROJECTS": "PROJECTS.md", "PENDING": "PENDING.md"}
    res = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].strip().upper()
        if key in name_map:
            res[name_map[key]] = parts[i + 1].strip()
    return res or None


def optimize_bot(bcfg, name):
    b = BOTS.get(name)
    if not b or memory_lib is None:
        return
    mem_bot = b["mem_bot"]
    keep = memory_lib.RECENT_INJECT_TAIL
    old, _ = memory_lib.split_recent(mem_bot, keep)
    n = memory_lib.recent_count(mem_bot)
    if not old.strip() or n <= keep + 2:
        log(f"[{name}] RECENT 仅 {n} 条，无需归并")
        return
    prompt = build_optimize_prompt(
        old,
        memory_lib.read_doc(mem_bot, "MEMORY.md"),
        memory_lib.read_doc(mem_bot, "PROJECTS.md"),
        memory_lib.read_doc(mem_bot, "PENDING.md"))
    model = bcfg.get("optimize_model") or bcfg["brain_model"]
    log(f"[{name}] 归并 {n - keep} 条旧对话（model={model}）…")
    out = ask_brain(bcfg, prompt, model=model, timeout=240)
    parsed = parse_optimize(out)
    if not parsed:
        log(f"[{name}] 归并输出解析失败，保留原状")
        return
    for fn, content in parsed.items():
        if content.strip():
            memory_lib.write_doc(mem_bot, fn, content)
    memory_lib.trim_recent_to_tail(mem_bot, keep)
    log(f"[{name}] 归并完成，RECENT 修剪到 {keep} 条 ✓")


def maybe_optimize(raw):
    """每天最多归并一次（跨天触发），逐个 bot 按各自的 optimize_enabled。"""
    state = load_state()
    today = time.strftime("%Y-%m-%d")
    if state.get("last_optimize_day") == today:
        return
    for name in BOTS:
        bcfg = bot_config(raw, name)
        if not bcfg.get("optimize_enabled", True):
            continue
        try:
            optimize_bot(bcfg, name)
        except Exception as e:
            log(f"[{name}] optimize 异常: {e}")
    state = load_state()
    state["last_optimize_day"] = today
    save_state(state)


# ── 调度 ──────────────────────────────────────────────────────────────────────

def tick(ignore_limits=False, force=False, only_bot=None, respect_interval=False):
    raw = load_config()
    state = load_state()
    names = [only_bot] if only_bot else list(BOTS)
    for name in names:
        if name not in BOTS:
            log(f"未知 bot: {name}")
            continue
        bcfg = bot_config(raw, name)
        # 手动指定单 bot（only_bot）时无视 enabled；循环模式只跑已开启的
        if not force and not ignore_limits and not only_bot and not bcfg.get("enabled"):
            continue
        try:
            tick_bot(bcfg, state, name, ignore_limits=ignore_limits,
                     force=force, respect_interval=respect_interval)
        except Exception as e:
            log(f"[{name}] tick 异常: {e}")
    save_state(state)


def main_loop():
    log("主动大脑启动（per-bot 配置）")
    while True:
        raw = load_config()
        # 睡眠周期 = 所有已开启 bot 中最小的 interval（无则 30 分钟兜底）
        intervals = [bot_config(raw, n)["interval_min"] for n in BOTS
                     if bot_config(raw, n).get("enabled")]
        base = min(intervals) if intervals else 30
        time.sleep(max(60, base * 60))
        try:
            tick(respect_interval=True)
        except Exception as e:
            log(f"loop 异常: {e}")
        try:
            maybe_optimize(raw)        # 每天归并一次零散记忆
        except Exception as e:
            log(f"optimize 异常: {e}")


def _arg_bot():
    if "--bot" in sys.argv:
        i = sys.argv.index("--bot")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    only = _arg_bot()
    if "--optimize" in sys.argv:
        log(f"=== --optimize{' bot='+only if only else ''}：归并 RECENT 进长期记忆 ===")
        raw = load_config()
        for nm in ([only] if only else list(BOTS)):
            if nm in BOTS:
                optimize_bot(bot_config(raw, nm), nm)
    elif "--force" in sys.argv:
        log(f"=== --force{' bot='+only if only else ''}：跳过判断，强制发一条 ===")
        tick(force=True, only_bot=only)
    elif "--test" in sys.argv:
        log(f"=== --test{' bot='+only if only else ''}：忽略 quiet/gap/上限，跑一次 ===")
        tick(ignore_limits=True, only_bot=only)
    elif "--once" in sys.argv:
        log(f"=== --once{' bot='+only if only else ''}：遵守规则，跑一次 ===")
        tick(ignore_limits=False, only_bot=only)
    else:
        main_loop()
