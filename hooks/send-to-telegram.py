#!/usr/bin/env python3
"""Claude Code Stop hook - sends response back to Telegram or Feishu.

可靠性三道防线（2026-06-17 彻底版）：
1. flock 互斥：每个 Bot 一把锁，并发的第二个 Stop 实例拿不到锁直接退出
   → 根治「同一条回复被两个实例各发一遍」。
2. outbox 队列：回复按分段写进 ~/.claude/outbox_<bot>.jsonl，发成功才删；
   发失败/进程被杀都留在队列，每次钩子启动先补发积压 → 根治「回答了却丢失」。
3. 指纹去重：每个分段按内容 md5 记账，近 600s 内已发过的不再发
   → 补发/并发都不会重复。
"""
import sys, os, json, re, time, fcntl, hashlib, urllib.request, urllib.error

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

DEDUP_WINDOW = 600   # 秒：同一分段指纹在此窗口内已发过则跳过
OUTBOX_TTL   = 86400 # 秒：outbox 条目超过 1 天未发出则丢弃，防无限堆积


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
MAX_PARTS = 5


def split_chunks(text, limit):
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


def build_parts(token, text):
    """把整段回复拆成「带 (i/n) 头、可直接发送」的分段列表。"""
    limit = 27000 if token == "feishu" else 3800
    raw = cap_parts(split_chunks(text, limit))
    total = len(raw)
    return [(f"({i+1}/{total})\n" if total > 1 else "") + ch for i, ch in enumerate(raw)]


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{msg}\n")


def fp_of(s):
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()[:16]


# ── 指纹去重账本 ─────────────────────────────────────────────────────────────
def _sent_path(bot):
    return os.path.expanduser(f"~/.claude/sent_{bot}.log")


def already_sent(bot, fp, window=DEDUP_WINDOW):
    p = _sent_path(bot)
    if not os.path.exists(p):
        return False
    now = time.time()
    try:
        for line in open(p, encoding="utf-8", errors="ignore"):
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[0] == fp and now - float(parts[1]) < window:
                return True
    except Exception:
        return False
    return False


def mark_sent(bot, fp):
    p = _sent_path(bot)
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{fp}\t{time.time()}\n")
        # 截断到最近 300 行，防无限增长
        lines = open(p, encoding="utf-8", errors="ignore").readlines()
        if len(lines) > 300:
            open(p, "w", encoding="utf-8").writelines(lines[-300:])
    except Exception as e:
        log(f"mark_sent error: {e}")


# ── outbox 待发队列 ──────────────────────────────────────────────────────────
def _outbox_path(bot):
    return os.path.expanduser(f"~/.claude/outbox_{bot}.jsonl")


def outbox_load(bot):
    p = _outbox_path(bot)
    if not os.path.exists(p):
        return []
    items = []
    for line in open(p, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def outbox_save(bot, items):
    p = _outbox_path(bot)
    if not items:
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
        return
    with open(p, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def outbox_add(bot, item):
    items = outbox_load(bot)
    items.append(item)
    outbox_save(bot, items)


# ── Feishu 回复 ────────────────────────────────────────────────────────────────
def feishu_get_token() -> str:
    body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        body, {"Content-Type": "application/json"}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if resp.get("code") != 0:
        raise RuntimeError(f"feishu token error: {resp}")
    return resp["tenant_access_token"]


def feishu_send_one(message_id: str, text: str) -> bool:
    """回复飞书指定消息（线程内）。单段，调用方负责分段。"""
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
    for attempt in range(6):
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
            time.sleep(min(2 ** attempt, 20))
    log(f"feishu send FAILED: {last_err}")
    return False


# ── Telegram 回复 ──────────────────────────────────────────────────────────────
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


def tg_send(token, chat_id, txt, mode=None):
    payload = {"chat_id": chat_id, "text": txt}
    if mode:
        payload["parse_mode"] = mode
    body = json.dumps(payload).encode()
    last_err = None
    for attempt in range(6):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                body, {"Content-Type": "application/json"}
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            log(f"send ok={resp.get('ok')} mode={mode} attempt={attempt+1}")
            return bool(resp.get("ok"))
        except urllib.error.HTTPError as e:
            log(f"send HTTP {e.code} mode={mode} attempt={attempt+1}: {e.read()[:200]}")
            return False
        except Exception as e:
            last_err = e
            log(f"send error mode={mode} attempt={attempt+1}: {e}")
            time.sleep(min(2 ** attempt, 20))
    log(f"send FAILED after 6 attempts: {last_err}")
    return False


def deliver_part(route, target, part_text):
    """发送单个分段，返回是否成功。"""
    if route["token"] == "feishu":
        return feishu_send_one(target, part_text)
    ok = tg_send(route["token"], target, md_to_html(part_text), "HTML")
    if not ok:
        log("HTML failed, trying plain text")
        ok = tg_send(route["token"], target, part_text[:4096], None)
    return ok


def deliver_with_outbox(route, target, parts):
    """把每个分段过一遍：去重→落 outbox→发送→成功则记账+移除。"""
    bot = route["bot"]
    for part in parts:
        fp = fp_of(part)
        if already_sent(bot, fp):
            log(f"dup skip part fp={fp}")
            continue
        item = {"ts": time.time(), "target": target, "text": part, "fp": fp}
        outbox_add(bot, item)
        if deliver_part(route, target, part):
            mark_sent(bot, fp)
            _outbox_drop(bot, fp)
        else:
            log(f"deliver failed, kept in outbox fp={fp}")
        time.sleep(0.4)


def _outbox_drop(bot, fp):
    items = [it for it in outbox_load(bot) if it.get("fp") != fp]
    outbox_save(bot, items)


def outbox_flush(route):
    """钩子启动先补发该 Bot 的 outbox 积压（发成功/已发过的移除，超时丢弃）。"""
    bot = route["bot"]
    items = outbox_load(bot)
    if not items:
        return
    now = time.time()
    remaining = []
    for it in items:
        fp = it.get("fp", "")
        if now - it.get("ts", 0) > OUTBOX_TTL:
            log(f"outbox drop stale fp={fp}")
            continue
        if already_sent(bot, fp):
            continue   # 已经发过了，丢弃
        if deliver_part(route, it.get("target", ""), it.get("text", "")):
            mark_sent(bot, fp)
            log(f"outbox resent fp={fp}")
        else:
            remaining.append(it)
        time.sleep(0.4)
    outbox_save(bot, remaining)


def _extract(lines, last_user_idx):
    texts = []
    ti, to = 0, 0
    for line in lines[last_user_idx + 1:]:
        try:
            obj = json.loads(line)
            if obj.get("type") == "assistant" and "message" in obj:
                blk = [b["text"] for b in obj["message"].get("content", [])
                       if b.get("type") == "text"]
                if blk:
                    texts = blk   # 只保留最后一条含文字的 assistant 消息＝收尾总结
                usage = obj["message"].get("usage", {})
                if usage:
                    ti += usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                    to += usage.get("output_tokens", 0)
        except:
            continue
    return "\n\n".join(texts).strip(), ti, to


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

    # ── 防线1：flock 互斥。并发的第二个实例拿不到锁就退出，不重复发 ──────────
    lock_fd = open(os.path.expanduser(f"~/.claude/hook_lock_{BOT_KEY}"), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log("another instance holds lock, skip (防重复)")
        return   # 不动 pending，让持锁实例处理

    # ── 防线2：先补发 outbox 积压（上次没发出去的，这次先发）──────────────────
    try:
        outbox_flush(route)
    except Exception as e:
        log(f"outbox_flush error: {e}")

    if not os.path.exists(PENDING_FILE):
        log("no pending file, skip")
        return

    try:
        pending_time = int(open(PENDING_FILE).read().strip())
    except:
        os.remove(PENDING_FILE)
        log("bad pending time, skip")
        return

    if time.time() - pending_time > 1800:
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

    # 最近一条「人类」用户消息（跳过 tool_result），仅用于写记忆
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

    text, total_in, total_out = _extract(lines, last_user_idx)
    # 提取为空多半是 Stop 早于 transcript 落盘（时序）或被 API 错误打断：
    # 重读重试几次，扛住「回答了但还没 flush」→ 否则回复被误判空而丢弃。
    for _retry in range(3):
        if text:
            break
        time.sleep(1.2)
        try:
            lines = open(transcript_path).readlines()
        except Exception:
            break
        text, total_in, total_out = _extract(lines, last_user_idx)
        log(f"empty-retry {_retry+1}: len={len(text)}")

    assistant_raw = text
    log(f"extracted: len={len(text)} preview={text[:100]}")

    if not text:
        os.remove(PENDING_FILE)
        log("empty text after retries, skip")
        return

    # ── 防线2+3：分段→指纹去重→落 outbox→发送（成功才记账移除）──────────────
    parts = build_parts(TOKEN, text)
    deliver_with_outbox(route, reply_target, parts)

    # ── Token 统计 ──────────────────────────────────────────────────────────────
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

    # ── 写记忆（best-effort）───────────────────────────────────────────────────
    if memory_lib is not None:
        try:
            memory_lib.append_recent(BOT_KEY, human_user_text, assistant_raw)
            log(f"memory appended: bot={BOT_KEY}")
        except Exception as e:
            log(f"memory write error: {e}")

    try:
        os.remove(PENDING_FILE)
    except FileNotFoundError:
        pass
    log("done")


def flush_all():
    """仅补发所有 Bot 的 outbox 积压（bridge 后台线程定时调用）。每个 Bot 走
    flock：拿不到锁说明正有正常 Stop 实例在处理，跳过，避免并发重复发送。"""
    for r in BOT_ROUTES:
        try:
            lock_fd = open(os.path.expanduser(f"~/.claude/hook_lock_{r['bot']}"), "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            continue
        try:
            outbox_flush(r)
        except Exception as e:
            log(f"flush_all error bot={r['bot']}: {e}")
        finally:
            try: lock_fd.close()
            except Exception: pass


if __name__ == "__main__":
    if "--flush-all" in sys.argv:
        try:
            flush_all()
        except Exception as e:
            log(f"flush_all crashed: {e}")
        sys.exit(0)
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
