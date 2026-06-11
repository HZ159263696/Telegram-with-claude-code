#!/usr/bin/env python3
"""飞书发文件助手 — 上传本地文件/图片并发送到飞书群。

Claude（claude_feishu session）在需要把生成的文件交给用户时直接调用：

    python3 ~/.claude/hooks/feishu_send_file.py <文件路径> [更多文件...] [--chat <chat_id>]

- chat_id 默认读 ~/.claude/feishu_chat_id（最近一次飞书对话的群/会话）
- 图片扩展名(jpg/png/gif/webp 等)走图片消息，其余走文件消息
- 纯标准库实现（手工 multipart），与项目"核心零依赖"原则一致
- 需要飞书后台开通「上传图片或文件资源」(im:resource) 权限
"""
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid

APP_ID       = os.environ.get("FEISHU_APP_ID",     "REDACTED_FEISHU_APP_ID")
APP_SECRET   = os.environ.get("FEISHU_APP_SECRET", "")
CHAT_ID_FILE = os.path.expanduser("~/.claude/feishu_chat_id")
MAX_SIZE     = 30 * 1024 * 1024   # 飞书文件上限 30MB（图片实际上限 10MB，由 API 报错兜底）

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "ico", "heic"}
# 飞书 im/v1/files 的 file_type 枚举映射；不认识的一律 stream
FILE_TYPE_BY_EXT = {
    "pdf": "pdf", "doc": "doc", "docx": "doc",
    "xls": "xls", "xlsx": "xls", "csv": "xls",
    "ppt": "ppt", "pptx": "ppt",
    "mp4": "mp4", "opus": "opus",
}


def _api(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"code": e.code, "msg": str(e)}


def get_token():
    resp = _api(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode(),
        {"Content-Type": "application/json"},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {resp}")
    return resp["tenant_access_token"]


def _multipart(fields, file_field, filename, content):
    """构造 multipart/form-data 请求体，返回 (body, content_type)。"""
    boundary = "----feishu" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
         f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_image(token, path):
    body, ctype = _multipart({"image_type": "message"}, "image",
                             os.path.basename(path), open(path, "rb").read())
    resp = _api("https://open.feishu.cn/open-apis/im/v1/images", body,
                {"Content-Type": ctype, "Authorization": f"Bearer {token}"})
    if resp.get("code") != 0:
        raise RuntimeError(f"图片上传失败: {resp}")
    return resp["data"]["image_key"]


def upload_file(token, path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ftype = FILE_TYPE_BY_EXT.get(ext, "stream")
    body, ctype = _multipart({"file_type": ftype, "file_name": os.path.basename(path)},
                             "file", os.path.basename(path), open(path, "rb").read())
    resp = _api("https://open.feishu.cn/open-apis/im/v1/files", body,
                {"Content-Type": ctype, "Authorization": f"Bearer {token}"})
    if resp.get("code") != 0:
        raise RuntimeError(f"文件上传失败: {resp}")
    return resp["data"]["file_key"]


def send_message(token, chat_id, msg_type, content):
    resp = _api(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        json.dumps({
            "receive_id": chat_id,
            "msg_type":   msg_type,
            "content":    json.dumps(content, ensure_ascii=False),
            "uuid":       uuid.uuid4().hex,   # 幂等，避免重试时重复发送
        }).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"消息发送失败: {resp}")


def main():
    args, files, chat_id = sys.argv[1:], [], ""
    i = 0
    while i < len(args):
        if args[i] == "--chat" and i + 1 < len(args):
            chat_id = args[i + 1]
            i += 2
        else:
            files.append(args[i])
            i += 1

    if not files:
        print(__doc__)
        sys.exit(1)

    if not chat_id:
        try:
            chat_id = open(CHAT_ID_FILE).read().strip()
        except Exception:
            pass
    if not chat_id:
        print("❌ 没有 chat_id：请先在飞书里发过消息，或用 --chat <chat_id> 指定")
        sys.exit(1)

    try:
        token = get_token()
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    ok = 0
    for p in files:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(p):
            print(f"❌ 文件不存在: {p}")
            continue
        if os.path.getsize(p) > MAX_SIZE:
            print(f"❌ 超过飞书 30MB 上限: {p}")
            continue
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        try:
            if ext in IMAGE_EXTS:
                key = upload_image(token, p)
                send_message(token, chat_id, "image", {"image_key": key})
            else:
                key = upload_file(token, p)
                send_message(token, chat_id, "file", {"file_key": key})
            print(f"✅ 已发送到飞书: {os.path.basename(p)}")
            ok += 1
        except Exception as e:
            print(f"❌ 发送失败 {p}: {e}")

    sys.exit(0 if ok == len(files) else 1)


if __name__ == "__main__":
    main()
