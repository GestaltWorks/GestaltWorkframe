param(
    [ValidateSet("llama-3.1-8b-q4", "llama-3.2-3b-q4", "qwen-2.5-coder-7b-q4")]
    [string]$Route = "llama-3.1-8b-q4"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $ScriptDir "runtime\llama-vulkan"
$Routes = @{
    "llama-3.1-8b-q4" = @{ Model = Join-Path $ScriptDir "models\Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"; Alias = "llama-3.1-8b-instruct-q4_k_m"; Port = 8080 }
    "llama-3.2-3b-q4" = @{ Model = Join-Path $ScriptDir "models\Meta-Llama-3.2-3B-Instruct-Q4_K_M.gguf"; Alias = "llama-3.2-3b-instruct-q4_k_m"; Port = 8081 }
    "qwen-2.5-coder-7b-q4" = @{ Model = Join-Path $ScriptDir "models\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"; Alias = "qwen-2.5-coder-7b-instruct-q4_k_m"; Port = 8082 }
}
$Config = $Routes[$Route]
$ModelFile = $Config.Model
$ModelAlias = $Config.Alias
$Port = $Config.Port

if (-not (Test-Path -Path $ModelFile)) {
    Write-Error "Model file not found. Run download_model.ps1 first."
}

# Find the llama-server executable inside the extracted folder
$ServerExe = Get-ChildItem -Path $RuntimeDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1

if (-not $ServerExe) {
    Write-Error "llama-server.exe not found. Ensure extraction was successful."
}

Write-Host "Starting $Route on port $Port with Vulkan backend..."
Write-Host "Endpoint will be http://localhost:$Port/v1"

# Run llama-server
# -m model
# --port route-specific
# -c 8192 context size
# -ngl 999 number of gpu layers to offload
# -fa flash attention
& $ServerExe.FullName -m $ModelFile --alias $ModelAlias --port $Port -c 8192 -ngl 999 -fa on --host 0.0.0.0
