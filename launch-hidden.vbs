' Silent launcher for yarophoto:// — runs launch.ps1 without a console window.
' Argument is passed through as-is; launch.ps1 parses the URL.
If WScript.Arguments.Count < 1 Then WScript.Quit 1

Dim sh, fso, scriptDir, url
url = WScript.Arguments(0)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptDir & "\launch.ps1"" """ & url & """", 0, False
