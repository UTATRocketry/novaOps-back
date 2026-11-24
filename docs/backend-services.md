# Backend Architecture

## Service Overview

The backend is organized around a few core runtime services:

- `ConfigService`: loads, validates, and updates YAML-backed system configuration.
- `SensorParser`: translates raw hardware packets to UI-friendly sensor output.
- `CommandParser`: converts UI actuator commands to device-facing MQTT commands.
- `MqttService`: manages MQTT connectivity, subscriptions, and publishes.
- `WebSocketManager`: tracks clients, assigns roles, broadcasts state/data.
- `RuntimeState`: keeps calibration/data-saving flags and latest actuator/sensor state.

## Parsing Pipeline

### Engine telemetry parsing

1. Receive packet from MQTT topic `nova/telemetry/engine` (`source`, `sensors`, and from a Pi `gpios`).
2. Select config section by source. `GCS`, `novaGround`, `novaOps` and `novaMock` are `GCS`; `TCS` and `novaThermo` are `TCS`; `FAS` and `novaFAS` are `FAS` (case-insensitive). A packet from any other source is dropped, and each unknown name is logged once. When one target gets telemetry from two sources within 5 s (novaMock next to the real novaGround), a warning is logged.
3. Map `(hat_id, channel_id)` to configured sensor metadata.
4. A `TC` sensor on `GCS`/`TCS` reading exactly `-9999.0` (open), `-7777.0` (common mode) or `-8888.0` (overrange) is a daqhats fault code, not a temperature: it is sent with `value` and `avg` null and `fault` set to `"open"`, `"common_mode"` or `"overrange"`, and left out of the rolling average.
5. Optionally calibrate using piecewise linear interpolation.
6. Apply rolling average smoothing.
7. Turn each `gpios` entry (`{pin_id, state}`) whose pin is a configured `DI` sensor on that Pi into a reading of 0 or 1 (no calibration or averaging). It takes the packet's newest sensor timestamp.
8. Broadcast parsed payload over websocket as `engine_data`.
9. Also broadcast `parsed_data` for compatibility with existing clients.

Engine telemetry websocket payload. `fault` is only present on a faulted reading:

```json
{ "type": "engine_data", "data": [
  { "name": "TC_Tank", "value": null, "avg": null, "unit": "C", "timestamp": 1795110000000, "fault": "open" }
] }
```

### Flight telemetry routing

Lower-rate FAS avionics packets arrive on `nova/telemetry/flight`.

Flight data is stored and broadcast without parsing:

```json
{ "source": "FAS", "data": { "...": "..." } }
```

```json
{ "type": "flight_data", "data": {} }
```

Flight events are stored and broadcast without parsing:

```json
{ "source": "FAS", "events": [] }
```

```json
{ "type": "flight_events", "events": [] }
```

### Console and lockout routing

- `nova/console`: raw passthrough between frontend, backend, novaGround, and FAS. Backend publishes and broadcasts payloads without adding formatting.
- `nova/control`: physical lockout updates. A payload such as `{ "source": "novaLock", "state": "locked" }` sets the runtime lockout state and blocks commands matched by `safetyRules.hazardous` while locked.

### Ad-hoc topic subscriptions

For testing a new topic before it has a real implementation, `POST /api/mqtt/topics` `{"topic": "nova/other/#"}` subscribes the backend to any filter (`+` and `#` allowed) and `POST /api/mqtt/topics/unsubscribe` removes it; `GET /api/mqtt/topics` lists them. Messages on these topics are only relayed to WebSocket clients as `{"type": "mqtt_message", "topic": ..., "payload": ...}` (non-JSON payloads arrive as a string) and never reach the engine, flight, console or control handlers. Both changes need operator or admin role. Filters that overlap a built-in topic (for example `nova/#`) are rejected, and subscriptions are in memory only, so they are cleared on restart. Code that acts on a topic belongs in a dedicated subscription, not here. The frontend UI is on the Console page, WS / MQTT tab. `nova/stats/#`, `nova/state/#` and `nova/config/+/status` are built in (below), so a filter that overlaps them is rejected too.

### Node stats and actuator state

Each node that runs with stats on (novaGround, novaThermo, novaSim) publishes, and the backend subscribes to `nova/stats/#` and `nova/state/#` (see novaGround's `docs/instrumentation.md` and `docs/bench-safety.md`):

| Topic | Retained | Backend keeps | WebSocket message |
|---|---|---|---|
| `nova/stats/<node>/info` | yes | `runtime.node_stats_info[node]` | `node_stats_info` `{node, info}` |
| `nova/stats/<node>` | no | `runtime.node_stats[node]` | `node_stats` `{node, stats}` |
| `nova/state/<node>` | yes | `runtime.node_state[node]` | `node_state` `{node, state}` |

The payloads pass through unchanged; the topic names the node. A message that is not a JSON object is logged and dropped, and an empty payload (a node clearing its retained message) removes that node's entry and is broadcast with a null body. Stats are dropped after `NOVA_STALE_TTL_S` without an update, like telemetry; info and state are retained, so check `started_ms` and `time_ms` before trusting them. A client that connects gets the cached messages. `GET /api/nodes` and `GET /api/nodes/{node}` return `{info, stats, state}` for each node that has sent something. Every message is also relayed as `mqtt_message`, like other topics.

`state` is what the drivers hold, not what was commanded, so under `--stop-actuate` (`info.actuation` is `"stopped"`) it stays put after a relay command. Telemetry packets from a node with stats on carry extra `seq`, `sample_ms` and `publish_ms` fields, which the backend ignores.

### Command parsing

1. Receive UI command payload (`type`, `name`, `state`).
2. Resolve actuator by configured `name`.
3. Translate to MQTT command format:
   - relay/gpio state commands
   - servo angle commands
4. Publish command over MQTT on the target's topic and broadcast updated actuator state.

Command topics (Nova-Collected `docs/design/pi-io-config.md`):

| Topic | Carries |
|---|---|
| `nova/command/novaGround` | `relay`, `gpio`, `servo` for `GCS` actuators and direct `GCS` relay/servo commands |
| `nova/command/novaThermo` | `relay`, `gpio`, `servo` for `TCS` actuators |
| `nova/command` | FAS commands (addressed by board), and the broadcasts every node acts on: `data_file` (data saving) and `console` |

`CommandParser.route()` returns the topic with the commands. Any other target
is an error, so a `TCS` actuator can never reach the FAS path, and config
validation rejects actuators whose target has no command path (`OPS`).

### Per-Pi config

On every MQTT connect and every active-config change (startup, load, upload,
edit, reload), the backend publishes a retained `nova/config/novaGround` (from
the `GCS` part of the config) and `nova/config/novaThermo` (`TCS`):

```json
{
  "config_hash": "3f9a1c0be27d4a55",
  "gpio": { "outputs": { "17": { "max_on_ms": 1800000, "on_comms_loss": "safe" } }, "inputs": [5] },
  "relays": { "3": { "max_on_ms": null, "on_comms_loss": "hold" } },
  "daq": { "1": { "0": "K", "1": "T" } },
  "heartbeat": { "timeout_ms": 2000 }
}
```

- `config_hash` is the active config file's content hash, the same one the config history uses.
- `gpio.outputs`: every `gpio_device` and `powered_gpio_device` on that Pi, by `gpio_channel`, with its limits. `gpio.inputs`: the pins of its `DI` sensors.
- `relays`: only the relay channels of actuators that set `max_on_s` or `on_comms_loss: safe`. A servo can set them only when it has a power relay (`binding.relay_channel`); the limits switch that relay off, never its PWM output. Two actuators on one output get the smaller `max_on_s` and `safe` if either is.
- `daq`: HAT address -> channel -> thermocouple type, for its `TC` sensors (`tc_type`, default K).
- `heartbeat.timeout_ms`: the config's `Heartbeat.timeout_ms` (default 2000).

The backend subscribes to the retained `nova/config/+/status` each Pi answers
with, and keeps the latest per node in `runtime.pi_config_status`. A Pi shows a
mismatch when its `config_hash` differs from the active config's or its
`rejected` list isn't empty. Each `tripped` entry (`gpio:N` or `relay:N`) sets
the matching actuators to their safe state (GPIO level 0, relay de-energized)
using the command rules (`relay_type`, `solenoid_type`, motor patterns), once
per trip, and adds `tripped`, `tripped_output` and `tripped_ms` to their entry
in `actuator_states` until the Pi stops listing it. The backend never sends a
command because of a trip.

Each Pi's view (`status_view` in `app/services/pi_config.py`) adds three
fields to the status:

- `apply_state`:
  - `waiting`: no status from the Pi yet.
  - `applied`: its hash matches and it rejected nothing.
  - `pending`: its hash differs and it rejected nothing, so it hasn't applied the config yet.
  - `rejected`: it rejected items. This wins over `pending`.
- `received_ms`: when the backend last got this Pi's status.
- `published_ms`: when the backend first published the active config to this
  Pi, set only when the publish succeeded. Republishing the same hash, as on
  reconnect, keeps the first time.

Both times are backend wall-clock milliseconds.

The frontend gets this as a `pi_config_status` WebSocket message per Pi
(`{type, node, status}`), sent on connect, when a status arrives and when the
active config changes, and from `GET /api/config/pi`, which also returns the
config messages, the reserved pins (`0, 1, 2, 3, 7, 8, 9, 10, 11, 12, 13, 16,
20, 21, 26`), `gpio_max` (27) and the thermocouple types.

### Heartbeat

The backend publishes `{"online": true, "seq": n, "time_ms": t}` on
`nova/heartbeat/backend` every 250 ms, QoS 0 and not retained, from the main
event loop, so a stuck loop stops the beats. Its MQTT last will is a retained
`{"online": false}` on the same topic. On connect it publishes a retained
`{"online": true}` to replace the will, and on a clean shutdown a retained
`{"online": false}`, since a clean disconnect doesn't trigger the will. The Pis
use it to put outputs set to `on_comms_loss: safe` in their safe state.

## Managers and Runtime Coordination

### WebSocketManager

- First client becomes `operator`, subsequent clients become `viewer`.
- If operator disconnects, next connected client is promoted.
- Broadcasts:
  - `session`
  - `snapshot`
  - `engine_data`
  - `parsed_data`
  - `flight_data`
  - `flight_events`
  - `physical_lockout`
  - `actuator_states`
  - `pi_config_status`

### RuntimeState

- `calibration_enabled`
- `data_saving_enabled`
- `latest_sensors`
- `latest_engine_data`
- `latest_flight_data`
- `latest_events`
- `physical_lockout_state`
- `actuator_states`
- `pi_config_status` (latest `nova/config/<node>/status` per Pi)

## API and OpenAPI

OpenAPI docs are available at:

- `/docs` (Swagger UI)
- `/redoc` (ReDoc)

API groups in docs:

- `System`
- `Config`
- `Flags`
- `Data`
- `Commands`
