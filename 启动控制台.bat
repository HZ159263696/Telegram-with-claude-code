@echo off
chcp 65001 > nul

:: 检查是否已在运行
wsl -d Ubuntu -- bash -c "pgrep -f dashboard.py > /dev/null && echo RUNNING || echo STOPPED" > %TEMP%\dash_check.txt 2>&1
set /p STATUS=<%TEMP%\dash_check.txt

if "%STATUS%"=="RUNNING" (
    echo Claude Bridge 控制台已在运行，正在打开浏览器...
    start http://localhost:8888
    exit /b 0
)

:: 启动 dashboard（后台运行，日志写到 /tmp/dashboard.log）
wsl -d Ubuntu -- bash -c "source /etc/claude_env.sh && cd /mnt/d/AI/claudecode-telegram-main && nohup python dashboard.py > /tmp/dashboard.log 2>&1 &"

:: 等待服务启动
timeout /t 3 /nobreak > nul

:: 打开浏览器
start http://localhost:8888
