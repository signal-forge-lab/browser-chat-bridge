# Stop exactly the Bridge and Driver processes recorded by scripts/start.ps1.
# Never scans for, focuses, or closes any user-owned Chromium or other processes.
$ErrorActionPreference = 'SilentlyContinue'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'

function Stop-Recorded([string]$Name) {
    $PidFile = Join-Path $Runtime "$Name.pid"
    if (-not (Test-Path $PidFile)) { Write-Output "browser-chat: no recorded $Name pid; nothing to stop."; return }
    $Id = [int](Get-Content $PidFile)
    try {
        Get-Process -Id $Id -ErrorAction Stop | Out-Null
        Stop-Process -Id $Id -Force -ErrorAction Stop
        Write-Output "browser-chat: stopped $Name (pid $Id)."
    } catch {
        Write-Output "browser-chat: $Name (pid $Id) was not running."
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}

# Bridge first (it depends on the Driver), then the Driver.
Stop-Recorded 'bridge'
Stop-Recorded 'driver'
