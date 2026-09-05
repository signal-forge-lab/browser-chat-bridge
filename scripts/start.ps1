# Start Browser Chat's lightweight control plane as background processes.
# Microsoft Edge is intentionally lazy-started by Browser Host only when the
# Bridge admits a real browser-chat turn.
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
$BrowserUp = $null -ne $BrowserHealth
if ($BrowserUp -and $BrowserHealth.lazy_start -ne $true) {
    Write-Output 'browser-chat: upgrading recorded Browser Host to lazy-start runtime.'
    Stop-RecordedServer -Name 'browser'
    $BrowserUp = $false
    $BrowserHealth = $null
}
if (-not $BrowserUp) {
    $BrowserPidFile = Join-Path $Runtime 'browser.pid'
    if ((Test-Path $BrowserPidFile) -and (Test-PidAlive -Id ([int](Get-Content $BrowserPidFile)))) {
        Write-Output 'browser-chat: browser host is alive but unhealthy; restarting recorded browser host only.'
        Stop-RecordedServer -Name 'browser'
    }
    $ExistingCdp = Find-BrowserChatEdgeCdpEndpoint
    if ($ExistingCdp) {
        $env:CHAT_BROWSER_ATTACH_ENDPOINT = $ExistingCdp
        Write-Output "browser-chat: Browser Host will reattach Browser Chat Edge at $ExistingCdp on the next browser-chat turn."
    } else {
        Remove-Item Env:CHAT_BROWSER_ATTACH_ENDPOINT -ErrorAction SilentlyContinue
        Write-Output "browser-chat: Browser Host is idle; Edge will launch on the next browser-chat turn."
    }
    Start-Server -Name 'browser' -Module 'browser_chat_bridge.browser_server' -Port $BrowserPort
    $BrowserHealth = Get-Health "http://127.0.0.1:$BrowserPort/health"
}

$BridgeHealth = Get-Health "http://127.0.0.1:$BridgePort/health"
$BridgeUp = $null -ne $BridgeHealth
if ($BridgeUp -and $BridgeHealth.lazy_browser -ne $true) {
    Write-Output 'browser-chat: upgrading recorded Bridge to lazy-browser runtime.'
    Stop-RecordedServer -Name 'bridge'
    $BridgeUp = $false
}
$DriverHealth = Get-Health "http://127.0.0.1:$DriverPort/health"
$DriverUp = $null -ne $DriverHealth
if ($DriverUp -and $DriverHealth.runtime_rebind -ne $true) {
    Write-Output 'browser-chat: upgrading recorded Driver to runtime-rebind support.'
    Stop-RecordedServer -Name 'driver'
    $DriverUp = $false
}

$env:CHAT_DRIVER_BACKEND = 'chromium'
Remove-Item Env:CHAT_DRIVER_CDP_ENDPOINT -ErrorAction SilentlyContinue
$env:CHAT_BRIDGE_BROWSER_URL = "http://127.0.0.1:$BrowserPort"
$env:CHAT_BRIDGE_DRIVER_URL = "http://127.0.0.1:$DriverPort"

if (-not $DriverUp) { Start-Server -Name 'driver' -Module 'browser_chat_bridge.driver_server' -Port $DriverPort }
if (-not $BridgeUp) { Start-Server -Name 'bridge' -Module 'browser_chat_bridge.bridge_server' -Port $BridgePort }
Write-Output 'browser-chat: control plane ready; Edge/nodriver will start only when browser-chat is actually dispatched.'
