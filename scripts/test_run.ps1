# Test run of all sources (Instagram, Gmail, ADE programme) with a readable summary at the end.
#   powershell -ExecutionPolicy Bypass -File scripts\test_run.ps1          dry run: prints the WhatsApp messages, sends nothing
#   powershell -ExecutionPolicy Bypass -File scripts\test_run.ps1 -Live    real run: sends WhatsApp alerts like a scheduled run
# Instagram is checked even if the last check was under 25 minutes ago. Claude checks cost a few cents either way.
param([switch]$Live)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$RadarArgs = @("run", "--source", "all", "--force")
if (-not $Live) { $RadarArgs += "--dry-run" }
Write-Host ("Starting a {0} run of Instagram, Gmail and the ADE programme (takes a few minutes)..." -f $(if ($Live) { "LIVE" } else { "dry" })) -ForegroundColor Cyan
& .venv\Scripts\python.exe -m ade_radar @RadarArgs
if ($LASTEXITCODE -eq 0) {
    Write-Host "Test run finished: everything OK." -ForegroundColor Green
} else {
    Write-Host "Test run finished with a problem: see the FAILED line(s) above." -ForegroundColor Red
}
