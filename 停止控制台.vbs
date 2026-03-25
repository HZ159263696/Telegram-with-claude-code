Set WShell = CreateObject("WScript.Shell")

WShell.Run "wsl -d Ubuntu -- bash -c ""pkill -f dashboard.py; pkill -f bridge.py; echo done""", 0, True

MsgBox "Claude Bridge 已停止。", 64, "Claude Bridge"
