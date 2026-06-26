# ============================================================
#  Windows Task Scheduler - Daily Batch Registration
#  Admin PowerShell: powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#  Schedule: Mon-Fri 16:30 (after TSE close)
# ============================================================
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $root "scripts\run_daily.bat"
$taskName = "SurgeRadarDaily"

$action  = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 3)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force -Description "JP Surge Radar daily prediction pipeline (Mon-Fri 16:30)"

$next = (Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo).NextRunTime
Write-Host "Registered: $taskName"
Write-Host "Next run: $next"
Write-Host "Log: $root\data\logs\daily_YYYYMMDD.log"
Write-Host "Web: scripts\serve.bat"
