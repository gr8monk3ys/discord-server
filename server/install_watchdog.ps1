<#
.SYNOPSIS
  Registers (or updates) the Task Scheduler task "Front Desk watchdog", which runs
  server\watchdog.py every 5 minutes with the shared venv's pythonw.exe (no window).

.DESCRIPTION
  No elevation needed: the task runs as the current user, interactive logon, limited
  rights. Trade-off: it only runs while that user is signed in (it pauses after a sign-out
  and resumes at the next sign-in). Running it signed out (S4U or a stored password) needs an
  elevated PowerShell; the bot task itself already runs at boot as S4U.

  Idempotent: re-running replaces the task's settings (Register-ScheduledTask -Force).
  Run it from the checkout the live bot runs from, so watchdog.py reads that bot\data.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File server\install_watchdog.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Front Desk watchdog',
    [int]$EveryMinutes = 5
)

$ErrorActionPreference = 'Stop'

$serverDir = $PSScriptRoot
$pythonw = Join-Path $serverDir '.venv\Scripts\pythonw.exe'
$script = Join-Path $serverDir 'watchdog.py'
foreach ($p in @($pythonw, $script)) {
    if (-not (Test-Path -LiteralPath $p)) { throw "Not found: $p" }
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $serverDir
# A one-time trigger that repeats forever (no -RepetitionDuration = indefinitely).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Restarts Front Desk bot and alerts the owner when its heartbeat goes stale (server\watchdog.py).' `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registered '$TaskName' for $user every $EveryMinutes min (state: $($task.State))."
Write-Host "Check it with: `"$($serverDir)\.venv\Scripts\python.exe`" `"$script`" --check"
