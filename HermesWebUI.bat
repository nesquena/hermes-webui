@echo off
setlocal enabledelayedexpansion

title Hermes WebUI - One-Click Installer & Launcher
cls

echo.
echo  ========================================================================
echo    __  __                                  _       __     __    __  ______
echo   / / / /___   _____ ____ ___   ___   _____^| ^|     / /___ / /_  / / / /  _/
echo  / /_/ / _ \ / ___// __ `__ \ / _ \ / ___/^| ^| /^|/ // _ \ / __ \/ / / // /  
echo / __  /  __// /   / / / / / //  __/(__  ) ^| ^|/ ^|/ //  __/ /_/ / /_/ // /   
echo/_/ /_/\___//_/   /_/ /_/ /_/ \___//____/  ^|__/^|__/ \___/_.___/\____/___/   
echo.
echo           Autonomous AI Agent Web Interface ^& Workspace
echo  ========================================================================
echo.

cd /d "%~dp0"

echo [1/5] Checking Python Runtime...
set "PYTHON_CMD="

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "tokens=*" %%i in ('where python') do (
        if not defined PYTHON_CMD set "PYTHON_CMD=%%i"
    )
)

if not defined PYTHON_CMD (
    where py >nul 2>&1
    if %ERRORLEVEL% equ 0 set "PYTHON_CMD=py"
)

if not defined PYTHON_CMD (
    echo [*] Python not found. Installing Python 3.12 automatically via winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
    set "PYTHON_CMD=python"
)
echo [OK] Python runtime ready.

echo.
echo [2/5] Checking Git...
where git >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [*] Git not found. Installing Git automatically via winget...
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
)
echo [OK] Git ready.

echo.
echo [3/5] Connecting Hermes Agent Core...
set "HERMES_DIR=%USERPROFILE%\.hermes\hermes-agent"
if not exist "%HERMES_DIR%" (
    if exist "%~dp0..\hermes-agent" (
        set "HERMES_DIR=%~dp0..\hermes-agent"
    ) else (
        echo [*] Downloading Hermes Agent core to %HERMES_DIR%...
        if not exist "%USERPROFILE%\.hermes" mkdir "%USERPROFILE%\.hermes"
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git "%HERMES_DIR%"
    )
)
echo [OK] Hermes Agent linked at %HERMES_DIR%

echo.
echo [4/5] Setting up Virtual Environment ^& Installing Dependencies...
if not exist ".venv" (
    echo [*] Creating isolated virtual environment...
    "%PYTHON_CMD%" -m venv .venv
)

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
set "VENV_PIP=%~dp0.venv\Scripts\pip.exe"

if not exist "%VENV_PYTHON%" (
    set "VENV_PYTHON=%PYTHON_CMD%"
    set "VENV_PIP=pip"
)

echo [*] Updating packages and dependencies...
"%VENV_PIP%" install --quiet --upgrade pip setuptools wheel
if exist "requirements.txt" (
    "%VENV_PIP%" install --quiet -r requirements.txt
)
if exist "%HERMES_DIR%\requirements.txt" (
    "%VENV_PIP%" install --quiet -r "%HERMES_DIR%\requirements.txt"
)
"%VENV_PIP%" install --quiet psutil edge-tts python-docx openpyxl python-pptx

echo [OK] All dependencies successfully installed.

echo.
echo [5/5] Launching Hermes WebUI...
echo.
echo  ========================================================================
echo    Status : Installation complete! Starting server and opening browser...
echo    URL    : http://127.0.0.1:8787
echo  ========================================================================
echo.

set "HERMES_WEBUI_AGENT_DIR=%HERMES_DIR%"
set "PYTHONPATH=%HERMES_DIR%;%PYTHONPATH%"

start "" http://127.0.0.1:8787
"%VENV_PYTHON%" server.py

pause
