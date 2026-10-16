# Are the tasks running? Shows last/next run per task, the last log lines and the radar's own run history.
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Get-ScheduledTask -TaskPath "\ADE Radar\" -ErrorAction SilentlyContinue | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    [pscustomobject]@{
        Task       = $_.TaskName
        State      = $_.State
        LastRun    = $i.LastRunTime
        LastResult = $i.LastTaskResult   # 0 = ok, 1 = a source failed, 267009 = running
        NextRun    = $i.NextRunTime
    }
} | Format-Table -AutoSize

Write-Host "--- last run summaries (data\logs\summary.log) ---"
if (Test-Path data\logs\summary.log) { Get-Content data\logs\summary.log -Tail 16 }
Write-Host "--- data\logs\radar.log (last 25 lines) ---"
if (Test-Path data\logs\radar.log) { Get-Content data\logs\radar.log -Tail 25 }
Write-Host "--- ade-radar status ---"
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m ade_radar status
