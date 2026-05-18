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
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import hmac
import hashlib
import base64

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
        "token":         "",            # 运行时填充
        "system_prompt": None,          # 走全局 CLAUDE.md
        "direct_api":    False,         # 走 tmux → Claude Code
        "model":         None,          # 跟随用户 /model 设置
    },
    "/stock": {
        "name":          "股票Bot",
        "token":         "",            # 运行时填充
        "direct_api":    True,          # 独立对话，不走 tmux，完全隔离
        "model":         "deepseek-v4-pro",   # 默认用 DeepSeek，快且免费
        "system_prompt": (
            "你是专业A股量化投资分析师，具备深厚的技术分析、基本面分析和量化策略能力。\n"
            "【数据获取】优先用 akshare 库拉取实时/历史数据，不要捏造数据。\n"
            "【分析必含】K线走势、MACD、RSI14、KDJ、成交量、均线(MA5/10/20/60)、布林带。\n"
            "【操作建议】必须给出明确结论：买入 / 持有 / 减仓 / 卖出，并注明关键支撑位和压力位。\n"
            "【语言】始终用中文回复，数字保留2位小数。\n"
            "【态度】直接给结论，不要废话。"
        ),
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
_conv_history = {}    # key: "<bot_path>:<chat_id>" -> list of {"role": ..., "content": ...}

GLOBAL_CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
LITELLM_BASE_URL = "http://localhost:4000"
ANTHROPIC_PROXY_URL = "http://localhost:4001"


def load_global_context():
    """Read ~/.claude/CLAUDE.md as global system context."""
    try:
        if os.path.exists(GLOBAL_CLAUDE_MD):
            return open(GLOBAL_CLAUDE_MD, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


MODELS = [
    # (model_id, display_label, provider, has_thinking)
    ("claude-opus-4-7",           "Opus 4.7 — 最强",         "claude",   True),
    ("claude-sonnet-4-6",         "Sonnet 4.6 — 均衡",        "claude",   True),
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
    "claude":   ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
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


def get_model():
    if os.path.exists(MODEL_FILE):
        m = open(MODEL_FILE).read().strip()
        if m:
            return m
    return None


def set_model(model):
    with open(MODEL_FILE, "w") as f:
        f.write(model)


def model_flag():
    m = get_model()
    return f" --model {m}" if m else ""


# 厂商原生支持 Anthropic 格式的端点：直连，跳过 anthropic_proxy + LiteLLM
NATIVE_ANTHROPIC_BASE = {
    "deepseek": "https://api.deepseek.com/anthropic",
}


def claude_launch_cmd(model=None, extra_args=""):
    """Build the full claude launch command, with ANTHROPIC_BASE_URL for non-Claude models."""
    m = model or get_model() or "claude-opus-4-7"
    provider = get_provider(m)
    if provider == "claude":
        return f"claude --dangerously-skip-permissions --model {m}{extra_args}"
    # 原生 Anthropic 端点：直连厂商，model 用真名，key 用真 key
    if provider in NATIVE_ANTHROPIC_BASE:
        key = get_api_keys().get(provider, "")
        if key:
            _approve_custom_key(key)
            base = NATIVE_ANTHROPIC_BASE[provider]
            return f"ANTHROPIC_API_KEY={key} ANTHROPIC_BASE_URL={base} claude --dangerously-skip-permissions --model {m}{extra_args}"
    # 兜底：走本地 anthropic_proxy → LiteLLM
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


def _zhipu_jwt(api_key):
    id_, secret = api_key.split(".", 1)
    def b64(s):
        return base64.urlsafe_b64encode(s if isinstance(s, bytes) else s.encode()).rstrip(b"=").decode()
    header  = b64(json.dumps({"alg": "HS256", "sign_type": "SIGN"}, separators=(",", ":")))
    ts      = int(time.time() * 1000)
    payload = b64(json.dumps({"api_key": id_, "exp": ts + 3600000, "timestamp": ts}, separators=(",", ":")))
    sig     = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(sig)}"


def _call_openai_compat(base_url, api_key, model, messages):
    if not api_key:
        raise ValueError("API key not configured")
    data = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())
    content = result["choices"][0]["message"]["content"]
    usage   = result.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def call_direct_api(provider, model, messages):
    """Call non-Claude API directly. Returns (reply_text, in_tokens, out_tokens) or None."""
    keys = get_api_keys()
    try:
        if provider == "deepseek":
            return _call_openai_compat(
                "https://api.deepseek.com/v1",
                keys.get("deepseek", ""), model, messages)
        elif provider == "zhipu":
            token = _zhipu_jwt(keys.get("zhipu", ""))
            return _call_openai_compat(
                "https://open.bigmodel.cn/api/paas/v4",
                token, model, messages)
        elif provider == "minimax":
            return _call_openai_compat(
                "https://api.minimax.chat/v1",
                keys.get("minimax", ""), model, messages)
        elif provider == "bailian":
            return _call_openai_compat(
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                keys.get("bailian", ""), model, messages)
    except Exception as e:
        print(f"[API] {provider}/{model} error: {e}")
        return None


def update_token_stats(in_tokens, out_tokens):
    stats = {"input": 0, "output": 0}
    if os.path.exists(TOKEN_STATS_FILE):
        try:
            stats = json.load(open(TOKEN_STATS_FILE))
        except Exception:
            pass
    stats["input"]  = stats.get("input", 0)  + in_tokens
    stats["output"] = stats.get("output", 0) + out_tokens
    with open(TOKEN_STATS_FILE, "w") as f:
        json.dump(stats, f)


def get_token_stats():
    if os.path.exists(TOKEN_STATS_FILE):
        try:
            return json.load(open(TOKEN_STATS_FILE))
        except Exception:
            pass
    return {"input": 0, "output": 0}


def clear_conv_history(chat_id=None):
    if chat_id is None:
        _conv_history.clear()
    else:
        _conv_history.pop(str(chat_id), None)

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
            # 4xx 不重试（请求本身有错）
            err_body = e.read().decode(errors="ignore")
            print(f"Telegram API error [{method}]: {e} | {err_body}")
            return None
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


def send_typing_loop(chat_id, token=None):
    start = time.time()
    while os.path.exists(PENDING_FILE):
        if time.time() - start > 300:
            if os.path.exists(PENDING_FILE):
                os.remove(PENDING_FILE)
            return
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, token=token)
        time.sleep(4)


def tmux_exists():
    return subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0


def tmux_send(text, literal=True):
    cmd = ["tmux", "send-keys", "-t", TMUX_SESSION]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd)


def tmux_send_enter():
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"])


def tmux_send_with_enter(text):
    """Send text + Enter reliably using tmux buffer paste."""
    # Use load-buffer + paste-buffer to handle spaces and special chars
    subprocess.run(["tmux", "load-buffer", "-"], input=text.encode())
    subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION])
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"])


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


def tmux_send_escape():
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Escape"])


def get_recent_sessions(limit=5):
    if not os.path.exists(HISTORY_FILE):
        return []
    sessions = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                try:
                    sessions.append(json.loads(line.strip()))
                except:
                    continue
    except:
        return []
    sessions.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return sessions[:limit]


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
    def _get_profile(self):
        """Return bot profile matching the request path, fallback to '/'."""
        path = self.path.split("?")[0].rstrip("/") or "/"
        return BOT_PROFILES.get(path) or BOT_PROFILES.get("/")

    def do_POST(self):
        self.profile = self._get_profile()
        self.bot_token = self.profile.get("token") or BOT_TOKEN
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Bridge")

    def handle_callback(self, cb):
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")}, token=self.bot_token)

        if chat_id not in ALLOWED_CHAT_IDS:
            return

        if data.startswith("model:"):
            chosen = data.split(":", 1)[1]
            set_model(chosen)
            label    = next((l for m, l, *_ in MODELS if m == chosen), chosen)
            provider = get_provider(chosen)
            self._relaunch_claude_for_model(chat_id, chosen, provider)
            self.reply(chat_id, f"已切换到 {label}")
            return

        # Commands below require tmux
        if not tmux_exists():
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
            threading.Thread(target=self._do_resume, args=(chat_id, session_id), daemon=True).start()

    def download_file(self, file_id, filename):
        """Download a file from Telegram by file_id, return local file path or None."""
        result = telegram_api("getFile", {"file_id": file_id}, token=self.bot_token)
        if not result or not result.get("ok"):
            return None
        file_path = result["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        ext = filename.rsplit(".", 1)[-1] if "." in filename else file_path.rsplit(".", 1)[-1]
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
        local_path = f"/tmp/telegram_photo_{int(time.time())}.{ext}"
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
        if document:
            filename = document.get("file_name", f"file_{int(time.time())}")
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in SUPPORTED_EXTS:
                self.reply(chat_id, f"不支持的文件类型：.{ext}")
                return
            with open(CHAT_ID_FILE, "w") as f:
                f.write(str(chat_id))
            local_path = self.download_file(document.get("file_id"), filename)
            if not local_path:
                self.reply(chat_id, "文件下载失败")
                return
            text = f"{caption}\n\n文件路径：{local_path}".strip() if caption else f"文件路径：{local_path}"

        # Photo message
        if photo:
            with open(CHAT_ID_FILE, "w") as f:
                f.write(str(chat_id))
            local_path = self.download_photo(photo)
            if not local_path:
                self.reply(chat_id, "图片下载失败")
                return
            text = f"{caption}\n\n图片路径：{local_path}".strip() if caption else f"图片路径：{local_path}"

        if not text or not chat_id:
            return

        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))

        if text.startswith("/"):
            cmd = text.split()[0].lower()

            if cmd == "/status":
                cur_model = get_model() or "claude-opus-4-7"
                cur_label = next((l for m, l, *_ in MODELS if m == cur_model), cur_model)
                cur_provider = get_provider(cur_model)
                tmux_status = "running" if tmux_exists() else "not found"
                lines = [
                    f"模型: {cur_label}",
                    f"Provider: {cur_provider}",
                    f"tmux '{TMUX_SESSION}': {tmux_status}",
                ]
                self.reply(chat_id, "\n".join(lines))
                return

            if cmd == "/stop":
                if tmux_exists():
                    tmux_send_escape()
                if os.path.exists(PENDING_FILE):
                    os.remove(PENDING_FILE)
                self.reply(chat_id, "Interrupted")
                return

            if cmd == "/clear":
                bot_path = self.path.split("?")[0].rstrip("/") or "/"
                hist_key = f"{bot_path}:{chat_id}"
                _conv_history.pop(hist_key, None)
                if not self.profile.get("direct_api") and tmux_exists():
                    tmux_send_escape()
                    time.sleep(0.2)
                    tmux_send("/clear")
                    tmux_send_enter()
                self.reply(chat_id, "Cleared")
                return

            if cmd == "/loop":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.reply(chat_id, "Usage: /loop <prompt>")
                    return
                prompt = parts[1].replace('"', '\\"')
                full = f'{prompt} Output <promise>DONE</promise> when complete.'
                with open(PENDING_FILE, "w") as f:
                    f.write(str(int(time.time())))
                threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token), daemon=True).start()
                tmux_send(f'/ralph-loop:ralph-loop "{full}" --max-iterations 5 --completion-promise "DONE"')
                time.sleep(0.3)
                tmux_send_enter()
                self.reply(chat_id, "Ralph Loop started (max 5 iterations)")
                return

            if cmd == "/restart":
                self.reply(chat_id, "Bridge restarting...")
                with open(RESTART_NOTIFY_FILE, "w") as f:
                    f.write(str(chat_id))
                threading.Thread(target=self._do_restart, daemon=True).start()
                return

            if cmd == "/relaunch":
                self.reply(chat_id, "Relaunching Claude Code in tmux...")
                threading.Thread(target=self._do_relaunch, args=(chat_id,), daemon=True).start()
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
                current = get_model() or "claude-opus-4-7"
                kb = [[{"text": f"{'✓ ' if current == m else ''}{label}", "callback_data": f"model:{m}"}] for m, label, *_ in MODELS]
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"当前模型：{current}\n选择新模型：",
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
                    sid = get_session_id(s.get("project", ""))
                    if sid:
                        kb.append([{"text": s.get("display", "?")[:40] + "...", "callback_data": f"resume:{sid}"}])
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

        # ── direct_api bots: fully isolated conversation, never touch tmux ──
        if self.profile.get("direct_api"):
            model    = self.profile.get("model") or get_model() or "deepseek-v4-pro"
            provider = get_provider(model)
            threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token), daemon=True).start()
            threading.Thread(
                target=self._call_api_and_reply,
                args=(chat_id, text, model, provider, bot_path),
                daemon=True,
            ).start()
            return

        # ── tmux bots: route through Claude Code CLI ──
        model    = get_model() or "claude-opus-4-7"
        provider = get_provider(model)

        # Busy-guard: if Claude is still processing previous message, reject to avoid tmux queue pile-up
        if os.path.exists(PENDING_FILE):
            try:
                pt = int(open(PENDING_FILE).read().strip())
                if time.time() - pt < 600:
                    self.reply(chat_id, "⏳ Claude 还在处理上一条消息，请稍候再发（如要中断用 /stop）")
                    return
            except:
                try: os.remove(PENDING_FILE)
                except: pass

        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token), daemon=True).start()

        if tmux_exists():
            tmux_send_with_enter(text)
        elif provider != "claude":
            threading.Thread(
                target=self._call_api_and_reply,
                args=(chat_id, text, model, provider, bot_path),
                daemon=True,
            ).start()
        else:
            self.reply(chat_id, "tmux not found, 请先用 /relaunch 启动 Claude Code")
            if os.path.exists(PENDING_FILE):
                os.remove(PENDING_FILE)

    def _call_api_and_reply(self, chat_id, text, model, provider, bot_path="/"):
        hist_key = f"{bot_path}:{chat_id}"
        history = _conv_history.setdefault(hist_key, [])
        history.append({"role": "user", "content": text})
        global_ctx = load_global_context()
        # Merge: bot system_prompt overrides global CLAUDE.md for specialised bots
        sys_prompt = self.profile.get("system_prompt") or global_ctx
        messages = ([{"role": "system", "content": sys_prompt}] if sys_prompt else []) + list(history)
        result = call_direct_api(provider, model, messages)
        # Only remove PENDING_FILE for tmux-routed bots (direct_api bots never set it)
        if not self.profile.get("direct_api") and os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)
        if result:
            reply_text, in_tok, out_tok = result
            history.append({"role": "assistant", "content": reply_text})
            if len(history) > 40:
                history[:] = history[-40:]
            update_token_stats(in_tok, out_tok)
            self.reply(chat_id, reply_text)
        else:
            self.reply(chat_id, "❌ API 调用失败，请检查 Key 配置")

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
        if not tmux_exists():
            self.reply(chat_id, "tmux session not found")
            return
        self.reply(chat_id, f"🔍 正在让 Claude 分析 {code}...")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))
        threading.Thread(target=send_typing_loop, args=(chat_id, self.bot_token), daemon=True).start()
        prompt = f"帮我分析股票 {code} 的当前走势，包括均线、MACD、RSI、KDJ，给出操作建议"
        tmux_send_with_enter(prompt)

    def _relaunch_claude_for_model(self, chat_id, model, provider):
        """Exit current Claude CLI and relaunch with the new model. Creates tmux session if needed."""
        # 清残留 PENDING，否则切完模型再发消息会被 busy-guard 挡住
        if os.path.exists(PENDING_FILE):
            try: os.remove(PENDING_FILE)
            except: pass

        if tmux_exists():
            # Escape 退出 Claude UI，C-c 中断任何 shell 前台进程
            tmux_send_escape()
            time.sleep(0.2)
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"])
            time.sleep(0.3)
            tmux_send("/exit")
            tmux_send_enter()
            time.sleep(1.5)
            # 清掉 shell 当前行（防止历史里残留的 claude --resume 被错误回车）
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"])
            time.sleep(0.1)
            subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-u"])
            time.sleep(0.2)

        # Re-check: session may have died after /exit (tmux kills session when initial command exits)
        if not tmux_exists():
            subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION], capture_output=True)
            time.sleep(0.5)

        # 用 paste-buffer 送启动命令；claude_launch_cmd 已预写 approved key，不会弹确认
        tmux_send_with_enter(claude_launch_cmd(model))

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
        sys.path.insert(0, "/mnt/d/cao_stock/scripts")
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

    def _do_resume(self, chat_id, session_id):
        # 清残留 PENDING
        if os.path.exists(PENDING_FILE):
            try: os.remove(PENDING_FILE)
            except: pass
        time.sleep(0.3)
        tmux_send_escape()
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"])
        time.sleep(0.5)
        tmux_send("/exit")
        tmux_send_enter()
        time.sleep(2.0)  # Wait for Claude to fully exit before launching resume
        # 清掉 shell 当前行
        subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"])
        time.sleep(0.1)
        subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-u"])
        time.sleep(0.2)
        # Re-check: session may have died after /exit
        if not tmux_exists():
            subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION], capture_output=True)
            time.sleep(0.5)
        tmux_send_with_enter(claude_launch_cmd(extra_args=f" --resume {session_id}"))

    def _do_relaunch(self, chat_id):
        """Kill current Claude Code process in tmux and start a fresh one."""
        time.sleep(0.3)
        cur_model = get_model() or "claude-opus-4-7"
        cur_provider = get_provider(cur_model)
        self._relaunch_claude_for_model(chat_id, cur_model, cur_provider)
        time.sleep(2)
        telegram_api("sendMessage", {"chat_id": chat_id, "text": "Claude Code relaunched ✓"})

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
    try:
        req = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
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
        result = telegram_api("setWebhook", {"url": url})
        if result and result.get("ok"):
            print(f"[watchdog] Webhook re-registered: {url}")
            return url
        print(f"[watchdog] setWebhook failed: {result}")
    else:
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
    if os.path.exists(RESTART_NOTIFY_FILE):
        try:
            chat_id = int(open(RESTART_NOTIFY_FILE).read().strip())
            os.remove(RESTART_NOTIFY_FILE)
            telegram_api("sendMessage", {"chat_id": chat_id, "text": "Bridge restarted successfully ✓"})
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
    """Register webhooks for all configured bots."""
    for path, profile in BOT_PROFILES.items():
        token = profile.get("token", "")
        if not token:
            continue
        suffix = "" if path == "/" else path
        webhook_url = f"{tunnel_url}{suffix}"
        result = telegram_api("setWebhook", {"url": webhook_url}, token=token)
        ok = result and result.get("ok")
        print(f"  [{profile['name']}] webhook {webhook_url} → {'OK' if ok else 'FAIL'}")
        if ok:
            telegram_api("setMyCommands", {"commands": BOT_COMMANDS}, token=token)


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return

    # Fill runtime tokens into BOT_PROFILES
    BOT_PROFILES["/"]["token"]      = BOT_TOKEN
    BOT_PROFILES["/stock"]["token"] = STOCK_BOT_TOKEN

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
    try:
        HTTPServer.allow_reuse_address = True
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
