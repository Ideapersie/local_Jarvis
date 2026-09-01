# Start llama-server for Jarvis.
#
# Models and the llama.cpp build live on D: because C: has under 70 GB free and
# a single GGUF is 14-17 GB. Override with JARVIS_MODEL_DIR / JARVIS_LLAMA_DIR
# if they move.
#
# -NGL is the number of layers pushed onto the GPU. The 4060 Laptop has 8 GB, so
# a dense 27B cannot fit entirely; raise -NGL until VRAM is nearly full and
# llama-server still starts, then leave it. Watch it with `nvidia-smi`.
#
# Usage:
#   .\scripts\start-llama.ps1                       # first .gguf found
#   .\scripts\start-llama.ps1 -Ngl 44 -Ctx 32768
#   .\scripts\start-llama.ps1 -Model "Qwen3.8-27B-Q3_K_M.gguf"

param(
    [string]$Model = "",
    [int]$Ngl = 40,
    [int]$Ctx = 16384,
    [int]$Port = 8080,
    [switch]$NoQuantKv
)

$ErrorActionPreference = "Stop"

$modelDir = if ($env:JARVIS_MODEL_DIR) { $env:JARVIS_MODEL_DIR } else { "D:\LLM\models" }
$llamaDir = if ($env:JARVIS_LLAMA_DIR) { $env:JARVIS_LLAMA_DIR } else { "D:\LLM\llama.cpp" }

$server = Get-ChildItem -Path $llamaDir -Filter "llama-server.exe" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $server) {
    Write-Host "llama-server.exe not found under $llamaDir" -ForegroundColor Red
    Write-Host "Download a CUDA release from https://github.com/ggml-org/llama.cpp/releases"
    Write-Host "and unzip it there."
    exit 1
}

if ($Model) {
    $gguf = Join-Path $modelDir $Model
    if (-not (Test-Path $gguf)) {
        Write-Host "No such model: $gguf" -ForegroundColor Red
        exit 1
    }
} else {
    $found = Get-ChildItem -Path $modelDir -Filter "*.gguf" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 1
    if (-not $found) {
        Write-Host "No .gguf under $modelDir" -ForegroundColor Red
        Write-Host "Pull one, e.g. Qwen3.8-27B at Q3_K_M (~13.8 GB)."
        exit 1
    }
    $gguf = $found.FullName
}

$sizeGb = [math]::Round((Get-Item $gguf).Length / 1GB, 1)
Write-Host "model   $([System.IO.Path]::GetFileName($gguf)) ($sizeGb GB)" -ForegroundColor Cyan
Write-Host "server  $($server.FullName)" -ForegroundColor Cyan
Write-Host "ngl=$Ngl ctx=$Ctx port=$Port" -ForegroundColor Cyan

# --jinja is required or the model's tool-calling chat template is ignored and
# every tool call comes back as plain text.
$serverArgs = @(
    "-m", $gguf,
    "-ngl", $Ngl,
    "-c", $Ctx,
    "--host", "127.0.0.1",
    "--port", $Port,
    "--jinja"
)

# Quantised KV cache buys back roughly a third of the cache footprint, which on
# 8 GB is the difference between 16k and 32k context.
if (-not $NoQuantKv) {
    $serverArgs += @("--cache-type-k", "q8_0", "--cache-type-v", "q8_0")
}

& $server.FullName @serverArgs
