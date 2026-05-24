@echo off
"C:\Users\AHMED\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" "%~dp0test_webui.py" > "%~dp0test_webui.log" 2>&1
type "%~dp0test_webui.log"
pause
