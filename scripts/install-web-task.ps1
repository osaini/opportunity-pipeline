<#
.SYNOPSIS
    Install and start the always-on local opportunity dashboard task.

.DESCRIPTION
    Creates stable private role tokens in the gitignored .env file when they
    are absent, registers a per-user task at sign-in, configures restart on
    failure, and starts the task immediately.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'internship-pipeline-web',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot '.env'
$environmentExamplePath = Join-Path $projectRoot '.env.example'
$launcherPath = Join-Path $PSScriptRoot 'start-web.vbs'

if (-not (Test-Path -LiteralPath $environmentPath)) {
    Copy-Item -LiteralPath $environmentExamplePath -Destination $environmentPath
}

function New-PrivateToken {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Ensure-PrivateSetting([string]$Content, [string]$Name) {
    $pattern = "(?m)^$([Regex]::Escape($Name))=(.*)$"
    $match = [Regex]::Match($Content, $pattern)
    if ($match.Success -and $match.Groups[1].Value.Trim()) {
        return $Content
    }

    $replacement = "$Name=$(New-PrivateToken)"
    if ($match.Success) {
        return [Regex]::Replace($Content, $pattern, $replacement, 1)
    }
    return $Content.TrimEnd() + "`r`n$replacement`r`n"
}

$environmentContent = [IO.File]::ReadAllText($environmentPath)
foreach ($setting in @('PIPELINE_WEB_TOKEN', 'PIPELINE_EMPLOYER_TOKEN', 'PIPELINE_ADMIN_TOKEN')) {
    $environmentContent = Ensure-PrivateSetting -Content $environmentContent -Name $setting
}
$utf8WithoutBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($environmentPath, $environmentContent, $utf8WithoutBom)

# Launch through the windowless shim instead of powershell.exe. Running the host
# directly under an interactive principal shows a console the task cannot suppress;
# scripts/start-web.vbs explains why -WindowStyle Hidden is too late to help.
$wscriptExecutable = Join-Path ([Environment]::SystemDirectory) 'wscript.exe'
$arguments = "//nologo `"$launcherPath`" $Port"
$action = New-ScheduledTaskAction -Execute $wscriptExecutable -Argument $arguments -WorkingDirectory $projectRoot
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
# Stays Interactive so the install works without elevation. An S4U principal would
# also avoid the console, but registering one requires an elevated session, and the
# shim above already keeps the task windowless under this principal.
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Keep the local Opportunity Pipeline dashboard available at sign-in.' `
    -Action $action `
    -Trigger @($logonTrigger, $watchdogTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "Installed and started $TaskName."
Write-Output "Dashboard: http://127.0.0.1:$Port"
Write-Output "Log: $projectRoot\data\web.log"
