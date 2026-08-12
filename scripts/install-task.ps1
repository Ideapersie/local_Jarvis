<#
.SYNOPSIS
  Registers a Windows scheduled task that starts Jarvis at logon.

.DESCRIPTION
  APScheduler runs inside the server process, so the 06:30 brief only fires if
  Jarvis is running at 06:30. This starts it at logon so that is usually true.

  It does not fully close the gap on its own: if the machine is off through
  06:30 and you log in at 09:00, the cron job never fires for that day. The
  catch-up in app/jobs.py handles that case by building a missed brief on
  startup. Both pieces are needed.

  Runs as the current user, hidden, and does not require admin.

.EXAMPLE
  .\scripts\install-task.ps1
  .\scripts\install-task.ps1 -DelaySeconds 60
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'Jarvis',
    # Logon fires before the network stack is always ready; a short delay avoids
    # a first run that cannot reach the weather API.
    [int]$DelaySeconds = 30
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Starter = Join-Path $PSScriptRoot 'start-jarvis.ps1'

if (-not (Test-Path $Starter)) {
    Write-Error "Missing $Starter"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Starter`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelaySeconds}S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Interactive principal, so it runs as you with your credentials file at
# ~/.claude/.credentials.json - which is what the subscription auth depends on.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Task '$TaskName' exists, replacing it."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Starts the Jarvis personal dashboard at logon.' | Out-Null

Write-Host ""
Write-Host "Registered scheduled task '$TaskName'."
Write-Host "  Starts at logon, ${DelaySeconds}s delay, hidden, as $env:USERNAME."
Write-Host "  Logs: $(Join-Path $Root 'logs\jarvis.log')"
Write-Host ""
Write-Host "Test without logging out:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:                    .\scripts\uninstall-task.ps1"
