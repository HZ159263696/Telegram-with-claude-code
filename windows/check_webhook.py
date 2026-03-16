import subprocess, json, urllib.request

# Load token
token = subprocess.check_output(
    ["bash", "-c", "source /etc/claude_env.sh && echo $TELEGRAM_BOT_TOKEN"],
    text=True
).strip()

def api(method):
    url = f"https://api.telegram.org/bot{token}/{method}"
    return json.loads(urllib.request.urlopen(url, timeout=10).read())

# 1. Check webhook
print("=== Webhook Info ===")
wh = api("getWebhookInfo")
info = wh.get("result", {})
url = info.get("url", "")
print(f"URL     : {url or '(none - not set)'}")
print(f"Pending : {info.get('pending_update_count', 0)}")
print(f"Error   : {info.get('last_error_message', 'none')}")

# 2. Check bridge port
import socket
s = socket.socket()
try:
    s.connect(("127.0.0.1", 8080))
    print("\n=== Bridge ===")
    print("Port 8080: LISTENING (bridge is running)")
except:
    print("\n=== Bridge ===")
    print("Port 8080: NOT reachable (bridge not running?)")
finally:
    s.close()

# 3. Diagnosis
print("\n=== Diagnosis ===")
if not url:
    print("PROBLEM: Webhook not registered. Telegram has no URL to send messages to.")
    print("Need to: start localtunnel and register webhook URL.")
elif "8080" not in url and "loca.lt" not in url:
    print(f"PROBLEM: Webhook URL looks wrong: {url}")
else:
    print("Webhook OK. If bot still not responding, check tmux session.")
