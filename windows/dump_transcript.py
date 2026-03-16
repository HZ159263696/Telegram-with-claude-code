import json, sys, glob, os

# Find latest transcript
pattern = os.path.expanduser("~/.claude/projects/-mnt-d-AI-claudecode-telegram-main/*.jsonl")
files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
path = files[0] if files else sys.exit("no transcript")

print(f"File: {path}")
with open(path) as f:
    for i, line in enumerate(f, 1):
        try:
            obj = json.loads(line)
        except:
            print(f"  line {i}: [parse error]")
            continue
        t = obj.get("type", "?")
        if t in ("user", "assistant"):
            msg = obj.get("message", {})
            content = msg.get("content", [])
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"][:80])
                elif isinstance(block, str):
                    texts.append(f"[str: {block[:40]}]")
            print(f"  line {i}: type={t} role={msg.get('role','?')} texts={texts}")
        else:
            print(f"  line {i}: type={t}")
