# Creates the Task Scheduler jobs (re-run safely; existing tasks are replaced).
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
# Instagram every 30 min, Gmail every 60 min, ADE programme every 2 h (every hour from 19 Oct),
# heartbeat + digest daily at 09:00. Runs only while you are logged on (Instagram needs a visible Chrome).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run.ps1"
$TaskPath = "\ADE Radar\"
$User = "$env:USERDOMAIN\$env:USERNAME"
$Now = Get-Date
$End = Get-Date "2026-10-27T00:00:00"
$Busy = Get-Date "2026-10-19T00:00:00"

function New-RadarAction([string]$ArgLine) {
    New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" $ArgLine"
}
function New-Every([int]$Minutes, [datetime]$From, [datetime]$Until) {
    New-ScheduledTaskTrigger -Once -At $From -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
        -RepetitionDuration ($Until - $From)
}

$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

function Register-Radar([string]$Name, $Trigger, [string]$ArgLine) {
    Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Action (New-RadarAction $ArgLine) `
        -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
    Write-Host "registered $TaskPath$Name"
}

Register-Radar "Instagram" (New-Every 30 $Now.AddMinutes(2) $End) "run --source instagram"
Register-Radar "Gmail" (New-Every 60 $Now.AddMinutes(9) $End) "run --source gmail"

if ($Now -lt $Busy) {
    $AdeTriggers = @((New-Every 120 $Now.AddMinutes(16) $Busy), (New-Every 60 $Busy $End))
} else {
    $AdeTriggers = @(New-Every 60 $Now.AddMinutes(16) $End)
}
Register-Radar "ADE programme" $AdeTriggers "run --source ade"
Register-Radar "Heartbeat" (New-ScheduledTaskTrigger -Daily -At "09:00") "daily"

Write-Host "`nCheck with: powershell -ExecutionPolicy Bypass -File scripts\status.ps1" -ForegroundColor Green
