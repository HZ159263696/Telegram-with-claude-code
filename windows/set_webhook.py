import subprocess, json, urllib.request, urllib.parse, sys

token = subprocess.check_output(
    ["bash", "-c", "source /etc/claude_env.sh && echo $TELEGRAM_BOT_TOKEN"],
    text=True
).strip()

webhook_url = sys.argv[1] if len(sys.argv) > 1 else ""
if not webhook_url:
    print("Usage: set_webhook.py <url>")
    raise SystemExit(1)

print(f"URL   : {webhook_url}")

data = urllib.parse.urlencode({"url": webhook_url}).encode()
req = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/setWebhook", data=data
)
result = json.loads(urllib.request.urlopen(req, timeout=10).read())
print(f"Result: {result}")
