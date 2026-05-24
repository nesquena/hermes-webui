# install_windows.ps1
# Native Windows Installer for Hermes Web UI (No Docker / No WSL2)
#
# Usage:
#   irm https://raw.githubusercontent.com/1PROO/hermes-webui/main/install_windows.ps1 | iex

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Green
Write-Host "   Hermes Web UI - Windows Native Installer (No Docker)" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green

# 1. Ask for installation directories
$DefaultTargetDir = "C:\Users\AHMED\Desktop\dev\2026\hermes-webui"
$DefaultAgentDir = "C:\Users\AHMED\AppData\Local\hermes\hermes-agent"

$TargetDir = Read-Host "Enter installation path [$DefaultTargetDir]"
if ([string]::IsNullOrWhiteSpace($TargetDir)) { $TargetDir = $DefaultTargetDir }

$AgentDir = Read-Host "Enter Hermes Agent path [$DefaultAgentDir]"
if ([string]::IsNullOrWhiteSpace($AgentDir)) { $AgentDir = $DefaultAgentDir }

$AgentPython = Join-Path $AgentDir "venv\Scripts\python.exe"

# 2. Check if Git is installed
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git is not installed or not in PATH! Please install Git and try again."
    exit 1
}

# 3. Clone Repository
if (Test-Path $TargetDir) {
    if (Test-Path (Join-Path $TargetDir ".git")) {
        Write-Host "[install] Directory exists. Pulling latest updates..." -ForegroundColor Cyan
        Set-Location $TargetDir
        git pull
    } else {
        Write-Host "[install] Directory exists but is not a Git repo. Re-creating..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $TargetDir
        git clone https://github.com/1PROO/hermes-webui.git $TargetDir
    }
} else {
    Write-Host "[install] Cloning repository to $TargetDir..." -ForegroundColor Cyan
    git clone https://github.com/1PROO/hermes-webui.git $TargetDir
}

# 4. Generate .env file
$EnvFile = Join-Path $TargetDir ".env"
Write-Host "[install] Creating configuration .env..." -ForegroundColor Cyan

$EnvContent = @"
# Hermes Web UI Configuration
HERMES_WEBUI_AGENT_DIR=$($AgentDir.Replace('\', '/'))
HERMES_WEBUI_PYTHON=$($AgentPython.Replace('\', '/'))
HERMES_WEBUI_HOST=127.0.0.1
HERMES_WEBUI_PORT=8787
"@

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($EnvFile, $EnvContent, $Utf8NoBom)

# 5. Generate start.ps1
$StartPs = Join-Path $TargetDir "start.ps1"
Write-Host "[install] Generating start.ps1..." -ForegroundColor Cyan

$StartPsContent = @"
# PowerShell script to start Hermes Web UI on Windows
`$ErrorActionPreference = "Stop"

# Resolve directories
`$RepoRoot = Split-Path -Parent `$MyInvocation.MyCommand.Path
if ([string]::IsNullOrEmpty(`$RepoRoot)) { `$RepoRoot = Get-Item . }

# Load .env variables if present
`$EnvFile = Join-Path `$RepoRoot ".env"
if (Test-Path `$EnvFile) {
    Write-Host "[start] Loading configuration from .env..." -ForegroundColor Cyan
    Get-Content `$EnvFile | ForEach-Object {
        `$line = `$_.Trim()
        if (`$line -and -not `$line.StartsWith("#") -and `$line.Contains("=")) {
            `$key, `$value = `$line.Split("=", 2)
            `$key = `$key.Trim()
            `$value = `$value.Trim().Trim('"').Trim("'")
            if (`$key) {
                [System.Environment]::SetEnvironmentVariable(`$key, `$value, [System.EnvironmentVariableTarget]::Process)
            }
        }
    }
}

# Ensure HERMES_WEBUI_AGENT_DIR is configured
if (-not `$env:HERMES_WEBUI_AGENT_DIR) {
    `$env:HERMES_WEBUI_AGENT_DIR = "$($AgentDir)"
}

if (-not (Test-Path `$env:HERMES_WEBUI_AGENT_DIR)) {
    Write-Error "[XX] Could not find the Hermes Agent directory at `$env:HERMES_WEBUI_AGENT_DIR!"
    exit 1
}

# Resolve Python Executable
if (-not `$env:HERMES_WEBUI_PYTHON) {
    `$env:HERMES_WEBUI_PYTHON = "$($AgentPython)"
}

if (-not (Test-Path `$env:HERMES_WEBUI_PYTHON)) {
    Write-Error "[XX] Python executable not found at `$env:HERMES_WEBUI_PYTHON!"
    exit 1
}

`$HostIP = if (`$env:HERMES_WEBUI_HOST) { `$env:HERMES_WEBUI_HOST } else { "127.0.0.1" }
`$Port = if (`$env:HERMES_WEBUI_PORT) { `$env:HERMES_WEBUI_PORT } else { "8787" }

Write-Host "[start] Launching server on http://`${HostIP}:`${Port}..." -ForegroundColor Green

Set-Location `$RepoRoot

# Run Python server directly using agent Python environment
& `$env:HERMES_WEBUI_PYTHON (Join-Path `$RepoRoot "server.py") *>&1 | Tee-Object -FilePath (Join-Path `$RepoRoot "webui_server.log")
"@

[System.IO.File]::WriteAllText($StartPs, $StartPsContent, $Utf8NoBom)

# 6. Generate start.bat
$StartBat = Join-Path $TargetDir "start.bat"
Write-Host "[install] Generating start.bat..." -ForegroundColor Cyan

$StartBatContent = @"
@echo off
REM Windows Batch file to launch start.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
"@

[System.IO.File]::WriteAllText($StartBat, $StartBatContent, $Utf8NoBom)

# 7. Generate test_webui.py
$TestPy = Join-Path $TargetDir "test_webui.py"
Write-Host "[install] Generating test_webui.py..." -ForegroundColor Cyan

$TestPyContent = @"
import urllib.request
import json
import time
import sys

URL = "http://127.0.0.1:8787"

def main():
    print("Waiting for Hermes Web UI server to start on http://127.0.0.1:8787...")
    for i in range(30):
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:
                if response.status == 200:
                    print("[OK] Server is up and responding!")
                    break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("[ERROR] Server did not start in time. Please make sure start.bat is running.")
        sys.exit(1)

    session_url = f"{URL}/api/session/new"
    payload = json.dumps({
        "workspace": "$($TargetDir.Replace('\', '\\'))"
    }).encode("utf-8")
    
    req = urllib.request.Request(
        session_url, 
        data=payload, 
        headers={"Content-Type": "application/json"}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            session_id = res_data["session"]["session_id"]
            print(f"[OK] Created session with ID: {session_id}")
    except Exception as e:
        print(f"[ERROR] Failed to create session: {e}")
        sys.exit(1)

    chat_url = f"{URL}/api/chat"
    chat_payload = json.dumps({
        "session_id": session_id,
        "message": "hi"
    }).encode("utf-8")
    
    chat_req = urllib.request.Request(
        chat_url, 
        data=chat_payload, 
        headers={"Content-Type": "application/json"}
    )
    
    print("Sending message 'hi' to Hermes...")
    try:
        with urllib.request.urlopen(chat_req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            answer = res_data.get("answer")
            print(f"[OK] Received response from Hermes:\n\n{answer}\n")
    except Exception as e:
        print(f"[ERROR] Failed to send message: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
"@

[System.IO.File]::WriteAllText($TestPy, $TestPyContent, $Utf8NoBom)

# 8. Generate test_webui.bat
$TestBat = Join-Path $TargetDir "test_webui.bat"
Write-Host "[install] Generating test_webui.bat..." -ForegroundColor Cyan

$TestBatContent = @"
@echo off
"$($AgentPython)" "%~dp0test_webui.py" > "%~dp0test_webui.log" 2>&1
type "%~dp0test_webui.log"
pause
"@

[System.IO.File]::WriteAllText($TestBat, $TestBatContent, $Utf8NoBom)

Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "  Setup completed successfully!" -ForegroundColor Green
Write-Host "  Project directory: $TargetDir" -ForegroundColor White
Write-Host "  To launch the Web UI, run: start.bat" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Green
