@echo off
echo ==========================================================
echo    Pushing Hermes Web UI Windows Native Fork to GitHub
echo ==========================================================

cd /d "%~dp0"

REM 1. Rename old origin to upstream (if not already done)
git remote | findstr /i "upstream" >nul
if errorlevel 1 (
    echo Renaming original origin to upstream...
    git remote rename origin upstream
)

REM 2. Add new origin (if not already done)
git remote | findstr /i "origin" >nul
if errorlevel 1 (
    echo Adding new origin pointing to your repository...
    git remote add origin https://github.com/1PROO/hermes-webui.git
) else (
    echo Updating origin URL...
    git remote set-url origin https://github.com/1PROO/hermes-webui.git
)

REM 3. Add all custom files and changes
echo Staging files...
git add start.bat start.ps1 test_webui.py test_webui.bat install_windows.ps1 README.md push_to_github.bat

REM 4. Commit changes
echo Committing...
git commit -m "feat: Custom native Windows support (no Docker/WSL2) with PowerShell one-liner installer"

REM 5. Push to GitHub
echo Pushing to GitHub main branch...
git push -u origin main
if errorlevel 1 (
    echo Push to main failed, trying master branch...
    git push -u origin master
)

echo ==========================================================
echo    Finished pushing to GitHub!
echo ==========================================================
pause
