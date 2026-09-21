' Double-click launcher for the local Opportunity Pipeline.
' PowerShell runs hidden; the user's default browser opens once the app is ready.
Option Explicit

Dim shell, fileSystem, projectRoot, command, exitCode

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & _
    projectRoot & "\scripts\open-web.ps1"""

exitCode = shell.Run(command, 0, True)
If exitCode <> 0 Then
    MsgBox "The Opportunity Pipeline could not be started." & vbCrLf & vbCrLf & _
        "See data\web.log for details.", vbCritical, "Opportunity Pipeline"
End If

