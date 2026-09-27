<#
Registers feeder.py as a Windows Scheduled Task that:
  - starts at boot (no one needs to log in),
  - checks every 5 minutes and starts it again if it is not running (this is what
    revives it after a crash or a kill; Task Scheduler's own "restart on failure"
    option does not fire when the program is killed or crashes mid-run),
  - never stops it for running "too long" (Task Scheduler's default 72h limit is turned off),
  - never runs two copies at once.

Run from an ELEVATED (Run as administrator) PowerShell, from anywhere:
    powershell -ExecutionPolicy Bypass -File scripts\register_feeder_task.ps1

Re-running it replaces the existing task. Use -CurrentUser only for testing
without administrator rights (starts at YOUR logon instead of at boot).
#>
param(
    [string]$TaskName = "TallyFeeder",
    [switch]$CurrentUser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Cannot find $python. Create the virtual environment first (see FEEDER_SETUP.md, step 4)."
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Warning ".env not found in $root - the feeder will exit immediately until it exists (see FEEDER_SETUP.md, step 5)."
}

# Repeating trigger: a daily trigger that repeats every 5 minutes for the whole day
# (Task Scheduler rejects an "indefinite" repetition from PowerShell, and a daily
# trigger that repeats for 24 hours is the same thing). With MultipleInstances=IgnoreNew
# a tick while the feeder is already running does nothing; a tick after it died starts it.
$repeat = New-ScheduledTaskTrigger -Daily -At "00:00"
$repeat.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition

$action = New-ScheduledTaskAction -Execute $python -Argument "feeder.py" -WorkingDirectory $root
if ($CurrentUser) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
} else {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
}
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $repeat) `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started. Check logs\feeder.log in a minute."
