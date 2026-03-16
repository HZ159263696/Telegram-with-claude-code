import os, subprocess, urllib.request, json

# Load token from env file
env_file = "/etc/claude_env.sh"
token = ""
try:
    out = subprocess.check_output(
        ["bash", "-c", f"source {env_file} && echo $TELEGRAM_BOT_TOKEN"],
        text=True
    ).strip()
    token = out
except Exception as e:
    print(f"Load env failed: {e}")

# Also check process environment
token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN is empty")
    raise SystemExit(1)

print(f"Token prefix: {token[:10]}...")

# Test Telegram API
url = f"https://api.telegram.org/bot{token}/getMe"
try:
    resp = urllib.request.urlopen(url, timeout=10)
    data = json.loads(resp.read())
    if data.get("ok"):
        bot = data["result"]
        print(f"Bot OK: @{bot['username']} ({bot['first_name']})")
    else:
        print(f"API error: {data}")
except Exception as e:
    print(f"Request failed: {e}")
