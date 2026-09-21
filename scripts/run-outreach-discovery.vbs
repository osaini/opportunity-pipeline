' Windowless launcher for the twice-weekly outreach deep search task.
'
' Same reason as run-daily.vbs: registering powershell.exe directly shows a
' blank console each time the task fires, and closing it kills the run.
' Run(..., 0, True) starts the script hidden and waits, so its exit code reaches
' LastTaskResult. Arguments (such as -Scheduled) are forwarded to the script.
Option Explicit

Dim shell, fileSystem, scriptDirectory, command, index

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
scriptDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)

command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & _
    scriptDirectory & "\run-outreach-discovery.ps1"""
For index = 0 To WScript.Arguments.Count - 1
    command = command & " """ & WScript.Arguments(index) & """"
Next

WScript.Quit shell.Run(command, 0, True)
