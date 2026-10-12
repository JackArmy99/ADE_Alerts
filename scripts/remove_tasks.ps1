# Removes all ADE Radar scheduled tasks.
Get-ScheduledTask -TaskPath "\ADE Radar\" -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -Confirm:$false
    Write-Host "removed $($_.TaskName)"
}
