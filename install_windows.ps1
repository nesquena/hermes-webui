# install_windows.ps1
# Fully Automated Native Windows Installer for Hermes Web UI (No Docker / No WSL2)
#
# Usage:
#   irm https://1proo.github.io/hermes-webui/install_windows.ps1 | iex

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Green
Write-Host "   Hermes Web UI - Automated Windows Native Installer" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
Write-Host ""

# Determine target directories dynamically
$UserAppDataLocal = "$env:USERPROFILE\AppData\Local"
$HermesBaseDir = Join-Path $UserAppDataLocal "hermes"

# Create the base hermes directory if it doesn't exist
if (-not (Test-Path $HermesBaseDir)) {
    New-Item -ItemType Directory -Force -Path $HermesBaseDir | Out-Null
}

# If we are running this script locally within a checkout directory, use it
$ScriptDir = $null
try {
    if ($PSScriptRoot) {
        $ScriptDir = $PSScriptRoot
    }
} catch {}

if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "server.py")) -and (Test-Path (Join-Path $ScriptDir "index.html"))) {
    $TargetDir = $ScriptDir
    Write-Host "[install] Running from local directory. Using current folder: $TargetDir" -ForegroundColor Cyan
} else {
    $TargetDir = Join-Path $HermesBaseDir "hermes-webui"
}

$AgentDir = Join-Path $HermesBaseDir "hermes-agent"

# --- 1. Check/Install Git ---
Write-Host "[install] Checking for Git..." -ForegroundColor Cyan
$GitPath = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $GitPath = "git"
    Write-Host "[install] Git is already installed." -ForegroundColor Green
} else {
    # Check default install path
    $DefaultGitPath = "C:\Program Files\Git\bin\git.exe"
    if (Test-Path $DefaultGitPath) {
        $GitPath = $DefaultGitPath
        Write-Host "[install] Git found at $GitPath" -ForegroundColor Green
    } else {
        Write-Host "[install] Git not found! Attempting to install Git via winget..." -ForegroundColor Yellow
        try {
            Start-Process winget -ArgumentList "install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements" -Wait -NoNewWindow
            # Refresh path
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            if (Get-Command git -ErrorAction SilentlyContinue) {
                $GitPath = "git"
                Write-Host "[install] Git installed successfully!" -ForegroundColor Green
            } elseif (Test-Path $DefaultGitPath) {
                $GitPath = $DefaultGitPath
                Write-Host "[install] Git installed successfully at standard location!" -ForegroundColor Green
            } else {
                throw "Git installation was completed but git command is still not accessible in path."
            }
        } catch {
            Write-Error "Could not install Git automatically. Please install Git manually from https://git-scm.com/ and re-run this script."
            exit 1
        }
    }
}

# --- 2. Check/Install Python ---
Write-Host "[install] Checking for Python..." -ForegroundColor Cyan
$SystemPython = $null
$PossiblePythons = @(
    "python",
    "python3",
    "$env:USERPROFILE\AppData\Local\Programs\Python\Python311\python.exe",
    "$env:USERPROFILE\AppData\Local\Programs\Python\Python312\python.exe",
    "C:\Program Files\Python311\python.exe",
    "C:\Program Files\Python312\python.exe"
)

foreach ($py in $PossiblePythons) {
    if ($py -eq "python" -or $py -eq "python3") {
        if (Get-Command $py -ErrorAction SilentlyContinue) {
            $SystemPython = $py
            break
        }
    } else {
        if (Test-Path $py) {
            $SystemPython = $py
            break
        }
    }
}

if ($SystemPython) {
    Write-Host "[install] Found Python interpreter at: $SystemPython" -ForegroundColor Green
} else {
    Write-Host "[install] Python not found! Attempting to install Python 3.11 via winget..." -ForegroundColor Yellow
    try {
        Start-Process winget -ArgumentList "install --id Python.Python.3.11 -e --source winget --accept-package-agreements --accept-source-agreements" -Wait -NoNewWindow
        # Refresh path
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        $InstalledPythonPath = "$env:USERPROFILE\AppData\Local\Programs\Python\Python311\python.exe"
        if (Test-Path $InstalledPythonPath) {
            $SystemPython = $InstalledPythonPath
            Write-Host "[install] Python 3.11 installed successfully at $SystemPython" -ForegroundColor Green
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $SystemPython = "python"
            Write-Host "[install] Python installed successfully!" -ForegroundColor Green
        } else {
            throw "Python installation was completed but python command is still not accessible."
        }
    } catch {
        Write-Error "Could not install Python automatically. Please install Python 3.11 manually and re-run this installer."
        exit 1
    }
}

# --- 3. Clone/Update Web UI Repository ---
Write-Host "[install] Setting up Hermes Web UI..." -ForegroundColor Cyan
if (Test-Path $TargetDir) {
    if (Test-Path (Join-Path $TargetDir ".git")) {
        Write-Host "[install] Hermes Web UI folder exists. Fetching updates..." -ForegroundColor Cyan
        Set-Location $TargetDir
        & $GitPath pull
    } else {
        Write-Host "[install] Folder exists but is not a Git repo. Re-creating..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $TargetDir -ErrorAction SilentlyContinue
        & $GitPath clone https://github.com/1PROO/hermes-webui.git $TargetDir
    }
} else {
    Write-Host "[install] Cloning Hermes Web UI into $TargetDir..." -ForegroundColor Cyan
    & $GitPath clone https://github.com/1PROO/hermes-webui.git $TargetDir
}

# --- 4. Clone/Setup Hermes Agent ---
$AgentPython = Join-Path $AgentDir "venv\Scripts\python.exe"
if (-not (Test-Path $AgentPython)) {
    Write-Host "[install] Hermes Agent not detected. Cloning and initializing..." -ForegroundColor Yellow
    if (Test-Path $AgentDir) {
        Remove-Item -Recurse -Force $AgentDir -ErrorAction SilentlyContinue
    }
    
    # Clone hermes-agent
    Write-Host "[install] Cloning NousResearch/hermes-agent..." -ForegroundColor Cyan
    & $GitPath clone https://github.com/NousResearch/hermes-agent.git $AgentDir
    
    # Initialize virtual environment inside hermes-agent
    Write-Host "[install] Creating virtual environment (venv) in $AgentDir..." -ForegroundColor Cyan
    Set-Location $AgentDir
    & $SystemPython -m venv venv
    
    # Verify venv python creation
    if (-not (Test-Path $AgentPython)) {
        Write-Error "[install] Failed to create Python virtualenv inside hermes-agent!"
        exit 1
    }
    
    # Upgrade pip and install dependencies
    Write-Host "[install] Upgrading pip and installing requirements..." -ForegroundColor Cyan
    & $AgentPython -m pip install --upgrade pip
    
    $AgentReqs = Join-Path $AgentDir "requirements.txt"
    if (Test-Path $AgentReqs) {
        & $AgentPython -m pip install -r $AgentReqs
    }
    
    # Install agent in editable mode
    & $AgentPython -m pip install -e .
    Write-Host "[install] Hermes Agent dependency installation complete." -ForegroundColor Green
} else {
    Write-Host "[install] Existing Hermes Agent detected at $AgentDir" -ForegroundColor Green
}

# --- 5. Generate configuration .env ---
$EnvFile = Join-Path $TargetDir ".env"
Write-Host "[install] Writing environment config file (.env)..." -ForegroundColor Cyan

$EnvContent = @"
# Hermes Web UI Configuration
HERMES_WEBUI_AGENT_DIR=$($AgentDir.Replace('\', '/'))
HERMES_WEBUI_PYTHON=$($AgentPython.Replace('\', '/'))
HERMES_WEBUI_HOST=127.0.0.1
HERMES_WEBUI_PORT=8787
"@

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($EnvFile, $EnvContent, $Utf8NoBom)
Write-Host "[install] .env file written successfully." -ForegroundColor Green

# --- 6. Generate start.ps1 ---
$StartPs = Join-Path $TargetDir "start.ps1"
Write-Host "[install] Generating start.ps1..." -ForegroundColor Cyan

$StartPsContent = @"
# PowerShell script to start Hermes Web UI on Windows
`$ErrorActionPreference = "Stop"

# Resolve directories
`$RepoRoot = Split-Path -Parent `$MyInvocation.MyCommand.Path
if ([string]::IsNullOrEmpty(`$RepoRoot)) { `$RepoRoot = Get-Item . }

# === Auto-Update: Pull latest commits from GitHub fork silently on launch ===
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[start] Checking for updates from GitHub..." -ForegroundColor Cyan
    try {
        Push-Location `$RepoRoot
        # Silently pull. Since .gitattributes is set to 'merge=ours', local configs are 100% safe!
        git pull origin master --quiet 2>`$null
        Write-Host "[start] Up to date with repository!" -ForegroundColor Green
    } catch {
        Write-Host "[start] Offline mode or update check skipped." -ForegroundColor Yellow
    } finally {
        Pop-Location
    }
}

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

# --- 7. Generate start.bat ---
$StartBat = Join-Path $TargetDir "start.bat"
Write-Host "[install] Generating start.bat..." -ForegroundColor Cyan

$StartBatContent = @"
@echo off
REM Windows Batch file to launch start.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
"@

[System.IO.File]::WriteAllText($StartBat, $StartBatContent, $Utf8NoBom)

# --- 8. Generate global hermes-webui command ---
$GlobalCmdFile = Join-Path $TargetDir "hermes-webui.cmd"
Write-Host "[install] Creating global terminal shortcut..." -ForegroundColor Cyan

$GlobalCmdContent = @"
@echo off
REM Global cmd shortcut for Hermes Web UI
cd /d "$TargetDir"
start.bat
"@

[System.IO.File]::WriteAllText($GlobalCmdFile, $GlobalCmdContent, $Utf8NoBom)

# Register TargetDir in PATH
$UserPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if ($UserPath -notlike "*$TargetDir*") {
    Write-Host "[install] Adding target directory to user PATH variable..." -ForegroundColor Cyan
    $NewUserPath = "$UserPath;$TargetDir"
    [System.Environment]::SetEnvironmentVariable("PATH", $NewUserPath, "User")
    $env:PATH += ";$TargetDir"
}

# --- 9. Generate test_webui.py ---
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

# --- 10. Generate test_webui.bat ---
$TestBat = Join-Path $TargetDir "test_webui.bat"
Write-Host "[install] Generating test_webui.bat..." -ForegroundColor Cyan

$TestBatContent = @"
@echo off
"$($AgentPython)" "%~dp0test_webui.py" > "%~dp0test_webui.log" 2>&1
type "%~dp0test_webui.log"
pause
"@

[System.IO.File]::WriteAllText($TestBat, $TestBatContent, $Utf8NoBom)

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "  Setup completed successfully!" -ForegroundColor Green
Write-Host "  Project directory: $TargetDir" -ForegroundColor White
Write-Host "  Hermes Agent directory: $AgentDir" -ForegroundColor White
Write-Host ""
Write-Host "  You can now open a new terminal window and type:" -ForegroundColor White
Write-Host "      hermes-webui" -ForegroundColor Yellow
Write-Host "  To launch the Web UI immediately from anywhere!" -ForegroundColor White
Write-Host "==========================================================" -ForegroundColor Green
