# One-time setup on native Windows. Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Neither uv nor Python was found. Install uv (it downloads Python 3.12 for you):" -ForegroundColor Yellow
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host "Then close this window, open a new PowerShell, cd C:\ade-radar and run this script again."
    exit 1
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv .venv --python 3.12
    uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
} else {
    py -3.12 -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example - fill in the secrets now (notepad .env)." -ForegroundColor Yellow
}
# The radar drives your installed Google Chrome (channel=chrome); no Playwright browser download needed.
.venv\Scripts\ade-radar.exe init
Write-Host "Done. Next: .venv\Scripts\ade-radar.exe check-template" -ForegroundColor Green
