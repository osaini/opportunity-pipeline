<#
.SYNOPSIS
    Register the twice-weekly outreach deep search with Task Scheduler.

.DESCRIPTION
    Registers (or replaces) a per-user task that runs
    scripts/run-outreach-discovery.ps1 -Scheduled through the windowless
    scripts/run-outreach-discovery.vbs shim on the chosen days.

    Besides the weekly time, the task fires at sign-in, on unlock, and on wake
    from sleep. The action names the weekly slots (-Days, -At), so each of those
    starts asks the search whether the newest slot already past has a successful
    run, and ends at once, silently, when it does. A slot missed because the
    computer was off or asleep is therefore searched for as soon as the computer
    is opened, and only once. (Task Scheduler's own "run as soon as possible
    after a missed start" setting did not catch up a Monday run missed with the
    computer off; it stays on as well.)

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
# The slots go to the search as text, so it can work out which one a sign-in or wake start is owed.
$dayList = ($DaysOfWeek | ForEach-Object { $_.ToString() }) -join ','
$action = New-ScheduledTaskAction `
    -Execute $wscriptExecutable `
    -Argument "//nologo `"$launcherPath`" -Scheduled -Days $dayList -At $($At.ToString('HH:mm'))" `
    -WorkingDirectory $projectRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$taskSchedulerNamespace = 'Root/Microsoft/Windows/TaskScheduler'
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $DaysOfWeek -At $At
# Opening the computer: signing in after it was off, unlocking after the lid closed, waking from sleep.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
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
    -Description 'Twice-weekly deep search for cold outreach targets. Catches up a slot missed with the computer off when it is next opened. Adds verified companies to the Outreach tab; never sends email. Logs to data\outreach-discovery.log.' `
    -Action $action `
    -Trigger @($weeklyTrigger, $logonTrigger, $unlockTrigger, $wakeTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

$dayNames = ($DaysOfWeek | ForEach-Object { $_.ToString() }) -join ' and '
Write-Output "Registered $TaskName ($dayNames at $($At.ToString('HH:mm')); a missed slot is searched when the computer is next opened, at sign-in, unlock or wake; windowless)."
Write-Output "Log: $projectRoot\data\outreach-discovery.log"
