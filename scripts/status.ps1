# Report Browser Chat Edge/nodriver, Driver, Bridge, and CDP health. Read-only.
$ErrorActionPreference = 'SilentlyContinue'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'

function Show-Health([string]$Label, [string]$Url) {
    try {
        $Body = Invoke-RestMethod -Uri $Url -TimeoutSec 3 -ErrorAction Stop
        Write-Host "$Label : ok  $($Body | ConvertTo-Json -Compress)"
        return $Body
    } catch {
        Write-Host "$Label : DOWN ($($_.Exception.Message))"
        return $null
    }
}

function Show-Pid([string]$Name) {
    $PidFile = Join-Path $Runtime "$Name.pid"
    if (-not (Test-Path $PidFile)) { Write-Output "pid $Name : no pid file (not started by scripts/start.ps1)"; return }
    $Id = [int](Get-Content $PidFile)
    try {
        $Proc = Get-Process -Id $Id -ErrorAction Stop
        Write-Output "pid $Name : alive ($($Proc.ProcessName) $Id)"
    } catch {
        Write-Output "pid $Name : dead ($Id)"
    }
}

$BrowserBody = Show-Health 'browser 8764' 'http://127.0.0.1:8764/health'
$DriverBody = Show-Health 'driver 8766' 'http://127.0.0.1:8766/health'
Show-Health 'bridge 8765' 'http://127.0.0.1:8765/health' | Out-Null

$Cdp = if ($BrowserBody -and $BrowserBody.cdp_endpoint) {
    [string]$BrowserBody.cdp_endpoint
} elseif ($DriverBody -and $DriverBody.cdp_endpoint) {
    [string]$DriverBody.cdp_endpoint
} else {
    ''
}
if ($Cdp) { Show-Health 'cdp' "$Cdp/json/version" | Out-Null }
if ($BrowserBody -and $DriverBody -and $BrowserBody.cdp_endpoint -and $DriverBody.cdp_endpoint) {
    if (([string]$BrowserBody.cdp_endpoint).TrimEnd('/') -ne ([string]$DriverBody.cdp_endpoint).TrimEnd('/')) {
        Write-Output "driver cdp : STALE (run scripts/start.ps1 to rebind)"
    }
}
Show-Pid 'browser'
Show-Pid 'driver'
Show-Pid 'bridge'
