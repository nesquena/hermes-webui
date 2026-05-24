@echo off
REM Windows Batch file to launch start.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause