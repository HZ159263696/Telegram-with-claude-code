#!/usr/bin/env python3
"""飞书 ↔ Claude Code 桥接（长连接模式）

群里 @机器人 或私聊机器人 → 调用 Claude Code CLI → 回复消息
"""

import json
import os
import signal
import subprocess
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

APP_ID = os.environ.get("FEISHU_APP_ID", "REDACTED_FEISHU_APP_ID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/<user>/.local/bin/claude")
_t = os.environ.get("CLAUDE_TIMEOUT", "0").strip()  # 0 或空 = 无超时
CLAUDE_TIMEOUT = int(_t) if _t and _t != "0" else None
MAX_REPLY_LEN = 28000  # 飞书单条文本上限 ~30000

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()

# 正在运行的 Claude 子进程：chat_id -> [Popen, ...]，供 /stop 中断
_running_procs: dict[str, list[subprocess.Popen]] = {}
_procs_lock = threading.Lock()
_start_time = time.time()


def _register_proc(chat_id: str, proc: subprocess.Popen) -> None:
    with _procs_lock:
        _running_procs.setdefault(chat_id, []).append(proc)


def _unregister_proc(chat_id: str, proc: subprocess.Popen) -> None:
    with _procs_lock:
        lst = _running_procs.get(chat_id) or []
        if proc in lst:
            lst.remove(proc)
        if not lst and chat_id in _running_procs:
            _running_procs.pop(chat_id, None)


def _kill_chat_procs(chat_id: str) -> int:
    """终止某 chat 下所有正在运行的 Claude 子进程，返回数量。"""
    with _procs_lock:
        procs = list(_running_procs.get(chat_id) or [])
    n = 0
    for p in procs:
        if p.poll() is None:
            try:
                # 杀整个进程组，避免 shell/子孙残留
                if os.name != "nt":
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                else:
                    p.terminate()
                n += 1
            except Exception:
                try:
                    p.terminate()
                    n += 1
                except Exception:
                    pass
    return n


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
    """从飞书下载图片，保存到 /tmp，返回本地路径；失败返回空字符串。"""
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


def call_claude(prompt: str, chat_id: str = "", image_path: str = "") -> str:
    """调用 Claude Code CLI；image_path 非空时把路径写进 prompt 让 Claude 用 Read 工具读图。"""
    proc = None
    try:
        if image_path:
            prompt = f"请用 Read 工具查看图片 {image_path}\n\n用户问题：{prompt}"
        cmd = [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"]
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # POSIX 下开新进程组，便于 killpg 整组终止
        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid
        proc = subprocess.Popen(cmd, **popen_kwargs)
        if chat_id:
            _register_proc(chat_id, proc)
        try:
            stdout, stderr = proc.communicate(timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                pass
            return f"[超时] Claude 在 {CLAUDE_TIMEOUT} 秒内未返回（设置 CLAUDE_TIMEOUT=0 可取消限制）"

        # 被 /stop 杀掉
        if proc.returncode and proc.returncode < 0:
            return "[已中断] /stop"

        out = (stdout or "").strip()
        if not out:
            err = (stderr or "").strip()
            return f"[Claude 无输出]\n{err[:500]}" if err else "[Claude 无输出]"
        return out
    except Exception as e:
        return f"[调用失败] {e}"
    finally:
        if proc is not None and chat_id:
            _unregister_proc(chat_id, proc)


def send_reply(message_id: str, text: str) -> None:
    """以回复方式发送文本消息。"""
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


HELP_TEXT = (
    "可用指令：\n"
    "/stop — 中断当前会话内正在运行的 Claude\n"
    "/status — 查看运行状态\n"
    "/clear — 清空当前会话的运行记录（不影响 Claude 历史，本机器人为一次性模式）\n"
    "/ping — 健康检查\n"
    "/help — 显示本帮助\n"
    "其他文本将作为 prompt 发给 Claude Code。"
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
        with _procs_lock:
            running = sum(1 for ps in _running_procs.values() for p in ps if p.poll() is None)
            mine = sum(1 for p in (_running_procs.get(chat_id) or []) if p.poll() is None)
        uptime = int(time.time() - _start_time)
        send_reply(
            message_id,
            f"运行中: {running} 个 Claude 子进程（本会话 {mine}）\n"
            f"Uptime: {uptime}s\n"
            f"CLAUDE_TIMEOUT: {CLAUDE_TIMEOUT if CLAUDE_TIMEOUT else '无'}",
        )
        return True

    if cmd == "/stop":
        n = _kill_chat_procs(chat_id)
        send_reply(message_id, f"已中断 {n} 个运行中的 Claude" if n else "当前会话没有运行中的 Claude")
        return True

    if cmd == "/clear":
        # 一次性模式无持久会话；仅杀掉当前 chat 的运行进程，给个确认
        _kill_chat_procs(chat_id)
        send_reply(message_id, "已清理当前会话的运行进程（本机器人每次调用都是独立子进程，无持久上下文）")
        return True

    return False


def handle_message(data: P2ImMessageReceiveV1) -> None:
    """异步处理：取文本/图片 → 调 Claude → 回复。"""
    msg = data.event.message
    chat_id = getattr(msg, "chat_id", "") or ""
    text, image_key = extract_content(msg)

    if not text and not image_key:
        send_reply(msg.message_id, "（暂只支持文本和图片消息哦）")
        return

    image_path = ""
    if image_key:
        image_path = download_feishu_image(msg.message_id, image_key)
        if not image_path:
            send_reply(msg.message_id, "图片下载失败，请重试")
            return
        if not text:
            text = "请描述这张图片的内容"

    lark.logger.info(f"收到消息: {text[:80]}" + (f" [图片:{image_path}]" if image_path else ""))

    if text.startswith("/"):
        if handle_command(chat_id, msg.message_id, text):
            return

    send_reply(msg.message_id, "⏳ 正在处理...")
    reply = call_claude(text, chat_id=chat_id, image_path=image_path)
    send_reply(msg.message_id, reply)


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
    print(f"[feishu_bridge] 启动中… App={APP_ID}", flush=True)
    ws.start()


if __name__ == "__main__":
    main()
