[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$WslScriptPath,

    [string]$Distro,

    [ValidateNotNullOrEmpty()]
    [string]$TaskName = "HermesWebUIAutoStart",

    [switch]$RunNow,

    [switch]$SkipValidation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function ConvertTo-WindowsArgument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ($Value -notmatch '[\s\"]') {
        return $Value
    }

    $escaped = $Value.Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Get-WslExePath {
    $systemWsl = Join-Path $env:SystemRoot "System32\wsl.exe"
    if (Test-Path $systemWsl) {
        return $systemWsl
    }
    return "wsl.exe"
}

$wslExe = Get-WslExePath

$wslArgs = @()
if ($Distro) {
    $wslArgs += @("-d", $Distro)
}
$wslArgs += @("--exec", "bash", $WslScriptPath)
$actionArguments = ($wslArgs | ForEach-Object { ConvertTo-WindowsArgument -Value $_ }) -join " "

if (-not $SkipValidation) {
    $validationArgs = @()
    if ($Distro) {
        $validationArgs += @("-d", $Distro)
    }
    $validationArgs += @("--exec", "test", "-f", $WslScriptPath)

    & $wslExe @validationArgs
    if ($LASTEXITCODE -ne 0) {
        throw "WSL script path was not found inside the selected distro: $WslScriptPath"
    }
}

$description = "Auto-start Hermes WebUI inside WSL at Windows logon. Runs $WslScriptPath."
$action = New-ScheduledTaskAction -Execute $wslExe -Argument $actionArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($existingTask) {
    Write-Host "Updating existing scheduled task '$TaskName'."
} else {
    Write-Host "Creating scheduled task '$TaskName'."
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register Windows Scheduled Task for Hermes WebUI WSL autostart")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description $description `
        -Force | Out-Null

    Write-Host "Task '$TaskName' is installed."
    Write-Host "Action: $wslExe $actionArguments"

    if ($RunNow) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Task '$TaskName' started."
    }
}
