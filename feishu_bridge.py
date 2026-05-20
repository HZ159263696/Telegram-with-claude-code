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
PENDING_FILE        = os.path.expanduser("~/.claude/telegram_pending_feishu")
FEISHU_CHAT_ID_FILE = os.path.expanduser("~/.claude/feishu_chat_id")
FEISHU_MSG_ID_FILE  = os.path.expanduser("~/.claude/feishu_reply_message_id")

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


# ── 飞书消息工具 ───────────────────────────────────────────────────────────────

def extract_content(message) -> tuple[str, str]:
    """从飞书消息体提取文本和图片 key，返回 (text, image_key)。"""
    if message.message_type == "text":
        try:
            content = json.loads(message.content)
            text = content.get("text", "")
        except Exception:
            return "", ""
        if message.mentions:
            for m in message.mentions:
                text = text.replace(m.key, "")
        return text.strip(), ""
    if message.message_type == "image":
        try:
            content = json.loads(message.content)
            return "", content.get("image_key", "")
        except Exception:
            return "", ""
    return "", ""


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
        local_path = f"/tmp/feishu_image_{int(time.time())}.jpg"
        with open(local_path, "wb") as f:
            raw = resp.data.file
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
    text, image_key = extract_content(msg)

    if not text and not image_key:
        send_reply(msg.message_id, "（暂只支持文本和图片消息哦）")
        return

    # 图片：下载到本地，把路径拼入 prompt
    if image_key:
        image_path = download_feishu_image(msg.message_id, image_key)
        if not image_path:
            send_reply(msg.message_id, "图片下载失败，请重试")
            return
        text = f"请用 Read 工具查看图片 {image_path}\n\n用户问题：{text or '请描述这张图片的内容'}"

    lark.logger.info(f"收到消息: {text[:80]}")

    if text.startswith("/"):
        if handle_command(chat_id, msg.message_id, text):
            return

    if not tmux_exists():
        send_reply(msg.message_id, "❌ Claude 未启动（tmux session claude_feishu 不存在）")
        return

    # 写 pending 文件（Stop 钩子据此决定回复哪个 bot）
    with open(PENDING_FILE, "w") as f:
        f.write(str(int(time.time())))
    with open(FEISHU_CHAT_ID_FILE, "w") as f:
        f.write(chat_id)
    with open(FEISHU_MSG_ID_FILE, "w") as f:
        f.write(msg.message_id)

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
