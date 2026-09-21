<#
.SYNOPSIS
    Register the twice-weekly outreach deep search with Task Scheduler.

.DESCRIPTION
    Registers (or replaces) a per-user task that runs
    scripts/run-outreach-discovery.ps1 -Scheduled through the windowless
    scripts/run-outreach-discovery.vbs shim on the chosen days. A run missed
    because the computer was off or asleep starts when it is next available.

    The deep search uses your logged-in Claude Code subscription (run `claude`
    once to sign in). It never sends email.

.PARAMETER DaysOfWeek
    Days to run. Defaults to Monday and Thursday.

.PARAMETER At
    Time of day. Defaults to 07:00.

.EXAMPLE
    .\scripts\install-outreach-task.ps1

.EXAMPLE
    .\scripts\install-outreach-task.ps1 -DaysOfWeek Tuesday, Friday -At 6:30am

    Check on it later with:  Get-ScheduledTaskInfo -TaskName 'internship-pipeline-outreach'
    Remove it with:          Unregister-ScheduledTask -TaskName 'internship-pipeline-outreach'
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'internship-pipeline-outreach',
    [System.DayOfWeek[]]$DaysOfWeek = @('Monday', 'Thursday'),
    [datetime]$At = '07:00'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcherPath = Join-Path $PSScriptRoot 'run-outreach-discovery.vbs'
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Launcher not found: $launcherPath"
}

# Launch through the windowless shim instead of powershell.exe; run-daily.vbs
# explains why a hidden window style on PowerShell itself is too late.
$wscriptExecutable = Join-Path ([Environment]::SystemDirectory) 'wscript.exe'
$action = New-ScheduledTaskAction `
    -Execute $wscriptExecutable `
    -Argument "//nologo `"$launcherPath`" -Scheduled" `
    -WorkingDirectory $projectRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $DaysOfWeek -At $At

# Interactive so installing needs no elevation; the shim keeps it windowless.
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
# Start a missed run when the laptop is next on, on battery too, and give a
# long web search room to finish. IgnoreNew keeps overlapping starts from queueing.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Twice-weekly deep search for cold outreach targets. Adds verified companies to the Outreach tab; never sends email. Logs to data\outreach-discovery.log.' `
    -Action $action `
    -Trigger $weeklyTrigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

$dayNames = ($DaysOfWeek | ForEach-Object { $_.ToString() }) -join ' and '
Write-Output "Registered $TaskName ($dayNames at $($At.ToString('HH:mm')); catches up missed runs; windowless)."
Write-Output "Log: $projectRoot\data\outreach-discovery.log"
