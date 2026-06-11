#!/usr/bin/env python3
"""飞书 ↔ Claude Code 桥接（tmux 常驻模式）

消息通过 tmux send-keys 注入 claude_feishu session，Stop 钩子读 transcript 后回复飞书。
"""

import json
import os
import subprocess
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

APP_ID     = os.environ.get("FEISHU_APP_ID",     "REDACTED_FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
MAX_REPLY_LEN = 28000

TMUX_SESSION        = "claude_feishu"
WORK_DIR            = "/mnt/d/AI/feishu_workspace"
MODEL_FILE          = os.path.expanduser("~/.claude/telegram_model_feishu")
DEFAULT_MODEL       = "claude-sonnet-4-6"
PENDING_FILE        = os.path.expanduser("~/.claude/telegram_pending_feishu")
FEISHU_CHAT_ID_FILE = os.path.expanduser("~/.claude/feishu_chat_id")
FEISHU_MSG_ID_FILE  = os.path.expanduser("~/.claude/feishu_reply_message_id")
PENDING_TIMEOUT     = 600   # 与 bridge.py 一致：pending 超过 600s 视为过期

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
_start_time = time.time()


# ── tmux 工具 ──────────────────────────────────────────────────────────────────

def tmux_exists() -> bool:
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    return r.returncode == 0 and TMUX_SESSION in r.stdout.split()


def tmux_send_with_enter(text: str) -> bool:
    """通过 tmux buffer paste 发送文本 + Enter（多行安全）。"""
    if not tmux_exists():
        return False
    subprocess.run(["tmux", "load-buffer", "-"], input=text.encode())
    subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION])
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"])
    return True


def tmux_interrupt():
    """向 tmux session 发送 Ctrl+C。"""
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "C-c"])


def get_model() -> str:
    try:
        m = open(MODEL_FILE).read().strip()
        if m:
            return m
    except Exception:
        pass
    return DEFAULT_MODEL


def ensure_session() -> bool:
    """session 不存在时自动创建并启动 Claude（与 bridge.py 行为对齐）。
    返回 True 表示是新拉起的（调用方需延迟注入消息等 Claude 就绪）。"""
    if tmux_exists():
        return False
    os.makedirs(WORK_DIR, exist_ok=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-c", WORK_DIR],
                   capture_output=True)
    time.sleep(0.3)
    cmd = f"claude --dangerously-skip-permissions --model {get_model()}"
    subprocess.run(["tmux", "load-buffer", "-"], input=cmd.encode())
    subprocess.run(["tmux", "paste-buffer", "-t", TMUX_SESSION])
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"])
    lark.logger.info(f"自动拉起 tmux session {TMUX_SESSION} (model={get_model()})")
    return True


# ── 飞书消息工具 ───────────────────────────────────────────────────────────────

def extract_content(message) -> tuple[str, list[str]]:
    """从飞书消息体提取文本和图片 key，返回 (text, image_keys)。

    支持三种消息类型：
    - text：纯文本
    - image：单张图片
    - post：富文本（群里 @机器人 同时发图即为此类型），可含多张图
    """
    mtype = message.message_type

    if mtype == "text":
        try:
            content = json.loads(message.content)
            text = content.get("text", "")
        except Exception:
            return "", []
        if message.mentions:
            for m in message.mentions:
                text = text.replace(m.key, "")
        return text.strip(), []

    if mtype == "image":
        try:
            content = json.loads(message.content)
            key = content.get("image_key", "")
            return "", [key] if key else []
        except Exception:
            return "", []

    if mtype == "post":
        # 富文本 content 形如 {"title": "..", "content": [[{tag,...}, ..], ..]}
        try:
            content = json.loads(message.content)
        except Exception:
            return "", []
        texts: list[str] = []
        image_keys: list[str] = []
        if content.get("title"):
            texts.append(content["title"])
        for paragraph in content.get("content", []):
            for el in paragraph:
                tag = el.get("tag")
                if tag == "text":
                    texts.append(el.get("text", ""))
                elif tag == "a":
                    texts.append(el.get("href", "") or el.get("text", ""))
                elif tag == "img":
                    k = el.get("image_key", "")
                    if k:
                        image_keys.append(k)
        text = " ".join(t for t in texts if t).strip()
        if message.mentions:
            for m in message.mentions:
                text = text.replace(m.key, "")
        return text.strip(), image_keys

    return "", []


def download_feishu_image(message_id: str, image_key: str) -> str:
    """从飞书下载图片到 /tmp，返回本地路径；失败返回空字符串。"""
    try:
        req = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()
        resp = client.im.v1.message_resource.get(req)
        if not resp.success():
            lark.logger.error(f"图片下载失败 code={resp.code} msg={resp.msg}")
            return ""
        # lark_oapi 二进制下载：文件内容在 resp.file（旧版本可能在 resp.data.file）
        raw = getattr(resp, "file", None)
        if raw is None and getattr(resp, "data", None) is not None:
            raw = getattr(resp.data, "file", None)
        if raw is None:
            lark.logger.error("图片下载：响应里找不到文件内容")
            return ""
        local_path = f"/tmp/feishu_image_{int(time.time()*1000)}.jpg"
        with open(local_path, "wb") as f:
            f.write(raw.read() if hasattr(raw, "read") else raw)
        return local_path
    except Exception as e:
        lark.logger.error(f"图片下载异常: {e}")
        return ""


def send_reply(message_id: str, text: str) -> None:
    """以回复方式发送飞书文本消息。"""
    text = text[:MAX_REPLY_LEN]
    body = ReplyMessageRequestBody.builder() \
        .content(json.dumps({"text": text}, ensure_ascii=False)) \
        .msg_type("text") \
        .build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        lark.logger.error(f"回复失败 code={resp.code} msg={resp.msg}")


# ── 指令处理 ───────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "可用指令：\n"
    "/stop   — 中断 Claude 当前任务（Ctrl+C）\n"
    "/clear  — 清空 Claude 上下文（/clear）\n"
    "/status — 查看 tmux session 状态\n"
    "/ping   — 健康检查\n"
    "/help   — 显示本帮助\n"
    "其他文本或图片将注入 Claude Code（持续上下文模式）。"
)


def handle_command(chat_id: str, message_id: str, text: str) -> bool:
    """处理 / 开头的指令；返回 True 表示已处理。"""
    cmd = text.split()[0].lower()

    if cmd in ("/help", "/?"):
        send_reply(message_id, HELP_TEXT)
        return True

    if cmd == "/ping":
        send_reply(message_id, "pong")
        return True

    if cmd == "/status":
        session_ok = tmux_exists()
        pending = os.path.exists(PENDING_FILE)
        uptime = int(time.time() - _start_time)
        send_reply(
            message_id,
            f"tmux claude_feishu: {'✅ 运行中' if session_ok else '❌ 未运行'}\n"
            f"等待回复: {'是' if pending else '否'}\n"
            f"Uptime: {uptime}s",
        )
        return True

    if cmd == "/stop":
        if tmux_exists():
            tmux_interrupt()
            try:
                os.remove(PENDING_FILE)
            except FileNotFoundError:
                pass
            send_reply(message_id, "已中断 Claude")
        else:
            send_reply(message_id, "Claude session 未运行")
        return True

    if cmd == "/clear":
        if tmux_exists():
            tmux_interrupt()
            time.sleep(0.3)
            tmux_send_with_enter("/clear")
            send_reply(message_id, "已清空 Claude 上下文")
        else:
            send_reply(message_id, "Claude session 未运行")
        return True

    return False


# ── 消息处理 ───────────────────────────────────────────────────────────────────

def handle_message(data: P2ImMessageReceiveV1) -> None:
    """提取文本/图片 → 写 pending 文件 → 注入 tmux；Stop 钩子负责回复。"""
    msg = data.event.message
    chat_id = getattr(msg, "chat_id", "") or ""
    text, image_keys = extract_content(msg)
    lark.logger.info(f"消息类型={msg.message_type} 文本={text[:60]!r} 图片数={len(image_keys)}")

    if not text and not image_keys:
        send_reply(msg.message_id, "（暂只支持文本和图片消息哦）")
        return

    # 图片：逐张下载到本地，把路径拼入 prompt
    if image_keys:
        paths = []
        for key in image_keys:
            p = download_feishu_image(msg.message_id, key)
            if p:
                paths.append(p)
        if not paths:
            send_reply(msg.message_id, "图片下载失败，请确认飞书后台已开通「获取与上传图片或文件资源」(im:resource) 权限")
            return
        user_q = text or "请描述这些图片的内容"
        path_lines = "\n".join(f"- {p}" for p in paths)
        text = f"请用 Read 工具查看以下图片，然后回答：\n{path_lines}\n\n用户问题：{user_q}"

    lark.logger.info(f"注入 Claude: {text[:80]}")

    if text.startswith("/"):
        if handle_command(chat_id, msg.message_id, text):
            return

    # 忙碌保护（与 bridge.py 一致）：上一轮还没回完就来新消息，
    # 直接覆盖 pending/message_id 会让两轮回复互相错乱，先挡掉
    if os.path.exists(PENDING_FILE):
        try:
            pt = int(open(PENDING_FILE).read().strip())
            if time.time() - pt < PENDING_TIMEOUT:
                send_reply(msg.message_id, "⏳ Claude 还在处理上一条消息，请稍候再发（要中断用 /stop）")
                return
        except Exception:
            pass
        try:
            os.remove(PENDING_FILE)   # 过期/损坏的 pending 直接清掉
        except FileNotFoundError:
            pass

    # session 不存在则自动拉起（之前只会报错，挂了得重跑 start.sh）
    just_started = ensure_session()

    # 写 pending 文件（Stop 钩子据此决定回复哪个 bot）
    with open(PENDING_FILE, "w") as f:
        f.write(str(int(time.time())))
    with open(FEISHU_CHAT_ID_FILE, "w") as f:
        f.write(chat_id)
    with open(FEISHU_MSG_ID_FILE, "w") as f:
        f.write(msg.message_id)

    if just_started:
        send_reply(msg.message_id, "🚀 Claude 正在启动，消息将在 5 秒后自动发送...")
        time.sleep(5)   # 本函数已在独立线程中，阻塞不影响其它消息
    tmux_send_with_enter(text)


def on_message(data: P2ImMessageReceiveV1) -> None:
    """事件回调：丢到线程池避免阻塞长连接。"""
    threading.Thread(target=handle_message, args=(data,), daemon=True).start()


def main() -> None:
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    ws = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,
    )
    print(f"[feishu_bridge] 启动中… App={APP_ID} tmux={TMUX_SESSION}", flush=True)
    ws.start()


if __name__ == "__main__":
    main()
