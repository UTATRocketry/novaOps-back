<#
.SYNOPSIS
    Run the dev backend (and optional extras) in the foreground with hot reload.

.DESCRIPTION
    Bench/development launcher. Unlike prod this DOES use --reload, because
    that is the point of dev.

    Changed from the previous version:
      * dependencies are no longer reinstalled on every launch (it hung on
        machines without internet and added ~20 s to every restart). Pass
        -InstallDeps when you actually want them refreshed.
      * ports and paths come from ops/nova.config.ps1, so dev cannot silently
        collide with the prod stack on the same PC.

.EXAMPLE
    .\scripts\dev_windows.ps1
.EXAMPLE
    .\scripts\dev_windows.ps1 -Broker hivemq -WithDummy -InstallDeps
#>
[CmdletBinding()]
param(
    [string]$Broker,
    [int]$Port = 0,
    [switch]$WithDummy,
    [switch]$WithFas,
    [switch]$InstallDeps
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Look for the ops config wherever it lives: $env:NOVA_OPS_DIR, this repo, or
# the installed location. Falls back to built-in defaults if none is found.
$configPath = $null
$candidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:NOVA_OPS_DIR)) { $candidates += $env:NOVA_OPS_DIR }
$candidates += (Join-Path $repoRoot 'ops')
$candidates += 'C:\Nova\ops'
foreach ($dir in $candidates) {
    $probe = Join-Path $dir 'nova.config.ps1'
    if (Test-Path $probe) { $configPath = $probe; break }
}

if ($null -ne $configPath) {
    . $configPath
    $cfg = $NovaConfig
    $envCfg = $cfg.Environments['dev']
    if ($Port -eq 0) { $Port = $envCfg.BackendPort }
    if ([string]::IsNullOrWhiteSpace($Broker)) { $Broker = $cfg.Broker.Host }
    $brokerPort = $cfg.Broker.Port
    $adminPassword = $envCfg.AdminPassword
} else {
    # Standalone fallback so this script still works in a bare checkout.
    if ($Port -eq 0) { $Port = 8001 }
    if ([string]::IsNullOrWhiteSpace($Broker)) { $Broker = 'localhost' }
    $brokerPort = 1883
    $adminPassword = 'dev'
}

if ($Broker -eq 'hivemq') { $Broker = 'broker.hivemq.com' }
if ($Broker -eq 'local') { $Broker = 'localhost' }

Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        Write-Host "Creating virtualenv" -ForegroundColor Cyan
        python -m venv .venv
        $InstallDeps = $true
    }

    if ($InstallDeps) {
        Write-Host "Installing dependencies" -ForegroundColor Cyan
        & $python -m pip install --upgrade pip
        & $python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    }

    $env:NOVA_MQTT_BROKER = $Broker
    $env:NOVA_MQTT_PORT = "$brokerPort"
    $env:NOVA_ADMIN_PASSWORD = $adminPassword
    $env:PYTHONUNBUFFERED = '1'

    $children = @()

    if ($WithDummy) {
        $proc = Start-Process -FilePath $python -ArgumentList 'tools\novaSystem_dummy.py' `
                              -WorkingDirectory $repoRoot -PassThru
        $children += $proc
        Write-Host "Started novaSystem_dummy.py (PID $($proc.Id))" -ForegroundColor Cyan
    }

    if ($WithFas) {
        $fasArgs = "tools\fas_bridge.py --broker ${Broker}:${brokerPort} --ops-url http://127.0.0.1:$Port --data-dir data"
        if ($null -ne $envCfg -and -not [string]::IsNullOrWhiteSpace($envCfg.FasPort)) {
            $fasArgs = "$fasArgs --port $($envCfg.FasPort)"
        }
        $proc = Start-Process -FilePath $python -ArgumentList $fasArgs `
                              -WorkingDirectory $repoRoot -PassThru
        $children += $proc
        Write-Host "Started fas_bridge.py (PID $($proc.Id))" -ForegroundColor Cyan
    }

    try {
        Write-Host "Backend (dev, reload) on http://0.0.0.0:$Port - broker $Broker`:$brokerPort" -ForegroundColor Green
        & $python -m uvicorn app.main:app --host 0.0.0.0 --port $Port --reload
    }
    finally {
        foreach ($child in $children) {
            if (-not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
        }
    }
}
finally { Pop-Location }
