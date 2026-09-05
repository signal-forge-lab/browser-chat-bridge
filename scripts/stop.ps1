# Stop exactly the Browser Chat Bridge, Driver, nodriver host, and its dedicated Edge profile.
$ErrorActionPreference = 'SilentlyContinue'
$Repo = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Repo '.runtime'

function Test-PidAlive([int]$Id) {
    try { Get-Process -Id $Id -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Stop-Recorded([string]$Name) {
    $PidFile = Join-Path $Runtime "$Name.pid"
    if (-not (Test-Path $PidFile)) { Write-Output "browser-chat: no recorded $Name pid; nothing to stop."; return }
    $Id = [int](Get-Content $PidFile)
    if (Test-PidAlive -Id $Id) {
        Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
        Write-Output "browser-chat: stopped $Name (pid $Id)."
    } else {
        Write-Output "browser-chat: $Name (pid $Id) was not running."
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}

function Stop-BrowserHost {
    $PidFile = Join-Path $Runtime 'browser.pid'
    if (-not (Test-Path $PidFile)) { Write-Output 'browser-chat: no recorded browser pid; nothing to stop.'; return }
    $Id = [int](Get-Content $PidFile)
    try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8764/shutdown' -TimeoutSec 2 | Out-Null } catch {}
    for ($i = 0; $i -lt 20 -and (Test-PidAlive -Id $Id); $i++) { Start-Sleep -Milliseconds 250 }
    if (Test-PidAlive -Id $Id) { Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
    Write-Output "browser-chat: stopped nodriver browser host (pid $Id)."
}

function Stop-BrowserChatEdge {
    $Rows = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" | Where-Object {
        $_.CommandLine -and $_.CommandLine -match 'BrowserChatEdge[\\/]User Data'
    }
    foreach ($Row in $Rows) {
        Stop-Process -Id $Row.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (@($Rows).Count -gt 0) { Write-Output 'browser-chat: stopped dedicated BrowserChatEdge processes.' }
}

Stop-Recorded 'bridge'
Stop-Recorded 'driver'
Stop-BrowserHost
Stop-BrowserChatEdge
