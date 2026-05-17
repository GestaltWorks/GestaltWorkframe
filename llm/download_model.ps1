$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ModelDir = Join-Path $ScriptDir "models"
$RuntimeDir = Join-Path $ScriptDir "runtime"
New-Item -ItemType Directory -Force -Path $ModelDir, $RuntimeDir | Out-Null

$Models = @(
    @{ Url = "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"; File = Join-Path $ModelDir "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"; MinBytes = 4500000000 },
    @{ Url = "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"; File = Join-Path $ModelDir "Meta-Llama-3.2-3B-Instruct-Q4_K_M.gguf"; MinBytes = 1900000000 },
    @{ Url = "https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"; File = Join-Path $ModelDir "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"; MinBytes = 4600000000 }
)

foreach ($Model in $Models) {
    if (-not (Test-Path -Path $Model.File) -or (Get-Item -Path $Model.File).Length -lt $Model.MinBytes) {
        Write-Host "Downloading $($Model.File) (resuming if partially downloaded)..."
        & curl.exe -C - -L --fail --retry 5 --retry-delay 5 -o $Model.File $Model.Url
        Write-Host "Download complete."
    } else {
        Write-Host "Model $($Model.File) already exists."
    }
}

# Download llama-server (Vulkan release for Windows)
$LlamaUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b8978/llama-b8978-bin-win-vulkan-x64.zip"
$LlamaZip = Join-Path $RuntimeDir "llama-vulkan.zip"
$LlamaDir = Join-Path $RuntimeDir "llama-vulkan"

if (-not (Test-Path -Path $LlamaZip) -or (Get-Item -Path $LlamaZip).Length -lt 10000000) {
    Write-Host "Downloading llama.cpp Vulkan server..."
    # Always do a fresh download for the zip since it's small (~15MB) and curl -C - can fail on GitHub redirects
    if (Test-Path -Path $LlamaZip) { Remove-Item -Path $LlamaZip -Force }
    Invoke-WebRequest -Uri $LlamaUrl -OutFile $LlamaZip
    Write-Host "Extracting..."
    New-Item -ItemType Directory -Force -Path $LlamaDir | Out-Null
    tar -xf $LlamaZip -C $LlamaDir
}
