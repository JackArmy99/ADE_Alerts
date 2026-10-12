# Wrapper used by Task Scheduler. Examples:
#   scripts\run.ps1 run --source instagram
#   scripts\run.ps1 daily            (heartbeat + suggested-follows digest)
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$RadarArgs)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$Exe = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("task_" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Invoke-Radar([string[]]$a) {
    "==== $(Get-Date -Format s) ade-radar $($a -join ' ')" | Out-File -Append -Encoding utf8 $Log
    & $Exe -m ade_radar @a *>&1 | Out-File -Append -Encoding utf8 $Log
    return $LASTEXITCODE
}

if ($RadarArgs.Count -eq 1 -and $RadarArgs[0] -eq "daily") {
    $a = Invoke-Radar @("heartbeat")
    $b = Invoke-Radar @("digest")
    exit [Math]::Max($a, $b)
}
exit (Invoke-Radar $RadarArgs)
