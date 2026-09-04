# Report Browser Chat Bridge / Driver health, recorded PIDs, and CDP reachability.
# Strictly read-only.
$ErrorActionPreference = 'SilentlyContinue'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'

function Show-Health([string]$Label, [string]$Url) {
    try {
        $Body = Invoke-RestMethod -Uri $Url -TimeoutSec 3 -ErrorAction Stop
        Write-Output "$Label : ok  $($Body | ConvertTo-Json -Compress)"
    } catch {
        Write-Output "$Label : DOWN ($($_.Exception.Message))"
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
    }
    return $null
}

Show-Health 'bridge 8765' 'http://127.0.0.1:8765/health'
$DriverBody = $null
try {
    $DriverBody = Invoke-RestMethod -Uri 'http://127.0.0.1:8766/health' -TimeoutSec 3 -ErrorAction Stop
    Write-Output "driver 8766 : ok  $($DriverBody | ConvertTo-Json -Compress)"
} catch {
    Write-Output "driver 8766 : DOWN ($($_.Exception.Message))"
}
$Cdp = if ($DriverBody -and $DriverBody.cdp_endpoint) {
    [string]$DriverBody.cdp_endpoint
} elseif ($env:CHAT_DRIVER_CDP_ENDPOINT) {
    $env:CHAT_DRIVER_CDP_ENDPOINT
} else {
    $DetectedCdp = Find-AegisChromeCdpEndpoint
    if ($DetectedCdp) { $DetectedCdp } else { 'http://127.0.0.1:51881' }
}
$DetectedCdp = Find-AegisChromeCdpEndpoint
if ($DriverBody -and $DriverBody.cdp_endpoint -and $DetectedCdp) {
    $BoundCdp = ([string]$DriverBody.cdp_endpoint).TrimEnd('/')
    $CurrentCdp = $DetectedCdp.TrimEnd('/')
    if ($BoundCdp -ne $CurrentCdp) {
        Write-Output "driver cdp : STALE ($BoundCdp; current AegisChrome is $CurrentCdp; run scripts/start.ps1 to rebind)"
    }
}
Show-Health 'cdp' "$Cdp/json/version"
Show-Pid 'bridge'
Show-Pid 'driver'
