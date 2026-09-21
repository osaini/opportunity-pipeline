<#
.SYNOPSIS
    Run the browser-driven UI/UX suite.

.DESCRIPTION
    Wraps pytest with the project's isolated .venv-ui interpreter so the suite can
    be run without activating anything. Creates the environment on first use.

.EXAMPLE
    ./scripts/ui-test.ps1
    Run every non-visual browser test.

.EXAMPLE
    ./scripts/ui-test.ps1 -Headed -Filter test_keyboard
    Watch the keyboard suite run in a visible browser.

.EXAMPLE
    ./scripts/ui-test.ps1 -Visual
    Compare screenshots against the committed baselines for this platform.

.EXAMPLE
    ./scripts/ui-test.ps1 -UpdateBaselines
    Rewrite those baselines after an intentional design change.
#>
[CmdletBinding()]
param(
    [string]$Filter,
    [switch]$Headed,
    [switch]$Visual,
    [switch]$UpdateBaselines,
    [switch]$Setup
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv-ui"
$python = Join-Path $venv "Scripts\python.exe"

if ($Setup -or -not (Test-Path $python)) {
    Write-Host "Creating $venv ..." -ForegroundColor Cyan
    # 3.12 matches the CI runner; Playwright wheels lag newer Python releases.
    $base = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $base)) { $base = "py"; $baseArgs = @("-3.12") } else { $baseArgs = @() }
    & $base @baseArgs -m venv $venv
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -r (Join-Path $root "requirements-web.lock") -r (Join-Path $root "requirements-ui.txt")
    & $python -m playwright install chromium
    Write-Host "Environment ready." -ForegroundColor Green
    if ($Setup) { return }
}

$arguments = @("-m", "pytest", "tests/ui")
if ($Visual -or $UpdateBaselines) { $arguments += @("-m", "visual") }
if ($UpdateBaselines) { $arguments += "--update-visual-baselines" }
if ($Headed) { $arguments += @("--headed", "--slowmo", "250") }
if ($Filter) { $arguments += @("-k", $Filter) }
$arguments += @("--tracing", "retain-on-failure", "--output", (Join-Path $root "data\ui-artifacts"))

Push-Location $root
try {
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
