# Hermes WebUI Startup Script
# Runs the WebUI via start.ps1 and re-establishes Tailscale Serve proxy
# This script is invoked by Windows Task Scheduler at logon

$ErrorActionPreference = 'SilentlyContinue'

# Wait for Tailscale to be ready (up to 30s)
for ($i = 0; $i -lt 30; $i++) {
    $ts = & tailscale status 2>&1
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 1
}

# Re-establish Tailscale Serve proxy (survives reboot)
& tailscale serve --bg 8787 2>$null

# Start the WebUI server
$WebUIDir = "C:\Users\JSEOK\hermes-webui"
Push-Location $WebUIDir
& powershell.exe -ExecutionPolicy Bypass -File "$WebUIDir\start.ps1"
Pop-Location
