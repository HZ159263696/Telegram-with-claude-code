# setup_autostart.ps1 - Claude Bridge auto-start + proxy setup
# Run once as normal user (no admin needed)

$ProjectPath = "D:\AI\claudecode-telegram-main"
$TaskName    = "ClaudeTelegramBridge"
$BatPath     = "$ProjectPath\windows\start_bridge.bat"

Write-Host "=== Claude Bridge Setup ===" -ForegroundColor Cyan

# 1. Write proxy to PowerShell Profile
Write-Host "`n[1] Writing proxy to PowerShell Profile..." -ForegroundColor Green

$ProfileDir = Split-Path $PROFILE
if (-not (Test-Path $ProfileDir)) { New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null }
if (-not (Test-Path $PROFILE))    { New-Item -ItemType File -Path $PROFILE -Force | Out-Null }

$profileContent = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($profileContent -notmatch "65533") {
    $ProxyLines = "`r`n# Proxy auto-config`r`n" +
                  '$env:HTTP_PROXY  = "http://127.0.0.1:65533"' + "`r`n" +
                  '$env:HTTPS_PROXY = "http://127.0.0.1:65533"' + "`r`n"
    Add-Content -Path $PROFILE -Value $ProxyLines
    Write-Host "    Written to: $PROFILE" -ForegroundColor Gray
} else {
    Write-Host "    Proxy already exists, skipped." -ForegroundColor Gray
}

# 2. Generate start_bridge.bat
Write-Host "`n[2] Generating start_bridge.bat..." -ForegroundColor Green
$BatContent = "@echo off`r`nwsl -d Ubuntu -- bash /mnt/d/AI/claudecode-telegram-main/windows/start.sh`r`n"
[System.IO.File]::WriteAllText($BatPath, $BatContent, [System.Text.Encoding]::ASCII)
Write-Host "    Created: $BatPath" -ForegroundColor Gray

# 3. Register Task Scheduler (no admin needed with RunLevel Limited)
Write-Host "`n[3] Registering auto-start task..." -ForegroundColor Green

$Action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`""
$Trigger  = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$result = Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action   `
    -Trigger     $Trigger  `
    -Settings    $Settings `
    -Description "Auto-start Claude Telegram Bridge (WSL)" `
    -ErrorAction SilentlyContinue

if ($result) {
    Write-Host "    Task '$TaskName' registered OK." -ForegroundColor Gray
} else {
    Write-Host "    Task Scheduler failed. Using Startup folder instead..." -ForegroundColor Yellow
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = "$StartupDir\ClaudeBridge.bat"
    [System.IO.File]::WriteAllText($ShortcutPath, $BatContent, [System.Text.Encoding]::ASCII)
    Write-Host "    Startup shortcut created: $ShortcutPath" -ForegroundColor Gray
}

# 4. Apply proxy to current session
Write-Host "`n[4] Applying proxy to current session..." -ForegroundColor Green
$env:HTTP_PROXY  = "http://127.0.0.1:65533"
$env:HTTPS_PROXY = "http://127.0.0.1:65533"
Write-Host "    HTTP_PROXY  = $env:HTTP_PROXY" -ForegroundColor Gray
Write-Host "    HTTPS_PROXY = $env:HTTPS_PROXY" -ForegroundColor Gray

Write-Host "`n=== Done! Bridge will auto-start on next Windows login. ===" -ForegroundColor Cyan
Write-Host "Check task: Get-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Read-Host "`nPress Enter to exit"
