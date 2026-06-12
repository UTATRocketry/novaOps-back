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
