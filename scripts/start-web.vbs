' Windowless launcher for the dashboard scheduled task.
'
' A task running under an interactive principal has a console allocated by the OS
' before PowerShell starts, so -WindowStyle Hidden cannot suppress it: that is a
' host preference applied only once the host is already running. With Windows
' Terminal as the default terminal application the allocation surfaced as a stray
' tab on the desktop. wscript.exe is a GUI-subsystem host, and Run(..., 0, True)
' starts the child hidden, so no window is shown and no terminal handoff happens.
' Waiting on the child lets its exit code pass through, which keeps LastTaskResult
' meaningful and leaves restart-on-failure working.
Option Explicit

Dim shell, fileSystem, scriptDirectory, port, command

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)

port = "8765"
If WScript.Arguments.Count > 0 Then
    port = WScript.Arguments(0)
End If

' The port is interpolated into a command line, so refuse anything but a number.
If Not IsNumeric(port) Then
    WScript.Quit 1
End If

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & _
    scriptDirectory & "\start-web.ps1"" -Port " & port

WScript.Quit shell.Run(command, 0, True)
