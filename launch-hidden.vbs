' Скрытый запуск обработчика yarophoto:// (без окна консоли PowerShell)
' Аргумент передаётся как есть, разбор делает launch.ps1
If WScript.Arguments.Count < 1 Then WScript.Quit 1
url = WScript.Arguments(0)
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\launch.ps1"" """ & url & """", 0, False
