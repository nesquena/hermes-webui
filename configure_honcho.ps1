param(
    [string]$HonchoApiKey = "",
    [switch]$Force
)

# Helper to configure Honcho as the memory provider for Hermes WebUI on Windows.
# Usage examples:
#   powershell -ExecutionPolicy Bypass -File .\configure_honcho.ps1            # copies example config and prints next steps
#   powershell -ExecutionPolicy Bypass -File .\configure_honcho.ps1 -HonchoApiKey "sk-..."  # also sets HONCHO_API_KEY persistently
#   .\configure_honcho.ps1 -HonchoApiKey "sk-..."

function Write-Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Write-Ok($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$example = Join-Path $repoRoot "docs\honcho-config-example.yaml"
if (-not (Test-Path $example)) {
    Write-Warn "Example config not found at $example. Ensure you're running from the repo root."; exit 2
}

# Resolve HERMES_HOME
$envHermes = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'User')
if ([string]::IsNullOrWhiteSpace($envHermes)) {
    $userProfile = [Environment]::GetFolderPath('UserProfile')
    $hermesHome = Join-Path $userProfile ".hermes"
} else {
    $hermesHome = $envHermes
}

Write-Info "Resolved HERMES_HOME => $hermesHome"
if (-not (Test-Path $hermesHome)) {
    Write-Info "Creating HERMES_HOME directory..."
    New-Item -ItemType Directory -Path $hermesHome -Force | Out-Null
}

$targetConfig = Join-Path $hermesHome "config.yaml"
if (Test-Path $targetConfig) {
    if (-not $Force) {
        $ts = Get-Date -Format "yyyyMMddHHmmss"
        $backup = "$targetConfig.bak.$ts"
        Write-Info "Backing up existing config to: $backup"
        Copy-Item -Path $targetConfig -Destination $backup -Force
    } else {
        Write-Info "Force mode: overwriting existing config without creating a timestamped backup."
    }
}

Write-Info "Copying example config to $targetConfig"
Copy-Item -Path $example -Destination $targetConfig -Force

if (-not [string]::IsNullOrWhiteSpace($HonchoApiKey)) {
    Write-Info "Setting HONCHO_API_KEY as a persistent user environment variable."
    # setx writes to user environment; requires new shell to be visible
    & setx HONCHO_API_KEY "$HonchoApiKey" | Out-Null
    # set in current process for immediate use in this shell
    [Environment]::SetEnvironmentVariable('HONCHO_API_KEY', $HonchoApiKey, 'Process')
    Write-Ok "HONCHO_API_KEY set. New shells will see this variable."
} else {
    Write-Warn 'No API key provided. The config references "${HONCHO_API_KEY}" — set HONCHO_API_KEY in your environment before starting Hermes WebUI.'
}

Write-Ok "Configuration created at: $targetConfig"

Write-Host "`nNext steps:" -ForegroundColor White
Write-Host '  - Confirm HONCHO_API_KEY is set for the profile or process (if not done above).' -ForegroundColor Gray
Write-Host '      setx HONCHO_API_KEY "your-key-here"' -ForegroundColor DarkGray
Write-Host '  - Start or restart Hermes WebUI (bootstrap.py, start.sh, or ctl.sh depending on your setup).' -ForegroundColor Gray
Write-Host '      python bootstrap.py   # foreground (Ctrl-C to stop)' -ForegroundColor DarkGray
Write-Host '      OR use your existing ctl.sh/start.sh wrapper if applicable.' -ForegroundColor DarkGray
Write-Host '  - Open WebUI at http://127.0.0.1:8787 and verify Settings → Plugins → Memory shows Honcho as Active provider.' -ForegroundColor Gray

Write-Host "`nQuick verification commands (in PowerShell):" -ForegroundColor White
Write-Host '  python -c "import honcho; print(''Honcho client:'', honcho.Honcho)"' -ForegroundColor DarkGray
Write-Host '  python -c "import importlib; importlib.invalidate_caches(); import honcho_ai; print(''honcho_ai OK'')"' -ForegroundColor DarkGray

Write-Ok "Done."
