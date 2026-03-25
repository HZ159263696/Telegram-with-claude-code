Set WShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' 获取本文件所在目录
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' 无窗口运行 bat
WShell.Run "cmd /c """ & ScriptDir & "\启动控制台.bat""", 0, False
