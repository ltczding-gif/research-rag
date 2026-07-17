# research-rag bootstrap (Windows / PowerShell).
#
# Default: creates a single .venv\ at the repo root with everything.
# -Isolated: creates separate scanner\.venv and service\.venv.
# See README.md "Advanced installation" for when to use isolated mode.
#
# Idempotent — safe to re-run.

param(
    [switch]$Isolated,
    [switch]$Simple,    # explicit; same as default
    [switch]$SkipInit,
    [switch]$Help
)

if ($Help) {
    @"
Usage: .\setup.ps1 [-Isolated] [-SkipInit]

Default: single .venv\ at the repo root with everything installed.
         Simplest setup; works for most users with a clean Python 3.10+.

-Isolated: separate scanner\.venv and service\.venv (advanced).
           Use this if your Python environment has had transitive-dep
           conflicts before (anaconda + uv + system Python coexisting,
           etc.) and you want ChromaDB's heavy dep tree fully isolated
           from the scanner side.
           See README.md "Advanced installation" for details.

-SkipInit: install dependencies only. By default setup immediately starts
           the interactive configuration + health-check walkthrough.
"@
    exit 0
}

$ErrorActionPreference = 'Stop'
$REPO_ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $REPO_ROOT

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$Description,
        [switch]$Quiet
    )

    if ($Quiet) {
        & $FilePath @ArgumentList | Out-Null
    } else {
        & $FilePath @ArgumentList
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit $LASTEXITCODE)"
    }
}

Write-Host "==> research-rag setup"
Write-Host "    repo: $REPO_ROOT"
$mode = if ($Isolated) { "isolated (two venvs)" } else { "default (single venv)" }
Write-Host "    mode: $mode"

# --- Python check ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Error "[FATAL] python not found on PATH"
    exit 1
}
$pyv = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "    python: $($python.Source) ($pyv)"
& $python.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "[FATAL] Python 3.10+ is required (found $pyv)"
    exit 1
}

if ($Isolated) {
    # --- Isolated: two venvs ---
    Write-Host "==> [1/3] service venv: .\service\.venv"
    if (-not (Test-Path "service\.venv")) {
        Invoke-NativeChecked $python.Source @("-m", "venv", "service\.venv") "service venv creation"
    }
    Invoke-NativeChecked "service\.venv\Scripts\python.exe" @("-m", "pip", "install", "--upgrade", "pip") "service pip upgrade" -Quiet
    Invoke-NativeChecked "service\.venv\Scripts\python.exe" @("-m", "pip", "install", "-r", "requirements-rag.txt") "service dependency install"
    Write-Host "    OK"

    Write-Host "==> [2/3] scanner venv: .\scanner\.venv"
    if (-not (Test-Path "scanner\.venv")) {
        Invoke-NativeChecked $python.Source @("-m", "venv", "scanner\.venv") "scanner venv creation"
    }
    Invoke-NativeChecked "scanner\.venv\Scripts\python.exe" @("-m", "pip", "install", "--upgrade", "pip") "scanner pip upgrade" -Quiet
    Invoke-NativeChecked "scanner\.venv\Scripts\python.exe" @("-m", "pip", "install", "-r", "requirements-scanner.txt") "scanner dependency install"
    Write-Host "    OK"

    $nextPython = "scanner\.venv\Scripts\python.exe"
} else {
    # --- Default: single venv ---
    Write-Host "==> [1/2] single venv: .\.venv"
    if (-not (Test-Path ".venv")) {
        Invoke-NativeChecked $python.Source @("-m", "venv", ".venv") "venv creation"
    }
    Invoke-NativeChecked ".venv\Scripts\python.exe" @("-m", "pip", "install", "--upgrade", "pip") "pip upgrade" -Quiet
    Invoke-NativeChecked ".venv\Scripts\python.exe" @("-m", "pip", "install", "-r", "requirements.txt") "dependency install"
    Write-Host "    OK"

    $nextPython = ".venv\Scripts\python.exe"
}

Write-Host ""
Write-Host "==> Dependencies installed"

if ($SkipInit) {
    Write-Host "    Interactive configuration skipped (-SkipInit). Run it later with:"
    Write-Host "      $nextPython scanner\init_environment.py"
    exit 0
}

Write-Host "==> Starting interactive configuration and health check"
& $nextPython "scanner\init_environment.py"
$initStatus = $LASTEXITCODE
if ($initStatus -eq 0) {
    Write-Host "==> Setup complete"
    exit 0
}
if ($initStatus -eq 2) {
    Write-Host "==> Setup complete with warnings; review the doctor output above"
    exit 0
}
Write-Error "[ERROR] interactive setup exited with status $initStatus"
exit $initStatus
