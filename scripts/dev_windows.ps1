param(
  [string]$Broker = "localhost",
  [int]$Port = 8000,
  [switch]$WithDummy
)

$ErrorActionPreference = "Stop"

if ($Broker -eq "hivemq") { $Broker = "broker.hivemq.com" }
if ($Broker -eq "local") { $Broker = "localhost" }

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

$env:NOVA_MQTT_BROKER = $Broker
$env:NOVA_MQTT_PORT = "1883"

$dummyProcess = $null
if ($WithDummy) {
  $dummyProcess = Start-Process -FilePath "python" -ArgumentList "tools/novaGround_dummy.py" -PassThru
  Write-Host "Started novaGround_dummy.py (PID $($dummyProcess.Id))"
}

try {
  Write-Host "Starting backend with NOVA_MQTT_BROKER=$env:NOVA_MQTT_BROKER on port $Port"
  python -m uvicorn app.main:app --host 0.0.0.0 --port $Port --reload
}
finally {
  if ($null -ne $dummyProcess -and -not $dummyProcess.HasExited) {
    Stop-Process -Id $dummyProcess.Id -Force
  }
}
