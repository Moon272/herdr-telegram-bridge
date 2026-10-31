Set WshShell = CreateObject("WScript.Shell")
Set FileSystem = CreateObject("Scripting.FileSystemObject")
ProjectDirectory = FileSystem.GetParentFolderName(WScript.ScriptFullName)
PythonExecutable = FileSystem.BuildPath(ProjectDirectory, ".venv\Scripts\python.exe")
BridgeScript = FileSystem.BuildPath(ProjectDirectory, "bridge.py")
WshShell.CurrentDirectory = ProjectDirectory
WshShell.Run Chr(34) & PythonExecutable & Chr(34) & " " & Chr(34) & BridgeScript & Chr(34), 0, False
