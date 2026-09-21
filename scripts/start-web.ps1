<#
.SYNOPSIS
    Run the local opportunity dashboard for Windows Task Scheduler.

.DESCRIPTION
    Anchors execution to the project directory, refuses to compete with an
    unrelated listener on the configured port, and appends server output to
    data/web.log. Task Scheduler owns restart-on-failure behavior.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$logDirectory = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory 'web.log'

function Write-WebLog([string]$Message) {
    "$(Get-Date -Format o)  $Message" | Add-Content -LiteralPath $logPath -Encoding utf8
}

$listener = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 5
        if ($health.ok) {
            Write-WebLog "Dashboard is already healthy on port $Port (PID $($listener.OwningProcess)); no second server started."
            exit 0
        }
    } catch {
        # The listener is not this application, so report the conflict below.
    }
    Write-WebLog "FATAL: port $Port is occupied by PID $($listener.OwningProcess), but the dashboard health check failed."
    exit 1
}

# A project virtualenv (python -m venv .venv, see SETUP.md) wins over the system
# interpreter, which may not have the web dependencies installed.
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonArguments = @()
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
    $pythonArguments = @('-3')
}
if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    $pythonArguments = @()
}
if (-not $python) {
    Write-WebLog 'FATAL: no Python interpreter was found on PATH.'
    exit 1
}

$pythonArguments += @('-m', 'opportunity_app.api', '--host', '127.0.0.1', '--port', [string]$Port)
Write-WebLog "Starting local dashboard on http://127.0.0.1:$Port (launcher PID $PID, interpreter $python)"

# Restarts have been observed with no exit line at all, which means the launcher
# was torn down rather than the server returning. Report the ending from a
# finally block so a terminating error or Ctrl+C still leaves a reason behind; a
# run that ends with no line of either kind was killed outright, and the Task
# Scheduler operational log is the place to look for who did it.
$serverExit = $null
$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $python @pythonArguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        if ($line -notmatch '^(Access token|Employer API token|Admin API token|Local web access token):') {
            $line | Add-Content -LiteralPath $logPath -Encoding utf8
        }
    }
    $serverExit = $LASTEXITCODE
} catch {
    Write-WebLog "FATAL: the dashboard launcher failed: $($_.Exception.Message)"
    throw
} finally {
    $ErrorActionPreference = $previousErrorPreference
    if ($null -eq $serverExit) {
        Write-WebLog 'Dashboard launcher stopped before the server reported an exit code.'
    } else {
        Write-WebLog "Dashboard process exited with code $serverExit."
    }
}

if ($null -eq $serverExit) {
    exit 1
}
exit $serverExit
