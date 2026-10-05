<#
.SYNOPSIS
    Twice-weekly deep search for cold outreach targets, for Windows Task Scheduler.

.DESCRIPTION
    Runs `python -m opportunity_app.outreach_cli discover`. Claude Code searches
    the web with only its web search and fetch tools, and Python checks every
    proposal (a working website and at least one source URL that loads) before
    adding it to the Outreach tab. Existing targets are never overwritten.
    Contacts come from each company's own site, and drafts wait for your
    approval: nothing is sent. Output is appended to data/outreach-discovery.log.

    After the search, `outreach_cli enrich --limit 15` fills in where older
    targets are based, from their own sites and SEC Form D filings (the latter
    only when PIPELINE_SEC_USER_AGENT is set in .env). A dry run skips it.

    With -Scheduled a run is skipped when one already succeeded in the last 48
    hours, so a missed Monday run caught up late does not double up with Thursday.

    With -Scheduled -Days and -At (the installed task passes them) the task also
    fires at sign-in, unlock and wake, and each start asks the search whether the
    newest Monday-or-Thursday slot already past has a successful run. If it does,
    the start ends at once, silently, with no backfill and nothing in the log
    (outreach_cli exit 76). If not, this is the catch-up for a slot missed with
    the computer off, and it runs the search.

.PARAMETER Scopes
    Which searches to run. Defaults to all three.

.PARAMETER Days
    With -Scheduled: the weekdays the task is due, comma separated (Monday,Thursday).

.PARAMETER At
    With -Scheduled and -Days: the time of day the task is due (07:00).

.PARAMETER DryRun
    Write data/outreach-discovered-<date>-dry-run.json only; change no rows.

.EXAMPLE
    .\scripts\run-outreach-discovery.ps1 -DryRun

.EXAMPLE
    Register it for Monday and Thursday mornings without a console window:

    .\scripts\install-outreach-task.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('local-accelerators', 'us-startups', 'recently-funded')]
    [string[]]$Scopes = @('local-accelerators', 'us-startups', 'recently-funded'),
    [switch]$Scheduled,
    # A string, not [DayOfWeek[]]: a PowerShell enum would read "Monday,Thursday" as the flags 1 and 4 added up.
    [string]$Days = '',
    [string]$At = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$logDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'outreach-discovery.log'
# Used only when the main log is locked, so this run's output is still kept.
$fallbackLog = Join-Path $logDir ("outreach-discovery-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Write-Log([string]$Message) {
    # Anything holding the log open (a `tail -f`, an editor) makes Add-Content
    # throw on Windows. That must never end the search or lose its output:
    # retry briefly, then write this run's lines to a separate file instead.
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        try {
            $Message | Add-Content -Path $log -Encoding utf8 -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    try {
        $Message | Add-Content -Path $fallbackLog -Encoding utf8 -ErrorAction Stop
    } catch { }
}

# Schedulers run with a minimal environment, so a bare `py` may not resolve.
# A project virtualenv (python -m venv .venv, see SETUP.md) wins over the system
# interpreter, which may not have the web dependencies installed.
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Write-Log "$(Get-Date -Format o)  FATAL: no python interpreter on PATH"
    exit 1
}

$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$arguments = @('-m', 'opportunity_app.outreach_cli', 'discover', '--scopes') + $Scopes
if ($Scheduled) { $arguments += @('--trigger', 'scheduled') }
# The task names its slots so the search can tell whether one is owed; a task installed before that names none.
$catchUp = [bool]($Scheduled -and $Days -and $At)
if ($catchUp) {
    $dayNames = @($Days -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $arguments += @('--due-days') + $dayNames + @('--due-at', $At)
}
if ($DryRun) { $arguments += '--dry-run' }

$mode = if ($DryRun) { ' (dry run)' } else { '' }
$header = "=== $(Get-Date -Format o) deep search: $($Scopes -join ', ')$mode ==="
# Native stderr must not become a terminating ErrorRecord; see run-daily.ps1.
$ErrorActionPreference = 'Continue'
# The header waits for the search's first line of output, so a start with nothing owed writes nothing at all.
$headed = $false
& $python @arguments 2>&1 | ForEach-Object {
    if (-not $headed) { Write-Log $header; $headed = $true }
    Write-Log "$_"
}
$exitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
# 76: the slot this start was for already has its run. Nothing was searched, so there is nothing to back fill either.
if ($catchUp -and $exitCode -eq 76) { exit 0 }
if (-not $headed) { Write-Log $header }
Write-Log "=== finished with exit code $exitCode ==="

# The backfill is independent of the search: it still runs when the search was
# skipped or failed, and its own failure does not hide the search's exit code.
if (-not $DryRun) {
    Write-Log "=== $(Get-Date -Format o) location and Form D backfill ==="
    $ErrorActionPreference = 'Continue'
    & $python -m opportunity_app.outreach_cli enrich --limit 15 2>&1 | ForEach-Object { Write-Log "$_" }
    $enrichCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Write-Log "=== backfill finished with exit code $enrichCode ==="
}

# 75 means another deep search was already running, which is not a failure.
if ($exitCode -eq 75) { exit 0 }
exit $exitCode
