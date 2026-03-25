@echo off
chcp 65001 > nul
echo ========================================
echo   Claude Bridge Desktop App - Build
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install dependencies
echo [1/3] Installing dependencies...
pip install PyQt6 PyQt6-WebEngine pyinstaller --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

:: Build exe
echo [2/3] Building exe with PyInstaller...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "Claude Bridge" ^
    --noconfirm ^
    --clean ^
    claude_bridge_app.py

if errorlevel 1 (
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo [3/3] Build complete!
echo Output: dist\Claude Bridge.exe
echo.

:: Ask to create desktop shortcut
set /p SHORTCUT="Create desktop shortcut? (y/n): "
if /i "%SHORTCUT%"=="y" (
    powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Claude Bridge.lnk'); $s.TargetPath = '%CD%\dist\Claude Bridge.exe'; $s.WorkingDirectory = '%CD%'; $s.Description = 'Claude Bridge Desktop App'; $s.Save()"
    echo Desktop shortcut created!
)

echo.
echo Done. You can now run "dist\Claude Bridge.exe"
pause
