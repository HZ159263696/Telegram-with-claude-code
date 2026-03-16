# Step 3: Start the bridge (run daily when you want to use it)
# No admin needed

$ProjectPath = "/mnt/d/AI/claudecode-telegram-main"

Write-Host "=== Starting Claude-Telegram Bridge ===" -ForegroundColor Cyan

# Check if WSL Ubuntu is available
$wslCheck = wsl -d Ubuntu -- echo "ok" 2>&1
if ($wslCheck -ne "ok") {
    Write-Host "ERROR: WSL Ubuntu not found. Run install_ubuntu_to_d.ps1 first." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "`n[1] Starting tmux session with Claude Code..." -ForegroundColor Green
wsl -d Ubuntu -- bash -c @"
source ~/.bashrc 2>/dev/null; source ~/.profile 2>/dev/null
# Kill old session if exists
tmux kill-session -t claude 2>/dev/null || true
# Create new session running Claude Code
tmux new-session -d -s claude 'claude --dangerously-skip-permissions'
echo 'tmux session started'
"@

Write-Host "[2] Setting up public URL with cloudflared..." -ForegroundColor Green
Write-Host "    (Installing cloudflared if needed)" -ForegroundColor Gray
$TunnelOutput = wsl -d Ubuntu -- bash -c @"
source ~/.profile 2>/dev/null
# Install cloudflared if not present
if ! command -v cloudflared &>/dev/null; then
    curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
    sudo chmod +x /usr/local/bin/cloudflared
fi
# Start tunnel in background, capture URL
cloudflared tunnel --url http://localhost:8080 2>&1 &
CFPID=\$!
echo \$CFPID > /tmp/cf_pid
sleep 4
# Extract the public URL
LOGFILE=/tmp/cf_log_\$\$
cloudflared tunnel --url http://localhost:8080 --logfile \$LOGFILE 2>/dev/null &
echo \$! >> /tmp/cf_pid
sleep 5
grep -o 'https://[^[:space:]]*\.trycloudflare\.com' \$LOGFILE 2>/dev/null | head -1
"@

if ($TunnelOutput -match "https://.*trycloudflare\.com") {
    $PublicUrl = $Matches[0]
    Write-Host "[3] Public URL: $PublicUrl" -ForegroundColor Green

    Write-Host "[4] Registering Telegram webhook..." -ForegroundColor Green
    wsl -d Ubuntu -- bash -c @"
source ~/.profile 2>/dev/null
curl -s "https://api.telegram.org/bot\${TELEGRAM_BOT_TOKEN}/setWebhook?url=$PublicUrl" | python3 -c "import sys,json; r=json.load(sys.stdin); print('Webhook OK' if r.get('ok') else 'Webhook FAILED: '+str(r))"
"@
} else {
    Write-Host "WARNING: Could not get cloudflared URL automatically." -ForegroundColor Yellow
    Write-Host "Start cloudflared manually and set webhook:" -ForegroundColor Yellow
    Write-Host "  wsl -d Ubuntu -- cloudflared tunnel --url http://localhost:8080" -ForegroundColor Gray
    Write-Host "  Then set webhook via Telegram API" -ForegroundColor Gray
}

Write-Host "`n[5] Starting bridge server..." -ForegroundColor Green
Write-Host "    (Press Ctrl+C to stop)" -ForegroundColor Gray
Write-Host ""
wsl -d Ubuntu -- bash -c "source ~/.profile 2>/dev/null; cd $ProjectPath && python3 bridge.py"
