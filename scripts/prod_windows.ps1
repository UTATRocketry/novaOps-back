<#
.SYNOPSIS
    Run the production Nova stack in the foreground, without Windows services.

.DESCRIPTION
    The normal way to run prod is as services (see ops/Nova.ps1). This script is
    the manual fallback: it starts the same programs, with the same settings
    read from ops/nova.config.ps1, in visible windows you can watch and Ctrl+C.

    Use it when:
      * you are debugging why a service will not stay up,
      * the machine is not the ground-station PC and you just need prod-like
        behaviour for a bench test,
      * you want to run the stack without installing anything.

    Unlike the old version of this script it does NOT use --reload (which
    double-spawns the process and breaks clean shutdown) and does NOT reinstall
    dependencies on every launch (which hangs on a machine with no internet).
    Pass -InstallDeps when you actually want dependencies refreshed.

.EXAMPLE
    .\scripts\prod_windows.ps1
.EXAMPLE
    .\scripts\prod_windows.ps1 -Only backend -InstallDeps
#>
[CmdletBinding()]
param(
    [ValidateSet('backend', 'frontend', 'fas', 'console')]
    [string[]]$Only,

    [switch]$InstallDeps,

    # Override the broker for a bench test without editing the config file.
    [string]$Broker,

    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Find nova.config.ps1 wherever the ops directory currently lives: alongside
# this repo, at the installed location, or wherever $env:NOVA_OPS_DIR points.
# This is what lets ops/ move to C:\Nova\ops (or its own repo) without edits.
function Resolve-NovaOpsDir {
    param([string]$RepoRoot)
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:NOVA_OPS_DIR)) { $candidates += $env:NOVA_OPS_DIR }
    $candidates += (Join-Path $RepoRoot 'ops')
    $candidates += 'C:\Nova\ops'
    foreach ($dir in $candidates) {
        if (Test-Path (Join-Path $dir 'nova.config.ps1')) { return $dir }
    }
    return $null
}

$opsDir = Resolve-NovaOpsDir $repoRoot
if ($null -eq $opsDir) {
    throw "Could not find nova.config.ps1. Looked in `$env:NOVA_OPS_DIR, $repoRoot\ops, and C:\Nova\ops."
}
$configPath = Join-Path $opsDir 'nova.config.ps1'
. $configPath

$cfg = $NovaConfig
$envCfg = $cfg.Environments['prod']
if ($null -eq $Only -or $Only.Count -eq 0) { $Only = @('backend', 'frontend', 'fas', 'console') }

if ([string]::IsNullOrWhiteSpace($Broker)) { $Broker = $cfg.Broker.Host }
if ($Broker -eq 'hivemq') { $Broker = 'broker.hivemq.com' }
if ($Broker -eq 'local') { $Broker = 'localhost' }

# Run against the paths in the config if they exist (the real ground-station
# machine), otherwise against this repo (a developer's checkout).
$backendPath = $envCfg.BackendPath
if (-not (Test-Path $backendPath)) { $backendPath = $repoRoot }
$frontendPath = $envCfg.FrontendPath

$python = Join-Path $backendPath '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "Creating virtualenv in $backendPath\.venv" -ForegroundColor Cyan
    python -m venv (Join-Path $backendPath '.venv')
    $InstallDeps = $true
}

if ($InstallDeps) {
    Write-Host "Installing Python dependencies" -ForegroundColor Cyan
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $backendPath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

$brokerSvc = Get-Service -Name $cfg.Broker.ServiceName -ErrorAction SilentlyContinue
if ($null -eq $brokerSvc) {
    Write-Host "WARNING: Mosquitto service not installed - nothing will have a broker." -ForegroundColor Yellow
} elseif ($brokerSvc.Status -ne 'Running') {
    Write-Host "Starting Mosquitto" -ForegroundColor Cyan
    Start-Service -Name $cfg.Broker.ServiceName
}

$env:NOVA_MQTT_BROKER = $Broker
$env:NOVA_MQTT_PORT = "$($cfg.Broker.Port)"
$env:NOVA_ADMIN_PASSWORD = $envCfg.AdminPassword
$env:NOVA_PUBLIC_BASE_URL = "http://$($cfg.Hosts.Server):$($envCfg.BackendPort)"
$env:PYTHONUNBUFFERED = '1'

$started = @()

function Start-Child {
    param([string]$Title, [string]$Exe, [string]$Arguments, [string]$WorkDir)
    if (-not (Test-Path $WorkDir)) {
        Write-Host "SKIP $Title - missing path $WorkDir" -ForegroundColor Yellow
        return
    }
    Write-Host "Starting $Title" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $Exe -ArgumentList $Arguments `
                          -WorkingDirectory $WorkDir -PassThru
    $script:started += [pscustomobject]@{ Title = $Title; Process = $proc }
}

try {
    if ($Only -contains 'console') {
        Start-Child 'console' $python `
            "`"$(Join-Path $opsDir 'console\nova_console.py')`" --config `"$($cfg.ConsoleCfg)`" --port $($envCfg.ConsolePort)" `
            $opsDir
    }

    if ($Only -contains 'fas') {
        $fasArgs = "tools\fas_bridge.py --broker ${Broker}:$($cfg.Broker.Port)" +
                   " --ops-url http://127.0.0.1:$($envCfg.BackendPort)" +
                   " --data-dir data --baud $($envCfg.FasBaud)"
        if (-not [string]::IsNullOrWhiteSpace($envCfg.FasPort)) {
            $fasArgs = "$fasArgs --port $($envCfg.FasPort)"
        }
        Start-Child 'fas_bridge' $python $fasArgs $backendPath
    }

    if ($Only -contains 'frontend') {
        $node = Get-Command node.exe -ErrorAction SilentlyContinue
        if ($null -eq $node) {
            Write-Host "SKIP frontend - node.exe not on PATH" -ForegroundColor Yellow
        } elseif (-not (Test-Path (Join-Path $frontendPath '.next'))) {
            Write-Host "SKIP frontend - no production build. Run 'npm run build' in $frontendPath" -ForegroundColor Yellow
        } else {
            Start-Child 'frontend' $node.Source `
                "node_modules\next\dist\bin\next start -p $($envCfg.FrontendPort) -H 0.0.0.0" `
                $frontendPath
        }
    }

    if (-not $NoBrowser) {
        Start-Sleep -Seconds 2
        Start-Process "http://localhost:$($envCfg.ConsolePort)"
    }

    if ($Only -contains 'backend') {
        # Backend runs in THIS window so Ctrl+C stops everything via the finally block.
        Write-Host ""
        Write-Host "Backend on http://0.0.0.0:$($envCfg.BackendPort) (broker $Broker) - Ctrl+C to stop the stack" -ForegroundColor Green
        Push-Location $backendPath
        try {
            & $python -m uvicorn app.main:app --host 0.0.0.0 --port $envCfg.BackendPort
        } finally { Pop-Location }
    } else {
        Write-Host ""
        Write-Host "Running. Press Ctrl+C to stop." -ForegroundColor Green
        while ($true) { Start-Sleep -Seconds 3600 }
    }
}
finally {
    foreach ($child in $started) {
        if (-not $child.Process.HasExited) {
            Write-Host "Stopping $($child.Title)" -ForegroundColor Cyan
            Stop-Process -Id $child.Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
