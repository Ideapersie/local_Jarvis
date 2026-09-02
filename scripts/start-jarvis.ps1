<#
.SYNOPSIS
  Starts llama-server and then the Jarvis server, unless already running.

.DESCRIPTION
  Wrapper the scheduled task runs at logon. The guard matters: Task Scheduler
  will happily start a second instance, and a second uvicorn binding port 8000
  fails in a way that is easy to miss - you end up with an old process serving
  stale code while the new one silently died. That has happened repeatedly
  during development.

  Kept as its own script rather than inlined into the task action so it can be
  run by hand to reproduce exactly what the task does.

  llama-server is started first and waited on. Every tier except portfolio runs
  on the local model now, so starting the app without it gives a dashboard whose
  chat, triage and 06:30 brief all fail - and the brief failing is silent until
  you notice the panel is empty. Waiting for /health costs about half a minute
  at logon and removes that whole class of morning.
#>

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1',
    [int]$ModelPort = 8080,
    [int]$ModelWaitSeconds = 180,
    [switch]$SkipModel
)

$ErrorActionPreference = 'Stop'

# Repo root is this script's parent directory. Resolved rather than assumed,
# because the task's working directory is not guaranteed.
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'jarvis.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Path $Python)) {
    Write-Error "No venv at $Python. Create it first: python -m venv .venv"
    exit 1
}

# --- the local model ---------------------------------------------------------

if (-not $SkipModel) {
    $modelUp = Get-NetTCPConnection -LocalPort $ModelPort -State Listen -ErrorAction SilentlyContinue
    if ($modelUp) {
        Write-Host "llama-server already listening on $ModelPort (PID $($modelUp.OwningProcess))."
    } else {
        $startLlama = Join-Path $PSScriptRoot 'start-llama.ps1'
        if (Test-Path $startLlama) {
            Write-Host "Starting llama-server..."
            Start-Process -FilePath 'powershell.exe' `
                -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startLlama) `
                -WorkingDirectory $Root `
                -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $LogDir 'llama.log') `
                -RedirectStandardError (Join-Path $LogDir 'llama.err.log')
        } else {
            Write-Warning "No start-llama.ps1; the app will start without a model."
        }
    }

    # Loading a 13.8 GB GGUF takes about half a minute, and llama-server answers
    # /health with 503 until the weights are in. Waiting for 200 rather than for
    # the port means the app never starts against a model that cannot answer yet.
    $deadline = (Get-Date).AddSeconds($ModelWaitSeconds)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ModelPort/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if ($ready) {
        Write-Host "llama-server ready on $ModelPort."
    } else {
        Write-Warning "llama-server not ready after ${ModelWaitSeconds}s. Starting the app anyway; chat and the brief will fail until it is up."
    }
}

# --- the app -----------------------------------------------------------------

# Already listening? Leave it alone.
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Jarvis already listening on port $Port (PID $($existing.OwningProcess)). Nothing to do."
    exit 0
}


# Working directory must be the repo root, or .env and .claude/ do not load and
# the agent silently runs without its project config.
Start-Process -FilePath $Python `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', $Port) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError (Join-Path $LogDir 'jarvis.err.log')

Write-Host "Jarvis starting on http://${BindHost}:${Port} (logging to $LogFile)"
