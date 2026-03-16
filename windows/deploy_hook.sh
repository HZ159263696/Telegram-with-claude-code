#!/bin/bash
cp /mnt/d/AI/claudecode-telegram-main/hooks/send-to-telegram.py ~/.claude/hooks/send-to-telegram.py
chmod +x ~/.claude/hooks/send-to-telegram.py

cat > ~/.claude/settings.json << 'JSONEOF'
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/<user>/.claude/hooks/send-to-telegram.py"
          }
        ]
      }
    ]
  },
  "skipDangerousModePermissionPrompt": true
}
JSONEOF

echo "settings.json:"
cat ~/.claude/settings.json
