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
        # Reset tracked scripts to avoid merge conflicts from previous dynamic generations
        & $GitPath checkout -- start.ps1 start.bat test_webui.py test_webui.bat 2>$null
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

# # --- 6. Verify tracked launch scripts ---
Write-Host "[install] Verifying start scripts..." -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $TargetDir "start.ps1")) -or -not (Test-Path (Join-Path $TargetDir "start.bat"))) {
    Write-Host "[install] Checking out missing start scripts from Git..." -ForegroundColor Yellow
    Set-Location $TargetDir
    & $GitPath checkout -- start.ps1 start.bat 2>$null
}

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

# --- 9. Verify test scripts ---
Write-Host "[install] Verifying test scripts..." -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $TargetDir "test_webui.py")) -or -not (Test-Path (Join-Path $TargetDir "test_webui.bat"))) {
    Write-Host "[install] Checking out missing test scripts from Git..." -ForegroundColor Yellow
    Set-Location $TargetDir
    & $GitPath checkout -- test_webui.py test_webui.bat 2>$null
}

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
