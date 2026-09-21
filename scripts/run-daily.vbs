' Windowless launcher for the daily pipeline scheduled task.
'
' Registering powershell.exe directly as the task action shows a blank console
' every time the task fires, and closing that window kills the run partway through
' (LastTaskResult 0xC000013A). Under an interactive principal the OS allocates the
' console before PowerShell starts, so -WindowStyle Hidden is applied too late.
' wscript.exe is a GUI-subsystem host and Run(..., 0, True) starts the child with
' a hidden window, so nothing appears. See start-web.vbs for the same fix applied
' to the dashboard task. Waiting on the child passes its exit code through to
' LastTaskResult. Arguments (such as -Scheduled) are forwarded to run-daily.ps1.
Option Explicit

Dim shell, fileSystem, scriptDirectory, command, index

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & _
    scriptDirectory & "\run-daily.ps1"""
For index = 0 To WScript.Arguments.Count - 1
    command = command & " """ & WScript.Arguments(index) & """"
Next

WScript.Quit shell.Run(command, 0, True)
