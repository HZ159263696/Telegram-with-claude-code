Set WShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
Desktop = WShell.SpecialFolders("Desktop")

' ── 创建「启动」快捷方式 ──
Set oLink = WShell.CreateShortcut(Desktop & "\Claude Bridge 控制台.lnk")
oLink.TargetPath = ScriptDir & "\启动控制台.vbs"
oLink.WorkingDirectory = ScriptDir
oLink.Description = "启动 Claude Bridge 控制台"
' 用 wscript 运行（无黑窗口）
oLink.TargetPath = "wscript.exe"
oLink.Arguments = """" & ScriptDir & "\启动控制台.vbs"""
' 图标：用项目自带图标
oLink.IconLocation = ScriptDir & "\icon.ico, 0"
oLink.Save

' ── 创建「停止」快捷方式 ──
Set oLink2 = WShell.CreateShortcut(Desktop & "\Claude Bridge 停止.lnk")
oLink2.TargetPath = "wscript.exe"
oLink2.Arguments = """" & ScriptDir & "\停止控制台.vbs"""
oLink2.WorkingDirectory = ScriptDir
oLink2.Description = "停止 Claude Bridge"
oLink2.IconLocation = ScriptDir & "\icon.ico, 0"
oLink2.Save

MsgBox "桌面快捷方式已创建！" & Chr(13) & Chr(10) & Chr(13) & Chr(10) & "· Claude Bridge 控制台（启动）" & Chr(13) & Chr(10) & "· Claude Bridge 停止", 64, "完成"
