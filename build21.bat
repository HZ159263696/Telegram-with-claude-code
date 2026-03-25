@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Building Claude Bridge 2.1...
pyinstaller --onefile --windowed --noconfirm --clean ^
    --name "Claude Bridge2.1" ^
    --icon icon.ico ^
    --add-data "icon.ico;." ^
    --add-data "icon_128.png;." ^
    --add-data "anthropic_proxy.py;." ^
    claude_bridge_app.py
echo.
if exist "dist\Claude Bridge2.1.exe" (
    echo BUILD SUCCESS: dist\Claude Bridge2.1.exe
) else (
    echo BUILD FAILED
)
