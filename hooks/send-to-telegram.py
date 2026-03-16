#!/usr/bin/env python3
"""Claude Code Stop hook - sends response back to Telegram"""
import sys, os, json, re, time, urllib.request

LOG = "/tmp/hook_debug.log"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN")
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
PENDING_FILE = os.path.expanduser("~/.claude/telegram_pending")

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

    # Collect assistant text after last user message
    texts = []
    for line in lines[last_user_idx + 1:]:
        try:
            obj = json.loads(line)
            if obj.get("type") == "assistant" and "message" in obj:
                for block in obj["message"].get("content", []):
                    if block.get("type") == "text":
                        texts.append(block["text"])
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

    # Send to Telegram
    def send(txt, mode=None):
        payload = {"chat_id": chat_id, "text": txt}
        if mode:
            payload["parse_mode"] = mode
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json.dumps(payload).encode(),
                {"Content-Type": "application/json"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
            log(f"send ok={resp.get('ok')} mode={mode}")
            return resp.get("ok")
        except Exception as e:
            log(f"send FAILED mode={mode} error={e}")
            return False

    if not send(text, "HTML"):
        log("HTML failed, trying plain text")
        send("\n\n".join(texts).strip()[:4096])

    os.remove(PENDING_FILE)
    log("done")

if __name__ == "__main__":
    main()
