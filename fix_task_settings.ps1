# fix_task_settings.ps1 - allow "AI Briefing Daily" to run on battery
# and catch up missed runs. Right-click > Run with PowerShell as administrator,
# or run from an elevated prompt:
#   powershell -NoProfile -ExecutionPolicy Bypass -File fix_task_settings.ps1

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Set-ScheduledTask -TaskName 'AI Briefing Daily' -TaskPath '\Ai\' -Settings $settings | Out-Null

$task = Get-ScheduledTask -TaskName 'AI Briefing Daily' -TaskPath '\Ai\'
$task.Settings | Select-Object DisallowStartIfOnBatteries, StopIfGoingOnBatteries,
    StartWhenAvailable, WakeToRun, ExecutionTimeLimit | Format-List
Write-Host "Done. DisallowStartIfOnBatteries/StopIfGoingOnBatteries should be False, StartWhenAvailable/WakeToRun True."
