# Step 2: Install all dependencies inside WSL Ubuntu
# Run as normal user (no admin needed)

$ProjectPath = "/mnt/d/AI/claudecode-telegram-main"
$BotToken = Read-Host "Enter your Telegram Bot Token"
if (-not $BotToken) {
    Write-Host "ERROR: Bot token is required" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "`n=== Setting up project inside WSL ===" -ForegroundColor Cyan

$SetupScript = @"
set -e
echo '--- Installing system packages ---'
sudo apt-get update -qq
sudo apt-get install -y tmux jq curl python3 python3-pip

echo '--- Installing Node.js (for Claude Code) ---'
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs

echo '--- Installing Claude Code ---'
sudo npm install -g @anthropic-ai/claude-code

echo '--- Saving environment variables ---'
grep -q TELEGRAM_BOT_TOKEN ~/.bashrc || echo 'export TELEGRAM_BOT_TOKEN="$BotToken"' >> ~/.bashrc
grep -q TELEGRAM_BOT_TOKEN ~/.profile || echo 'export TELEGRAM_BOT_TOKEN="$BotToken"' >> ~/.profile

echo '--- Configuring Claude Code Stop hook ---'
mkdir -p ~/.claude/hooks
cp $ProjectPath/hooks/send-to-telegram.sh ~/.claude/hooks/send-to-telegram.sh
chmod +x ~/.claude/hooks/send-to-telegram.sh
sed -i 's|YOUR_BOT_TOKEN_HERE|$BotToken|g' ~/.claude/hooks/send-to-telegram.sh

# Write settings.json
SETTINGS=~/.claude/settings.json
if [ -f "\$SETTINGS" ]; then
    python3 -c "
import json, sys
with open('$HOME/.claude/settings.json') as f:
    s = json.load(f)
hook = {'matcher': '', 'hooks': [{'type': 'command', 'command': '$HOME/.claude/hooks/send-to-telegram.sh'}]}
s.setdefault('hooks', {}).setdefault('Stop', [])
if hook not in s['hooks']['Stop']:
    s['hooks']['Stop'].append(hook)
with open('$HOME/.claude/settings.json', 'w') as f:
    json.dump(s, f, indent=2)
print('Updated existing settings.json')
"
else
    mkdir -p ~/.claude
    cat > ~/.claude/settings.json << 'JSON'
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "HOOK_PATH"
          }
        ]
      }
    ]
  }
}
JSON
    sed -i "s|HOOK_PATH|\$HOME/.claude/hooks/send-to-telegram.sh|g" ~/.claude/settings.json
    echo 'Created new settings.json'
fi

echo ''
echo '=== Setup complete! ==='
echo "Run: wsl -d Ubuntu -- bash $ProjectPath/windows/start.sh"
"@

# Replace token placeholder in script
$SetupScript = $SetupScript.Replace('$BotToken', $BotToken)

Write-Host "Running setup inside WSL..." -ForegroundColor Green
wsl -d Ubuntu -- bash -c $SetupScript

Write-Host "`n=== Done! ===" -ForegroundColor Cyan
Write-Host "Now run: windows\3_start.ps1" -ForegroundColor Yellow
Read-Host "Press Enter to exit"
