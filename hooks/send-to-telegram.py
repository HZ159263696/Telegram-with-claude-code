#!/usr/bin/env python3
"""Claude Code Stop hook - sends response back to Telegram or Feishu"""
import sys, os, json, re, time, urllib.request, urllib.error

LOG = "/tmp/hook_debug.log"
TOKEN_STATS_FILE = os.path.expanduser("~/.claude/telegram_token_stats.json")

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID",     "REDACTED_FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# ── Bot 路由表 ─────────────────────────────────────────────────────────────────
# (keyword, token, pending_file, reply_target_file)
# keyword: transcript_path 中的匹配关键词；空字符串 = 兜底
# token: Telegram bot token，或 "feishu"（特殊标志）
# reply_target_file: Telegram 时存 chat_id；Feishu 时存 message_id
BOT_ROUTES = [
    (
        "cao-stock",
        os.environ.get("STOCK_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        os.path.expanduser("~/.claude/telegram_pending_stock"),
        os.path.expanduser("~/.claude/telegram_chat_id_stock"),
    ),
    (
        "feishu",   # 飞书工作区：-mnt-d-AI-feishu-workspace 含 "feishu"
        "feishu",   # 特殊标志，触发 Feishu API 回复而非 Telegram
        os.path.expanduser("~/.claude/telegram_pending_feishu"),
        os.path.expanduser("~/.claude/feishu_reply_message_id"),
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
    """Return (token, pending_file, reply_target_file) based on transcript path."""
    for keyword, token, pending, reply_target in BOT_ROUTES:
        if not keyword or keyword in transcript_path:
            return token, pending, reply_target
    return BOT_ROUTES[-1][1], BOT_ROUTES[-1][2], BOT_ROUTES[-1][3]


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{msg}\n")


# ── Feishu 回复 ────────────────────────────────────────────────────────────────

def feishu_get_token() -> str:
    """获取飞书 tenant_access_token。"""
    body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body, {"Content-Type": "application/json"}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if resp.get("code") != 0:
        raise RuntimeError(f"feishu token error: {resp}")
    return resp["tenant_access_token"]


def feishu_send_reply(message_id: str, text: str) -> bool:
    """回复飞书指定消息（线程内）。"""
    try:
        token = feishu_get_token()
    except Exception as e:
        log(f"feishu get_token failed: {e}")
        return False

    if len(text) > 28000:
        text = text[:28000] + "\n..."

    body = json.dumps({
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "msg_type": "text",
    }).encode()
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, body,
                {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            ok = resp.get("code") == 0
            log(f"feishu send ok={ok} attempt={attempt+1}")
            return ok
        except urllib.error.HTTPError as e:
            log(f"feishu HTTP {e.code} attempt={attempt+1}: {e.read()[:200]}")
            return False
        except Exception as e:
            last_err = e
            log(f"feishu send error attempt={attempt+1}: {e}")
            time.sleep(0.8 * (attempt + 1))
    log(f"feishu send FAILED: {last_err}")
    return False


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except:
        log(f"bad json input: {raw[:200]}")
        return

    transcript_path = data.get("transcript_path", "")
    log(f"hook start: transcript={transcript_path}")

    TOKEN, PENDING_FILE, REPLY_TARGET_FILE = resolve_bot(transcript_path)
    log(f"resolved bot: token=...{TOKEN[-10:]} pending={PENDING_FILE}")

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

    if not os.path.exists(REPLY_TARGET_FILE) or not os.path.exists(transcript_path):
        os.remove(PENDING_FILE)
        log(f"missing file: reply_target={os.path.exists(REPLY_TARGET_FILE)} transcript={os.path.exists(transcript_path)}")
        return

    reply_target = open(REPLY_TARGET_FILE).read().strip()

    time.sleep(0.5)

    lines = open(transcript_path).readlines()
    log(f"transcript lines: {len(lines)}")

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

    # ── 飞书回复（纯文本，无需 Markdown 转换）─────────────────────────────────
    if TOKEN == "feishu":
        feishu_send_reply(reply_target, text)
    else:
        # ── Telegram 回复（Markdown → HTML）───────────────────────────────────
        if len(text) > 4000:
            text = text[:4000] + "\n..."

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

        def send(txt, mode=None):
            payload = {"chat_id": reply_target, "text": txt}
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
                    log(f"send HTTP {e.code} mode={mode} attempt={attempt+1}: {e.read()[:200]}")
                    return False
                except Exception as e:
                    last_err = e
                    log(f"send error mode={mode} attempt={attempt+1}: {e}")
                    time.sleep(0.8 * (attempt + 1))
            log(f"send FAILED after 3 attempts: {last_err}")
            return False

        if not send(text, "HTML"):
            log("HTML failed, trying plain text")
            send("\n\n".join(texts).strip()[:4096])

    # ── Token 统计 ─────────────────────────────────────────────────────────────
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
        with open("/tmp/hook_probe.log", "a") as _p:
            _p.write(f"[{time.strftime('%H:%M:%S')}] invoked, cwd={os.getcwd()}, argv={sys.argv}\n")
    except Exception:
        pass
    try:
        main()
    except Exception as e:
        log(f"hook crashed: {e}")
        sys.exit(0)
