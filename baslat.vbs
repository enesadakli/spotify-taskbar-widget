' Widget'i konsol penceresi acmadan sessizce baslatir
Set sh = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
sh.Run "pythonw.exe """ & scriptDir & "\widget.py""", 0, False
