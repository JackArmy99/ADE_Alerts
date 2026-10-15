# One-time setup on native Windows. Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# Uses the official python.org Python 3.12 (signed, so Windows Smart App Control allows it). uv's own Python
# builds are unsigned and Smart App Control can block their DLLs (e.g. _socket.pyd).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$HasPy = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -c "import sys" 2>$null
    $HasPy = ($LASTEXITCODE -eq 0)
}
if (-not $HasPy) {
    Write-Host "Official Python 3.12 not found. Install it (signed by the Python Software Foundation):" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12"
    Write-Host "Then close this window, open a new PowerShell, cd $Root and run this script again."
    exit 1
}

if (Test-Path .venv) {
    Write-Host "Removing the old .venv (it may use an unsigned Python)..."
    Remove-Item -Recurse -Force .venv
}
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example - fill in the secrets now (notepad .env)." -ForegroundColor Yellow
}
# The radar drives your installed Google Chrome (channel=chrome); no Playwright browser download needed.
.venv\Scripts\python.exe -m ade_radar init
Write-Host "Done. Check: .venv\Scripts\python.exe -m ade_radar status" -ForegroundColor Green
