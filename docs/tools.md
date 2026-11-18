# Tools

The programs in `tools/`. All need `paho-mqtt`; the FAS bridge also needs
`pyserial`.

| Tool | What it's for |
|---|---|
| `fas_bridge.py` | The production serial ↔ MQTT link to the flight computer (FMC) |
| `novaMock.py` | **The main simulator**: stands in for novaGround and the FAS bridge together |
| `nova_dummy.py` | Lighter novaGround simulator |
| `serial_probe.py` | See what's on a serial port |
| `sim_control.html` | Browser controls for the simulators |
| `novaGround_dummy.py`, `novaSystem_dummy.py` | Legacy simulators; `dev_linux.sh --with-dummy` still uses `novaSystem_dummy.py` |

## fas_bridge.py

Connects the FMC's serial link to the MQTT broker. It publishes
`nova/telemetry/engine` (EPB ADC samples) and `nova/telemetry/flight` (the full
board state), and turns `type: "fas"` commands from `nova/command` into wire
frames. The full list of commands and console messages is in the docstring at
the top of the file.

```bash
pip install paho-mqtt pyserial
python tools/fas_bridge.py --port COM3 --baud 115200 --broker localhost:1883
```

| Option | Default | Notes |
|---|---|---|
| `--port` | `NOVA_FAS_PORT` | Optional. Without one, the bridge starts idle and novaOps picks a port at runtime (console `list_ports`, then `configure`) |
| `--baud` | `115200` | 115200 for the FMC's RS-422 link; 921600 for its USB-C link |
| `--broker` | `localhost:1883` | |
| `--ops-url` | `NOVA_OPS_URL` | The backend's address, if the bridge runs on another host (for soundboard clip downloads) |
| `--publish-ms` | `50` | Telemetry publish interval |

> [!WARNING]
> The bridge re-implements the firmware's `gs/protocol.py`; it doesn't import
> it. When the FAS team changes the protocol, change the bridge to match
> before pinning the new firmware, with a `!` PR title.

A missing or unplugged port is retried in the background instead of stopping
the bridge. The link state is published on `nova/console` as `console_serial`
and in flight telemetry as `data.fas_link`.

## novaMock.py

```bash
python tools/novaMock.py --broker localhost --ui
```

Publishes GCS and FAS engine packets and the nested flight snapshot, and
answers the same commands as novaGround and the FAS bridge, so the backend and
frontend can be exercised end to end with no hardware.

## nova_dummy.py

```bash
python tools/nova_dummy.py --broker localhost:1883 --verbosity 2
```

Sine-wave channels for two mock DAQ HATs, FAS EPB samples, two EPB boards
always online, and IMC state that follows arm/disarm commands.
