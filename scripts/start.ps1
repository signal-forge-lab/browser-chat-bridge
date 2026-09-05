# Start Browser Chat's nodriver-owned Edge, Driver, and Bridge as background processes.
param(
    [string]$BrowserPort = '8764',
    [string]$BridgePort = '8765',
    [string]$DriverPort = '8766'
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'
$BrowserProfile = if ($env:CHAT_BROWSER_PROFILE) {
    $env:CHAT_BROWSER_PROFILE
} else {
    Join-Path $env:LOCALAPPDATA 'Intelligence Works\BrowserChatEdge\User Data'
}
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null

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

function Find-BrowserChatEdgeCdpEndpoint {
    try {
        $Candidates = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match 'BrowserChatEdge[\\/]User Data' -and
            $_.CommandLine -match '--remote-debugging-port=(\d+)'
        }
        foreach ($Candidate in $Candidates) {
            if ($Candidate.CommandLine -match '--remote-debugging-port=(\d+)') {
                $Endpoint = "http://127.0.0.1:$($Matches[1])"
                if (Test-Health "$Endpoint/json/version") { return $Endpoint }
            }
        }
    } catch {
    }
    return $null
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
    for ($i = 0; $i -lt 40; $i++) {
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

$env:PYTHONPATH = Join-Path $Repo 'src'
$env:CHAT_BROWSER_PROFILE = $BrowserProfile

$BrowserHealth = Get-Health "http://127.0.0.1:$BrowserPort/health"
if (-not $BrowserHealth) {
    $BrowserPidFile = Join-Path $Runtime 'browser.pid'
    if ((Test-Path $BrowserPidFile) -and (Test-PidAlive -Id ([int](Get-Content $BrowserPidFile)))) {
        Write-Output 'browser-chat: browser host is alive but unhealthy; restarting recorded browser host only.'
        Stop-RecordedServer -Name 'browser'
    }
    $ExistingCdp = Find-BrowserChatEdgeCdpEndpoint
    if ($ExistingCdp) {
        $env:CHAT_BROWSER_ATTACH_ENDPOINT = $ExistingCdp
        Write-Output "browser-chat: nodriver will reattach Browser Chat Edge at $ExistingCdp."
    } else {
        Remove-Item Env:CHAT_BROWSER_ATTACH_ENDPOINT -ErrorAction SilentlyContinue
        Write-Output "browser-chat: nodriver will launch Microsoft Edge with profile $BrowserProfile."
    }
    Start-Server -Name 'browser' -Module 'browser_chat_bridge.browser_server' -Port $BrowserPort
    $BrowserHealth = Get-Health "http://127.0.0.1:$BrowserPort/health"
}
if (-not $BrowserHealth -or -not $BrowserHealth.cdp_endpoint) {
    throw 'browser-chat: nodriver Edge did not expose a CDP endpoint'
}
$CdpEndpoint = [string]$BrowserHealth.cdp_endpoint

$BridgeUp = Test-Health "http://127.0.0.1:$BridgePort/health"
$DriverHealth = Get-Health "http://127.0.0.1:$DriverPort/health"
$DriverUp = $null -ne $DriverHealth
if ($DriverUp -and $DriverHealth.cdp_endpoint) {
    $BoundCdp = ([string]$DriverHealth.cdp_endpoint).TrimEnd('/')
    $WantedCdp = $CdpEndpoint.TrimEnd('/')
    if ($BoundCdp -ne $WantedCdp) {
        Write-Output "browser-chat: Driver CDP changed ($BoundCdp -> $WantedCdp); restarting recorded Driver only."
        Stop-RecordedServer -Name 'driver'
        $DriverUp = $false
    }
}

$env:CHAT_DRIVER_BACKEND = 'chromium'
$env:CHAT_DRIVER_CDP_ENDPOINT = $CdpEndpoint
$env:CHAT_BRIDGE_DRIVER_URL = "http://127.0.0.1:$DriverPort"

if (-not $DriverUp) { Start-Server -Name 'driver' -Module 'browser_chat_bridge.driver_server' -Port $DriverPort }
if (-not $BridgeUp) { Start-Server -Name 'bridge' -Module 'browser_chat_bridge.bridge_server' -Port $BridgePort }
Write-Output "browser-chat: Edge/nodriver ready at $CdpEndpoint."
