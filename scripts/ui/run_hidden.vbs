Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(fso.GetParentFolderName(scriptDir))
Set WshShell = CreateObject("WScript.Shell")
cmd = "cmd /c cd /d """ & repoRoot & """ && streamlit run scripts\ui\app.py"
WshShell.Run cmd, 0, False
