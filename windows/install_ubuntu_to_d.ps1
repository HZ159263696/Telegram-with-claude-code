# Install Ubuntu WSL2 to D drive
# Run as Administrator

param(
    [string]$InstallPath = "D:\WSL\Ubuntu",
    [string]$Username = "hz"
)

# Check admin
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Write-Host "ERROR: Please run as Administrator" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "=== Install Ubuntu WSL2 to D:\WSL\Ubuntu ===" -ForegroundColor Cyan

# Step 1: Update WSL2 kernel
Write-Host "`n[1/5] Updating WSL2 kernel..." -ForegroundColor Green
wsl --update
wsl --shutdown
Start-Sleep -Seconds 3

# Step 2: Clean up any previous broken Ubuntu install
Write-Host "`n[2/5] Cleaning up previous installs..." -ForegroundColor Green
$existing = wsl --list --quiet 2>&1
if ($existing -match "Ubuntu") {
    Write-Host "Found existing Ubuntu, removing..." -ForegroundColor Yellow
    wsl --unregister Ubuntu 2>$null
    wsl --unregister Ubuntu-24.04 2>$null
    wsl --unregister Ubuntu-22.04 2>$null
}
# Clean D:\WSL\Ubuntu if exists
if (Test-Path $InstallPath) {
    Remove-Item -Recurse -Force $InstallPath
}

# Step 3: Download Ubuntu rootfs directly (more reliable than --install)
Write-Host "`n[3/5] Downloading Ubuntu 22.04 rootfs..." -ForegroundColor Green
$TarPath = "D:\WSL\ubuntu2204.tar.gz"
New-Item -ItemType Directory -Force -Path "D:\WSL" | Out-Null
New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null

if (-not (Test-Path $TarPath)) {
    Write-Host "Downloading from Ubuntu servers (about 200MB)..." -ForegroundColor Yellow
    $url = "https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $TarPath -UseBasicParsing
    Write-Host "Download complete." -ForegroundColor Green
} else {
    Write-Host "Using cached download." -ForegroundColor Gray
}

# Step 4: Import to D drive
Write-Host "`n[4/5] Installing Ubuntu to $InstallPath..." -ForegroundColor Green
wsl --import Ubuntu $InstallPath $TarPath --version 2
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Import failed. WSL2 kernel may still need updating." -ForegroundColor Red
    Write-Host "Please download and install the kernel manually:" -ForegroundColor Yellow
    Write-Host "  https://aka.ms/wsl2kernel" -ForegroundColor Cyan
    Read-Host "After installing the kernel, press Enter to retry import"
    wsl --import Ubuntu $InstallPath $TarPath --version 2
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Import still failed. Please restart Windows and try again." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# Step 5: Create user
Write-Host "`n[5/5] Creating user '$Username'..." -ForegroundColor Green
wsl -d Ubuntu -u root -- bash -c "
useradd -m -s /bin/bash $Username 2>/dev/null || true
echo 'Set password for user $Username:'
passwd $Username
usermod -aG sudo $Username
echo -e '[user]\ndefault=$Username' > /etc/wsl.conf
echo 'User created OK'
"
wsl --terminate Ubuntu

Write-Host "`n=== Done! ===" -ForegroundColor Cyan
Write-Host "Ubuntu installed at: $InstallPath" -ForegroundColor Green
Write-Host "Username: $Username" -ForegroundColor Green
Write-Host ""
Write-Host "Test it: wsl -d Ubuntu" -ForegroundColor Yellow
Write-Host "Next:    Run windows\2_setup_env.ps1" -ForegroundColor Yellow
Read-Host "Press Enter to exit"
