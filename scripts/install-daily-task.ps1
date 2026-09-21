<#
.SYNOPSIS
    Register the unattended daily pipeline run with Task Scheduler.

.DESCRIPTION
    Registers (or replaces) a per-user task that runs scripts/run-daily.ps1
    through the windowless scripts/run-daily.vbs shim, so no console window
    appears and nothing can be closed mid-run.

    Besides the daily time, the task fires at sign-in, on unlock, on wake from
    sleep, and every 30 minutes. run-daily.ps1 -Scheduled makes those extra
    starts cheap no-ops once the day's run is done, and uses them to resume a run
    that sleep, shutdown or a lost connection cut short, or to start a run the
    daily time missed.

.PARAMETER At
    Time of day for the run. Defaults to 08:00.

.EXAMPLE
    .\scripts\install-daily-task.ps1

.EXAMPLE
    .\scripts\install-daily-task.ps1 -At 7:30am
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'internship-pipeline',
    [datetime]$At = '08:00'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$launcherPath = Join-Path $PSScriptRoot 'run-daily.vbs'
if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Launcher not found: $launcherPath"
}

# Launch through the windowless shim instead of powershell.exe; run-daily.vbs
# explains why a hidden window style on PowerShell itself is too late.
$wscriptExecutable = Join-Path ([Environment]::SystemDirectory) 'wscript.exe'
$action = New-ScheduledTaskAction `
    -Execute $wscriptExecutable `
    -Argument "//nologo `"$launcherPath`" -Scheduled -NotBefore $($At.ToString('HH:mm'))" `
    -WorkingDirectory $projectRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$taskSchedulerNamespace = 'Root/Microsoft/Windows/TaskScheduler'
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $At
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
# Closing the lid suspends the run; opening it again unlocks the session and
# logs a wake event. Either one restarts the task, which resumes the run.
$unlockTrigger = New-CimInstance -ClientOnly `
    -CimClass (Get-CimClass -Namespace $taskSchedulerNamespace -ClassName MSFT_TaskSessionStateChangeTrigger) `
    -Property @{ StateChange = [uint32]8; UserId = $userId; Enabled = $true }  # 8 = TASK_SESSION_UNLOCK
$wakeTrigger = New-CimInstance -ClientOnly `
    -CimClass (Get-CimClass -Namespace $taskSchedulerNamespace -ClassName MSFT_TaskEventTrigger) `
    -Property @{
        Enabled      = $true
        Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
            "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" +
            '</Select></Query></QueryList>'
    }
# Safety net for everything the others miss: a run killed by the time limit, or
# a fetch paused because the network was not back yet.
$retryTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 30)

# Interactive so installing needs no elevation; the shim keeps it windowless.
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
# The defaults refuse to start on battery and kill a run when the charger is
# unplugged, so on a laptop the daily fetch would sit queued or die partway.
# IgnoreNew keeps overlapping triggers from queueing duplicate instances.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Daily opportunity fetch, scoring, bounded liveness check, and expired-posting purge. Resumes after sleep or shutdown. Logs to data\run.log.' `
    -Action $action `
    -Trigger @($dailyTrigger, $logonTrigger, $unlockTrigger, $wakeTrigger, $retryTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Output "Registered $TaskName (daily at $($At.ToString('HH:mm')), resumes on sign-in, unlock and wake; windowless)."
Write-Output "Log: $projectRoot\data\run.log"
Write-Output "Progress: $projectRoot\data\daily-run.json"
