# PowerShell script to start Hermes Web UI on Windows
$ErrorActionPreference = "Stop"

# Resolve directories
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrEmpty($RepoRoot)) { $RepoRoot = Get-Item . }

# Load .env variables if present
$EnvFile = Join-Path $RepoRoot ".env"
if (Test-Path $EnvFile) {
    Write-Host "[start] Loading configuration from .env..." -ForegroundColor Cyan
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $key, $value = $line.Split("=", 2)
            $key = $key.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            if ($key) {
                [System.Environment]::SetEnvironmentVariable($key, $value, [System.EnvironmentVariableTarget]::Process)
            }
        }
    }
}

# Ensure HERMES_WEBUI_AGENT_DIR is configured
if (-not $env:HERMES_WEBUI_AGENT_DIR) {
    $Candidates = @(
        "C:\Users\AHMED\AppData\Local\hermes\hermes-agent",
        (Join-Path $env:USERPROFILE ".hermes\hermes-agent")
    )
    foreach ($cand in $Candidates) {
        if (Test-Path (Join-Path $cand "run_agent.py")) {
            $env:HERMES_WEBUI_AGENT_DIR = (Get-Item $cand).FullName
            break
        }
    }
}

if (-not $env:HERMES_WEBUI_AGENT_DIR) {
    Write-Error "[XX] Could not find the Hermes Agent directory! Please set HERMES_WEBUI_AGENT_DIR in your environment or .env file."
    exit 1
} else {
    Write-Host "[start] Found Hermes Agent at: $env:HERMES_WEBUI_AGENT_DIR" -ForegroundColor Green
}

# Resolve Python Executable
if (-not $env:HERMES_WEBUI_PYTHON) {
    $PythonCandidates = @(
        (Join-Path $env:HERMES_WEBUI_AGENT_DIR "venv\Scripts\python.exe"),
        (Join-Path $env:HERMES_WEBUI_AGENT_DIR ".venv\Scripts\python.exe")
    )
    foreach ($cand in $PythonCandidates) {
        if (Test-Path $cand) {
            $env:HERMES_WEBUI_PYTHON = $cand
            break
        }
    }
}

if (-not $env:HERMES_WEBUI_PYTHON) {
    $env:HERMES_WEBUI_PYTHON = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}

if (-not $env:HERMES_WEBUI_PYTHON) {
    Write-Error "[XX] Python executable not found! Please set HERMES_WEBUI_PYTHON."
    exit 1
} else {
    Write-Host "[start] Using Python: $env:HERMES_WEBUI_PYTHON" -ForegroundColor Green
}

$HostIP = if ($env:HERMES_WEBUI_HOST) { $env:HERMES_WEBUI_HOST } else { "127.0.0.1" }
$Port = if ($env:HERMES_WEBUI_PORT) { $env:HERMES_WEBUI_PORT } else { "8787" }

Write-Host "[start] Launching server on http://${HostIP}:${Port}..." -ForegroundColor Green

Set-Location $RepoRoot

# Run Python server directly using agent Python environment and log output
& $env:HERMES_WEBUI_PYTHON (Join-Path $RepoRoot "server.py") *>&1 | Tee-Object -FilePath (Join-Path $RepoRoot "webui_server.log")