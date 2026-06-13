# NovaOps Backend

FastAPI backend for the Nova rocket ground-support system. Receives sensor telemetry from novaGround over MQTT, exposes a REST + WebSocket API to the frontend, translates frontend actuator commands into typed MQTT command frames, and manages config, data files, and system state.

---

## Directory structure

```
novaOps-back/
├── app/
│   ├── context.py          # AppContext — shared state passed to services
│   ├── models.py           # Pydantic models: sensors, actuators, bindings, config
│   ├── routes/             # FastAPI routers (actuators, sensors, config, data-files, …)
│   ├── services/
│   │   ├── command_service.py   # Command parsing and MQTT publish
│   │   ├── config_service.py    # System config load/reload
│   │   ├── mqtt_service.py      # MQTT client wrapper
│   │   └── sensor_service.py    # Telemetry ingestion and calibration
│   └── static/
├── config/                 # system.yaml lives here
├── data/                   # CSV data files uploaded from novaGround
├── tools/
│   ├── nova_dummy.py       # Simulates current novaGround (nova/telemetry, FAS, console)
│   ├── fas_bridge.py       # Direct FAS RS-422 → MQTT bridge (runs without novaGround)
│   ├── novaGround_dummy.py # Legacy GCS simulator (nova/telemetry/engine)
│   └── novaSystem_dummy.py # Legacy system simulator
├── scripts/                # Dev environment scripts (Linux + Windows)
├── tests/
├── docker-compose.yml
└── Dockerfile
```

---

## Features

- MQTT subscribe/publish integration with novaGround and FAS
- WebSocket channel delivering parsed sensor data and actuator states in real time
- Config-driven sensor and actuator parsing via `config/system.yaml`
- Linear interpolation and polynomial calibration, rolling average smoothing
- FAS actuator support: EPB PWM, load switches, IMC arm/disarm, GPIO
- REST API for config, flags, data files, sensors, and actuators
- Logging to console and rotating file logs
- Unit tests for parsing and command translation

---

## MQTT topics

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribe | `nova/telemetry` | Sensor + FAS telemetry from novaGround |
| Subscribe | `nova/console` | Raw FAS frame passthrough (console mode) |
| Subscribe | `nova/control` | Physical lockout state from novaLock |
| Publish | `nova/command` | Actuator commands to novaGround |
| Publish | `nova/control` | Data-saving control to novaGround |

Environment variables:

```
NOVA_MQTT_BROKER   MQTT broker host (default: localhost)
NOVA_MQTT_PORT     MQTT broker port  (default: 1883)
```

### Command envelope

All commands are published as:

```json
{"source": "novaOps", "command": {"type": "<type>", ...}}
```

### Telemetry format (nova/telemetry)

```json
{
  "source": "novaGround",
  "sensors":    [{"hat_id": 0, "channel_id": 0, "value": 1.234, "timestamp": 12345}],
  "gpios":      [{"pin_id": 17, "state": 0}],
  "fas_boards": [{"key": "EPB:0", "online": true, "uptime_ms": 4200}],
  "fas_imc":    {"board_id": 0, "armed": false, "arm_line": false, "disarm_line": false}
}
```

FAS EPB ADC channels appear in `sensors` at `hat_id = 100 + board_id`.

---

## API summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/actuators` | All actuator states |
| GET | `/api/sensors` | Latest sensor readings |
| GET | `/api/config` | Current system config |
| POST | `/api/config/upload` | Upload a new config file |
| PUT / PATCH | `/api/config` | Update config fields |
| POST | `/api/config/reload` | Reload config from disk |
| POST | `/api/flags/calibration` | Toggle calibration mode |
| POST | `/api/flags/data-saving` | Start/stop data saving |
| GET | `/api/data-files` | List saved CSV files |
| GET | `/api/data-files/{file_name}` | Download a CSV file |
| POST | `/api/commands` | Send an actuator command |
| WS | `/ws?role=operator\|pad\|viewer\|dev` | Real-time telemetry and state |

---

## Config — system.yaml

### Actuator binding fields (FAS targets)

```yaml
Actuators:
  - name: MAIN_VALVE
    type: solenoid
    binding:
      target: FAS
      board_type: EPB      # board kind string
      board_id: 0          # 0-based index (default 0 if omitted)
      relay_channel: 2
    actions:
      solenoid_type: nominally_open

  - name: TVC_SERVO
    type: servo
    binding:
      target: FAS
      board_type: EPB
      board_id: 1
      servo_channel: 0
      relay_channel: 1     # power relay on the same EPB
    actions:
      position_aliases: [retract, extend]
      positions: [1000, 2000]
```

`node` (legacy string form `"EPB_1"`) is still accepted for backward compatibility and is automatically converted to `board_type`/`board_id`.

### FAS command shapes emitted

```jsonc
// Solenoid / relay
{"type":"fas","board_type":"EPB","board_id":0,"port":"relay","channel":2,"action":"on"}

// Servo positional move (relay energised first, then PWM)
{"type":"fas","board_type":"EPB","board_id":1,"port":"relay","channel":1,"action":"on"}
{"type":"fas","board_type":"EPB","board_id":1,"port":"servo","channel":0,"action":"extend","value":2000}

// Servo disable → PWM 0 µs
{"type":"fas","board_type":"EPB","board_id":1,"port":"servo","channel":0,"value":0}

// Servo enable → no command sent

// IMC arm/disarm
{"type":"fas","board_type":"EPB","board_id":0,"port":"gpio","channel":0,"action":"ARM"}
```

`value` is always a pulse width in microseconds.

---

## Tools

All tools are in `tools/` and require `paho-mqtt` (`pip install paho-mqtt`).

### `nova_dummy.py` — novaGround simulator

Simulates a live novaGround instance. Publishes `nova/telemetry` at a configurable rate with:
- Sine-wave ADC channels for 2 mock MCC DAQ hats
- FAS EPB ADC samples at `hat_id = 100 + board_id`
- 2 FAS EPB boards always online
- FAS IMC state that follows arm/disarm commands

Handles all command types including both FAS shapes and console start/stop.

```bash
python tools/nova_dummy.py --broker localhost:1883 --verbosity 2
python tools/nova_dummy.py --help
```

### `fas_bridge.py` — direct FAS serial bridge

Connects a FAS RS-422 serial port directly to the MQTT broker. Use this when novaGround hardware is unavailable but FAS hardware is present, or to run FAS from a laptop instead of a Pi.

Publishes `nova/telemetry` in the same format as novaGround. Translates inbound `nova/command` FAS commands to wire frames. All non-FAS command types are silently dropped.

```bash
pip install paho-mqtt pyserial
python tools/fas_bridge.py --port /dev/ttyUSB0 --broker localhost:1883
python tools/fas_bridge.py --help
```

---

## Running

### Without Docker

```bash
# Linux
bash scripts/dev_linux.sh --broker localhost
bash scripts/dev_linux.sh --broker hivemq --with-dummy

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File scripts/dev_windows.ps1 -Broker localhost
powershell -ExecutionPolicy Bypass -File scripts/dev_windows.ps1 -Broker hivemq  -WithDummy
```

Broker shortcuts: `localhost` / `local`, `hivemq` (maps to `broker.hivemq.com`), or any host string.

### With Docker

```bash
sudo docker-compose up --build
```

Access the app at `http://localhost:8000`. To view container stdout/stderr:

```bash
docker attach <CONTAINER>
```

To stop:

```bash
sudo docker-compose down
```

---

## Raspberry Pi deployment

1. Clone and enter the repo:
   ```bash
   git clone https://github.com/UTATRocketry/novaOps-back.git
   cd novaOps-back
   ```

2. Update the log path in `initial_config.sh` (line 4).

3. Make scripts executable and run them:
   ```bash
   chmod +x initial_config.sh post_reboot_config.sh
   ./initial_config.sh
   # After reboot:
   ./post_reboot_config.sh
   ```

The server starts at `http://192.168.0.1:8000` (Ethernet) or `http://raspberrypi.local:8000`.

---

## Troubleshooting

**Can't connect from a client:**
- Check server is up: `curl http://0.0.0.0:8000/api/config`
- Check static IP: `ip addr show eth0`
- Check DHCP: `sudo systemctl status dhcpcd`
- Check DNS: `sudo systemctl status dnsmasq`
- Check Docker: `sudo docker ps`

**Stop and remove containers:**
```bash
sudo docker-compose down
```
