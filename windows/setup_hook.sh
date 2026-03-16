#!/bin/bash
set -e
BOT_TOKEN="$1"

if [ -z "$BOT_TOKEN" ]; then
    echo "Usage: setup_hook.sh <TELEGRAM_BOT_TOKEN>"
    exit 1
fi

PROJECT="/mnt/d/AI/claudecode-telegram-main"

# Save token to profile
grep -q TELEGRAM_BOT_TOKEN ~/.bashrc 2>/dev/null || echo "export TELEGRAM_BOT_TOKEN=\"$BOT_TOKEN\"" >> ~/.bashrc
grep -q TELEGRAM_BOT_TOKEN ~/.profile 2>/dev/null || echo "export TELEGRAM_BOT_TOKEN=\"$BOT_TOKEN\"" >> ~/.profile

# Copy and configure stop hook
mkdir -p ~/.claude/hooks
cp "$PROJECT/hooks/send-to-telegram.sh" ~/.claude/hooks/send-to-telegram.sh
sed -i "s|YOUR_BOT_TOKEN_HERE|$BOT_TOKEN|g" ~/.claude/hooks/send-to-telegram.sh
chmod +x ~/.claude/hooks/send-to-telegram.sh

# Write Claude Code settings.json
mkdir -p ~/.claude
cat > ~/.claude/settings.json << JSON
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/send-to-telegram.sh"
          }
        ]
      }
    ]
  }
}
JSON

echo "Hook configured at: ~/.claude/hooks/send-to-telegram.sh"
echo "Settings saved at:  ~/.claude/settings.json"
echo "Bot token saved to: ~/.bashrc and ~/.profile"
