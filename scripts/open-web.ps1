<#
.SYNOPSIS
    Start the private local dashboard if needed and open it in the browser.

.DESCRIPTION
    Intended to be invoked by the root-level Open Pipeline.vbs shortcut. The
    existing per-user Scheduled Task owns the long-running server; this script
    installs that task on first use, starts it on later uses, waits for the
    loopback health endpoint, and opens the user's default browser.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = 'internship-pipeline-web'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $projectRoot 'data'
$logPath = Join-Path $logDirectory 'web.log'
$url = "http://127.0.0.1:$Port"
$healthUrl = "$url/api/v1/health"

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-LauncherLog([string]$Message) {
    "$(Get-Date -Format o)  launcher: $Message" |
        Add-Content -LiteralPath $logPath -Encoding utf8
}

function Test-PipelineHealth {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return [bool]$health.ok
    } catch {
        return $false
    }
}

try {
    if (-not (Test-PipelineHealth)) {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-LauncherLog "Installing the per-user background task on port $Port."
            & (Join-Path $PSScriptRoot 'install-web-task.ps1') `
                -TaskName $TaskName `
                -Port $Port | Out-Null
        } else {
            Write-LauncherLog "Starting the existing background task on port $Port."
            Start-ScheduledTask -TaskName $TaskName
        }

        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and -not (Test-PipelineHealth)) {
            Start-Sleep -Milliseconds 250
        }
    }

    if (-not (Test-PipelineHealth)) {
        throw "The health endpoint did not become ready within 30 seconds."
    }

    # The Python launcher trades the owner token in .env for a one-time sign-in
    # ticket, so the browser opens signed in. Without an interpreter, fall back
    # to the plain URL and the sign-in gate.
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $pythonArguments = @()
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command py -ErrorAction SilentlyContinue).Source
        $pythonArguments = @('-3')
    }
    if ($python) {
        Write-LauncherLog "Opening a signed-in browser tab on port $Port."
        Push-Location $projectRoot
        try {
            & $python @pythonArguments -m opportunity_app.launch --port $Port open 2>&1 | ForEach-Object { Write-LauncherLog "$_" }
            if ($LASTEXITCODE -eq 0) { exit 0 }
        } finally {
            Pop-Location
        }
        Write-LauncherLog "The signed-in launch failed; opening the sign-in page instead."
    }
    Write-LauncherLog "Opening $url in the default browser."
    Start-Process $url
    exit 0
} catch {
    Write-LauncherLog "FATAL: $($_.Exception.Message)"
    exit 1
}

