#!/usr/bin/env python3
"""Claude Bridge Dashboard — Visual control console at http://localhost:9999"""

import os
import sys
import json
import re
import time
import queue
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from pathlib import Path

DASHBOARD_PORT   = int(os.environ.get("DASHBOARD_PORT", "8888"))
BRIDGE_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.py")
MODEL_FILE       = os.path.expanduser("~/.claude/telegram_model")
TOKEN_STATS_FILE = os.path.expanduser("~/.claude/telegram_token_stats.json")
PENDING_FILE     = os.path.expanduser("~/.claude/telegram_pending")
API_KEYS_FILE    = os.path.expanduser("~/.claude/telegram_api_keys.json")
LTLOG            = "/tmp/lt_bridge.log"
CHAT_ID_FILE     = os.path.expanduser("~/.claude/telegram_chat_id")
TMUX_SESSION     = os.environ.get("TMUX_SESSION", "claude")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
ANTHROPIC_PROXY_URL = "http://localhost:4001"

# ── 多Bot配置 ──────────────────────────────────────────────────────────────────
# 与 bridge.py 的 BOT_PROFILES 保持一致
BOTS = {
    "main": {
        "name":         "主控Bot",
        "tmux_session": "claude",
        "model_file":   os.path.expanduser("~/.claude/telegram_model"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending"),
        "chat_id_file":os.path.expanduser("~/.claude/telegram_chat_id"),
        "work_dir":     None,
        "default_model":"claude-opus-4-7",
        "bridge_managed": True,   # 走 bridge.py，接收全局 bridge 日志
    },
    "stock": {
        "name":         "股票Bot",
        "tmux_session": "claude_stock",
        "model_file":   os.path.expanduser("~/.claude/telegram_model_stock"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending_stock"),
        "chat_id_file":os.path.expanduser("~/.claude/telegram_chat_id_stock"),
        "work_dir":     "/mnt/d/cao_stock",
        "default_model":"claude-sonnet-4-6",
        "bridge_managed": True,
    },
    "feishu": {
        "name":         "飞书Bot",
        "tmux_session": "claude_feishu",
        "model_file":   os.path.expanduser("~/.claude/telegram_model_feishu"),
        "pending_file": os.path.expanduser("~/.claude/telegram_pending_feishu"),
        "chat_id_file":os.path.expanduser("~/.claude/feishu_chat_id"),
        "work_dir":     "/mnt/d/AI/feishu_workspace",
        "default_model":"claude-sonnet-4-6",
        "bridge_managed": False,  # 独立进程 feishu_bridge.py，不接收 telegram bridge 全局日志
    },
}


def _bot_or_default(key):
    return BOTS.get(key, BOTS["main"])

# Claude model provider detection
_CLAUDE_MODELS = {"claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"}
# Non-Claude models need CLI alias for LiteLLM routing
_CLI_MODEL_ALIAS = {
    "deepseek-v4-flash": "claude-3-5-sonnet-20241022",
    "deepseek-v4-pro":   "claude-3-opus-20240229",
    "glm-4-plus":        "claude-3-sonnet-20240229",
    "glm-4-flash":       "claude-3-haiku-20240307",
    "abab6.5s-chat":     "claude-3-5-haiku-20241022",
    "qwen-max":          "claude-3-5-sonnet-latest",
    "qwen-plus":         "claude-3-opus-latest",
    "qwen-turbo":        "claude-3-haiku-20240307",
}

# 厂商原生 Anthropic 端点（直连，跳过本地代理）
_NATIVE_ANTHROPIC_BASE = {
    "deepseek-v4-flash": "https://api.deepseek.com/anthropic",
    "deepseek-v4-pro":   "https://api.deepseek.com/anthropic",
}


def _get_provider_key(model):
    """从 telegram_api_keys.json 读取该模型对应厂商的 API key"""
    if not os.path.exists(API_KEYS_FILE):
        return ""
    try:
        keys = json.load(open(API_KEYS_FILE))
    except Exception:
        return ""
    if model.startswith("deepseek"):
        return keys.get("deepseek", "")
    if model.startswith("glm"):
        return keys.get("zhipu", "")
    if model.startswith("abab"):
        return keys.get("minimax", "")
    if model.startswith("qwen"):
        return keys.get("bailian", "")
    return ""

CLAUDE_JSON_PATH = os.path.expanduser("~/.claude.json")


def _approve_custom_key(api_key):
    """预写 ~/.claude.json 让 Claude CLI 不再弹自定义 key 确认提示。"""
    if not api_key or len(api_key) < 20:
        return
    suffix = api_key[-20:]
    try:
        cfg = json.load(open(CLAUDE_JSON_PATH))
    except Exception:
        return
    resp = cfg.setdefault("customApiKeyResponses", {})
    approved = resp.setdefault("approved", [])
    rejected = resp.setdefault("rejected", [])
    changed = False
    if suffix in rejected:
        rejected.remove(suffix)
        changed = True
    if suffix not in approved:
        approved.append(suffix)
        changed = True
    if changed:
        try:
            with open(CLAUDE_JSON_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def _tmux_session_exists(session):
    """Exact-match has-session check (避免 tmux 前缀匹配把 'claude' 误判到 'claude_stock')。"""
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return session in r.stdout.split()


def _relaunch_claude(model, bot_key="main"):
    """Relaunch Claude Code in the bot's tmux session with the correct model/provider."""
    profile = _bot_or_default(bot_key)
    sess     = profile["tmux_session"]
    work_dir = profile.get("work_dir")
    name     = profile["name"]

    def _do():
        if _tmux_session_exists(sess):
            # Exit current Claude Code instance
            subprocess.run(["tmux", "send-keys", "-t", sess, "Escape", ""])
            time.sleep(0.2)
            subprocess.run(["tmux", "send-keys", "-t", sess, "/exit", "Enter"])
            time.sleep(1.5)  # Wait for exit + possible session death

        # Re-check: session may have died after /exit (tmux kills session when initial command exits)
        if not _tmux_session_exists(sess):
            new_args = ["tmux", "new-session", "-d", "-s", sess]
            if work_dir:
                new_args += ["-c", work_dir]
            subprocess.run(new_args, capture_output=True)
            time.sleep(0.5)

        # Start with correct model
        if model in _CLAUDE_MODELS:
            cmd = f"claude --dangerously-skip-permissions --model {model}"
        elif model in _NATIVE_ANTHROPIC_BASE and _get_provider_key(model):
            # 直连厂商原生 Anthropic 端点（如 DeepSeek V4）
            base = _NATIVE_ANTHROPIC_BASE[model]
            key = _get_provider_key(model)
            _approve_custom_key(key)
            cmd = f"ANTHROPIC_API_KEY={key} ANTHROPIC_BASE_URL={base} claude --dangerously-skip-permissions --model {model}"
        else:
            _approve_custom_key("sk-placeholder")
            cli_model = _CLI_MODEL_ALIAS.get(model, model)
            cmd = f"ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL={ANTHROPIC_PROXY_URL} claude --dangerously-skip-permissions --model {cli_model}"
        subprocess.run(["tmux", "send-keys", "-t", sess, cmd, "Enter"])
        _log(f"[{name}] Claude Code relaunched with model: {model}", bot_key=bot_key)
    threading.Thread(target=_do, daemon=True).start()

# ── Shared state ──────────────────────────────────────────────────────────────
_bridge_proc       = None
_bridge_start_time = None
# 每个 Bot 独立的缓冲、序号、SSE 队列
_log_buffers = {k: [] for k in BOTS}    # bot_key -> list of (seq, text)
_log_seqs    = {k: 0  for k in BOTS}
_log_lock    = threading.Lock()
_sse_queues  = {k: [] for k in BOTS}    # bot_key -> list of queue.Queue
_sse_lock    = threading.Lock()
MAX_LOG_LINES = 500


def _push_log(bot_key, text):
    """把一条已加好时间戳/前缀的日志写入指定 Bot 的缓冲和 SSE。"""
    if bot_key not in BOTS:
        return
    with _log_lock:
        _log_seqs[bot_key] += 1
        _log_buffers[bot_key].append((_log_seqs[bot_key], text))
        if len(_log_buffers[bot_key]) > MAX_LOG_LINES:
            _log_buffers[bot_key].pop(0)
    with _sse_lock:
        for q in list(_sse_queues[bot_key]):
            try:
                q.put_nowait(text)
            except queue.Full:
                pass


def _log(line, bot_key=None):
    """加时间戳的普通日志。bot_key=None 时广播到所有 bridge 托管的 Bot（telegram bridge 全局日志，
    独立进程的 Bot 如飞书不接收，保持日志相互独立）。"""
    ts   = time.strftime("%H:%M:%S")
    text = f"[{ts}] {line.rstrip()}"
    if bot_key:
        targets = [bot_key]
    else:
        targets = [k for k, v in BOTS.items() if v.get("bridge_managed", True)]
    for k in targets:
        _push_log(k, text)


def _read_bridge_output(proc):
    """Bridge 子进程的 stdout 同时给两个 Bot 看 — 因为 webhook 路由日志、错误等都是全局的。
    但单条 `[主控Bot][...]` / `[股票Bot][...]` 行会被路由到对应 Bot。"""
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        # bridge.py 打印 "[主控Bot][chat_id] ..." 这种行，根据前缀路由
        target = None
        if line.startswith("[主控Bot]"):
            target = "main"
        elif line.startswith("[股票Bot]"):
            target = "stock"
        _log(line, bot_key=target)


# ── tmux capture for Claude Code live output ──────────────────────────────────
_tmux_capture_running = False
# 每个 Bot 一份 prev 状态，用于 diff 出新增行
_tmux_states = {k: {"prev_snapshot": "", "prev_norm_lines": []} for k in BOTS}

def _normalize_tmux(line):
    """Strip all ANSI/OSC/control sequences and whitespace for reliable comparison."""
    import re
    # CSI sequences: \x1b[ ... letter
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line)
    # OSC sequences: \x1b] ... (terminated by BEL or ST)
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", s)
    # Other escape sequences: \x1b followed by one char
    s = re.sub(r"\x1b.", "", s)
    # Remaining control characters except newline/tab
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    # Strip terminal block/cursor glyphs that vary per-render 去除光标块等重绘差异
    s = re.sub(r"[\u2588\u2590\u258c▂▃▄▅▆▇]", "", s)
    return s.strip()

def _dedup_key(line):
    """Aggressive key for dedup: collapse whitespace so trivial render diffs match.
    用于去重的规范化 key：折叠所有空白，避免空格差异造成漏判。"""
    import re
    return re.sub(r"\s+", " ", line).strip()

def _capture_one_bot(bot_key):
    """单次捕获指定 Bot tmux pane 的新增行，写入该 Bot 的日志缓冲。"""
    profile = _bot_or_default(bot_key)
    sess    = profile["tmux_session"]
    state   = _tmux_states[bot_key]
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", sess, "-p", "-S", "-50"],
            capture_output=True, timeout=3
        )
    except Exception:
        return
    if result.returncode != 0:
        return
    raw_lines  = result.stdout.decode(errors="replace").splitlines()
    norm_lines = [_normalize_tmux(l) for l in raw_lines]
    while norm_lines and not norm_lines[-1]:
        norm_lines.pop()
    snapshot = "\n".join(norm_lines)
    if snapshot == state["prev_snapshot"]:
        state["prev_norm_lines"] = norm_lines
        state["prev_snapshot"]   = snapshot
        return
    prev = state["prev_norm_lines"]
    new_start = 0
    if prev:
        for i in range(len(prev), 0, -1):
            if norm_lines[:i] == prev[-i:]:
                new_start = i
                break
    for line in norm_lines[new_start:]:
        if line:
            _log_claude(line, bot_key=bot_key)
    state["prev_norm_lines"] = norm_lines
    state["prev_snapshot"]   = snapshot


def _tmux_capture_loop():
    """轮询所有 Bot 的 tmux pane，新增行分别写入各自缓冲。"""
    global _tmux_capture_running
    _tmux_capture_running = True
    while _tmux_capture_running:
        for bot_key in BOTS:
            _capture_one_bot(bot_key)
        time.sleep(3)


import re as _re
# Spinner symbols used by Claude Code (✶✷✸✹✺✻✼✽✾✿❀❁ and others)
_SPINNER_RE = _re.compile(r"^[✶✷✸✹✺✻✼✽✾✿❀❁·•◦⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]\s")

def _should_skip_claude_line(line):
    """Filter out noise: logo, spinners, prompts, decorative borders."""
    s = line.strip()
    if not s:
        return True
    # Bare prompt line (just ❯ with optional trailing space)
    if s == "❯" or s == ">":
        return True
    # Spinner / thinking status lines (e.g. "✶ Gesticulating…", "· Finagling… (running stop hook)")
    if _SPINNER_RE.match(s):
        return True
    # ASCII art logo characters (block elements)
    if any(ch in s for ch in "▐▛▜▌▝▘█▞▚▖▗▙▟"):
        return True
    # bypass permissions prompt, auto-update noise
    if "bypass permissions" in s or "Auto-update failed" in s:
        return True
    # esc to cancel prompt
    if s.startswith("esc to") or s.startswith("⏵⏵"):
        return True
    # claude doctor suggestion
    if "claude doctor" in s.lower():
        return True
    # Pure decorative lines
    if all(c in " ─━═│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬·•◦◈◇◆" for c in s):
        return True
    return False

_BLOCK_LEADERS = ("●", "❯", "⎿", "✢", "✶", "✷", "✸", "✺", "⏺", "*", "·")  # 新消息块起始字符
# 每个 Bot 一份去重集合 + 上次输出时间戳，避免互相干扰
_claude_state = {
    k: {"recent_set": set(), "recent_list": [], "last_log_time": 0}
    for k in BOTS
}

def _log_claude(line, bot_key="main"):
    """Log a Claude Code output line with [Claude] prefix into the bot's buffer.
    连续输出的续行（2秒内、且不是新块起始）只缩进不加时间戳前缀，避免把一段话切碎。"""
    if _should_skip_claude_line(line):
        return
    st = _claude_state.get(bot_key) or _claude_state["main"]
    # Deduplicate using whitespace-collapsed key to absorb re-render diffs
    key = _dedup_key(line)
    if not key:
        return
    if key in st["recent_set"]:
        return
    st["recent_set"].add(key)
    st["recent_list"].append(key)
    if len(st["recent_list"]) > 1000:
        old = st["recent_list"].pop(0)
        st["recent_set"].discard(old)
    now = time.time()
    stripped = line.lstrip()
    # 判定是否为一个新逻辑块：超过2秒没输出，或行首是块起始字符
    is_new_block = (now - st["last_log_time"] > 2.0) or any(stripped.startswith(c) for c in _BLOCK_LEADERS)
    if is_new_block:
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] [Claude] {line.rstrip()}"
    else:
        # 续行：用等宽空白对齐到前缀位置，读起来像一段
        entry = f"                    {line.rstrip()}"
    st["last_log_time"] = now
    _push_log(bot_key, entry)


def start_bridge():
    global _bridge_proc, _bridge_start_time
    if _bridge_proc and _bridge_proc.poll() is None:
        return {"ok": False, "error": "Bridge already running"}
    # Kill any stray bridge process (e.g. started by start.sh) before binding port。
    # 用 BRIDGE_SCRIPT 绝对路径精确匹配，避免误杀 feishu_bridge.py（独立进程，保持飞书Bot独立）。
    subprocess.call(["pkill", "-f", BRIDGE_SCRIPT], stderr=subprocess.DEVNULL)
    bridge_port = int(os.environ.get("PORT", "9999"))
    subprocess.call(["fuser", "-k", f"{bridge_port}/tcp"], stderr=subprocess.DEVNULL)
    import time as _t; _t.sleep(0.5)  # wait for port to free
    env = os.environ.copy()
    # Reset token stats on each bridge start
    try:
        with open(TOKEN_STATS_FILE, "w") as f:
            json.dump({"input": 0, "output": 0}, f)
    except Exception:
        pass
    _bridge_proc = subprocess.Popen(
        [sys.executable, BRIDGE_SCRIPT],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, close_fds=True, bufsize=1,
    )
    _bridge_start_time = time.time()
    threading.Thread(target=_read_bridge_output, args=(_bridge_proc,), daemon=True).start()
    _log(f"Bridge started (PID {_bridge_proc.pid})")
    return {"ok": True, "pid": _bridge_proc.pid}


def stop_bridge():
    global _bridge_proc, _bridge_start_time
    if not _bridge_proc or _bridge_proc.poll() is not None:
        return {"ok": False, "error": "Bridge not running"}
    pid = _bridge_proc.pid
    _bridge_proc.terminate()
    try:
        _bridge_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _bridge_proc.kill()
    _bridge_proc = None
    _bridge_start_time = None
    _log(f"Bridge stopped (PID {pid})")
    return {"ok": True}


def get_status(bot_key="main"):
    profile = _bot_or_default(bot_key)
    running = _bridge_proc is not None and _bridge_proc.poll() is None
    uptime  = int(time.time() - _bridge_start_time) if running and _bridge_start_time else 0
    pid     = _bridge_proc.pid if running and _bridge_proc else None

    # Per-bot 模型
    model_file = profile["model_file"]
    model = profile["default_model"]
    if os.path.exists(model_file):
        try:
            m = open(model_file).read().strip()
            if m:
                model = m
        except Exception:
            pass

    # Token 统计目前是全局的（bridge.py 没分桶），暂保留共用
    tokens = {"input": 0, "output": 0}
    if os.path.exists(TOKEN_STATS_FILE):
        try:
            tokens = json.load(open(TOKEN_STATS_FILE))
        except Exception:
            pass

    # Webhook 是 bridge 全局的，两个 Bot 共一个隧道
    webhook = None
    try:
        content = open(LTLOG).read()
        m = re.search(r'https://[^\s]+(?:\.ngrok-free\.dev|\.loca\.lt|\.trycloudflare\.com)', content)
        if m:
            webhook = m.group(0)
    except Exception:
        pass

    # Per-bot chat_id
    chat_id = None
    if os.path.exists(profile["chat_id_file"]):
        try:
            chat_id = open(profile["chat_id_file"]).read().strip()
        except Exception:
            pass

    # Per-bot tmux 状态
    tmux_running = _tmux_session_exists(profile["tmux_session"])

    return {
        "bot":     bot_key,
        "name":    profile["name"],
        "running": running,
        "pid":     pid,
        "uptime":  uptime,
        "model":   model,
        "tokens":  tokens,
        "webhook": webhook,
        "chat_id": chat_id,
        "tmux":    profile["tmux_session"],
        "tmux_running": tmux_running,
    }


def get_api_keys():
    if os.path.exists(API_KEYS_FILE):
        try:
            return json.load(open(API_KEYS_FILE))
        except Exception:
            pass
    return {}


def save_api_keys(keys):
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Claude Bridge 控制台</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Syne:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080d18;
  --surface:#0e1525;
  --surface2:#141d30;
  --border:#1e2d45;
  --accent:#00d4ff;
  --accent2:#0099bb;
  --green:#00e676;
  --red:#ff4444;
  --text:#c8d8f0;
  --text2:#5a7399;
  --text3:#8faac8;
  --font-mono:'JetBrains Mono',monospace;
  --font-display:'Syne',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:var(--font-mono);
  background:var(--bg);
  min-height:100vh;
  display:flex;flex-direction:column;align-items:center;
  padding:24px 16px 40px;
  color:var(--text);
  position:relative;overflow-x:hidden;
}

/* ── Background grid ── */
body::before{
  content:'';position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(0,212,255,.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,212,255,.025) 1px, transparent 1px);
  background-size:40px 40px;
  pointer-events:none;z-index:0;
}
body::after{
  content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse 70% 50% at 50% 0%, rgba(0,212,255,.06) 0%, transparent 70%);
  pointer-events:none;z-index:0;
}
*{position:relative;z-index:1}

/* ── Header ── */
.header{
  display:flex;align-items:center;gap:12px;
  margin-bottom:32px;width:100%;max-width:480px;
}
.header-dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--accent);
  box-shadow:0 0 10px var(--accent);
}
.header h1{
  font-family:var(--font-display);
  font-size:17px;font-weight:700;
  color:#fff;letter-spacing:1.5px;text-transform:uppercase;
  flex:1;
}
.header-ver{
  font-size:10px;color:var(--text2);
  border:1px solid var(--border);border-radius:4px;
  padding:2px 7px;letter-spacing:.5px;
}

/* ── Bot Switcher ── */
.bot-tabs{
  display:flex;gap:6px;
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:4px;
  max-width:600px;width:100%;margin-bottom:16px;
}
.bot-tab{
  flex:1;background:transparent;border:none;cursor:pointer;
  padding:8px 12px;border-radius:9px;
  font-family:var(--font-mono);font-size:12px;font-weight:600;
  color:var(--text2);letter-spacing:.5px;
  transition:all .2s;display:flex;align-items:center;justify-content:center;gap:6px;
}
.bot-tab:hover{color:var(--text3)}
.bot-tab.active{
  background:linear-gradient(135deg,rgba(0,212,255,.12),rgba(0,212,255,.05));
  color:var(--accent);
  box-shadow:0 0 0 1px rgba(0,212,255,.25);
}
.bot-tab-dot{width:6px;height:6px;border-radius:50%;background:var(--text2);transition:all .2s}
.bot-tab.active .bot-tab-dot{background:var(--green);box-shadow:0 0 6px var(--green)}

/* ── Power Button ── */
.power-wrap{
  display:flex;flex-direction:column;align-items:center;
  margin-bottom:28px;
}
.power-ring{
  width:156px;height:156px;border-radius:50%;
  border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  position:relative;
}
.power-ring::before{
  content:'';position:absolute;inset:-8px;border-radius:50%;
  border:1px solid transparent;
  background:linear-gradient(var(--bg),var(--bg)) padding-box,
              linear-gradient(135deg,rgba(0,212,255,.2),transparent,rgba(0,212,255,.1)) border-box;
}
.power-btn{
  width:120px;height:120px;border-radius:50%;border:none;cursor:pointer;
  background:var(--surface2);
  box-shadow:0 0 0 1px var(--border),inset 0 1px 0 rgba(255,255,255,.06);
  display:flex;align-items:center;justify-content:center;
  transition:all .25s ease;user-select:none;
  position:relative;overflow:hidden;
}
.power-btn::before{
  content:'';position:absolute;inset:0;border-radius:50%;
  background:radial-gradient(circle at 40% 30%, rgba(255,255,255,.08), transparent 60%);
}
.power-icon{
  width:44px;height:44px;
  stroke:var(--text2);stroke-width:2;fill:none;
  transition:stroke .3s;
}
.power-btn:hover .power-icon{stroke:var(--text)}
.power-btn:active{transform:scale(.96)}
.power-btn.running{
  background:linear-gradient(135deg,#0d2a1a,#0a1f14);
  box-shadow:0 0 0 1px rgba(0,230,118,.3),
             0 0 30px rgba(0,230,118,.15),
             inset 0 1px 0 rgba(255,255,255,.04);
}
.power-btn.running .power-icon{stroke:var(--green);filter:drop-shadow(0 0 6px rgba(0,230,118,.6))}
.power-btn.running::after{
  content:'';position:absolute;inset:-2px;border-radius:50%;
  background:conic-gradient(var(--green) 0deg, transparent 60deg, transparent 300deg, var(--green) 360deg);
  opacity:.15;animation:spin 4s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.status-row{
  margin-top:16px;display:flex;align-items:center;gap:8px;
  font-size:12px;color:var(--text2);min-height:20px;
}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--red);flex-shrink:0}
.status-dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.status-row.running{color:var(--green)}

/* ── Model selector trigger ── */
.model-trigger{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:12px 16px;
  display:flex;align-items:center;gap:12px;cursor:pointer;
  max-width:480px;width:100%;margin-bottom:16px;
  transition:border-color .2s,background .2s;
  user-select:none;
}
.model-trigger:hover{border-color:rgba(0,212,255,.3);background:var(--surface2)}
.model-trigger-icon{font-size:20px;flex-shrink:0}
.model-trigger-info{flex:1;min-width:0}
.model-trigger-label{font-size:10px;color:var(--text2);margin-bottom:2px;letter-spacing:.5px;text-transform:uppercase}
.model-trigger-name{font-size:14px;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.model-trigger-prov{font-size:11px;color:var(--accent);margin-top:1px}
.model-trigger-arrow{
  font-size:16px;color:var(--text2);transition:transform .2s;flex-shrink:0;
}
.model-trigger.open .model-trigger-arrow{transform:rotate(180deg)}

/* ── Stats cards ── */
.cards{
  display:grid;grid-template-columns:repeat(4,1fr);gap:8px;
  max-width:480px;width:100%;margin-bottom:16px;
}
.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:11px 8px;text-align:center;
  transition:border-color .2s;
}
.card:hover{border-color:rgba(0,212,255,.2)}
.card-icon{font-size:17px;margin-bottom:5px;opacity:.8}
.card-value{
  font-size:12px;font-weight:600;color:#fff;
  word-break:break-all;line-height:1.3;
  font-family:var(--font-mono);
}
.card-label{font-size:9px;color:var(--text2);margin-top:3px;letter-spacing:.3px}

/* ── Log panel ── */
.log-panel{
  background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:14px 16px;
  max-width:480px;width:100%;margin-bottom:16px;
}
.log-header{
  display:flex;justify-content:space-between;align-items:center;
  margin-bottom:10px;
}
.log-title{
  font-size:11px;font-weight:600;color:var(--text2);
  letter-spacing:1px;text-transform:uppercase;display:flex;align-items:center;gap:7px;
}
.log-title-dot{width:5px;height:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 6px var(--accent)}
.log-btns{display:flex;gap:6px}
.log-btn{
  background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;color:var(--text3);font-size:10px;
  padding:3px 9px;cursor:pointer;font-family:var(--font-mono);
  transition:all .15s;
}
.log-btn:hover{border-color:rgba(0,212,255,.3);color:var(--accent)}
#log-output{
  height:300px;overflow-y:auto;scroll-behavior:smooth;
  font-size:11px;line-height:1.6;
  color:#4a6a90;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}
#log-output .ll{padding:0;color:#4a6a90}
#log-output .ll.new{color:var(--text3)}
#log-output .ll.claude{color:#e0a040;font-weight:500}

/* ── Fullscreen log ── */
.log-panel.fullscreen{
  position:fixed;top:0;left:0;right:0;bottom:0;
  max-width:100%;width:100%;height:100%;
  margin:0;border-radius:0;z-index:9999;
  padding:14px 16px;display:flex;flex-direction:column;
}
.log-panel.fullscreen #log-output{
  height:auto;flex:1;
  font-size:13px;line-height:1.7;
}
.log-panel.fullscreen .log-header{flex-shrink:0}

/* ── Keys panel ── */
details{max-width:480px;width:100%}
summary{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:12px 16px;cursor:pointer;
  font-size:11px;font-weight:600;color:var(--text2);
  letter-spacing:1px;text-transform:uppercase;
  list-style:none;display:flex;align-items:center;gap:8px;
  transition:border-color .2s;
}
summary:hover{border-color:rgba(0,212,255,.2)}
summary::-webkit-details-marker{display:none}
details[open] summary{border-radius:10px 10px 0 0;border-bottom-color:transparent}
.keys-body{
  background:var(--surface);border:1px solid var(--border);
  border-top:none;border-radius:0 0 10px 10px;padding:16px;
}
.kr{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.kr label{font-size:11px;color:var(--text2);width:70px;flex-shrink:0;letter-spacing:.3px}
.kr input{
  flex:1;background:var(--bg);border:1px solid var(--border);border-radius:7px;
  padding:7px 11px;font-size:11px;font-family:var(--font-mono);
  color:var(--text);outline:none;transition:border-color .2s;
}
.kr input:focus{border-color:var(--accent)}
.save-btn{
  background:linear-gradient(135deg,var(--accent2),var(--accent));
  color:#000;border:none;border-radius:8px;
  padding:8px 22px;font-size:12px;font-weight:700;
  font-family:var(--font-mono);cursor:pointer;margin-top:4px;
  letter-spacing:.5px;transition:opacity .2s;
}
.save-btn:hover{opacity:.85}

/* ── Model Picker Sheet ── */
.sheet-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.7);
  z-index:100;opacity:0;pointer-events:none;transition:opacity .3s;
  backdrop-filter:blur(4px);
}
.sheet-overlay.open{opacity:1;pointer-events:all}
.sheet{
  position:fixed;bottom:0;left:50%;transform:translateX(-50%) translateY(100%);
  width:100%;max-width:520px;
  background:var(--surface);
  border:1px solid var(--border);border-bottom:none;
  border-radius:20px 20px 0 0;
  z-index:101;transition:transform .35s cubic-bezier(.32,1,.25,1);
  max-height:82vh;display:flex;flex-direction:column;
}
.sheet.open{transform:translateX(-50%) translateY(0)}
.sheet-handle{
  width:36px;height:4px;border-radius:2px;
  background:var(--border);margin:12px auto 0;flex-shrink:0;
}
.sheet-header{
  padding:16px 20px 12px;flex-shrink:0;
  border-bottom:1px solid var(--border);
}
.sheet-header-top{
  display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;
}
.sheet-title{
  font-family:var(--font-display);font-size:15px;font-weight:700;
  color:#fff;letter-spacing:.5px;
}
.sheet-count{font-size:11px;color:var(--text2)}
.sheet-subtitle{font-size:11px;color:var(--text2)}
.sheet-close{
  background:var(--surface2);border:1px solid var(--border);
  border-radius:50%;width:28px;height:28px;cursor:pointer;
  color:var(--text2);font-size:16px;line-height:28px;text-align:center;
  transition:all .15s;
}
.sheet-close:hover{color:#fff;border-color:rgba(255,255,255,.2)}
.sheet-body{overflow-y:auto;flex:1;padding:8px 0 20px;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;}

/* ── Provider group ── */
.prov-group{margin-bottom:4px}
.prov-label{
  font-size:10px;color:var(--text2);letter-spacing:1px;text-transform:uppercase;
  padding:10px 20px 5px;font-weight:600;
}
/* ── Model row item ── */
.model-item{
  display:flex;align-items:center;gap:14px;
  padding:13px 20px;cursor:pointer;
  border-left:2px solid transparent;
  transition:background .15s,border-color .15s;
  position:relative;
}
.model-item:hover{background:rgba(0,212,255,.04)}
.model-item.selected{
  background:rgba(0,212,255,.06);
  border-left-color:var(--accent);
}
.model-radio{
  width:20px;height:20px;border-radius:50%;flex-shrink:0;
  border:2px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  transition:border-color .2s;
}
.model-item.selected .model-radio{
  border-color:var(--accent);background:rgba(0,212,255,.15);
}
.model-radio-dot{
  width:8px;height:8px;border-radius:50%;
  background:var(--accent);
  transform:scale(0);transition:transform .2s;
  box-shadow:0 0 6px var(--accent);
}
.model-item.selected .model-radio-dot{transform:scale(1)}
.model-icon{font-size:22px;flex-shrink:0;width:28px;text-align:center}
.model-info{flex:1;min-width:0}
.model-name{font-size:13px;font-weight:600;color:#fff}
.model-item.selected .model-name{color:var(--accent)}
.model-desc{font-size:10px;color:var(--text2);margin-top:1px}
.model-badge{
  font-size:10px;font-weight:600;
  padding:3px 9px;border-radius:20px;flex-shrink:0;
  font-family:var(--font-mono);letter-spacing:.3px;
}
.badge-fast{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.2)}
.badge-smart{background:rgba(0,212,255,.12);color:var(--accent);border:1px solid rgba(0,212,255,.2)}
.badge-reason{background:rgba(255,180,0,.12);color:#ffb400;border:1px solid rgba(255,180,0,.2)}
.badge-cheap{background:rgba(150,100,255,.12);color:#b080ff;border:1px solid rgba(150,100,255,.2)}
.divider{height:1px;background:var(--border);margin:0 20px}

/* ── Toast ── */
.toast{
  position:fixed;bottom:32px;left:50%;transform:translateX(-50%);
  background:var(--surface2);border:1px solid var(--border);
  color:var(--text);padding:10px 20px;border-radius:20px;
  font-size:12px;opacity:0;transition:opacity .3s;
  pointer-events:none;z-index:200;letter-spacing:.3px;
  box-shadow:0 8px 30px rgba(0,0,0,.4);
  white-space:nowrap;
}
.toast.show{opacity:1}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-dot"></div>
  <h1>Claude Bridge</h1>
  <div class="header-ver">v2.0</div>
</div>

<!-- Bot Switcher -->
<div class="bot-tabs" id="botTabs">
  <button class="bot-tab active" data-bot="main" onclick="switchBot('main')">
    <span class="bot-tab-dot"></span>主控Bot
  </button>
  <button class="bot-tab" data-bot="stock" onclick="switchBot('stock')">
    <span class="bot-tab-dot"></span>股票Bot
  </button>
  <button class="bot-tab" data-bot="feishu" onclick="switchBot('feishu')">
    <span class="bot-tab-dot"></span>飞书Bot
  </button>
</div>

<!-- Power Button -->
<div class="power-wrap">
  <div class="power-ring">
    <button class="power-btn" id="powerBtn" onclick="toggleBridge()">
      <svg class="power-icon" viewBox="0 0 24 24">
        <path d="M12 2v6M5.636 5.636A9 9 0 1018.364 18.364 9 9 0 005.636 5.636z"/>
      </svg>
    </button>
  </div>
  <div class="status-row" id="statusRow">
    <div class="status-dot" id="statusDot"></div>
    <span id="statusText">加载中…</span>
  </div>
</div>

<!-- Model Selector Trigger -->
<div class="model-trigger" id="modelTrigger" onclick="openSheet()">
  <div class="model-trigger-icon" id="triggerIcon">🤖</div>
  <div class="model-trigger-info">
    <div class="model-trigger-label">当前模型</div>
    <div class="model-trigger-name" id="triggerName">Claude Sonnet 4.6</div>
    <div class="model-trigger-prov" id="triggerProv">Anthropic</div>
  </div>
  <div class="model-trigger-arrow" id="triggerArrow">▾</div>
</div>

<!-- Stats Cards -->
<div class="cards">
  <div class="card">
    <div class="card-icon">⬆</div>
    <div class="card-value" id="cOut">0</div>
    <div class="card-label">输出 Tokens</div>
  </div>
  <div class="card">
    <div class="card-icon">⬇</div>
    <div class="card-value" id="cIn">0</div>
    <div class="card-label">输入 Tokens</div>
  </div>
  <div class="card">
    <div class="card-icon">⏱</div>
    <div class="card-value" id="cUp">—</div>
    <div class="card-label">运行时长</div>
  </div>
  <div class="card">
    <div class="card-icon">🔗</div>
    <div class="card-value" id="cWH">—</div>
    <div class="card-label">Webhook</div>
  </div>
</div>

<!-- Log Panel -->
<div class="log-panel">
  <div class="log-header">
    <div class="log-title">
      <div class="log-title-dot"></div>
      实时日志
    </div>
    <div class="log-btns">
      <button class="log-btn" id="pauseBtn" onclick="togglePause()">⏸ 暂停</button>
      <button class="log-btn" onclick="clearLog()">清空</button>
      <button class="log-btn" id="fullscreenBtn" onclick="toggleFullscreen()">全屏</button>
    </div>
  </div>
  <div id="log-output"></div>
</div>

<!-- API Keys -->
<details>
  <summary>
    <span style="font-size:14px">🔑</span>
    API Keys 配置
    <span style="margin-left:auto;font-size:13px;letter-spacing:0">▾</span>
  </summary>
  <div class="keys-body">
    <div class="kr"><label>DeepSeek</label><input id="k-ds" type="password" placeholder="sk-..."></div>
    <div class="kr"><label>智谱AI</label><input id="k-zp" type="password" placeholder="id.secret"></div>
    <div class="kr"><label>MiniMax</label><input id="k-mm" type="password" placeholder="sk-api-..."></div>
    <div class="kr"><label>百炼</label><input id="k-bl" type="password" placeholder="sk-..."></div>
    <button class="save-btn" onclick="saveKeys()">保存 API Keys</button>
  </div>
</details>

<!-- Model Picker Sheet -->
<div class="sheet-overlay" id="sheetOverlay" onclick="closeSheet()"></div>
<div class="sheet" id="sheet">
  <div class="sheet-handle"></div>
  <div class="sheet-header">
    <div class="sheet-header-top">
      <div class="sheet-title">选择模型</div>
      <button class="sheet-close" onclick="closeSheet()">✕</button>
    </div>
    <div class="sheet-subtitle" id="sheetSub">共 10 个模型 · 已选：Claude Sonnet 4.6</div>
  </div>
  <div class="sheet-body" id="sheetBody"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const MODELS=[
  {id:"claude-opus-4-7",  name:"Claude Opus 4.7",    prov:"Anthropic", icon:"🟣", desc:"最强推理，复杂任务首选",  badge:"smart",  badgeTxt:"SMART"},
  {id:"claude-sonnet-4-6",name:"Claude Sonnet 4.6",  prov:"Anthropic", icon:"🔵", desc:"均衡性能，日常主力",      badge:"fast",   badgeTxt:"FAST"},
  {id:"claude-haiku-4-5-20251001",name:"Claude Haiku 4.5",prov:"Anthropic",icon:"⚪",desc:"超快响应，轻量任务",   badge:"cheap",  badgeTxt:"LITE"},
  {id:"deepseek-v4-flash",name:"DeepSeek V4 Flash",   prov:"DeepSeek",  icon:"🐋", desc:"经济快速，1M 上下文",    badge:"fast",   badgeTxt:"FAST"},
  {id:"deepseek-v4-pro",  name:"DeepSeek V4 Pro",     prov:"DeepSeek",  icon:"🧠", desc:"旗舰思考模式，1M 上下文",badge:"reason", badgeTxt:"THINK"},
  {id:"glm-4-plus",       name:"GLM-4 Plus",          prov:"ZhipuAI",   icon:"🌸", desc:"智谱旗舰，中文优化",     badge:"smart",  badgeTxt:"SMART"},
  {id:"glm-4-flash",      name:"GLM-4 Flash",         prov:"ZhipuAI",   icon:"⚡", desc:"闪电响应，低成本",       badge:"cheap",  badgeTxt:"FAST"},
  {id:"abab6.5s-chat",    name:"MiniMax 6.5s",        prov:"MiniMax",   icon:"🎭", desc:"多模态，长上下文",        badge:"smart",  badgeTxt:"MULTI"},
  {id:"qwen-max",         name:"通义千问 Max",         prov:"Bailian",   icon:"☁️", desc:"阿里旗舰模型",           badge:"smart",  badgeTxt:"SMART"},
  {id:"qwen-plus",        name:"通义千问 Plus",        prov:"Bailian",   icon:"🌤", desc:"性价比之选",             badge:"cheap",  badgeTxt:"FAST"},
];

const BOT_NAMES={main:"主控Bot",stock:"股票Bot",feishu:"飞书Bot"};
let paused=false,isRunning=false,curModel="claude-sonnet-4-6",curBot=(BOT_NAMES[localStorage.getItem("dash_curBot")]?localStorage.getItem("dash_curBot"):"main");

function switchBot(bot){
  if(bot===curBot)return;
  curBot=bot;
  try{localStorage.setItem("dash_curBot",bot);}catch(e){}
  document.querySelectorAll(".bot-tab").forEach(el=>{
    el.classList.toggle("active",el.dataset.bot===bot);
  });
  // 清空当前日志并重连 SSE 拉新 Bot 的内容
  document.getElementById("log-output").innerHTML="";
  _pollSeq=0;
  if(_sse){try{_sse.close();}catch(e){}_sse=null;}
  if(_pollTimer){clearTimeout(_pollTimer);_pollTimer=null;_usePolling=false;}
  fetchStatus();
  startSSE();
  toast("已切换到 "+(BOT_NAMES[bot]||bot));
}

// Build model sheet
function buildSheet(){
  const body=document.getElementById("sheetBody");
  const provs=[...new Set(MODELS.map(m=>m.prov))];
  body.innerHTML="";
  provs.forEach((prov,pi)=>{
    const grp=document.createElement("div");
    grp.className="prov-group";
    const lbl=document.createElement("div");
    lbl.className="prov-label";lbl.textContent=prov;
    grp.appendChild(lbl);
    MODELS.filter(m=>m.prov===prov).forEach(m=>{
      const row=document.createElement("div");
      row.className="model-item"+(m.id===curModel?" selected":"");
      row.dataset.id=m.id;
      row.innerHTML=`
        <div class="model-radio"><div class="model-radio-dot"></div></div>
        <div class="model-icon">${m.icon}</div>
        <div class="model-info">
          <div class="model-name">${m.name}</div>
          <div class="model-desc">${m.desc}</div>
        </div>
        <div class="model-badge badge-${m.badge}">${m.badgeTxt}</div>`;
      row.onclick=()=>selectModel(m);
      grp.appendChild(row);
      const div=document.createElement("div");div.className="divider";grp.appendChild(div);
    });
    body.appendChild(grp);
  });
}

function selectModel(m){
  curModel=m.id;
  switchModel(m.id,m);
  document.querySelectorAll(".model-item").forEach(el=>{
    el.classList.toggle("selected",el.dataset.id===m.id);
  });
  updateTrigger(m);
  document.getElementById("sheetSub").textContent=`共 ${MODELS.length} 个模型 · 已选：${m.name}`;
  setTimeout(closeSheet,320);
}

function updateTrigger(m){
  document.getElementById("triggerIcon").textContent=m.icon;
  document.getElementById("triggerName").textContent=m.name;
  document.getElementById("triggerProv").textContent=m.prov;
}

function openSheet(){
  buildSheet();
  document.getElementById("sheetOverlay").classList.add("open");
  document.getElementById("sheet").classList.add("open");
  document.getElementById("modelTrigger").classList.add("open");
  // scroll to selected
  setTimeout(()=>{
    const sel=document.querySelector(".model-item.selected");
    if(sel)sel.scrollIntoView({block:"center",behavior:"smooth"});
  },350);
}
function closeSheet(){
  document.getElementById("sheetOverlay").classList.remove("open");
  document.getElementById("sheet").classList.remove("open");
  document.getElementById("modelTrigger").classList.remove("open");
}

function fmt(n){
  if(n>=1e6)return(n/1e6).toFixed(1)+"M";
  if(n>=1e3)return(n/1e3).toFixed(1)+"K";
  return String(n);
}

function updateUI(s){
  isRunning=s.running;
  const btn=document.getElementById("powerBtn");
  const txt=document.getElementById("statusText");
  const dot=document.getElementById("statusDot");
  const row=document.getElementById("statusRow");
  if(isRunning){
    btn.classList.add("running");
    dot.classList.add("on");
    const u=s.uptime||0;
    const h=Math.floor(u/3600),m=Math.floor((u%3600)/60),sec=u%60;
    const up=h>0?`${h}h ${m}m`:m>0?`${m}m ${sec}s`:`${sec}s`;
    txt.textContent=`运行中  PID ${s.pid}  ${up}`;
    row.className="status-row running";
    document.getElementById("cUp").textContent=up;
  }else{
    btn.classList.remove("running");
    dot.classList.remove("on");
    txt.textContent="已停止 — 点击启动";
    row.className="status-row";
    document.getElementById("cUp").textContent="—";
  }
  if(s.model&&s.model!==curModel){
    curModel=s.model;
    const m=MODELS.find(x=>x.id===curModel);
    if(m)updateTrigger(m);
  }
  document.getElementById("cOut").textContent=fmt(s.tokens?.output||0);
  document.getElementById("cIn").textContent=fmt(s.tokens?.input||0);
  const wh=s.webhook;
  const whEl=document.getElementById("cWH");
  if(wh){whEl.textContent=wh.replace("https://","").slice(0,14)+"…";whEl.title=wh;}
  else whEl.textContent="未连接";
  // update sheet subtitle
  const m=MODELS.find(x=>x.id===curModel);
  document.getElementById("sheetSub").textContent=`共 ${MODELS.length} 个模型 · 已选：${m?m.name:curModel}`;
}

async function fetchStatus(){
  try{const r=await fetch("/api/status?bot="+curBot);updateUI(await r.json());}catch(e){}
}
async function toggleBridge(){
  const action=isRunning?"stop":"start";
  try{
    const r=await fetch("/api/bridge/"+action,{method:"POST"});
    const d=await r.json();
    toast(d.ok?(action==="start"?"Bridge 已启动":"Bridge 已停止"):"操作失败: "+d.error);
    setTimeout(fetchStatus,600);
  }catch(e){toast("请求失败");}
}
async function switchModel(model,m){
  try{
    const r=await fetch("/api/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({model,bot:curBot})});
    const d=await r.json();
    const tag="["+(BOT_NAMES[curBot]||curBot)+"] ";
    toast(d.ok?tag+"已切换：" +(m?m.name:model):"切换失败: "+d.error);
    fetchStatus();
  }catch(e){toast("请求失败");}
}
async function saveKeys(){
  const keys={
    deepseek:document.getElementById("k-ds").value,
    zhipu:document.getElementById("k-zp").value,
    minimax:document.getElementById("k-mm").value,
    bailian:document.getElementById("k-bl").value,
  };
  try{
    const r=await fetch("/api/keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(keys)});
    const d=await r.json();toast(d.ok?"API Keys 已保存 ✓":"保存失败");
  }catch(e){toast("请求失败");}
}
async function loadKeys(){
  try{
    const d=await (await fetch("/api/keys")).json();
    if(d.deepseek)document.getElementById("k-ds").value=d.deepseek;
    if(d.zhipu)   document.getElementById("k-zp").value=d.zhipu;
    if(d.minimax) document.getElementById("k-mm").value=d.minimax;
    if(d.bailian) document.getElementById("k-bl").value=d.bailian;
  }catch(e){}
}
let _sse=null;
let _sseReconnect=null;
let _sseFails=0;         // consecutive SSE failures
let _pollTimer=null;     // polling fallback timer
let _pollSeq=0;          // last seen log seq for polling
let _sseSeq=0;           // last seen seq from SSE (for resume-after-reconnect)
let _usePolling=false;   // fallback mode flag

function _appendLog(text,first){
  const logEl=document.getElementById("log-output");
  if(paused)return;
  const atBottom=(logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight)<40;
  const div=document.createElement("div");
  const isClaude=text.includes("] [Claude] ")||text.startsWith("                    ");
  div.className="ll"+(first?"":" new")+(isClaude?" claude":"");
  div.style.opacity="0";div.style.transition="opacity .5s ease";
  div.textContent=text;
  logEl.appendChild(div);
  requestAnimationFrame(()=>requestAnimationFrame(()=>div.style.opacity="1"));
  while(logEl.children.length>500){
    const removed=logEl.firstChild;
    const h=removed.offsetHeight;
    logEl.removeChild(removed);
    if(!atBottom)logEl.scrollTop=Math.max(0,logEl.scrollTop-h);
  }
  if(atBottom)logEl.scrollTop=logEl.scrollHeight;
}

function stopPolling(){
  if(_pollTimer){clearTimeout(_pollTimer);_pollTimer=null;}
}

function startPolling(){
  stopPolling();
  _usePolling=true;
  toast("SSE不可用，已切换轮询模式");
  function poll(){
    _pollTimer=null;
    fetch("/api/logs/snapshot?bot="+curBot+"&since="+_pollSeq)
      .then(r=>r.json())
      .then(d=>{
        if(d.lines&&d.lines.length>0){
          d.lines.forEach(t=>_appendLog(t,false));
        }
        if(d.seq)_pollSeq=d.seq;
        _pollTimer=setTimeout(poll,2000);
      }).catch(()=>{ _pollTimer=setTimeout(poll,3000); });
  }
  poll();
}

function startSSE(){
  if(_sse){try{_sse.close();}catch(e){}_sse=null;}
  if(_sseReconnect){clearTimeout(_sseReconnect);_sseReconnect=null;}
  const es=new EventSource("/api/logs?bot="+curBot+"&since="+_sseSeq);
  _sse=es;
  let first=(_sseSeq===0);
  // 3s timeout: if Cloudflare/proxy buffers SSE, fall back to polling fast
  let aliveTimer=setTimeout(()=>{
    if(es===_sse&&_sseFails<5){
      _sseFails=5;
      try{es.close();}catch(e){}
      _sse=null;
      startPolling();
    }
  },3000);
  function resetAlive(){clearTimeout(aliveTimer);}
  es.onmessage=(e)=>{
    if(es!==_sse)return;
    resetAlive();_sseFails=0;
    _appendLog(e.data,first);first=false;
  };
  es.addEventListener("ping",()=>{
    if(es!==_sse)return;
    resetAlive();_sseFails=0;
  });
  es.onerror=()=>{
    if(es!==_sse)return;
    resetAlive();
    try{es.close();}catch(e){}
    _sse=null;
    _sseFails++;
    if(_sseFails>=5){
      startPolling();
    }else{
      _sseReconnect=setTimeout(startSSE,3000);
    }
  };
}

// 页面重新可见时重置并重连，保证实时日志不中断
document.addEventListener("visibilitychange",()=>{
  if(!document.hidden){
    _sseFails=0;
    if(_usePolling){
      // 从轮询切回 SSE
      stopPolling();
      _usePolling=false;
      _sseSeq=_pollSeq;  // 继续从上次轮询到的位置接
      startSSE();
      toast("已重新连接实时日志");
    } else if(!_sse){
      startSSE();
    }
  }
});

function toggleFullscreen(){
  const panel=document.querySelector(".log-panel");
  const btn=document.getElementById("fullscreenBtn");
  panel.classList.toggle("fullscreen");
  const isFull=panel.classList.contains("fullscreen");
  btn.textContent=isFull?"退出全屏":"全屏";
  const logEl=document.getElementById("log-output");
  logEl.scrollTop=logEl.scrollHeight;
}
function togglePause(){
  paused=!paused;
  document.getElementById("pauseBtn").textContent=paused?"▶ 继续":"⏸ 暂停";
}
function clearLog(){document.getElementById("log-output").innerHTML="";}
function toast(msg){
  const t=document.getElementById("toast");
  t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),2500);
}

// Init
const initM=MODELS.find(x=>x.id===curModel);
if(initM)updateTrigger(initM);
// 刷新后恢复上次选中的 Bot 标签高亮（curBot 已从 localStorage 读取）
document.querySelectorAll(".bot-tab").forEach(el=>{
  el.classList.toggle("active",el.dataset.bot===curBot);
});
fetchStatus();loadKeys();
// 先拉一次快照，立即显示历史日志（防止 SSE 被云端缓冲时面板空白）
fetch("/api/logs/snapshot?bot="+curBot+"&since=0")
  .then(r=>r.json())
  .then(d=>{
    if(d.lines)d.lines.forEach(t=>_appendLog(t,true));
    if(d.seq){_sseSeq=d.seq;_pollSeq=d.seq;}
  })
  .catch(()=>{})
  .finally(()=>startSSE());
setInterval(fetchStatus,3000);
</script>
</body>
</html>"""


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _query_bot(self):
        """Parse ?bot= from query string, default 'main'."""
        from urllib.parse import parse_qs, urlparse as _up
        qs  = parse_qs(_up(self.path).query)
        bot = qs.get("bot", ["main"])[0]
        return bot if bot in BOTS else "main"

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._html(HTML)

        elif path == "/api/bots":
            self._json({k: {"name": v["name"]} for k, v in BOTS.items()})

        elif path == "/api/status":
            self._json(get_status(self._query_bot()))

        elif path == "/api/logs":
            self._sse_stream(self._query_bot())

        elif path == "/api/logs/snapshot":
            # Polling fallback: ?since=<seq>&bot=main|stock returns new lines with seq > since
            from urllib.parse import parse_qs, urlparse as _up
            qs = parse_qs(_up(self.path).query)
            try:
                since = int(qs.get("since", ["0"])[0])
            except Exception:
                since = 0
            bot_key = self._query_bot()
            with _log_lock:
                buf = _log_buffers[bot_key]
                new = [(seq, text) for seq, text in buf if seq > since]
                new = new[-50:]
                max_seq = buf[-1][0] if buf else 0
            self._json({"lines": [text for _, text in new], "seq": max_seq})

        elif path == "/api/keys":
            self._json(get_api_keys())

        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if path == "/api/bridge/start":
            self._json(start_bridge())

        elif path == "/api/bridge/stop":
            self._json(stop_bridge())

        elif path == "/api/model":
            model   = data.get("model", "")
            bot_key = data.get("bot") or self._query_bot()
            if bot_key not in BOTS:
                bot_key = "main"
            if not model:
                self._json({"ok": False, "error": "no model"})
                return
            try:
                with open(BOTS[bot_key]["model_file"], "w") as f:
                    f.write(model)
                _relaunch_claude(model, bot_key=bot_key)
                self._json({"ok": True, "model": model, "bot": bot_key})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        elif path == "/api/keys":
            try:
                save_api_keys(data)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self, bot_key="main"):
        from urllib.parse import parse_qs, urlparse as _up
        qs = parse_qs(_up(self.path).query)
        try:
            since = int(qs.get("since", ["0"])[0])
        except Exception:
            since = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        if bot_key not in BOTS:
            bot_key = "main"

        # 2KB padding comment to break Cloudflare/proxy buffer
        try:
            padding = ": " + ("x" * 2046) + "\n\n"
            self.wfile.write(padding.encode())
            self.wfile.flush()
        except Exception:
            return

        q = queue.Queue(maxsize=500)
        # Replay buffered lines: if since>0 replay only missed lines, else last 200
        with _log_lock:
            buf = _log_buffers[bot_key]
            if since > 0:
                lines = [text for seq, text in buf if seq > since]
            else:
                lines = [text for _, text in buf[-200:]]
        for line in lines:
            try:
                self.wfile.write(f"data: {line}\n\n".encode())
            except Exception:
                return
        try:
            self.wfile.flush()
        except Exception:
            return

        with _sse_lock:
            _sse_queues[bot_key].append(q)

        try:
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(f"data: {line}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b"event: ping\ndata: \n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_queues[bot_key].remove(q)
                except ValueError:
                    pass

    def log_message(self, *args):
        pass


class ThreadingDashboard(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("⚠  TELEGRAM_BOT_TOKEN not set — bridge won't start without it")
    else:
        start_bridge()  # auto-start bridge on dashboard launch
    # Start tmux capture thread for Claude Code live output
    threading.Thread(target=_tmux_capture_loop, daemon=True).start()
    print(f"Dashboard → http://localhost:{DASHBOARD_PORT}")
    print(f"Bridge script: {BRIDGE_SCRIPT}")
    try:
        ThreadingDashboard(("0.0.0.0", DASHBOARD_PORT), DashboardHandler).serve_forever()
    except KeyboardInterrupt:
        global _tmux_capture_running
        _tmux_capture_running = False
        stop_bridge()
        print("\nStopped")


if __name__ == "__main__":
    main()
