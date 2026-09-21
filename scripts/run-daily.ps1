<#
.SYNOPSIS
    Unattended daily pipeline run, for Windows Task Scheduler.

.DESCRIPTION
    Runs `pipeline.py run`, then a bounded liveness check on the imported
    postings that no source batch retires on its own, then deletes expired
    postings (retired, or past their stated deadline) from pipeline.db, syncs
    the web app's platform.db from it, and purges expired postings there too.

    Safe to run unattended: `run` is idempotent (already-seen postings are
    upserted, not duplicated), it only reads public job-board APIs, and it never
    applies to anything. Output is appended to data/run.log.

    Resumable. Progress is checkpointed to data/daily-run.json after every step,
    so a run cut short by sleep, hibernation, shutdown or a dead battery carries
    on from the step it stopped at the next time the script starts, and the fetch
    skips sources that already succeeded in that run. A fetch that could not
    reach some sources (no network yet after waking) stays unfinished and is
    retried, up to -MaxAttempts times.

.PARAMETER LivenessLimit
    How many postings the liveness pass may check, least recently seen first.
    Bounded so an unattended run cannot spend an unbounded amount of time
    fetching posting pages. Pass 0 to skip the liveness pass entirely.

.PARAMETER Scheduled
    Idempotent mode for the scheduled task, whose triggers (daily, sign-in,
    unlock, wake from sleep, every 30 minutes) fire far more often than once a
    day: resume an unfinished run if there is one, otherwise start today's run
    only if it has not happened yet and it is past -NotBefore, otherwise exit
    silently. Without it, the script resumes an unfinished run or starts a new one.

.PARAMETER NotBefore
    With -Scheduled, the time of day before which no new run starts.

.PARAMETER MaxAttempts
    How many times one run is started before it is accepted as it stands.

.EXAMPLE
    .\scripts\run-daily.ps1

.EXAMPLE
    Register it to run daily at 08:00 without a console window:

    .\scripts\install-daily-task.ps1

    Do not register powershell.exe as the task action directly: that shows a
    blank console on every run, and closing it kills the run.

    Check on it later with:  Get-ScheduledTaskInfo -TaskName 'internship-pipeline'
    Remove it with:          Unregister-ScheduledTask -TaskName 'internship-pipeline'
#>
[CmdletBinding()]
param(
    [int]$LivenessLimit = 40,
    [switch]$Scheduled,
    [string]$NotBefore = '08:00',
    [ValidateRange(1, 20)]
    [int]$MaxAttempts = 4
)

$ErrorActionPreference = 'Stop'

# Task Scheduler starts in %SystemRoot%\system32 unless told otherwise, and
# pipeline.py resolves config/ and data/ relative to its own location — but the
# log path below is relative to the working directory, so anchor both here.
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$logDir = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir 'run.log'
$statePath = Join-Path $logDir 'daily-run.json'

# Exit code pipeline.py uses when some sources were unreachable (EX_TEMPFAIL).
$tempFailExit = 75
# An unfinished run older than this is abandoned rather than resumed, so a
# laptop left closed for days starts a fresh run instead of finishing a stale one.
$resumeWindowHours = 20

function Write-Log([string]$Message) {
    $Message | Add-Content -Path $log -Encoding utf8
}

# The task's triggers overlap (wake and unlock arrive together) and a manual run
# can coincide with a scheduled one. Only one run per project may proceed. A
# mutex held by a process that was killed is reported as abandoned, which still
# hands ownership to the caller.
$mutexName = 'Local\internship-pipeline-daily-' + ($projectRoot -replace '[\\/:]', '_')
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
try {
    $ownsMutex = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $ownsMutex = $true
}
if (-not $ownsMutex) {
    if (-not $Scheduled) { Write-Output 'Another daily run is already in progress.' }
    exit 0
}

function Read-RunState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        return [ordered]@{
            runDate    = [string]$raw.runDate
            startedAt  = [string]$raw.startedAt
            attempts   = [int]$raw.attempts
            completed  = @($raw.completed | Where-Object { $_ })
            finishedAt = $raw.finishedAt
            exitCode   = $raw.exitCode
        }
    } catch {
        # A half-written or hand-edited file is not worth failing the day over.
        return $null
    }
}

function Save-RunState($State) {
    # Write-then-rename, so a power cut mid-write leaves the previous checkpoint.
    $temporary = "$statePath.tmp"
    [pscustomobject]$State | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $statePath -Force
}

function Wait-ForNetwork([int]$Seconds = 60) {
    # After waking, the adapter and DNS come back a few seconds after this
    # script does. Give them a minute rather than burning an attempt on errors.
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        if ([System.Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()) {
            try {
                [System.Net.Dns]::GetHostAddresses('boards-api.greenhouse.io') | Out-Null
                return $true
            } catch { }
        }
        if ((Get-Date) -ge $deadline) {
            # Connected but DNS still failing: go ahead, and let transient
            # errors from the fetch decide whether a retry is needed.
            return [System.Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()
        }
        Start-Sleep -Seconds 5
    }
}

$now = Get-Date
$today = $now.ToString('yyyy-MM-dd')
$state = Read-RunState
$resuming = $false

if ($state -and -not $state.finishedAt) {
    $startedAt = $null
    try {
        $startedAt = [datetime]::Parse($state.startedAt, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind)
    } catch { }
    $age = if ($startedAt) { ($now.ToUniversalTime() - $startedAt.ToUniversalTime()).TotalHours } else { [double]::MaxValue }
    if ($age -lt $resumeWindowHours) {
        $resuming = $true
    } else {
        Write-Log "--- abandoning unfinished run started $($state.startedAt) ---"
    }
}

if (-not $resuming) {
    if ($Scheduled) {
        if ($state -and $state.finishedAt -and $state.runDate -eq $today) { exit 0 }
        if ($now.TimeOfDay -lt ([datetime]$NotBefore).TimeOfDay) { exit 0 }
    }
    $state = [ordered]@{
        runDate    = $today
        startedAt  = $now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        attempts   = 0
        completed  = @()
        finishedAt = $null
        exitCode   = $null
    }
}

$steps = @('run')
if ($LivenessLimit -gt 0) { $steps += 'liveness' }
$steps += 'purge-expired'
if (Test-Path (Join-Path $logDir 'platform.db')) { $steps += 'platform-sync', 'platform-purge', 'outreach-remind' }
$pending = @($steps | Where-Object { $state.completed -notcontains $_ })
$needsNetwork = ($pending -contains 'run') -or ($pending -contains 'liveness')

if ($needsNetwork -and -not (Wait-ForNetwork)) {
    # Offline is not an attempt: save nothing new and wait for the next trigger.
    if (-not $Scheduled) { Write-Output 'No network connection; the run will start when one is available.' }
    exit 0
}

# Schedulers run with a minimal environment, so a bare `py` may not resolve.
# A project virtualenv (python -m venv .venv, see SETUP.md) wins over the system
# interpreter, which may not have the web dependencies installed.
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    "$(Get-Date -Format o)  FATAL: no python interpreter on PATH" | Add-Content -Path $log -Encoding utf8
    exit 1
}

# Python's output is decoded with the console code page, which turned every "…"
# into "à" in the log. Make both sides agree on UTF-8.
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Invoke-Logged([string[]]$Arguments) {
    # Windows PowerShell 5.1 turns each stderr line of a native command into an
    # ErrorRecord when it is redirected, and under 'Stop' the first one aborts the
    # script with exit 1. Any warning from a fetch therefore killed the whole run
    # mid-way. Relax the preference around the call and log stderr as plain text.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python @Arguments 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $log -Encoding utf8
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Complete-Step([string]$Step) {
    $state.completed = @($state.completed) + $Step
    Save-RunState $state
}

$state.attempts = [int]$state.attempts + 1
Save-RunState $state
$lastAttempt = $state.attempts -ge $MaxAttempts

Write-Log ""
if ($resuming) {
    Write-Log "=== $(Get-Date -Format o) run resume (attempt $($state.attempts), started $($state.startedAt), done: $(@($state.completed) -join ', ')) ==="
} else {
    Write-Log "=== $(Get-Date -Format o) run start ==="
}

$runExit = if ($null -ne $state.exitCode) { [int]$state.exitCode } else { 0 }

if ($pending -contains 'run') {
    $runExit = Invoke-Logged @('pipeline.py', 'run', '--resume-since', $state.startedAt)
    $state.exitCode = $runExit
    if ($runExit -eq $tempFailExit -and -not $lastAttempt) {
        # Leave 'run' unfinished so the next trigger fetches only what is missing.
        Save-RunState $state
        Write-Log "=== $(Get-Date -Format o) run paused: some sources were unreachable; will resume ==="
        exit $runExit
    }
    if ($runExit -eq $tempFailExit) {
        Write-Log "--- some sources still unreachable after $($state.attempts) attempts; keeping what arrived ---"
    }
    elseif ($runExit -ne 0) {
        # A fatal failure -- an unusable database rather than a source being
        # down. Only exit 75 was handled before, so any other non-zero exit
        # completed the step, ran the sync and purge on top of an aborted
        # fetch, and recorded the day as finished, after which -Scheduled
        # skipped it. Leave 'run' unfinished and stop instead.
        # finishedAt is deliberately NOT set, on any attempt. Setting it would
        # send every later -Scheduled trigger down the same-day no-op path,
        # which is precisely the silent "completed day" this branch exists to
        # prevent -- the fetch never completed and nothing downstream ran. The
        # run stays unfinished and resumable; the existing 20-hour stale-run
        # window is what eventually abandons it for a fresh day.
        Save-RunState $state
        Write-Log "=== $(Get-Date -Format o) run failed (exit $runExit) on attempt $($state.attempts); downstream steps skipped, run left unfinished ==="
        exit $runExit
    }
    Complete-Step 'run'
}

if ($pending -contains 'liveness') {
    Write-Log "--- liveness (limit $LivenessLimit) ---"
    $livenessExit = Invoke-Logged @('pipeline.py', 'liveness', '--limit', "$LivenessLimit")
    if ($livenessExit -ne 0) {
        Write-Log "--- liveness exited $livenessExit ---"
    }
    Complete-Step 'liveness'
}

# After liveness, so postings it just retired are deleted the same day. Only
# rows that are already retired or past a stated deadline go, and postings with
# an application behind them are kept, so this is safe after a failed `run`.
if ($pending -contains 'purge-expired') {
    Write-Log "--- purge expired ---"
    $purgeExit = Invoke-Logged @('pipeline.py', 'purge-expired')
    if ($purgeExit -ne 0) {
        Write-Log "--- purge-expired exited $purgeExit ---"
    }
    Complete-Step 'purge-expired'
}
# The web app reads platform.db, not pipeline.db. Without this sync every card
# kept the freshness of whenever someone last ran the migration by hand, and
# postings deleted above stayed active there. The sync retires those, so the
# platform purge below can delete them. A failed parity check is logged, not
# fatal: the rows were still written.
if ($pending -contains 'platform-sync') {
    Write-Log "--- sync platform database ---"
    $syncExit = Invoke-Logged @('-m', 'opportunity_app.migrate')
    if ($syncExit -ne 0) {
        Write-Log "--- platform sync exited $syncExit ---"
    }
    Complete-Step 'platform-sync'
}
if ($pending -contains 'platform-purge') {
    $platformPurgeExit = Invoke-Logged @('-m', 'opportunity_app.purge')
    if ($platformPurgeExit -ne 0) {
        Write-Log "--- platform purge exited $platformPurgeExit ---"
    }
    Complete-Step 'platform-purge'
}
# Queue in-app reminders for cold emails whose follow-up date has arrived.
# Nothing is sent; the Outreach tab shows them. Non-fatal like the purges.
if ($pending -contains 'outreach-remind') {
    $remindExit = Invoke-Logged @('-m', 'opportunity_app.outreach_cli', 'remind')
    if ($remindExit -ne 0) {
        Write-Log "--- outreach reminders exited $remindExit ---"
    }
    Complete-Step 'outreach-remind'
}

$state.finishedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
Save-RunState $state
Write-Log "=== $(Get-Date -Format o) run end (exit $runExit) ==="
exit $runExit
