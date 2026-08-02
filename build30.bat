@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ========================================
echo   AI Bridge 3.0 - Windows EXE Build
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.10+ not found.
    exit /b 1
)

python -c "import PyQt6, PyQt6.QtWebEngineWidgets, PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Missing PyQt6, PyQt6-WebEngine or PyInstaller.
    echo Run: pip install PyQt6 PyQt6-WebEngine pyinstaller
    exit /b 1
)

echo Building AI Bridge 3.0.exe...
pyinstaller --noconfirm --clean "AI Bridge 3.0.spec"
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

if not exist "dist\AI Bridge 3.0.exe" (
    echo BUILD FAILED: output file not found.
    exit /b 1
)

echo.
echo BUILD SUCCESS: dist\AI Bridge 3.0.exe
