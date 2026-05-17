param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._:-]+$')]
    [string]$Route
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ModelDir = Join-Path $ProjectRoot "llm\models"
$RuntimeDir = Join-Path $ProjectRoot "llm\runtime\llama-vulkan"
$LogDir = Join-Path $ProjectRoot "llm\runtime\logs"

$DefaultServer = $env:LLAMA_SERVER_EXE
if (-not $DefaultServer) { $DefaultServer = Join-Path $RuntimeDir "llama-server.exe" }

$DefaultLlama31Model = $env:LLAMA_31_8B_MODEL
if (-not $DefaultLlama31Model) { $DefaultLlama31Model = Join-Path $ModelDir "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf" }

$DefaultLlama32Model = $env:LLAMA_32_3B_MODEL
if (-not $DefaultLlama32Model) { $DefaultLlama32Model = Join-Path $ModelDir "Meta-Llama-3.2-3B-Instruct-Q4_K_M.gguf" }

$DefaultQwenCoderModel = $env:QWEN_25_CODER_7B_MODEL
if (-not $DefaultQwenCoderModel) { $DefaultQwenCoderModel = Join-Path $ModelDir "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" }

$Routes = @{
    "llama-3.1-8b-q4" = @{
        Provider = "llama_cpp"; Model = $DefaultLlama31Model; Server = $DefaultServer; Port = 8080
        Context = 8192; GpuLayers = 999; Alias = "llama-3.1-8b-instruct-q4_k_m"; ManagedProcessNames = @("llama-server")
    }
    "llama-3.2-3b-q4" = @{
        Provider = "llama_cpp"; Model = $DefaultLlama32Model; Server = $DefaultServer; Port = 8081
        Context = 8192; GpuLayers = 999; Alias = "llama-3.2-3b-instruct-q4_k_m"; ManagedProcessNames = @("llama-server")
    }
    "qwen-2.5-coder-7b-q4" = @{
        Provider = "llama_cpp"; Model = $DefaultQwenCoderModel; Server = $DefaultServer; Port = 8082
        Context = 8192; GpuLayers = 999; Alias = "qwen-2.5-coder-7b-instruct-q4_k_m"; ManagedProcessNames = @("llama-server")
    }
}

function Get-RouteConfig([string]$Name) {
    if ($Routes.ContainsKey($Name)) { return $Routes[$Name] }
    foreach ($key in $Routes.Keys) {
        if ($Routes[$key].Alias -eq $Name) { return $Routes[$key] }
    }
    throw "Unknown local model route: $Name"
}

function Get-ListeningProcess([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    return Get-Process -Id ([int]$conn.OwningProcess) -ErrorAction Stop
}

function Assert-ManagedPortProcess($Process, [string[]]$AllowedNames, [int]$Port) {
    if (-not $Process) { return }
    if ($AllowedNames -notcontains $Process.ProcessName) {
        throw "Port $Port is held by unmanaged process $($Process.ProcessName) pid=$($Process.Id)"
    }
}

function Stop-PortProcess([int]$Port, [string[]]$AllowedNames) {
    $process = Get-ListeningProcess $Port
    if (-not $process) {
        Write-Output "No process listening on port $Port"
        return
    }
    Assert-ManagedPortProcess $process $AllowedNames $Port
    Stop-Process -Id $process.Id -Force
    Write-Output "Stopped process $($process.Id) on port $Port"
}

function Get-RouteLogPath([string]$RouteName, [string]$Stream) {
    $safeRoute = $RouteName -replace '[^A-Za-z0-9._-]', '_'
    return Join-Path $LogDir "$safeRoute.$Stream.log"
}

function Get-LogTail([string]$Path) {
    if (-not (Test-Path $Path)) { return "" }
    return ((Get-Content -Path $Path -Tail 20 -ErrorAction SilentlyContinue) -join " ").Trim()
}

function Start-LlamaCpp($Config, [string]$RouteName) {
    if (-not $Config.Model) { throw "Model path env var is not set for $RouteName" }
    if (-not (Test-Path $Config.Model)) { throw "Model file not found: $($Config.Model)" }
    if (-not (Test-Path $Config.Server)) { throw "llama-server not found: $($Config.Server)" }

    $existing = Get-ListeningProcess ([int]$Config.Port)
    Assert-ManagedPortProcess $existing $Config.ManagedProcessNames ([int]$Config.Port)
    if ($existing) { Stop-PortProcess ([int]$Config.Port) $Config.ManagedProcessNames }
    $args = @(
        "-m", $Config.Model,
        "--alias", $Config.Alias,
        "--host", "0.0.0.0",
        "--port", [string]$Config.Port,
        "--ctx-size", [string]$Config.Context,
        "--n-gpu-layers", [string]$Config.GpuLayers
    )
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $stdoutLog = Get-RouteLogPath $RouteName "stdout"
    $stderrLog = Get-RouteLogPath $RouteName "stderr"
    Remove-Item -Path $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
    $workingDirectory = Split-Path -Parent $Config.Server
    $process = Start-Process -FilePath $Config.Server -ArgumentList $args -WorkingDirectory $workingDirectory -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Minimized -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        $stderrTail = Get-LogTail $stderrLog
        throw "llama-server exited immediately for $RouteName with code $($process.ExitCode). stderr=$stderrTail"
    }
    Write-Output "Started $RouteName on port $($Config.Port)"
}

function Start-Ollama($Config) {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) { throw "ollama is not available on PATH" }
    $existing = Get-ListeningProcess ([int]$Config.Port)
    Assert-ManagedPortProcess $existing $Config.ManagedProcessNames ([int]$Config.Port)
    if (-not $existing) {
        Start-Process -FilePath $ollama.Source -ArgumentList @("serve") -WindowStyle Minimized
        Start-Sleep -Seconds 2
    }
    $models = (& $ollama.Source list) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "ollama list failed" }
    if ($models -notmatch [regex]::Escape($Config.Model)) { throw "Ollama model not installed: $($Config.Model)" }
    Write-Output "Ollama ready for $($Config.Model) on port $($Config.Port)"
}

$config = Get-RouteConfig $Route

switch ($Action) {
    "status" {
        $process = Get-ListeningProcess ([int]$config.Port)
        if ($process) { Write-Output "running pid=$($process.Id) process=$($process.ProcessName) port=$($config.Port)" } else { Write-Output "stopped port=$($config.Port)" }
        if ($config.Provider -eq "llama_cpp") {
            Write-Output "server=$($config.Server) exists=$(Test-Path $config.Server)"
            Write-Output "model=$($config.Model) exists=$(if ($config.Model) { Test-Path $config.Model } else { $false })"
        } else {
            Write-Output "provider=ollama available=$(if (Get-Command ollama -ErrorAction SilentlyContinue) { $true } else { $false })"
        }
    }
    "stop" { Stop-PortProcess ([int]$config.Port) $config.ManagedProcessNames }
    "start" {
        if ($config.Provider -eq "ollama") { Start-Ollama $config } else { Start-LlamaCpp $config $Route }
    }
}