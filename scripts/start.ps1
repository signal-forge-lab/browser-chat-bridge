# Start the Browser Chat Bridge and Driver as separate background processes.
# Idempotent: refuses to double-start when both are already healthy.
# PID files and logs live under the ignored .runtime/ directory.
# The authenticated Chromium (with --remote-debugging-port) is user-owned and
# is NEVER started, focused, or closed by this script.
param(
    [string]$CdpEndpoint = '',
    [string]$BridgePort = '8765',
    [string]$DriverPort = '8766'
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

function Find-AegisChromeCdpEndpoint {
    try {
        $Candidates = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match 'AegisChrome[\\/]User Data' -and
            $_.CommandLine -match '--remote-debugging-port=(\d+)'
        }
        foreach ($Candidate in $Candidates) {
            if ($Candidate.CommandLine -match '--remote-debugging-port=(\d+)') {
                return "http://127.0.0.1:$($Matches[1])"
            }
        }
    } catch {
        # Detection is best-effort. The explicit/env/default paths below remain authoritative.
    }
    return $null
}

if (-not $CdpEndpoint) {
    $CdpEndpoint = if ($env:CHAT_DRIVER_CDP_ENDPOINT) {
        $env:CHAT_DRIVER_CDP_ENDPOINT
    } else {
        $DetectedCdp = Find-AegisChromeCdpEndpoint
        if ($DetectedCdp) { $DetectedCdp } else { 'http://127.0.0.1:51881' }
    }
}

function Test-Health([string]$Url) {
    try { Invoke-RestMethod -Uri $Url -TimeoutSec 3 -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Get-Health([string]$Url) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 3 -ErrorAction Stop }
    catch { return $null }
}

function Test-PidAlive([int]$Id) {
    try { Get-Process -Id $Id -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Stop-RecordedServer([string]$Name) {
    $PidFile = Join-Path $Runtime "$Name.pid"
    if (-not (Test-Path $PidFile)) {
        throw "browser-chat: refusing to replace healthy $Name because it was not started by scripts/start.ps1"
    }
    $Id = [int](Get-Content $PidFile)
    if (-not (Test-PidAlive -Id $Id)) {
        throw "browser-chat: refusing to replace healthy $Name because recorded pid $Id is not alive"
    }
    Stop-Process -Id $Id -Force -ErrorAction Stop
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}

function Start-Server([string]$Name, [string]$Module, [string]$Port) {
    $PidFile = Join-Path $Runtime "$Name.pid"
    if ((Test-Path $PidFile) -and (Test-PidAlive -Id ([int](Get-Content $PidFile)))) {
        Write-Output "browser-chat: $Name already running (pid $(Get-Content $PidFile))."
        return
    }
    $Out = Join-Path $Runtime "$Name.log"
    $Err = Join-Path $Runtime "$Name.err.log"
    $Proc = Start-Process -FilePath 'python' `
        -ArgumentList @('-m', $Module) `
        -WorkingDirectory $Repo `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Out -RedirectStandardError $Err
    Set-Content -Path $PidFile -Value $Proc.Id
    $Url = "http://127.0.0.1:$Port/health"
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Health $Url) {
            Write-Output "browser-chat: $Name healthy on $Url (pid $($Proc.Id))."
            return
        }
        if (-not (Test-PidAlive -Id $Proc.Id)) {
            Write-Output "browser-chat: $Name exited during startup; see $Err"
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Output "browser-chat: $Name did not become healthy on $Url in time; see $Err"
    exit 1
}

$BridgeUp = Test-Health "http://127.0.0.1:$BridgePort/health"
$DriverHealth = Get-Health "http://127.0.0.1:$DriverPort/health"
$DriverUp = $null -ne $DriverHealth

# AegisChrome may come back on a different random CDP port. A healthy Driver
# can therefore be alive but permanently attached to yesterday's endpoint.
# Rebind only a Driver this launcher owns; never kill an arbitrary process that
# happens to answer on the Driver port.
if ($DriverUp -and $DriverHealth.cdp_endpoint) {
    $BoundCdp = ([string]$DriverHealth.cdp_endpoint).TrimEnd('/')
    $WantedCdp = $CdpEndpoint.TrimEnd('/')
    if ($BoundCdp -ne $WantedCdp) {
        Write-Output "browser-chat: Driver CDP changed ($BoundCdp -> $WantedCdp); restarting recorded Driver only."
        Stop-RecordedServer -Name 'driver'
        $DriverUp = $false
    }
}
if ($BridgeUp -and $DriverUp) {
    Write-Output 'browser-chat: Bridge and Driver already healthy; nothing to start.'
    exit 0
}

# Child processes inherit these from this PowerShell session.
$env:PYTHONPATH = Join-Path $Repo 'src'
$env:CHAT_DRIVER_BACKEND = 'chromium'
$env:CHAT_DRIVER_CDP_ENDPOINT = $CdpEndpoint
$env:CHAT_BRIDGE_DRIVER_URL = "http://127.0.0.1:$DriverPort"

if (-not $DriverUp) { Start-Server -Name 'driver' -Module 'browser_chat_bridge.driver_server' -Port $DriverPort }
if (-not $BridgeUp) { Start-Server -Name 'bridge' -Module 'browser_chat_bridge.bridge_server' -Port $BridgePort }
Write-Output "browser-chat: CDP endpoint expected at $CdpEndpoint (bring up Chromium yourself; this script never touches your browser)."
