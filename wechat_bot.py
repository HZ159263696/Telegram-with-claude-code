#!/usr/bin/env python3
"""
微信网页版 Telegram 控制机器人
- 自动打开 wx.qq.com，截图二维码发到 Telegram
- 登录后监控新消息，转发到 Telegram
- 通过文件 IPC 接收回复指令
用法: TELEGRAM_BOT_TOKEN=xxx python3 wechat_bot.py
"""

import os
import sys
import json
import time
import base64
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── 配置 ──────────────────────────────────────────────────────────────────────
TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN",
           open(Path.home()/".claude/hooks/send-to-telegram.py").read()
           .split('TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "')[1].split('"')[0]
           if (Path.home()/".claude/hooks/send-to-telegram.py").exists() else "")
CHAT_ID  = (Path.home()/".claude/telegram_chat_id").read_text().strip() \
           if (Path.home()/".claude/telegram_chat_id").exists() else ""
SESSION_DIR = Path.home() / ".claude" / "wechat_session"
SEND_FILE   = Path("/tmp/wechat_send.json")   # IPC: bridge 写，本脚本读
WECHAT_URL  = "https://wx.qq.com"

# ── Telegram 工具函数 ──────────────────────────────────────────────────────────
def tg_api(method, **kwargs):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(kwargs).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[tg] {method} 失败: {e}")
        return {}

def send_text(text):
    return tg_api("sendMessage", chat_id=CHAT_ID, text=text,
                  parse_mode="HTML")

def send_photo(path, caption=""):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    with open(path, "rb") as f:
        img_data = f.read()
    import email.mime.multipart, email.mime.base, email.mime.text
    boundary = "----FormBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{CHAT_ID}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{caption}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="qr.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body,
          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[tg] sendPhoto 失败: {e}")
        return {}

# ── 主逻辑 ────────────────────────────────────────────────────────────────────
def run():
    if not TOKEN or not CHAT_ID:
        print("缺少 TELEGRAM_BOT_TOKEN 或 chat_id，退出")
        sys.exit(1)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    seen_msgs = set()   # 已发送过的消息 key

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-remote-fonts"],
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        def _screenshot(path):
            """绕过 Playwright 字体等待，直接用 CDP 截图"""
            try:
                cdp = page.context.new_cdp_session(page)
                result = cdp.send("Page.captureScreenshot", {"format": "png"})
                with open(path, "wb") as f:
                    f.write(base64.b64decode(result["data"]))
                cdp.detach()
            except Exception as e:
                print(f"[screenshot] CDP 失败，回退: {e}")
                page.screenshot(path=path, timeout=15000)

        # ── 1. 打开微信网页版 ──────────────────────────────────────────────
        send_text("⏳ 正在打开微信网页版...")
        page.goto(WECHAT_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # ── 2. 检查是否需要扫码登录 ───────────────────────────────────────
        qr_screenshot = "/tmp/wechat_qr.png"
        if _need_login(page):
            send_text("📱 请扫描二维码登录微信：")
            _screenshot(qr_screenshot)
            send_photo(qr_screenshot, "扫码后等待自动检测登录")

            # 等待登录（最多 3 分钟）
            logged_in = False
            for _ in range(36):
                time.sleep(5)
                if not _need_login(page):
                    logged_in = True
                    break
                # 每 30 秒刷新一次二维码截图
                _screenshot(qr_screenshot)

            if not logged_in:
                send_text("❌ 登录超时，请重新运行脚本")
                browser.close()
                return

        send_text("✅ 微信登录成功！开始监控新消息...")
        time.sleep(2)

        # ── 3. 主循环：监控消息 + 处理回复指令 ───────────────────────────
        while True:
            try:
                # 3a. 检查并转发新消息
                _check_new_messages(page, seen_msgs)

                # 3b. 检查回复指令文件
                if SEND_FILE.exists():
                    try:
                        cmd = json.loads(SEND_FILE.read_text())
                        SEND_FILE.unlink()
                        _send_wechat_msg(page, cmd.get("to",""), cmd.get("msg",""))
                    except Exception as e:
                        send_text(f"⚠️ 发送失败: {e}")

                time.sleep(3)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[loop] 错误: {e}")
                time.sleep(5)

        browser.close()

# ── 辅助函数 ──────────────────────────────────────────────────────────────────
def _need_login(page):
    """检查是否在登录页（二维码页面）"""
    try:
        # wx.qq.com 登录页有 #login_qrcode_img 或 .qrcode
        return page.locator("#login_qrcode_img, .qrcode, .login__code img").count() > 0
    except:
        return False

def _check_new_messages(page, seen):
    """检查未读消息并转发到 Telegram"""
    try:
        # 未读消息条目（有红点数字的聊天项）
        items = page.locator(".chat_item").all()
        for item in items:
            badge = item.locator(".count")
            if badge.count() == 0:
                continue
            count_text = badge.first.inner_text().strip()
            if not count_text:
                continue

            # 获取联系人名
            try:
                name = item.locator(".nickname").first.inner_text().strip()
            except:
                name = "未知"

            key = f"{name}:{count_text}"
            if key in seen:
                continue
            seen.add(key)

            # 点开聊天，截图
            item.click()
            time.sleep(1)
            ss_path = f"/tmp/wechat_chat_{int(time.time())}.png"
            _screenshot(ss_path)

            # 获取最新消息文字
            try:
                msgs = page.locator(".msg_content").all()
                last_text = msgs[-1].inner_text().strip() if msgs else "(无法读取内容)"
            except:
                last_text = "(无法读取内容)"

            send_text(f"💬 <b>{name}</b> 有 {count_text} 条新消息\n{last_text}")
            send_photo(ss_path, f"来自 {name}")

    except Exception as e:
        print(f"[check_msgs] {e}")

def _send_wechat_msg(page, to: str, msg: str):
    """在微信网页版中找到联系人并发送消息"""
    try:
        # 等待搜索框出现（AngularJS 异步渲染）
        try:
            page.wait_for_selector("#search_bar input", timeout=8000)
        except PWTimeout:
            send_text("❌ 找不到搜索框（页面未就绪）")
            return
        search = page.locator("#search_bar input")
        search.click()
        search.fill(to)
        time.sleep(1.5)

        # 等待并点击第一个搜索结果
        try:
            page.wait_for_selector(".chat_item", timeout=5000)
        except PWTimeout:
            send_text(f"❌ 找不到联系人：{to}")
            return
        page.locator(".chat_item").first.click()
        time.sleep(1)

        # 等待并输入消息
        try:
            page.wait_for_selector("#editArea, .editArea", timeout=5000)
        except PWTimeout:
            send_text("❌ 找不到输入框")
            return
        edit = page.locator("#editArea, .editArea").first
        edit.click()
        edit.fill(msg)
        page.keyboard.press("Enter")
        time.sleep(0.5)
        send_text(f"✅ 已向 <b>{to}</b> 发送：{msg}")

    except Exception as e:
        send_text(f"❌ 发送出错: {e}")

if __name__ == "__main__":
    run()
