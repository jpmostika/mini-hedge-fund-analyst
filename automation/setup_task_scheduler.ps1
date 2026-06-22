# Register the daily automation as a Windows Task Scheduler job.
# Run this script ONCE as Administrator.
# Fires at 17:15 Monday–Friday.

$TASK_NAME  = "MeridianCapital_DailyRun"
$SCRIPT     = "c:\Users\jpmos\OneDrive\Jarvis\ls_equity_fund\automation\daily_run.ps1"
$TRIGGER    = New-ScheduledTaskTrigger -Weekly `
                -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                -At "17:15"
$ACTION     = New-ScheduledTaskAction `
                -Execute "powershell.exe" `
                -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$SCRIPT`""
$SETTINGS   = New-ScheduledTaskSettingsSet `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
                -StartWhenAvailable

Register-ScheduledTask `
    -TaskName   $TASK_NAME `
    -Trigger    $TRIGGER `
    -Action     $ACTION `
    -Settings   $SETTINGS `
    -RunLevel   Highest `
    -Force

Write-Host "Task '$TASK_NAME' registered. Runs weekdays at 17:15."
Write-Host "View in Task Scheduler: taskschd.msc"
