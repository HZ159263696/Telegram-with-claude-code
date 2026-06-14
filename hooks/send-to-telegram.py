#!/usr/bin/env python3
"""Claude Code Stop hook - sends response back to Telegram or Feishu"""
import sys, os, json, re, time, urllib.request, urllib.error

# 共享记忆层（best-effort，缺失/出错都不影响回复发送）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import memory_lib
except Exception:
    memory_lib = None

LOG = "/tmp/hook_debug.log"
TOKEN_STATS_FILE = os.path.expanduser("~/.claude/telegram_token_stats.json")

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID",     "REDACTED_FEISHU_APP_ID")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# ── Bot 路由表（显式 工作区目录 → Bot 映射）────────────────────────────────────
# 与 bridge.py BOT_PROFILES / start.sh 的工作区保持一致。
# 路由依据：transcript 落在 ~/.claude/projects/<编码后的工作区路径>/ 下，
# 用编码后的目录名精确匹配，不再靠 "feishu"/"cao-stock" 关键词模糊匹配。
# token: Telegram bot token，或 "feishu"（特殊标志，走 Feishu API 回复）
# target: Telegram 时存 chat_id 的文件；Feishu 时存 message_id 的文件


def _enc(path):
    """Claude Code 把 cwd 的非字母数字字符转成 '-' 作为 projects 子目录名。"""
    return re.sub(r'[^a-zA-Z0-9]', '-', path)


BOT_ROUTES = [
    {
        "bot":     "stock",
        "workdir": "/mnt/d/cao_stock",
        "token":   os.environ.get("STOCK_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        "pending": os.path.expanduser("~/.claude/telegram_pending_stock"),
        "target":  os.path.expanduser("~/.claude/telegram_chat_id_stock"),
    },
    {
        "bot":     "feishu",
        "workdir": "/mnt/d/AI/feishu_workspace",
        "token":   "feishu",
        "pending": os.path.expanduser("~/.claude/telegram_pending_feishu"),
        "target":  os.path.expanduser("~/.claude/feishu_reply_message_id"),
    },
    {   # 主控Bot（兜底，workdir 空 = 匹配所有其它路径）
        "bot":     "main",
        "workdir": "",
        "token":   os.environ.get("TELEGRAM_BOT_TOKEN", "REDACTED_TELEGRAM_TOKEN"),
        "pending": os.path.expanduser("~/.claude/telegram_pending"),
        "target":  os.path.expanduser("~/.claude/telegram_chat_id"),
    },
]


def resolve_bot(transcript_path):
    """按 transcript 所在 projects 子目录精确路由，返回路由 dict。"""
    for r in BOT_ROUTES:
        if r["workdir"] and f"/projects/{_enc(r['workdir'])}/" in transcript_path:
            return r
    return BOT_ROUTES[-1]


# ── 长回复分段 ─────────────────────────────────────────────────────────────────
MAX_PARTS = 5   # 最多拆几条消息，防刷屏；超出部分截断


def split_chunks(text, limit):
    """按段落/行边界把长文本拆成 ≤limit 的多段（优先空行，其次换行，最后硬切）。"""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def cap_parts(chunks):
    if len(chunks) > MAX_PARTS:
        chunks = chunks[:MAX_PARTS]
        chunks[-1] += "\n\n…（回复过长，已截断）"
    return chunks


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

    route = resolve_bot(transcript_path)
    BOT_KEY, TOKEN = route["bot"], route["token"]
    PENDING_FILE, REPLY_TARGET_FILE = route["pending"], route["target"]
    log(f"resolved bot: {BOT_KEY} token=...{TOKEN[-10:]} pending={PENDING_FILE}")

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
            if obj.get("type") != "user":
                continue
            # tool_result 在 transcript 里也记成 type==user，必须跳过；否则「以工具调用
            # 结尾」的回合会把窗口起点设到 tool_result，其后无 assistant 文字 → 提取为空
            # → 回复被误判空而丢弃（老爸在 Telegram 收不到，2026-06-14 定位）。
            _c = obj.get("message", {}).get("content", "")
            if isinstance(_c, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in _c
            ):
                continue
            last_user_idx = i
        except:
            continue

    if last_user_idx < 0:
        os.remove(PENDING_FILE)
        log("no user message found")
        return

    # 找最近一条「人类」用户消息（跳过 tool_result），仅用于写记忆
    human_user_text = ""
    for _line in reversed(lines):
        try:
            _o = json.loads(_line)
        except Exception:
            continue
        if _o.get("type") != "user":
            continue
        _c = _o.get("message", {}).get("content", "")
        if isinstance(_c, str):
            human_user_text = _c
            break
        if isinstance(_c, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _c):
                continue
            human_user_text = " ".join(
                b.get("text", "") for b in _c
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if human_user_text:
                break

    texts = []
    total_in, total_out = 0, 0
    for line in lines[last_user_idx + 1:]:
        try:
            obj = json.loads(line)
            if obj.get("type") == "assistant" and "message" in obj:
                blk = [b["text"] for b in obj["message"].get("content", [])
                       if b.get("type") == "text"]
                if blk:
                    # 只保留「最后一条含文字的 assistant 消息」＝收尾总结：
                    # 回合中途的工具调用不会把回复挤空，也不会把全过程旁白都发出去。
                    texts = blk
                usage = obj["message"].get("usage", {})
                if usage:
                    total_in += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                    total_out += usage.get("output_tokens", 0)
        except:
            continue

    text = "\n\n".join(texts).strip()
    assistant_raw = text   # 写记忆用：保留未经 HTML 转换的原文
    log(f"extracted: len={len(text)} preview={text[:100]}")

    if not text:
        os.remove(PENDING_FILE)
        log("empty text, skip")
        return

    # ── 飞书回复（纯文本，长回复分段多条发送）─────────────────────────────────
    if TOKEN == "feishu":
        chunks = cap_parts(split_chunks(text, 27000))
        total = len(chunks)
        for i, ch in enumerate(chunks):
            head = f"({i+1}/{total})\n" if total > 1 else ""
            feishu_send_reply(reply_target, head + ch)
            if i < total - 1:
                time.sleep(0.5)
    else:
        # ── Telegram 回复（Markdown → HTML，长回复分段多条发送）───────────────
        def esc(s):
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        def md_to_html(t):
            blocks_saved, inlines_saved = [], []
            t = re.sub(r'```(\w*)\n?(.*?)```', lambda m: (blocks_saved.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks_saved)-1}\x00")[1], t, flags=re.DOTALL)
            t = re.sub(r'`([^`\n]+)`', lambda m: (inlines_saved.append(m.group(1)), f"\x00I{len(inlines_saved)-1}\x00")[1], t)
            t = esc(t)
            t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
            t = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', t)
            for i, (lang, code) in enumerate(blocks_saved):
                repl = f'<pre><code class="language-{lang}">{esc(code.strip())}</code></pre>' if lang else f'<pre>{esc(code.strip())}</pre>'
                t = t.replace(f"\x00B{i}\x00", repl)
            for i, code in enumerate(inlines_saved):
                t = t.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')
            return t

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

        # 按段落边界拆 ≤3800 字的块（HTML 转换有膨胀余量），逐条发送
        chunks = cap_parts(split_chunks(text, 3800))
        total = len(chunks)
        for i, ch in enumerate(chunks):
            head = f"({i+1}/{total})\n" if total > 1 else ""
            if not send(md_to_html(head + ch), "HTML"):
                log(f"HTML failed on part {i+1}, trying plain text")
                send((head + ch)[:4096])
            if i < total - 1:
                time.sleep(0.5)

    # ── Token 统计（per-bot 分桶；自动迁移旧扁平格式）──────────────────────────
    if total_in > 0 or total_out > 0:
        try:
            stats = {}
            if os.path.exists(TOKEN_STATS_FILE):
                stats = json.load(open(TOKEN_STATS_FILE))
            if not isinstance(stats, dict):
                stats = {}
            if isinstance(stats.get("input"), int):
                stats = {"main": {"input": stats.get("input", 0), "output": stats.get("output", 0)}}
            b = stats.setdefault(BOT_KEY, {"input": 0, "output": 0})
            b["input"]  = b.get("input", 0)  + total_in
            b["output"] = b.get("output", 0) + total_out
            with open(TOKEN_STATS_FILE, "w") as f:
                json.dump(stats, f)
            log(f"token stats updated: bot={BOT_KEY} in={total_in} out={total_out}")
        except Exception as e:
            log(f"token stats error: {e}")

    # ── 写记忆（best-effort）：把本轮压成摘要追加进 RECENT.md ──────────────────
    if memory_lib is not None:
        try:
            memory_lib.append_recent(BOT_KEY, human_user_text, assistant_raw)
            log(f"memory appended: bot={BOT_KEY}")
        except Exception as e:
            log(f"memory write error: {e}")

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
