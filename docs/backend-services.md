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

1. Receive packet from MQTT topic `nova/telemetry/engine` (`source`, `sensors`).
2. Select config section by source (`GCS`, `TCS`, `FAS`, fallback).
3. Map `(hat_id, channel_id)` to configured sensor metadata.
4. Optionally calibrate using piecewise linear interpolation.
5. Apply rolling average smoothing.
6. Broadcast parsed payload over websocket as `engine_data`.
7. Also broadcast `parsed_data` for compatibility with existing clients.

Engine telemetry websocket payload:

```json
{ "type": "engine_data", "data": [] }
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

For testing a new topic before it has a real implementation, `POST /api/mqtt/topics` `{"topic": "nova/other/#"}` subscribes the backend to any filter (`+` and `#` allowed) and `POST /api/mqtt/topics/unsubscribe` removes it; `GET /api/mqtt/topics` lists them. Messages on these topics are only relayed to WebSocket clients as `{"type": "mqtt_message", "topic": ..., "payload": ...}` (non-JSON payloads arrive as a string) and never reach the engine, flight, console or control handlers. Both changes need operator or admin role. Filters that overlap a built-in topic (for example `nova/#`) are rejected, and subscriptions are in memory only, so they are cleared on restart. Code that acts on a topic belongs in a dedicated subscription, not here. The frontend UI is on the Console page, WS / MQTT tab. `nova/stats/#` and `nova/state/#` are built in (below), so a filter that overlaps them is rejected too.

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
4. Publish command over MQTT and broadcast updated actuator state.

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

### RuntimeState

- `calibration_enabled`
- `data_saving_enabled`
- `latest_sensors`
- `latest_engine_data`
- `latest_flight_data`
- `latest_events`
- `physical_lockout_state`
- `actuator_states`

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
