#!/usr/bin/env python3
"""Claude Code Stop hook - sends response back to Telegram"""
import sys, os, json, re, time, urllib.request

LOG = "/tmp/hook_debug.log"
TOKEN_STATS_FILE = os.path.expanduser("~/.claude/telegram_token_stats.json")

# ── Bot 路由表：根据 Claude Code 工作区路径判断用哪个 Bot ──────────────────────
# key = 工作区路径关键词（在 transcript_path 中匹配）
# value = (bot_token, pending_file, chat_id_file)
BOT_ROUTES = [
    (
        "cao-stock",   # 股票工作区特征（Claude Code把路径下划线转为连字符）
        os.environ.get("STOCK_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        os.path.expanduser("~/.claude/telegram_pending_stock"),
        os.path.expanduser("~/.claude/telegram_chat_id_stock"),
    ),
    # 主控Bot（兜底）
    (
        "",
        os.environ.get("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        os.path.expanduser("~/.claude/telegram_pending"),
        os.path.expanduser("~/.claude/telegram_chat_id"),
    ),
]

def resolve_bot(transcript_path):
    """Return (token, pending_file, chat_id_file) based on transcript path."""
    for keyword, token, pending, chat_id in BOT_ROUTES:
        if not keyword or keyword in transcript_path:
            return token, pending, chat_id
    return BOT_ROUTES[-1][1], BOT_ROUTES[-1][2], BOT_ROUTES[-1][3]

def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{msg}\n")

def main():
    # Read input from Claude Code
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except:
        log(f"bad json input: {raw[:200]}")
        return

    transcript_path = data.get("transcript_path", "")
    log(f"hook start: transcript={transcript_path}")

    # Resolve which bot should reply based on transcript path
    TOKEN, PENDING_FILE, CHAT_ID_FILE = resolve_bot(transcript_path)
    log(f"resolved bot: token=...{TOKEN[-10:]} pending={PENDING_FILE}")

    # Check pending file
    if not os.path.exists(PENDING_FILE):
        log("no pending file, skip")
        return

    try:
        pending_time = int(open(PENDING_FILE).read().strip())
    except:
        os.remove(PENDING_FILE)
        log("bad pending time, skip")
        return

    if time.time() - pending_time > 600:
        os.remove(PENDING_FILE)
        log("pending expired, skip")
        return

    if not os.path.exists(CHAT_ID_FILE) or not os.path.exists(transcript_path):
        os.remove(PENDING_FILE)
        log(f"missing file: chat_id={os.path.exists(CHAT_ID_FILE)} transcript={os.path.exists(transcript_path)}")
        return

    chat_id = open(CHAT_ID_FILE).read().strip()

    # Small delay to ensure transcript is fully flushed
    time.sleep(0.5)

    # Extract last assistant response from transcript
    lines = open(transcript_path).readlines()
    log(f"transcript lines: {len(lines)}")

    # Find last user message line
    last_user_idx = -1
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
            if obj.get("type") == "user":
                last_user_idx = i
        except:
            continue

    if last_user_idx < 0:
        os.remove(PENDING_FILE)
        log("no user message found")
        return

    # Collect assistant text and token usage after last user message
    texts = []
    total_in, total_out = 0, 0
    for line in lines[last_user_idx + 1:]:
        try:
            obj = json.loads(line)
            if obj.get("type") == "assistant" and "message" in obj:
                for block in obj["message"].get("content", []):
                    if block.get("type") == "text":
                        texts.append(block["text"])
                usage = obj["message"].get("usage", {})
                if usage:
                    total_in += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                    total_out += usage.get("output_tokens", 0)
        except:
            continue

    text = "\n\n".join(texts).strip()
    log(f"extracted: len={len(text)} preview={text[:100]}")

    if not text:
        os.remove(PENDING_FILE)
        log("empty text, skip")
        return

    if len(text) > 4000:
        text = text[:4000] + "\n..."

    # Convert Markdown to Telegram HTML
    def esc(s):
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    blocks_saved, inlines_saved = [], []
    text = re.sub(r'```(\w*)\n?(.*?)```', lambda m: (blocks_saved.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks_saved)-1}\x00")[1], text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`', lambda m: (inlines_saved.append(m.group(1)), f"\x00I{len(inlines_saved)-1}\x00")[1], text)
    text = esc(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
    for i, (lang, code) in enumerate(blocks_saved):
        repl = f'<pre><code class="language-{lang}">{esc(code.strip())}</code></pre>' if lang else f'<pre>{esc(code.strip())}</pre>'
        text = text.replace(f"\x00B{i}\x00", repl)
    for i, code in enumerate(inlines_saved):
        text = text.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')

    # Send to Telegram, retry on SSL/transient network errors
    def send(txt, mode=None):
        payload = {"chat_id": chat_id, "text": txt}
        if mode:
            payload["parse_mode"] = mode
        body = json.dumps(payload).encode()
        last_err = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    body, {"Content-Type": "application/json"}
                )
                resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
                log(f"send ok={resp.get('ok')} mode={mode} attempt={attempt+1}")
                return resp.get("ok")
            except urllib.error.HTTPError as e:
                # 4xx 不重试（比如 parse_mode 错误）
                log(f"send HTTP {e.code} mode={mode} attempt={attempt+1}: {e.read()[:200]}")
                return False
            except Exception as e:
                last_err = e
                log(f"send error mode={mode} attempt={attempt+1}: {e}")
                time.sleep(0.8 * (attempt + 1))  # 0.8s, 1.6s 退避
        log(f"send FAILED after 3 attempts: {last_err}")
        return False

    if not send(text, "HTML"):
        log("HTML failed, trying plain text")
        send("\n\n".join(texts).strip()[:4096])

    # Update token stats for Claude model
    if total_in > 0 or total_out > 0:
        try:
            stats = {"input": 0, "output": 0}
            if os.path.exists(TOKEN_STATS_FILE):
                stats = json.load(open(TOKEN_STATS_FILE))
            stats["input"] = stats.get("input", 0) + total_in
            stats["output"] = stats.get("output", 0) + total_out
            with open(TOKEN_STATS_FILE, "w") as f:
                json.dump(stats, f)
            log(f"token stats updated: in={total_in} out={total_out}")
        except Exception as e:
            log(f"token stats error: {e}")

    os.remove(PENDING_FILE)
    log("done")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"hook crashed: {e}")
        sys.exit(0)
