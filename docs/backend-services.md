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

### Sensor parsing

1. Receive packet from MQTT (`source`, `sensors`).
2. Select config section by source (`MCC128DAQ`, `MCC134DAQ`, `FAS`, fallback).
3. Map `(hat_id, channel_id)` to configured sensor metadata.
4. Optionally calibrate using piecewise linear interpolation.
5. Apply rolling average smoothing.
6. Broadcast parsed payload over websocket as `parsed_data`.

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
  - `parsed_data`
  - `actuator_states`

### RuntimeState

- `calibration_enabled`
- `data_saving_enabled`
- `latest_sensors`
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
