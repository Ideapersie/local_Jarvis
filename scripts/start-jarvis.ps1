<#
.SYNOPSIS
  Starts the Jarvis server, unless it is already running.

.DESCRIPTION
  Wrapper the scheduled task runs at logon. The guard matters: Task Scheduler
  will happily start a second instance, and a second uvicorn binding port 8000
  fails in a way that is easy to miss - you end up with an old process serving
  stale code while the new one silently died. That has happened repeatedly
  during development.

  Kept as its own script rather than inlined into the task action so it can be
  run by hand to reproduce exactly what the task does.
#>

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'

# Repo root is this script's parent directory. Resolved rather than assumed,
# because the task's working directory is not guaranteed.
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'jarvis.log'

if (-not (Test-Path $Python)) {
    Write-Error "No venv at $Python. Create it first: python -m venv .venv"
    exit 1
}

# Already listening? Leave it alone.
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Jarvis already listening on port $Port (PID $($existing.OwningProcess)). Nothing to do."
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Working directory must be the repo root, or .env and .claude/ do not load and
# the agent silently runs without its project config.
Start-Process -FilePath $Python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', $Port) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError (Join-Path $LogDir 'jarvis.err.log')

Write-Host "Jarvis starting on http://${BindHost}:${Port} (logging to $LogFile)"
