#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge"""

import os
import sys
import json
import subprocess
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

TMUX_SESSION = os.environ.get("TMUX_SESSION", "claude")
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
RESTART_NOTIFY_FILE = os.path.expanduser("~/.claude/telegram_restart_notify")
PENDING_FILE = os.path.expanduser("~/.claude/telegram_pending")
HISTORY_FILE = os.path.expanduser("~/.claude/history.jsonl")
MODEL_FILE = os.path.expanduser("~/.claude/telegram_model")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

MODELS = [
    ("claude-opus-4-6",          "Opus 4.6 — 最强"),
    ("claude-sonnet-4-6",        "Sonnet 4.6 — 均衡"),
    ("claude-haiku-4-5-20251001","Haiku 4.5 — 最快"),
]


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

BOT_COMMANDS = [
    {"command": "clear", "description": "Clear conversation"},
    {"command": "resume", "description": "Resume session (shows picker)"},
    {"command": "continue_", "description": "Continue most recent session"},
    {"command": "loop", "description": "Ralph Loop: /loop <prompt>"},
    {"command": "stop", "description": "Interrupt Claude (Escape)"},
    {"command": "status", "description": "Check tmux status"},
    {"command": "model", "description": "Switch Claude model"},
    {"command": "restart", "description": "Restart bridge.py"},
]

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/settings", "/config", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]


def telegram_api(method, data):
    if not BOT_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        print(f"Telegram API error [{method}]: {e} | {body}")
        return None
    except Exception as e:
        print(f"Telegram API error [{method}]: {e}")
        return None


def setup_bot_commands():
    result = telegram_api("setMyCommands", {"commands": BOT_COMMANDS})
    if result and result.get("ok"):
        print("Bot commands registered")


def send_typing_loop(chat_id):
    while os.path.exists(PENDING_FILE):
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
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
    def do_POST(self):
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
        telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")})

        if not tmux_exists():
            self.reply(chat_id, "tmux session not found")
            return

        if data.startswith("model:"):
            chosen = data.split(":", 1)[1]
            set_model(chosen)
            label = next((l for m, l in MODELS if m == chosen), chosen)
            # Restart claude with new model
            if tmux_exists():
                tmux_send_escape()
                time.sleep(0.2)
                tmux_send("/exit")
                tmux_send_enter()
                time.sleep(0.5)
                tmux_send(f"claude --dangerously-skip-permissions --model {chosen}")
                tmux_send_enter()
            self.reply(chat_id, f"已切换到 {label}")
            return

        if data.startswith("resume:"):
            session_id = data.split(":", 1)[1]
            tmux_send_escape()
            time.sleep(0.2)
            tmux_send("/exit")
            tmux_send_enter()
            time.sleep(0.5)
            tmux_send(f"claude --resume {session_id} --dangerously-skip-permissions{model_flag()}")
            tmux_send_enter()
            self.reply(chat_id, f"Resuming: {session_id[:8]}...")

        elif data == "continue_recent":
            tmux_send_escape()
            time.sleep(0.2)
            tmux_send("/exit")
            tmux_send_enter()
            time.sleep(0.5)
            tmux_send(f"claude --continue --dangerously-skip-permissions{model_flag()}")
            tmux_send_enter()
            self.reply(chat_id, "Continuing most recent...")

    def download_photo(self, photo_list):
        """Download the largest photo from Telegram, return local file path or None."""
        largest = max(photo_list, key=lambda p: p.get("file_size", 0))
        file_id = largest.get("file_id")
        result = telegram_api("getFile", {"file_id": file_id})
        if not result or not result.get("ok"):
            return None
        file_path = result["result"]["file_path"]
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
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
        caption = msg.get("caption", "")

        if not chat_id:
            return

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
                status = "running" if tmux_exists() else "not found"
                self.reply(chat_id, f"tmux '{TMUX_SESSION}': {status}")
                return

            if cmd == "/stop":
                if tmux_exists():
                    tmux_send_escape()
                if os.path.exists(PENDING_FILE):
                    os.remove(PENDING_FILE)
                self.reply(chat_id, "Interrupted")
                return

            if cmd == "/clear":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                tmux_send_escape()
                time.sleep(0.2)
                tmux_send("/clear")
                tmux_send_enter()
                self.reply(chat_id, "Cleared")
                return

            if cmd == "/continue_":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                tmux_send_escape()
                time.sleep(0.2)
                tmux_send("/exit")
                tmux_send_enter()
                time.sleep(0.5)
                tmux_send(f"claude --continue --dangerously-skip-permissions{model_flag()}")
                tmux_send_enter()
                self.reply(chat_id, "Continuing...")
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
                threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
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

            if cmd == "/model":
                current = get_model() or "默认"
                kb = [[{"text": f"{'✓ ' if get_model() == m else ''}{label}", "callback_data": f"model:{m}"}] for m, label in MODELS]
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"当前模型：{current}\n选择新模型：",
                    "reply_markup": {"inline_keyboard": kb}
                })
                return

            if cmd == "/resume":
                sessions = get_recent_sessions()
                if not sessions:
                    self.reply(chat_id, "No sessions")
                    return
                kb = [[{"text": "Continue most recent", "callback_data": "continue_recent"}]]
                for s in sessions:
                    sid = get_session_id(s.get("project", ""))
                    if sid:
                        kb.append([{"text": s.get("display", "?")[:40] + "...", "callback_data": f"resume:{sid}"}])
                telegram_api("sendMessage", {"chat_id": chat_id, "text": "Select session:", "reply_markup": {"inline_keyboard": kb}})
                return

            if cmd in BLOCKED_COMMANDS:
                self.reply(chat_id, f"'{cmd}' not supported (interactive)")
                return

        # Regular message
        print(f"[{chat_id}] {text[:50]}...")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        if not tmux_exists():
            self.reply(chat_id, "tmux not found")
            os.remove(PENDING_FILE)
            return

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        tmux_send_with_enter(text)

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
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

    def log_message(self, *args):
        pass


def notify_restart_if_needed():
    if os.path.exists(RESTART_NOTIFY_FILE):
        try:
            chat_id = int(open(RESTART_NOTIFY_FILE).read().strip())
            os.remove(RESTART_NOTIFY_FILE)
            telegram_api("sendMessage", {"chat_id": chat_id, "text": "Bridge restarted successfully ✓"})
        except Exception as e:
            print(f"Restart notify error: {e}")


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return
    notify_restart_if_needed()
    setup_bot_commands()
    print(f"Bridge on :{PORT} | tmux: {TMUX_SESSION}")
    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
