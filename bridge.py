#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge"""

import os
import sys
import json
import re
import subprocess
import threading
import time
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets

# Bypass system proxy — proxy at 127.0.0.1:65533 breaks TLS to Telegram/external APIs
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

TMUX_SESSION  = os.environ.get("TMUX_SESSION", "claude")
LTLOG         = "/tmp/lt_bridge.log"
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
RESTART_NOTIFY_FILE = os.path.expanduser("~/.claude/telegram_restart_notify")
PENDING_FILE = os.path.expanduser("~/.claude/telegram_pending")
HISTORY_FILE = os.path.expanduser("~/.claude/history.jsonl")
MODEL_FILE = os.path.expanduser("~/.claude/telegram_model")
BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
STOCK_BOT_TOKEN = os.environ.get("STOCK_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "9999"))

# ── 多 Bot 配置 ────────────────────────────────────────────────────────────────
# key = webhook 路径, value = bot 配置
# system_prompt=None 表示不注入特殊提示词（主控Bot用全局CLAUDE.md）
BOT_PROFILES = {
    "/": {
        "name":          "主控Bot",
        "token":         "",
        "direct_api":    False,
        "tmux_session":  "claude",              # Claude Code 实例1
        "pending_file":  os.path.expanduser("~/.claude/telegram_pending"),
        "chat_id_file":  os.path.expanduser("~/.claude/telegram_chat_id"),
        "model_file":    os.path.expanduser("~/.claude/telegram_model"),
        "thinking_file": os.path.expanduser("~/.claude/telegram_thinking"),
        "work_dir":      None,                  # 不指定工作目录
        "secret":        "",                    # webhook secret_token，main() 启动时随机生成
    },
    "/stock": {
        "name":          "股票Bot",
        "token":         "",
        "direct_api":    False,
        "tmux_session":  "claude_stock",        # Claude Code 实例2（独立）
        "pending_file":  os.path.expanduser("~/.claude/telegram_pending_stock"),
        "chat_id_file":  os.path.expanduser("~/.claude/telegram_chat_id_stock"),
        "model_file":    os.path.expanduser("~/.claude/telegram_model_stock"),
        "thinking_file": os.path.expanduser("~/.claude/telegram_thinking_stock"),
        "work_dir":      "/mnt/d/cao_stock",    # 股票工作区
        "secret":        "",                    # webhook secret_token，main() 启动时随机生成
    },
}
WECHAT_SEND_FILE = "/tmp/wechat_send.json"
WECHAT_BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_bot.py")
TDX_MONITOR_SCRIPT = "/mnt/d/cao_stock/scripts/monitor/price_monitor.py"
TDX_SIGNAL_GEN_SCRIPT = "/mnt/d/cao_stock/scripts/broker/ths_trade_signal.py"
TDX_FORMULA_SCRIPT = "/mnt/d/cao_stock/scripts/tools/ths_formula.py"
THS_READER_SCRIPT = "/mnt/d/cao_stock/scripts/data/ths_reader.py"
THS_FORMULA_SCRIPT = "/mnt/d/cao_stock/scripts/tools/ths_formula.py"
STOCK_SYNC_SCRIPT = "/mnt/d/cao_stock/scripts/data/ths_reader.py"
THS_ORDER_MONITOR_SCRIPT = "/mnt/d/cao_stock/scripts/monitor/ths_order_monitor.py"
STOCK_WATCHLIST_FILE = "/mnt/d/cao_stock/data/my_watchlist.json"
API_KEYS_FILE    = os.path.expanduser("~/.claude/telegram_api_keys.json")
TOKEN_STATS_FILE = os.path.expanduser("~/.claude/telegram_token_stats.json")

_wechat_proc = None   # wechat_bot.py subprocess
_tdx_proc = None      # tdx_monitor.py subprocess
_order_proc = None    # ths_order_monitor.py subprocess

ANTHROPIC_PROXY_URL = "http://localhost:4001"


MODELS = [
    # (model_id, display_label, provider, has_thinking)
    ("claude-fable-5",            "Fable 5 — 最新旗舰",       "claude",   True),
    ("claude-opus-4-8",           "Opus 4.8 — 最强 Opus",     "claude",   True),
    ("claude-sonnet-5",         "Sonnet 5 — 均衡",        "claude",   True),
    ("claude-haiku-4-5-20251001", "Haiku 4.5 — 最快",         "claude",   False),
    ("deepseek-v4-flash",         "DeepSeek V4 Flash — 经济",  "deepseek", True),
    ("deepseek-v4-pro",           "DeepSeek V4 Pro — 旗舰",    "deepseek", True),
    ("glm-4-plus",                "GLM-4 Plus — 均衡",        "zhipu",    False),
    ("glm-4-flash",               "GLM-4 Flash — 快速免费",   "zhipu",    False),
    ("abab6.5s-chat",             "MiniMax 6.5s",             "minimax",  False),
    ("qwen-max",                  "通义千问 Max",              "bailian",  False),
    ("qwen-plus",                 "通义千问 Plus",             "bailian",  False),
]

PROVIDERS = {
    "claude":   ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
    "zhipu":    ["glm-4-plus", "glm-4-flash"],
    "minimax":  ["abab6.5s-chat"],
    "bailian":  ["qwen-max", "qwen-plus", "qwen-turbo"],
}


# Non-Claude models need a Claude-style alias for Claude Code CLI to accept
# Each model gets a UNIQUE alias so LiteLLM can route correctly
CLI_MODEL_ALIAS = {
    "deepseek-v4-flash": "claude-3-5-sonnet-20241022",
    "deepseek-v4-pro":   "claude-3-opus-20240229",
    "glm-4-plus":        "claude-3-sonnet-20240229",
    "glm-4-flash":       "claude-3-haiku-20240307",
    "abab6.5s-chat":     "claude-3-5-haiku-20241022",
    "qwen-max":          "claude-3-5-sonnet-latest",
    "qwen-plus":         "claude-3-opus-latest",
    "qwen-turbo":        "claude-3-haiku-20240307",
}


def get_cli_model(model):
    """Return the model name that Claude Code CLI should use."""
    return CLI_MODEL_ALIAS.get(model, model)


def get_model(model_file=None):
    f = model_file or MODEL_FILE
    if os.path.exists(f):
        m = open(f).read().strip()
        if m:
            return m
    return None


def set_model(model, model_file=None):
    f = model_file or MODEL_FILE
    with open(f, "w") as fp:
        fp.write(model)


def model_flag():
    m = get_model()
    return f" --model {m}" if m else ""


# ── 思考档位（reasoning effort）─────────────────────────────────────────────────
THINKING_FILE = os.path.expanduser("~/.claude/telegram_thinking")
# 档位 → MAX_THINKING_TOKENS 预算（仅对走 Claude Code CLI / Anthropic 端点的模型生效）
THINK_BUDGET = {"medium": 8000, "high": 16000, "xhigh": 24000, "max": 31999}
# 每个模型支持哪些思考档位（与 dashboard.py 的 _MODEL_THINK 保持一致）
MODEL_THINK = {
    "claude-fable-5":            ["medium", "high", "xhigh", "max"],
    "claude-opus-4-8":           ["medium", "high", "xhigh", "max"],
    "claude-sonnet-5":         ["medium", "high", "xhigh", "max"],
    "claude-haiku-4-5-20251001": [],
    "deepseek-v4-pro":           ["medium", "high", "xhigh", "max"],
    "deepseek-v4-flash":         ["medium", "high"],
}


def get_thinking(thinking_file=None):
    f = thinking_file or THINKING_FILE
    if os.path.exists(f):
        try:
            return open(f).read().strip()
        except Exception:
            pass
    return ""


def think_env_prefix(model, level):
    """返回该模型+档位对应的 MAX_THINKING_TOKENS 环境变量前缀；不支持则空串。"""
    level = (level or "").strip()
    if level and level in MODEL_THINK.get(model, []) and level in THINK_BUDGET:
        return f"MAX_THINKING_TOKENS={THINK_BUDGET[level]} "
    return ""


# DeepSeek 有原生 Anthropic 兼容端点，直连（真实 key + 真实 model 名）
# GLM/MiniMax/百炼 通过本地 anthropic_proxy（端口 4001）转发，proxy 直接路由到各厂商
NATIVE_ANTHROPIC_BASE = {
    "deepseek": "https://api.deepseek.com/anthropic",
    "zhipu":    "http://localhost:4001",
    "minimax":  "http://localhost:4001",
    "bailian":  "http://localhost:4001",
}


def claude_launch_cmd(model=None, extra_args="", thinking=None):
    """Build the full claude launch command, with ANTHROPIC_BASE_URL for non-Claude models.

    thinking: 思考档位 id（medium/high/xhigh/max）；对支持的模型注入 MAX_THINKING_TOKENS。"""
    m = model or get_model() or "claude-fable-5"
    provider = get_provider(m)
    think_pre = think_env_prefix(m, thinking)
    if provider == "claude":
        return f"{think_pre}claude --dangerously-skip-permissions --model {m}{extra_args}"
    if provider in NATIVE_ANTHROPIC_BASE:
        base = NATIVE_ANTHROPIC_BASE[provider]
        if "localhost" in base:
            # 本地代理（GLM/MiniMax/百炼）：placeholder key + CLI 别名，proxy 负责转发到真实厂商
            _approve_custom_key("sk-placeholder")
            cli_model = get_cli_model(m)
            return f"{think_pre}ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL={base} claude --dangerously-skip-permissions --model {cli_model}{extra_args}"
        else:
            # 原生 Anthropic 端点（DeepSeek）：真实 key + 真实 model 名，直连厂商
            key = get_api_keys().get(provider, "")
            if key:
                _approve_custom_key(key)
                return f"{think_pre}ANTHROPIC_API_KEY={key} ANTHROPIC_BASE_URL={base} claude --dangerously-skip-permissions --model {m}{extra_args}"
    # 兜底（不应走到这里）
    _approve_custom_key("sk-placeholder")
    cli_model = get_cli_model(m)
    return f"ANTHROPIC_API_KEY=sk-placeholder ANTHROPIC_BASE_URL={ANTHROPIC_PROXY_URL} claude --dangerously-skip-permissions --model {cli_model}{extra_args}"


# ── Multi-provider support ────────────────────────────────────────────────────

def get_provider(model):
    for provider, models in PROVIDERS.items():
        if model in models:
            return provider
    return "claude"


def get_api_keys():
    defaults = {
        "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
        "zhipu":    os.environ.get("ZHIPU_API_KEY", ""),
        "minimax":  os.environ.get("MINIMAX_API_KEY", ""),
        "bailian":  os.environ.get("BAILIAN_API_KEY", ""),
    }
    if os.path.exists(API_KEYS_FILE):
        try:
            stored = json.load(open(API_KEYS_FILE))
            defaults.update({k: v for k, v in stored.items() if v})
        except Exception:
            pass
    return defaults


# Token 统计由 hooks/send-to-telegram.py 写入（per-bot 分桶格式），
# dashboard.get_status 读取；bridge 不再直接读写。

BOT_COMMANDS = [
    {"command": "clear", "description": "Clear conversation"},
    {"command": "resume", "description": "Resume session (shows picker)"},
    {"command": "loop", "description": "Ralph Loop: /loop <prompt>"},
    {"command": "stop", "description": "Interrupt Claude (Escape)"},
    {"command": "status", "description": "Check tmux status"},
    {"command": "model", "description": "Switch Claude model"},
    {"command": "restart", "description": "Restart bridge.py"},
    {"command": "relaunch", "description": "Relaunch Claude Code in tmux"},
    {"command": "wechat", "description": "微信控制: start/stop/send <to> <msg>"},
    {"command": "gp", "description": "股票: 扫描/自选/同步/监控/加/删"},
]

ALLOWED_CHAT_IDS = {5821678806}

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/settings", "/config", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]


def telegram_api(method, data, token=None):
    t = token or BOT_TOKEN
    if not t:
        return None
    body = json.dumps(data).encode()
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{t}/{method}",
            data=body,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="ignore")
            # 4xx 是请求本身的错，重试也是同样结果；5xx 重试
            if 400 <= e.code < 500:
                print(f"Telegram API error [{method}]: {e} | {err_body}")
                return None
            last_err = f"HTTP {e.code}: {err_body[:200]}"
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))  # 0.8s, 1.6s 退避
    print(f"Telegram API failed [{method}] after 3 attempts: {last_err}")
    return None


def setup_bot_commands():
    result = telegram_api("setMyCommands", {"commands": BOT_COMMANDS})
    if result and result.get("ok"):
        print("Bot commands registered")


def send_typing_loop(chat_id, token=None, pending_file=None):
    pf = pending_file or PENDING_FILE
    while True:
        # Atomic heartbeat: open existing file with r+ — if hook just deleted it,
        # FileNotFoundError is raised and we exit instead of resurrecting the file.
        try:
            with open(pf, "r+") as f:
                f.seek(0)
                f.truncate()
                f.write(str(int(time.time())))
        except FileNotFoundError:
            return
        except Exception:
            pass
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, token=token)
        time.sleep(4)


# ── 回复轮询（替代不稳定的 Stop 钩子）────────────────────────────────────────
# Claude Code 2.1.x 的 Stop 钩子时灵时不灵（"Failed with non-blocking status
# code: No stderr output"，脚本根本不执行）。这里 bridge 自己轮询每个 bot 的
# transcript：检测到回合结束（transcript 写入停止 + 最后一条是 assistant 文本）
# 就调用 send-to-telegram.py 把回复发出去，不再依赖那个钩子。
HOOK_SCRIPT     = os.path.expanduser("~/.claude/hooks/send-to-telegram.py")
# 语音转写脚本（whisper small）：收到 Telegram 语音消息时 subprocess 调用
VOICE_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_transcribe.py")
POLL_INTERVAL   = 2      # 轮询间隔（秒）
REPLY_IDLE_SECS = 4      # transcript 多少秒不变才算回合结束
_poll_last_mtime = {}    # tmux_session -> 已处理过的 transcript mtime（防重发）


def _session_cwd(session):
    """读 tmux session 里 Claude 进程的真实 cwd（决定 transcript 落在哪个 projects 子目录）。"""
    try:
        r = subprocess.run(["tmux", "list-panes", "-t", f"{session}:0.0", "-F", "#{pane_pid}"],
                           capture_output=True, text=True)
        pid = r.stdout.strip().split()[0]
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def _transcript_dir(profile):
    """该 bot 的 Claude transcript 目录（~/.claude/projects/<编码后的 cwd>）。
    用 session 的真实 cwd 而非配置值，避免 session 启动时 cwd 与配置不一致导致找错目录。"""
    cwd = (_session_cwd(profile.get("tmux_session"))
           or profile.get("work_dir") or "/mnt/d/AI/claudecode-telegram-main")
    enc = re.sub(r'[^a-zA-Z0-9]', '-', cwd)   # Claude Code 把 cwd 非字母数字都转成 '-'
    return os.path.expanduser(f"~/.claude/projects/{enc}")


def _newest_transcript(profile):
    d = _transcript_dir(profile)
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
        return max(files, key=os.path.getmtime) if files else None
    except Exception:
        return None


def _last_is_assistant_text(jsonl):
    """最后一条有意义的消息是否为 assistant 文本（= 回合真的产出了回复）。"""
    try:
        lines = open(jsonl, encoding="utf-8").readlines()
    except Exception:
        return False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t == "assistant":
            for b in o.get("message", {}).get("content", []):
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    return True
            return False            # assistant 但只有 tool_use → 回合还没结束
        if t == "user":
            return False            # 最后是 user → 还没回复
    return False


def _poll_reply(profile):
    pf = profile.get("pending_file")
    if not pf or not os.path.exists(pf):
        return                      # 没有待回复的请求
    j = _newest_transcript(profile)
    if not j:
        return
    mt = os.path.getmtime(j)
    sess = profile["tmux_session"]
    if time.time() - mt < REPLY_IDLE_SECS:
        return                      # transcript 还在写，回合未结束
    if mt <= _poll_last_mtime.get(sess, 0):
        return                      # 没有新内容（已处理过的旧回复）
    if not _last_is_assistant_text(j):
        return                      # 回合结束在 tool_use/user，没有可发的文本
    _poll_last_mtime[sess] = mt
    try:
        subprocess.run(["python3", HOOK_SCRIPT],
                       input=json.dumps({"transcript_path": j}).encode(),
                       timeout=30)
        print(f"[poller] {profile['name']} 回合结束，已触发回复发送")
    except Exception as e:
        print(f"[poller] {profile['name']} 发送失败: {e}")


def reply_poller():
    # 启动时把现有 transcript 标记为已处理，避免误发上一轮的旧回复
    for prof in BOT_PROFILES.values():
        if not prof.get("token"):
            continue
        j = _newest_transcript(prof)
        _poll_last_mtime[prof["tmux_session"]] = os.path.getmtime(j) if j else 0
    while True:
        time.sleep(POLL_INTERVAL)
        for prof in BOT_PROFILES.values():
            if not prof.get("token"):
                continue
            try:
                _poll_reply(prof)
            except Exception as e:
                print(f"[poller] error: {e}")


def tmux_exists(session=None):
    # 精确匹配，避免 tmux has-session 的前缀匹配把 "claude" 误判为存在
    # （当只有 "claude_stock" 时，has-session -t claude 也会返回 0）
    s = session or TMUX_SESSION
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return s in r.stdout.split()


def _pane(session=None):
    """全限定 pane 目标 'session:0.0'。
    必须用全限定写法：三个 session 的窗口都叫 'claude'（tmux 用启动命令名命名窗口），
    且 'claude' 是 'claude_feishu'/'claude_stock' 的前缀，无冒号的 '-t claude' 会被
    tmux 解析成"最近活跃的名为 claude 的窗口"，导致主控消息误投到飞书/股票 session。"""
    return f"{session or TMUX_SESSION}:0.0"


def tmux_send(text, literal=True, session=None):
    s = session or TMUX_SESSION
    if not tmux_exists(s):
        return
    cmd = ["tmux", "send-keys", "-t", _pane(s)]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd)


def tmux_send_enter(session=None):
    s = session or TMUX_SESSION
    if not tmux_exists(s):
        return
    subprocess.run(["tmux", "send-keys", "-t", _pane(s), "Enter"])


def tmux_send_with_enter(text, session=None):
    """Send text + Enter reliably using tmux buffer paste.

    Uses a per-session named buffer + `-d` (delete after paste) so concurrent
    sends to different sessions cannot clobber each other's payloads.
    """
    s = session or TMUX_SESSION
    if not tmux_exists(s):
        return
    buf = f"tg_{s}_{threading.get_ident()}"
    subprocess.run(["tmux", "load-buffer", "-b", buf, "-"], input=text.encode())
    subprocess.run(["tmux", "paste-buffer", "-d", "-b", buf, "-t", _pane(s)])
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", _pane(s), "Enter"])


CLAUDE_JSON_PATH = os.path.expanduser("~/.claude.json")


def _approve_custom_key(api_key):
    """预写 ~/.claude.json 的 approved 列表，让 Claude CLI 不再弹 'Do you want to use this API key?'。
    CLI 用 key 的后 20 位做标识。"""
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


def tmux_send_escape(session=None):
    s = session or TMUX_SESSION
    if not tmux_exists(s):
        return
    subprocess.run(["tmux", "send-keys", "-t", _pane(s), "Escape"])


def get_recent_sessions(limit=5):
    """Return the most-recent N *distinct* sessions (one entry per sessionId).
    Each history.jsonl line is a single user input, so without dedup the picker
    would show 5 prompts from the same session instead of 5 different sessions."""
    if not os.path.exists(HISTORY_FILE):
        return []
    entries = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                try:
                    entries.append(json.loads(line.strip()))
                except:
                    continue
    except:
        return []
    entries.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    seen = set()
    result = []
    for e in entries:
        sid = e.get("sessionId")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        result.append(e)
        if len(result) >= limit:
            break
    return result


def get_session_id(project_path):
    encoded = project_path.replace("/", "-").lstrip("-")
    for prefix in [f"-{encoded}", encoded]:
        project_dir = Path.home() / ".claude" / "projects" / prefix
        if project_dir.exists():
            jsonls = list(project_dir.glob("*.jsonl"))
            if jsonls:
                return max(jsonls, key=lambda p: p.stat().st_mtime).stem
    return None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Only the exact webhook paths (/, /stock, ...) are Telegram webhooks.
        # Everything else — including /api/* coming from the dashboard frontend —
        # gets reverse-proxied to dashboard.py on localhost:8888.
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path not in BOT_PROFILES:
            return self._proxy_to_dashboard("POST")

        self.profile = BOT_PROFILES[path]
        self.bot_token = self.profile.get("token") or BOT_TOKEN
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        # Verify Telegram's secret_token header so forged POSTs to our public URL are dropped.
        # Telegram echoes the secret we passed at setWebhook in every legitimate request.
        expected_secret = self.profile.get("secret", "")
        if expected_secret:
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != expected_secret:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unauthorized")
                return
        try:
            update = json.loads(body)
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update)
        except Exception as e:
            print(f"Error: {e}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        path = self.path.split("?")[0]
        # /health is the watchdog probe — keep it served locally so the watchdog
        # can verify the bridge is alive regardless of dashboard state.
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Claude-Telegram Bridge")
            return
        # Everything else is for the mobile app / browser → proxy to dashboard.
        self._proxy_to_dashboard("GET")

    DASHBOARD_HOST = "127.0.0.1"
    DASHBOARD_PORT = 8888

    def _proxy_to_dashboard(self, method):
        """Reverse-proxy the current request to dashboard.py on localhost:8888.

        Streams the response in 8 KiB chunks so SSE (`/api/logs`) keeps working
        end-to-end and clients see events as they happen instead of after
        connection close. Requires ThreadingHTTPServer so a long-lived SSE
        connection doesn't block other requests.
        """
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        # Forward headers but rewrite Host and drop hop-by-hop/length headers
        fwd_headers = {}
        for h in self.headers:
            lh = h.lower()
            if lh in ("host", "content-length", "connection", "keep-alive",
                      "proxy-authenticate", "proxy-authorization", "te",
                      "trailers", "transfer-encoding", "upgrade"):
                continue
            fwd_headers[h] = self.headers[h]
        fwd_headers["Host"] = f"{self.DASHBOARD_HOST}:{self.DASHBOARD_PORT}"

        # SSE needs a long timeout; everything else is fast
        is_sse_request = "text/event-stream" in self.headers.get("Accept", "")
        timeout = 300 if is_sse_request else 30

        try:
            conn = http.client.HTTPConnection(
                self.DASHBOARD_HOST, self.DASHBOARD_PORT, timeout=timeout
            )
            conn.request(method, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(f"dashboard proxy error: {e}".encode())
            except Exception:
                pass
            return

        try:
            self.send_response(resp.status)
            for h, v in resp.getheaders():
                # Skip hop-by-hop headers; let our server set Connection/Encoding.
                if h.lower() in ("transfer-encoding", "connection",
                                 "keep-alive", "proxy-authenticate",
                                 "proxy-authorization", "te", "trailers",
                                 "upgrade"):
                    continue
                self.send_header(h, v)
            self.end_headers()

            # SSE 必须用 read1：read(8192) 会阻塞攒满 8KB 才返回，
            # 小事件被缓冲导致前端 3s 内收不到 ping 而误判 SSE 不可用
            is_sse_resp = "text/event-stream" in (resp.getheader("Content-Type") or "")
            while True:
                chunk = resp.read1(8192) if is_sse_resp else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected (typical for SSE) — nothing to do.
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def handle_callback(self, cb):
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")}, token=self.bot_token)

        if chat_id not in ALLOWED_CHAT_IDS:
            return

        if data.startswith("model:"):
            chosen = data.split(":", 1)[1]
            set_model(chosen, self.profile.get("model_file"))
            label    = next((l for m, l, *_ in MODELS if m == chosen), chosen)
            provider = get_provider(chosen)
            # Relaunch is slow (~3s tmux dance) — never run on the HTTP handler thread
            profile = self.profile
            token   = self.bot_token
            self.reply(chat_id, f"[{profile['name']}] 已切换到 {label}（正在重启 Claude...）")
            threading.Thread(
                target=self._relaunch_for_profile,
                args=(chat_id, chosen, profile, token),
                daemon=True
            ).start()
            return

        # Commands below require this bot's tmux session
        sess = self.profile.get("tmux_session", TMUX_SESSION)
        if not tmux_exists(sess):
            self.reply(chat_id, "tmux session not found")
            return

        if data.startswith("stock_watch:"):
            code = data.split(":", 1)[1]
            self._stock_add_watchlist(chat_id, code)
            return

        if data.startswith("stock_analyze:"):
            code = data.split(":", 1)[1]
            self._stock_trigger_analysis(chat_id, code)
            return

        if data.startswith("stock_dismiss:"):
            code = data.split(":", 1)[1]
            self.reply(chat_id, f"已忽略 {code}")
            return

        if data.startswith("trade_approve:"):
            order_id = data.split(":", 1)[1]
            self._handle_trade_approval(chat_id, order_id, action="approve")
            return

        if data.startswith("trade_reject:"):
            order_id = data.split(":", 1)[1]
            self._handle_trade_approval(chat_id, order_id, action="reject")
            return

        if data.startswith("trade_market:"):
            order_id = data.split(":", 1)[1]
            self._handle_trade_approval(chat_id, order_id, action="approve", market_price=True)
            return

        if data.startswith("resume:"):
            session_id = data.split(":", 1)[1]
            self.reply(chat_id, f"Resuming: {session_id[:8]}...")
            profile = self.profile
            token   = self.bot_token
            threading.Thread(
                target=self._do_resume,
                args=(chat_id, session_id, profile, token),
                daemon=True
            ).start()
            return

        if data.startswith("auq:"):
            # AskUserQuestion 按钮回调 — 方案 C
            # 格式：auq:{nonce}:{qi}:{oi|other}
            self._handle_auq_callback(chat_id, data, cb)
            return

    # ── AskUserQuestion 按钮回调处理 ───────────────────────────────────────
    def _handle_auq_callback(self, chat_id, data, cb):
        try:
            _, nonce, qi_s, oi_s = data.split(":", 3)
            qi = int(qi_s)
        except Exception:
            return

        state_file = os.path.expanduser(f"~/.claude/ask_pending/{nonce}.json")
        if not os.path.exists(state_file):
            self.reply(chat_id, "⏰ 这次问答已过期或被回收，请等太子重发")
            return

        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            return

        if state.get("cursor", 0) != qi:
            # 防重复点
            return

        questions = state.get("questions") or []
        if qi >= len(questions):
            return
        q = questions[qi]
        msg_id = cb.get("message", {}).get("message_id")

        # —— 老爸点了"自定义文本回复" ——
        if oi_s == "other":
            telegram_api("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reply_markup": {"inline_keyboard": []},
            }, token=self.bot_token)
            self.reply(chat_id, "✏️ 请直接打字回复（任何文字都会发给太子）")
            try:
                os.remove(state_file)
            except Exception:
                pass
            return

        # —— 老爸选了某个选项 ——
        try:
            oi = int(oi_s)
            opt = q["options"][oi]
        except Exception:
            return

        label = opt.get("label", "")
        desc = opt.get("description", "")
        answer_text = f"{label}（{desc}）" if desc else label
        state.setdefault("answers", []).append({
            "question": q.get("question", ""),
            "label": label,
            "description": desc,
        })
        state["cursor"] = qi + 1

        # 编辑原消息：去按钮 + 标注已选（纯文本，避免 Markdown 400）
        try:
            done_text = f"✅ 已选：{label}"
            telegram_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": (cb.get("message", {}).get("text", "") or q.get("question", "")) + f"\n\n{done_text}",
            }, token=self.bot_token)
        except Exception:
            pass

        # —— 还有下一题：推新按钮组 ——
        if state["cursor"] < len(questions):
            with open(state_file, "w") as f:
                json.dump(state, f, ensure_ascii=False)
            self._send_auq_next(chat_id, state)
            return

        # —— 全部答完：拼答案 paste 到 tmux ——
        if len(state["answers"]) == 1:
            payload = f"[Telegram 按钮回复] {state['answers'][0]['label']}"
        else:
            lines = ["[Telegram 按钮回复]"]
            for i, a in enumerate(state["answers"], 1):
                lines.append(f"Q{i}: {a['label']}")
            payload = "\n".join(lines)

        sess = state.get("tmux_session") or self.profile.get("tmux_session", TMUX_SESSION)
        if not tmux_exists(sess):
            self.reply(chat_id, f"⚠️ tmux session '{sess}' 不存在，答案没塞进去")
        else:
            # 关键：和 handle_message 一致，paste 前写 pending_file + 拉起 typing loop
            # 否则 Stop hook 看到 no pending file 会 skip，太子的回复推不到 Telegram
            pending_file = self.profile.get("pending_file", PENDING_FILE)
            chat_id_file = self.profile.get("chat_id_file", CHAT_ID_FILE)
            try:
                with open(chat_id_file, "w") as f:
                    f.write(str(chat_id))
            except Exception:
                pass
            try:
                with open(pending_file, "w") as f:
                    f.write(str(int(time.time())))
            except Exception:
                pass
            threading.Thread(
                target=send_typing_loop,
                args=(chat_id, self.bot_token, pending_file),
                daemon=True
            ).start()
            tmux_send_with_enter(payload, session=sess)
            self.reply(chat_id, "✅ 已转给太子")

        try:
            os.remove(state_file)
        except Exception:
            pass

    def _send_auq_next(self, chat_id, state):
        """推下一题的按钮组（纯文本）。"""
        questions = state["questions"]
        qi = state["cursor"]
        q = questions[qi]
        nonce = state["nonce"]
        lines = [f"🤔 太子在问老爸 (第 {qi+1}/{len(questions)} 题)", ""]
        lines.append(q["question"])
        lines.append("")
        for i, o in enumerate(q["options"]):
            if o.get("description"):
                lines.append(f"  {i+1}. {o['label']} — {o['description']}")
            else:
                lines.append(f"  {i+1}. {o['label']}")
        text = "\n".join(lines)

        kb_rows = []
        for i, o in enumerate(q["options"]):
            kb_rows.append([{
                "text": f"{i+1}. {o['label']}"[:60],
                "callback_data": f"auq:{nonce}:{qi}:{i}",
            }])
        kb_rows.append([{
            "text": "✏️ 自定义文本回复",
            "callback_data": f"auq:{nonce}:{qi}:other",
        }])

        resp = telegram_api("sendMessage", {
            "chat_id": int(chat_id),
            "text": text,
            "reply_markup": {"inline_keyboard": kb_rows},
        }, token=self.bot_token)
        if resp and resp.get("ok"):
            state_file = os.path.expanduser(f"~/.claude/ask_pending/{nonce}.json")
            state["message_id"] = resp["result"]["message_id"]
            with open(state_file, "w") as f:
                json.dump(state, f, ensure_ascii=False)

    def download_file(self, file_id, filename):
        """Download a file from Telegram by file_id, return local file path or None."""
        result = telegram_api("getFile", {"file_id": file_id}, token=self.bot_token)
        if not result or not result.get("ok"):
            return None
        file_path = result["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        safe_name = filename.replace("/", "_").replace("\\", "_")
        local_path = f"/tmp/{safe_name}"
        try:
            urllib.request.urlretrieve(url, local_path)
            return local_path
        except Exception as e:
            print(f"File download error: {e}")
            return None

    def download_photo(self, photo_list):
        """Download the largest photo from Telegram, return local file path or None."""
        largest = max(photo_list, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id")
        result = telegram_api("getFile", {"file_id": file_id}, token=self.bot_token)
        if not result or not result.get("ok"):
            return None
        file_path = result["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "jpg"
        # nanosecond timestamp avoids same-second collisions when batching photos
        local_path = f"/tmp/telegram_photo_{time.time_ns()}.{ext}"
        try:
            urllib.request.urlretrieve(url, local_path)
            return local_path
        except Exception as e:
            print(f"Photo download error: {e}")
            return None

    def handle_message(self, update):
        msg = update.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        photo = msg.get("photo")
        document = msg.get("document")
        voice = msg.get("voice") or msg.get("audio")
        caption = msg.get("caption", "")

        if not chat_id:
            return

        if chat_id not in ALLOWED_CHAT_IDS:
            self.reply(chat_id, "Unauthorized")
            return

        # Document message (PPT, Word, Excel, etc.)
        SUPPORTED_EXTS = {
            "pptx", "ppt", "pptm",
            "docx", "doc", "docm",
            "xlsx", "xls", "xlsm", "csv",
            "pdf", "txt"
        }
        _chat_id_file = self.profile.get("chat_id_file", CHAT_ID_FILE)
        _pending_file = self.profile.get("pending_file", PENDING_FILE)

        if document:
            filename = document.get("file_name", f"file_{int(time.time())}")
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in SUPPORTED_EXTS:
                self.reply(chat_id, f"不支持的文件类型：.{ext}")
                return
            local_path = self.download_file(document.get("file_id"), filename)
            if not local_path:
                self.reply(chat_id, "文件下载失败")
                return
            text = f"{caption}\n\n文件路径：{local_path}".strip() if caption else f"文件路径：{local_path}"

        # Photo message
        if photo:
            local_path = self.download_photo(photo)
            if not local_path:
                self.reply(chat_id, "图片下载失败")
                return
            text = f"{caption}\n\n图片路径：{local_path}".strip() if caption else f"图片路径：{local_path}"

        # Voice / audio message → whisper 转中文文字，当普通文字注入
        if voice:
            ogg = self.download_file(voice.get("file_id"), f"voice_{int(time.time())}.ogg")
            if not ogg:
                self.reply(chat_id, "语音下载失败")
                return
            self.reply(chat_id, "🎤 正在转写语音…")
            transcript = ""
            try:
                r = subprocess.run(["python3", VOICE_SCRIPT, ogg],
                                   capture_output=True, text=True, timeout=180)
                transcript = (r.stdout or "").strip()
                if not transcript and r.stderr:
                    print(f"[voice] {r.stderr.strip()[:200]}")
            except Exception as e:
                print(f"[voice] transcribe error: {e}")
            if not transcript:
                self.reply(chat_id, "语音转写失败，没识别出内容，换文字试试？")
                return
            self.reply(chat_id, f"🎤 听到：{transcript}")
            text = f"{caption}\n\n{transcript}".strip() if caption else transcript

        if not text:
            return

        with open(_chat_id_file, "w") as f:
            f.write(str(chat_id))

        if text.startswith("/"):
            cmd = text.split()[0].lower()

            if cmd == "/status":
                cur_model = get_model(self.profile.get("model_file")) or "claude-fable-5"
                cur_label = next((l for m, l, *_ in MODELS if m == cur_model), cur_model)
                cur_provider = get_provider(cur_model)
                sess = self.profile.get("tmux_session", TMUX_SESSION)
                tmux_status = "running" if tmux_exists(sess) else "not found"
                lines = [
                    f"[{self.profile['name']}]",
                    f"模型: {cur_label}",
                    f"Provider: {cur_provider}",
                    f"tmux '{sess}': {tmux_status}",
                ]
                self.reply(chat_id, "\n".join(lines))
                return

            if cmd == "/stop":
                sess  = self.profile.get("tmux_session", TMUX_SESSION)
                pfile = self.profile.get("pending_file", PENDING_FILE)
                if tmux_exists(sess):
                    tmux_send_escape(sess)
                if os.path.exists(pfile):
                    os.remove(pfile)
                self.reply(chat_id, "Interrupted")
                return

            if cmd == "/clear":
                sess  = self.profile.get("tmux_session", TMUX_SESSION)
                pfile = self.profile.get("pending_file", PENDING_FILE)
                if tmux_exists(sess):
                    tmux_send_escape(sess)
                    time.sleep(0.2)
                    tmux_send("/clear", session=sess)
                    tmux_send_enter(sess)
                self.reply(chat_id, "Cleared")
                return

            if cmd == "/loop":
                sess = self.profile.get("tmux_session", TMUX_SESSION)
                if not tmux_exists(sess):
                    self.reply(chat_id, "tmux not found")
                    return
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.reply(chat_id, "Usage: /loop <prompt>")
                    return
                prompt = parts[1].replace('"', '\\"')
                full = f'{prompt} Output <promise>DONE</promise> when complete.'
                with open(_pending_file, "w") as f:
                    f.write(str(int(time.time())))
                threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token, _pending_file), daemon=True).start()
                tmux_send(f'/ralph-loop:ralph-loop "{full}" --max-iterations 5 --completion-promise "DONE"', session=sess)
                time.sleep(0.3)
                tmux_send_enter(sess)
                self.reply(chat_id, "Ralph Loop started (max 5 iterations)")
                return

            if cmd == "/restart":
                self.reply(chat_id, "Bridge restarting...")
                # Record both chat_id AND token so post-restart notify uses the right bot
                with open(RESTART_NOTIFY_FILE, "w") as f:
                    f.write(f"{chat_id}\t{self.bot_token}")
                threading.Thread(target=self._do_restart, daemon=True).start()
                return

            if cmd == "/relaunch":
                self.reply(chat_id, "Relaunching Claude Code in tmux...")
                profile = self.profile
                token   = self.bot_token
                threading.Thread(
                    target=self._do_relaunch,
                    args=(chat_id, profile, token),
                    daemon=True,
                ).start()
                return

            # ── /gp 统一股票指令 + 兼容旧指令 /tdx /ths /stock ──
            if cmd in ("/gp", "/tdx", "/ths", "/stock"):
                self._handle_gp(chat_id, text, cmd)
                return

            if cmd == "/wechat":
                parts = text.split(maxsplit=2)
                sub = parts[1].lower() if len(parts) > 1 else "help"
                global _wechat_proc

                if sub == "start":
                    if _wechat_proc and _wechat_proc.poll() is None:
                        self.reply(chat_id, "微信机器人已经在运行中")
                    else:
                        self.reply(chat_id, "正在启动微信机器人...")
                        env = os.environ.copy()
                        _wechat_proc = subprocess.Popen(
                            [sys.executable, WECHAT_BOT_SCRIPT],
                            env=env, close_fds=True
                        )
                    return

                if sub == "stop":
                    if _wechat_proc and _wechat_proc.poll() is None:
                        _wechat_proc.terminate()
                        self.reply(chat_id, "微信机器人已停止")
                    else:
                        self.reply(chat_id, "微信机器人未在运行")
                    return

                if sub == "send":
                    # /wechat send 张三 你好
                    rest = parts[2] if len(parts) > 2 else ""
                    send_parts = rest.split(maxsplit=1)
                    if len(send_parts) < 2:
                        self.reply(chat_id, "用法：/wechat send <联系人> <消息>")
                        return
                    to, msg = send_parts
                    with open(WECHAT_SEND_FILE, "w") as f:
                        json.dump({"to": to, "msg": msg}, f)
                    self.reply(chat_id, f"指令已发出，等待微信机器人执行...")
                    return

                if sub == "status":
                    running = _wechat_proc and _wechat_proc.poll() is None
                    self.reply(chat_id, f"微信机器人：{'运行中 ✅' if running else '已停止 ❌'}")
                    return

                self.reply(chat_id,
                    "微信机器人命令：\n"
                    "/wechat start — 启动（发送二维码）\n"
                    "/wechat stop — 停止\n"
                    "/wechat status — 查看状态\n"
                    "/wechat send <联系人> <消息> — 发消息"
                )
                return

            if cmd == "/model":
                current = get_model(self.profile.get("model_file")) or "claude-fable-5"
                kb = [[{"text": f"{'✓ ' if current == m else ''}{label}", "callback_data": f"model:{m}"}] for m, label, *_ in MODELS]
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"[{self.profile['name']}] 当前模型：{current}\n选择新模型：",
                    "reply_markup": {"inline_keyboard": kb}
                }, token=self.bot_token)
                return

            if cmd == "/resume":
                sessions = get_recent_sessions()
                if not sessions:
                    self.reply(chat_id, "No sessions")
                    return
                kb = []
                for s in sessions:
                    # Prefer sessionId from history.jsonl; fall back to project-dir scan
                    sid = s.get("sessionId") or get_session_id(s.get("project", ""))
                    if sid:
                        kb.append([{"text": s.get("display", "?")[:40] + "...", "callback_data": f"resume:{sid}"}])
                if not kb:
                    self.reply(chat_id, "No resumable sessions")
                    return
                telegram_api("sendMessage", {"chat_id": chat_id, "text": "Select session:", "reply_markup": {"inline_keyboard": kb}}, token=self.bot_token)
                return

            if cmd in BLOCKED_COMMANDS:
                self.reply(chat_id, f"'{cmd}' not supported (interactive)")
                return

        # ── 自然语言股票快捷指令检测 ──
        if self._try_stock_shortcut(chat_id, text):
            return

        # Regular message
        bot_path = self.path.split("?")[0].rstrip("/") or "/"
        print(f"[{self.profile['name']}][{chat_id}] {text[:50]}...")

        # ── tmux bots: route through Claude Code CLI (per-bot session) ──
        tmux_sess    = self.profile.get("tmux_session", TMUX_SESSION)
        pending_file = self.profile.get("pending_file", PENDING_FILE)
        chat_id_file = self.profile.get("chat_id_file", CHAT_ID_FILE)
        model        = get_model(self.profile.get("model_file")) or "claude-fable-5"
        provider     = get_provider(model)

        # Write chat_id for this bot's hook to pick up
        with open(chat_id_file, "w") as f:
            f.write(str(chat_id))

        # Busy-guard: per-bot pending file
        if os.path.exists(pending_file):
            try:
                pt = int(open(pending_file).read().strip())
                if time.time() - pt < 600:
                    self.reply(chat_id, "⏳ Claude 还在处理上一条消息，请稍候再发（如要中断用 /stop）")
                    return
            except:
                try: os.remove(pending_file)
                except: pass

        with open(pending_file, "w") as f:
            f.write(str(int(time.time())))

        threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token, pending_file), daemon=True).start()

        if tmux_exists(tmux_sess):
            tmux_send_with_enter(text, session=tmux_sess)
        else:
            # 所有模型统一走 Claude Code tmux 模式，session 不存在则自动创建并启动
            work_dir = self.profile.get("work_dir")
            new_args = ["tmux", "new-session", "-d", "-s", tmux_sess]
            if work_dir:
                new_args += ["-c", work_dir]
            subprocess.run(new_args, capture_output=True)
            time.sleep(0.3)
            thinking_val = get_thinking(self.profile.get("thinking_file"))
            tmux_send_with_enter(claude_launch_cmd(model, thinking=thinking_val), session=tmux_sess)
            self.reply(chat_id, f"🚀 {model} 正在启动（Claude Code 模式），消息将在 5 秒后自动发送...")
            def _delayed_send(txt=text, sess=tmux_sess):
                time.sleep(5)
                tmux_send_with_enter(txt, session=sess)
            threading.Thread(target=_delayed_send, daemon=True).start()

    def _stock_add_watchlist(self, chat_id, code):
        """将股票加入自选列表文件"""
        try:
            watchlist = []
            if os.path.exists(STOCK_WATCHLIST_FILE):
                watchlist = json.load(open(STOCK_WATCHLIST_FILE, encoding="utf-8"))
            if code not in watchlist:
                watchlist.append(code)
                os.makedirs(os.path.dirname(STOCK_WATCHLIST_FILE), exist_ok=True)
                with open(STOCK_WATCHLIST_FILE, "w", encoding="utf-8") as f:
                    json.dump(watchlist, f, ensure_ascii=False, indent=2)
                self.reply(chat_id, f"✅ {code} 已加入自选列表（共 {len(watchlist)} 只）")
            else:
                self.reply(chat_id, f"{code} 已在自选列表中")
        except Exception as e:
            self.reply(chat_id, f"加入失败: {e}")

    def _stock_trigger_analysis(self, chat_id, code):
        """触发 Claude Code 对该股票进行完整技术分析"""
        sess = self.profile.get("tmux_session", TMUX_SESSION)
        pf   = self.profile.get("pending_file", PENDING_FILE)
        if not tmux_exists(sess):
            self.reply(chat_id, "tmux session not found")
            return
        self.reply(chat_id, f"🔍 正在让 Claude 分析 {code}...")
        with open(pf, "w") as f:
            f.write(str(int(time.time())))
        threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token, pf), daemon=True).start()
        prompt = f"帮我分析股票 {code} 的当前走势，包括均线、MACD、RSI、KDJ，给出操作建议"
        tmux_send_with_enter(prompt, session=sess)

    def _relaunch_for_profile(self, chat_id, model, profile=None, token=None):
        """Exit and relaunch the given bot's Claude session with the new model.
        Only touches the session/pending file/work_dir bound to `profile` —
        the other bot's session is untouched.

        `profile`/`token` are passed explicitly so this can be called from a
        background thread without depending on self.profile / self.bot_token,
        which are only valid for the duration of one HTTP request."""
        p = profile if profile is not None else self.profile
        sess         = p.get("tmux_session", TMUX_SESSION)
        pending_file = p.get("pending_file", PENDING_FILE)
        work_dir     = p.get("work_dir")
        thinking_file = p.get("thinking_file")
        model_file   = p.get("model_file")

        # 清残留 PENDING，否则切完模型再发消息会被 busy-guard 挡住
        if os.path.exists(pending_file):
            try: os.remove(pending_file)
            except: pass

        if tmux_exists(sess):
            # Escape 退出 Claude UI，C-c 中断任何 shell 前台进程
            tmux_send_escape(sess)
            time.sleep(0.2)
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-c"])
            time.sleep(0.3)
            tmux_send("/exit", session=sess)
            tmux_send_enter(sess)
            time.sleep(1.5)
            # 清掉 shell 当前行（防止历史里残留的 claude --resume 被错误回车）
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-c"])
            time.sleep(0.1)
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-u"])
            time.sleep(0.2)

        # Re-check: session may have died after /exit (tmux kills session when initial command exits)
        if not tmux_exists(sess):
            new_args = ["tmux", "new-session", "-d", "-s", sess]
            if work_dir:
                new_args += ["-c", work_dir]
            subprocess.run(new_args, capture_output=True)
            time.sleep(0.5)

        # 用 paste-buffer 送启动命令；claude_launch_cmd 已预写 approved key，不会弹确认
        thinking = get_thinking(thinking_file)
        # ensure the new model is reflected in claude_launch_cmd's lookup path
        if model_file and model:
            set_model(model, model_file)
        tmux_send_with_enter(claude_launch_cmd(model, thinking=thinking), session=sess)

    # ── 自然语言股票快捷指令 ──────────────────────────────────────────────

    def _try_stock_shortcut(self, chat_id, text):
        """检测自然语言中的股票快捷指令，匹配则执行并返回True，否则返回False"""
        import re
        t = text.strip()

        # "扫描" / "扫描信号" / "扫描 002371"
        m = re.match(r'^扫描(?:信号|一下)?(?:\s+(.+))?$', t)
        if m:
            self._handle_gp(chat_id, f"/gp 扫描 {m.group(1) or ''}".strip(), "/gp")
            return True

        # "自选" / "看自选" / "我的自选" / "自选股"
        if re.match(r'^(?:看|查看|我的)?自选(?:股|列表)?$', t):
            self._handle_gp(chat_id, "/gp 自选", "/gp")
            return True

        # "加自选 002371" / "加 002371" / "加自选 北方华创"
        m = re.match(r'^(?:加自选|加|添加)\s+(.+)$', t)
        if m:
            self._handle_gp(chat_id, f"/gp 加 {m.group(1)}", "/gp")
            return True

        # "删自选 002371" / "删 002371"
        m = re.match(r'^(?:删自选|删|移除)\s+(.+)$', t)
        if m:
            self._handle_gp(chat_id, f"/gp 删 {m.group(1)}", "/gp")
            return True

        # "同步自选" / "同步"
        if re.match(r'^同步(?:自选)?$', t):
            self._handle_gp(chat_id, "/gp 同步", "/gp")
            return True

        # "对比自选" / "对比"
        if re.match(r'^对比(?:自选)?$', t):
            self._handle_gp(chat_id, "/gp 对比", "/gp")
            return True

        # "开始监控" / "启动监控" / "开监控"
        if re.match(r'^(?:开始|启动|开)监控$', t):
            self._handle_gp(chat_id, "/gp 监控", "/gp")
            return True

        # "停止监控" / "关监控" / "停监控"
        if re.match(r'^(?:停止|关|停)监控$', t):
            self._handle_gp(chat_id, "/gp 停监控", "/gp")
            return True

        # "状态" / "监控状态" / "股票状态"
        if re.match(r'^(?:股票|监控)?状态$', t):
            self._handle_gp(chat_id, "/gp 状态", "/gp")
            return True

        # "生成公式" / "公式"
        if re.match(r'^(?:生成)?公式$', t):
            self._handle_gp(chat_id, "/gp 公式", "/gp")
            return True

        return False

    def _handle_gp(self, chat_id, text, original_cmd="/gp"):
        """统一股票指令处理器"""
        # 解析子命令：支持 /gp 扫描 002371 或 /tdx scan 002371 等
        parts = text.split(maxsplit=2)
        sub = parts[1].strip().lower() if len(parts) > 1 else "help"
        sub_arg = parts[2].strip() if len(parts) > 2 else ""

        # ── 中文别名映射 ──
        alias = {
            "扫描": "scan", "扫": "scan",
            "自选": "selfstock", "看自选": "selfstock", "列表": "selfstock",
            "加": "add", "添加": "add", "加自选": "add",
            "删": "del", "移除": "del", "删自选": "del",
            "同步": "sync",
            "对比": "diff", "比较": "diff",
            "监控": "start", "开监控": "start", "启动": "start",
            "停监控": "stopmon", "停止": "stopmon", "关监控": "stopmon",
            "状态": "status",
            "公式": "formula",
            "板块": "blocks",
            "查名": "name", "股票名": "name",
            # 英文保持兼容
            "scan": "scan", "selfstock": "selfstock", "self": "selfstock",
            "add": "add", "del": "del", "remove": "del",
            "sync": "sync", "diff": "diff", "show": "diff",
            "push": "push", "pull": "pull",
            "start": "start", "stop": "stopmon",
            "status": "status", "formula": "formula",
            "blocks": "blocks", "block": "blocks", "name": "name",
            "list": "selfstock", "trade": "trade",
        }
        sub = alias.get(sub, sub)

        global _tdx_proc, _order_proc

        # ── 扫描信号 ──
        if sub == "scan":
            self.reply(chat_id, "🔍 扫描信号中...")
            def do_scan():
                try:
                    cmd_args = [sys.executable, TDX_SIGNAL_GEN_SCRIPT]
                    if sub_arg:
                        cmd_args.extend(["--codes", sub_arg])
                    result = subprocess.run(cmd_args, capture_output=True, text=True, timeout=120)
                    signals_path = "/mnt/d/tongdaxin/T0001/export/signals.json"
                    sig_summary = "未发现新信号"
                    if os.path.exists(signals_path):
                        try:
                            data = json.load(open(signals_path, encoding="utf-8"))
                            sigs = data.get("signals", [])
                            if sigs:
                                lines = []
                                for s in sigs:
                                    icon = {"strong": "🔴", "medium": "🟡"}.get(s.get("strength"), "⚪")
                                    lines.append(f"{icon} {s['name']}: {s['signal_name']}")
                                sig_summary = "\n".join(lines)
                        except Exception:
                            sig_summary = "信号文件解析失败"
                    self.reply(chat_id, f"✅ {sig_summary}")
                except subprocess.TimeoutExpired:
                    self.reply(chat_id, "⏰ 扫描超时")
                except Exception as e:
                    self.reply(chat_id, f"❌ {e}")
            threading.Thread(target=do_scan, daemon=True).start()
            return

        # ── 查看自选 ──
        if sub == "selfstock":
            try:
                result = subprocess.run(
                    [sys.executable, THS_READER_SCRIPT, "selfstock"],
                    capture_output=True, text=True, timeout=10
                )
                self.reply(chat_id, result.stdout.strip() or "自选股为空")
            except Exception as e:
                self.reply(chat_id, f"查询失败: {e}")
            return

        # ── 添加自选（三端同步）──
        if sub == "add":
            if not sub_arg:
                self.reply(chat_id, "发送：加 002371 或 加 sz002371:北方华创")
                return
            self.reply(chat_id, f"➕ 添加 {sub_arg} ...")
            def do_add():
                try:
                    result1 = subprocess.run(
                        [sys.executable, TDX_SIGNAL_GEN_SCRIPT, "--add", sub_arg],
                        capture_output=True, text=True, timeout=10
                    )
                    code = sub_arg.split(":")[0]
                    result2 = subprocess.run(
                        [sys.executable, THS_READER_SCRIPT, "add", code],
                        capture_output=True, text=True, timeout=10
                    )
                    self.reply(chat_id, f"✅ 已添加到三端\n{result1.stdout.strip()}")
                except Exception as e:
                    self.reply(chat_id, f"添加失败: {e}")
            threading.Thread(target=do_add, daemon=True).start()
            return

        # ── 删除自选 ──
        if sub == "del":
            if not sub_arg:
                self.reply(chat_id, "发送：删 002371")
                return
            try:
                r1 = subprocess.run(
                    [sys.executable, TDX_SIGNAL_GEN_SCRIPT, "--remove", sub_arg],
                    capture_output=True, text=True, timeout=10
                )
                r2 = subprocess.run(
                    [sys.executable, THS_READER_SCRIPT, "remove", sub_arg],
                    capture_output=True, text=True, timeout=10
                )
                self.reply(chat_id, f"✅ 已从三端移除 {sub_arg}")
            except Exception as e:
                self.reply(chat_id, f"移除失败: {e}")
            return

        # ── 同步自选 ──
        if sub == "sync":
            self.reply(chat_id, "🔄 三端同步中...")
            def do_sync():
                try:
                    result = subprocess.run(
                        [sys.executable, STOCK_SYNC_SCRIPT, "sync"],
                        capture_output=True, text=True, timeout=60
                    )
                    self.reply(chat_id, f"✅ 同步完成\n{result.stdout[-1500:]}")
                except Exception as e:
                    self.reply(chat_id, f"同步失败: {e}")
            threading.Thread(target=do_sync, daemon=True).start()
            return

        # ── 对比自选 ──
        if sub == "diff":
            self.reply(chat_id, "🔍 对比三端...")
            def do_diff():
                try:
                    result = subprocess.run(
                        [sys.executable, STOCK_SYNC_SCRIPT, "show"],
                        capture_output=True, text=True, timeout=30
                    )
                    output = result.stdout.strip()[:3500]
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": f"<pre>{output}</pre>",
                        "parse_mode": "HTML"
                    })
                except Exception as e:
                    self.reply(chat_id, f"对比失败: {e}")
            threading.Thread(target=do_diff, daemon=True).start()
            return

        # ── push / pull ──
        if sub == "push":
            self.reply(chat_id, "📤 推送中...")
            def do_push():
                try:
                    result = subprocess.run([sys.executable, STOCK_SYNC_SCRIPT, "push"],
                                            capture_output=True, text=True, timeout=30)
                    self.reply(chat_id, result.stdout.strip() or "推送完成")
                except Exception as e:
                    self.reply(chat_id, f"推送失败: {e}")
            threading.Thread(target=do_push, daemon=True).start()
            return

        if sub == "pull":
            self.reply(chat_id, "📥 拉取中...")
            def do_pull():
                try:
                    result = subprocess.run([sys.executable, STOCK_SYNC_SCRIPT, "pull"],
                                            capture_output=True, text=True, timeout=30)
                    self.reply(chat_id, result.stdout.strip() or "拉取完成")
                except Exception as e:
                    self.reply(chat_id, f"拉取失败: {e}")
            threading.Thread(target=do_pull, daemon=True).start()
            return

        # ── 启动监控（信号+交易审批一起启动）──
        if sub == "start":
            msgs = []
            if not (_tdx_proc and _tdx_proc.poll() is None):
                env = os.environ.copy()
                _tdx_proc = subprocess.Popen(
                    [sys.executable, TDX_MONITOR_SCRIPT], env=env, close_fds=True)
                msgs.append("信号监控 ✅")
            else:
                msgs.append("信号监控已在运行")
            if not (_order_proc and _order_proc.poll() is None):
                env = os.environ.copy()
                _order_proc = subprocess.Popen(
                    [sys.executable, THS_ORDER_MONITOR_SCRIPT], env=env, close_fds=True)
                msgs.append("交易审批 ✅")
            else:
                msgs.append("交易审批已在运行")
            self.reply(chat_id, "🚀 监控已启动\n" + "\n".join(msgs))
            return

        # ── 停止监控 ──
        if sub == "stopmon":
            msgs = []
            if _tdx_proc and _tdx_proc.poll() is None:
                _tdx_proc.terminate()
                msgs.append("信号监控已停止")
            if _order_proc and _order_proc.poll() is None:
                _order_proc.terminate()
                msgs.append("交易审批已停止")
            self.reply(chat_id, "⏹ " + ("、".join(msgs) if msgs else "没有在运行的监控"))
            return

        # ── 状态 ──
        if sub == "status":
            tdx_running = _tdx_proc and _tdx_proc.poll() is None
            order_running = _order_proc and _order_proc.poll() is None
            # 读取监控列表
            wl_count = 0
            wl_names = []
            wl_path = "/mnt/d/cao_stock/data/watchlist.json"
            if os.path.exists(wl_path):
                try:
                    wl_data = json.load(open(wl_path, encoding="utf-8"))
                    wl_count = len(wl_data)
                    wl_names = [s.get("name", s["code"]) for s in wl_data[:8]]
                except Exception:
                    pass
            # 待确认订单
            pending_count = 0
            try:
                pf = "/mnt/d/cao_stock/data/pending_orders.json"
                if os.path.exists(pf):
                    pd_data = json.load(open(pf, encoding="utf-8"))
                    pending_count = sum(1 for o in pd_data.get("orders", []) if o.get("status") == "pending")
            except Exception:
                pass
            mode = "👤 手动确认" if order_running else "❌ 未启动"
            msg = (
                f"📊 股票状态\n\n"
                f"交易模式：{mode}\n"
                f"信号监控：{'✅ 运行中' if tdx_running else '❌ 停止'}\n"
                f"自选：{wl_count} 只"
            )
            if wl_names:
                msg += f" ({', '.join(wl_names)})"
            if pending_count:
                msg += f"\n待确认订单：{pending_count}"
            self.reply(chat_id, msg)
            return

        # ── 公式 ──
        if sub == "formula":
            self.reply(chat_id, "生成公式中...")
            def do_formula():
                try:
                    # 通达信公式
                    subprocess.run([sys.executable, TDX_FORMULA_SCRIPT, "all"],
                                   capture_output=True, text=True, timeout=30)
                    # 同花顺公式
                    subprocess.run([sys.executable, THS_FORMULA_SCRIPT, sub_arg or "all"],
                                   capture_output=True, text=True, timeout=30)
                    self.reply(chat_id, "✅ 通达信+同花顺公式已生成\n通达信: /mnt/d/cao_stock/output/tdx_formulas/")
                except Exception as e:
                    self.reply(chat_id, f"生成失败: {e}")
            threading.Thread(target=do_formula, daemon=True).start()
            return

        # ── 板块 ──
        if sub == "blocks":
            try:
                result = subprocess.run(
                    [sys.executable, THS_READER_SCRIPT, "blocks"],
                    capture_output=True, text=True, timeout=10
                )
                self.reply(chat_id, result.stdout.strip() or "无自定义板块")
            except Exception as e:
                self.reply(chat_id, f"查询失败: {e}")
            return

        # ── 查股票名 ──
        if sub == "name":
            if not sub_arg:
                self.reply(chat_id, "发送：查名 000001")
                return
            try:
                result = subprocess.run(
                    [sys.executable, THS_READER_SCRIPT, "name", sub_arg],
                    capture_output=True, text=True, timeout=10
                )
                self.reply(chat_id, result.stdout.strip())
            except Exception as e:
                self.reply(chat_id, f"查询失败: {e}")
            return

        # ── 兼容旧 /ths trade 指令 ──
        if sub == "trade":
            trade_sub = sub_arg.lower() if sub_arg else "status"
            if trade_sub == "start":
                self._handle_gp(chat_id, "/gp 监控", "/gp")
            elif trade_sub == "stop":
                self._handle_gp(chat_id, "/gp 停监控", "/gp")
            else:
                self._handle_gp(chat_id, "/gp 状态", "/gp")
            return

        # ── 帮助 ──
        self.reply(chat_id,
            "📈 股票指令（也可直接打中文）：\n\n"
            "扫描 — 扫描全部信号\n"
            "扫描 002371 — 扫描指定股票\n"
            "自选 — 查看自选股\n"
            "加 002371 — 添加自选（三端同步）\n"
            "删 002371 — 删除自选\n"
            "同步 — 三端自选同步\n"
            "对比 — 对比三端差异\n"
            "开监控 — 启动信号+交易监控(需确认)\n"
            "停监控 — 停止全部监控\n"
            "状态 — 查看监控状态\n"
            "公式 — 生成全部公式"
        )

    def _handle_trade_approval(self, chat_id, order_id, action="approve", market_price=False):
        """处理交易审批回调（确认/拒绝/改市价）"""
        ths_scripts = "/mnt/d/cao_stock/scripts"
        if ths_scripts not in sys.path:
            sys.path.insert(0, ths_scripts)
        try:
            from ths_order_monitor import approve_order, reject_order

            if action == "approve":
                execute_price = "zxjg" if market_price else None
                ok, msg = approve_order(order_id, execute_price=execute_price)
                if ok:
                    price_note = "（市价）" if market_price else ""
                    self.reply(chat_id, f"✅ 已确认{price_note}：{msg}\n同花顺将自动执行")
                else:
                    self.reply(chat_id, f"⚠️ 确认失败：{msg}")
            elif action == "reject":
                ok, msg = reject_order(order_id)
                if ok:
                    self.reply(chat_id, f"❌ {msg}")
                else:
                    self.reply(chat_id, f"⚠️ 拒绝失败：{msg}")
        except Exception as e:
            self.reply(chat_id, f"处理失败: {e}")

    def _do_resume(self, chat_id, session_id, profile=None, token=None):
        p = profile if profile is not None else self.profile
        sess          = p.get("tmux_session", TMUX_SESSION)
        pending_file  = p.get("pending_file", PENDING_FILE)
        work_dir      = p.get("work_dir")
        thinking_file = p.get("thinking_file")
        model_file    = p.get("model_file")

        # 清残留 PENDING
        if os.path.exists(pending_file):
            try: os.remove(pending_file)
            except: pass
        time.sleep(0.3)
        if tmux_exists(sess):
            tmux_send_escape(sess)
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-c"])
            time.sleep(0.5)
            tmux_send("/exit", session=sess)
            tmux_send_enter(sess)
            time.sleep(2.0)  # Wait for Claude to fully exit before launching resume
            # 清掉 shell 当前行
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-c"])
            time.sleep(0.1)
            subprocess.run(["tmux", "send-keys", "-t", _pane(sess), "C-u"])
            time.sleep(0.2)
        # Re-check: session may have died after /exit
        if not tmux_exists(sess):
            new_args = ["tmux", "new-session", "-d", "-s", sess]
            if work_dir:
                new_args += ["-c", work_dir]
            subprocess.run(new_args, capture_output=True)
            time.sleep(0.5)
        model = get_model(model_file) or "claude-fable-5"
        tmux_send_with_enter(
            claude_launch_cmd(model, extra_args=f" --resume {session_id}", thinking=get_thinking(thinking_file)),
            session=sess,
        )

    def _do_relaunch(self, chat_id, profile=None, token=None):
        """Kill current Claude Code process in tmux and start a fresh one."""
        p = profile if profile is not None else self.profile
        t = token   if token   is not None else self.bot_token
        time.sleep(0.3)
        cur_model = get_model(p.get("model_file")) or "claude-fable-5"
        self._relaunch_for_profile(chat_id, cur_model, profile=p, token=t)
        time.sleep(2)
        telegram_api("sendMessage", {"chat_id": chat_id, "text": f"[{p['name']}] Claude Code relaunched ✓"}, token=t)

    def _do_restart(self):
        time.sleep(0.3)
        # Use absolute paths so the subprocess doesn't depend on CWD or PATH
        python = sys.executable
        script = os.path.abspath(sys.argv[0])
        cwd = os.path.dirname(script)
        cmd = f"sleep 1 && exec {python} {script}"
        subprocess.Popen(["bash", "-c", cmd], env=os.environ.copy(),
                         close_fds=True, cwd=cwd)
        os._exit(0)

    def reply(self, chat_id, text):
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text}, token=self.bot_token)

    def log_message(self, *args):
        pass


# ── Localtunnel watchdog ──────────────────────────────────────────────────────

def _get_tunnel_url():
    # ngrok 通过本地 API 4040 取 URL（stdout 缓冲不可靠）
    try:
        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        for t in data.get("tunnels", []):
            url = t.get("public_url", "")
            if url.startswith("https://"):
                return url
    except Exception:
        pass
    return None


def _tunnel_alive(url):
    # GET / now reverse-proxies to the dashboard UI, so probe the dedicated
    # /health endpoint instead — it remains served by the bridge itself.
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/health",
                                     headers={"ngrok-skip-browser-warning": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return b"Claude-Telegram Bridge" in r.read(64)
    except Exception:
        return False


def _restart_tunnel_and_register():
    subprocess.run(["pkill", "-f", f"ngrok http {PORT}"], capture_output=True)
    time.sleep(1)
    open(LTLOG, "w").close()
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    subprocess.Popen(
        ["ngrok", "http", str(PORT), "--log", "stdout", "--log-format=logfmt"],
        stdout=open(LTLOG, "w"), stderr=subprocess.STDOUT, env=env
    )
    url = None
    for _ in range(30):
        time.sleep(1)
        url = _get_tunnel_url()
        if url:
            break
    if url:
        # Re-register webhook for EVERY configured bot, not just the main one.
        # Previously only the main token's webhook was updated, so the stock bot
        # silently stopped receiving messages after every tunnel restart.
        _register_all_webhooks(url)
        print(f"[watchdog] Webhooks re-registered on {url}")
        return url
    print("[watchdog] Could not get new tunnel URL")
    return None


def tunnel_watchdog():
    time.sleep(20)  # let bridge fully start first
    while True:
        url = _get_tunnel_url()
        if url and not _tunnel_alive(url):
            print(f"[watchdog] Tunnel dead ({url}), restarting...")
            new_url = _restart_tunnel_and_register()
            if not new_url:
                print("[watchdog] Tunnel restore failed, will retry in 30s")
        time.sleep(30)


def notify_restart_if_needed():
    if not os.path.exists(RESTART_NOTIFY_FILE):
        return
    try:
        raw = open(RESTART_NOTIFY_FILE).read().strip()
        os.remove(RESTART_NOTIFY_FILE)
        # New format: "chat_id\ttoken"; legacy fallback: bare chat_id (uses BOT_TOKEN)
        if "\t" in raw:
            cid_str, token = raw.split("\t", 1)
            chat_id = int(cid_str)
        else:
            chat_id = int(raw)
            token = None
        telegram_api("sendMessage", {"chat_id": chat_id, "text": "Bridge restarted successfully ✓"}, token=token)
    except Exception as e:
        print(f"Restart notify error: {e}")


_anthropic_proxy_proc = None

def _start_anthropic_proxy():
    """Start the Anthropic-to-OpenAI translation proxy as a subprocess."""
    global _anthropic_proxy_proc
    # Check if proxy is already running (e.g., started by start.sh)
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("localhost", 4001))
        s.close()
        print("[proxy] Anthropic proxy already running on port 4001")
        return
    except (ConnectionRefusedError, OSError):
        pass  # Not running, start it

    proxy_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_proxy.py")
    if not os.path.exists(proxy_script):
        print("[proxy] anthropic_proxy.py not found, non-Claude models won't work")
        return
    _anthropic_proxy_proc = subprocess.Popen(
        [sys.executable, proxy_script],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    print(f"[proxy] Anthropic proxy started (PID {_anthropic_proxy_proc.pid})")


def _register_all_webhooks(tunnel_url):
    """Register webhooks for all configured bots, passing each bot's secret_token."""
    for path, profile in BOT_PROFILES.items():
        token = profile.get("token", "")
        if not token:
            continue
        suffix = "" if path == "/" else path
        webhook_url = f"{tunnel_url}{suffix}"
        payload = {"url": webhook_url}
        secret = profile.get("secret", "")
        if secret:
            payload["secret_token"] = secret
        result = telegram_api("setWebhook", payload, token=token)
        ok = result and result.get("ok")
        print(f"  [{profile['name']}] webhook {webhook_url} → {'OK' if ok else 'FAIL'}")
        if ok:
            telegram_api("setMyCommands", {"commands": BOT_COMMANDS}, token=token)


def outbox_flush_loop():
    """第三道防线：定时补发各 Bot 的 outbox 积压。不依赖 Stop/poller 触发——
    即使老爸不再发消息，网络一恢复也会把之前没发出去的回复补发出去。
    走 hook 的 --flush-all（内部 per-bot flock，不会和正常发送并发重复）。"""
    while True:
        time.sleep(60)
        try:
            subprocess.run(
                ["python3", HOOK_SCRIPT, "--flush-all"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except Exception as e:
            print(f"[outbox] flush error: {e}")


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return

    # Fill runtime tokens + per-bot webhook secrets into BOT_PROFILES.
    # Telegram's secret_token allows [A-Za-z0-9_-], 1..256 chars; token_urlsafe gives that.
    BOT_PROFILES["/"]["token"]       = BOT_TOKEN
    BOT_PROFILES["/"]["secret"]      = secrets.token_urlsafe(32)
    BOT_PROFILES["/stock"]["token"]  = STOCK_BOT_TOKEN
    BOT_PROFILES["/stock"]["secret"] = secrets.token_urlsafe(32)

    notify_restart_if_needed()
    _start_anthropic_proxy()

    # Register webhooks for all bots
    tunnel_url = _get_tunnel_url()
    if tunnel_url:
        print(f"Tunnel: {tunnel_url}")
        _register_all_webhooks(tunnel_url)
    else:
        print("Tunnel not yet available, webhooks will be registered by start.sh")
        # Fallback: still register main bot via legacy path
        setup_bot_commands()

    print(f"Bridge on :{PORT} | tmux: {TMUX_SESSION}")
    print(f"Active bots: {[p['name'] for p in BOT_PROFILES.values() if p.get('token')]}")

    # 回复轮询线程：不依赖 Stop 钩子，回合结束后主动发回复
    threading.Thread(target=reply_poller, daemon=True).start()
    print("[poller] reply poller started (Stop-hook independent)")
    # outbox 补发线程：定时把没发出去的回复补发（第三道防线）
    threading.Thread(target=outbox_flush_loop, daemon=True).start()
    print("[outbox] flush loop started")
    try:
        # ThreadingHTTPServer so dashboard SSE streams don't block webhooks.
        ThreadingHTTPServer.allow_reuse_address = True
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
