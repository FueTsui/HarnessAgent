Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
BaseDir = Fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.Run chr(34) & BaseDir & "\start.bat" & chr(34), 0, True
Set WshShell = Nothing
Set Fso = Nothing
