<#
.SYNOPSIS
  Removes the Jarvis logon task.

.DESCRIPTION
  Anything that installs itself into a system must be removable the same way.
  Leaves any running server alone - stopping it is a separate decision, so pass
  -StopServer if that is what you want.

.EXAMPLE
  .\scripts\uninstall-task.ps1
  .\scripts\uninstall-task.ps1 -StopServer
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'Jarvis',
    [switch]$StopServer,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    Write-Host "No scheduled task named '$TaskName'. Nothing to remove."
}

if ($StopServer) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force
        Write-Host "Stopped the server on port $Port (PID $($conn.OwningProcess))."
    } else {
        Write-Host "Nothing listening on port $Port."
    }
} else {
    Write-Host "Any running server was left alone. Pass -StopServer to stop it too."
}
