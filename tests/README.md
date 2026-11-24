# Backend tests

pytest tests for novaOps-back: the FastAPI app, its services, the FAS bridge,
and two end-to-end tests that run a real MQTT broker.

## Run them

From the repo root, with the virtualenv active:

```powershell
pip install -r requirements.txt -r requirements-test.txt
python -m pytest -q
```

Common variations:

| To | Run |
|---|---|
| Run one file | `python -m pytest -q tests/test_command_safety.py` |
| Run one test | `python -m pytest -q tests/test_command_safety.py::test_name` |
| Skip the broker tests | `python -m pytest -q -m "not integration"` |
| Match CI, coverage included | `python -m pytest -q --cov=app --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=69` |

Every test has a 60 s timeout (`pytest-timeout`, set in `pyproject.toml`), so
a hung websocket or broker fails the test instead of stalling the run.

## What's here

| Path | Covers |
|---|---|
| `test_command_safety.py` | Actuator command gating: novaLock, hazardous rules, roles |
| `test_command_parser.py`, `test_actuator_state_sync.py` | Parsing commands, and actuator state across config switches |
| `test_config_routes.py` | Config REST routes, the content hash, switching configs |
| `test_station_config.py` | Config history and recording metadata |
| `test_models.py`, `test_sensor_parser.py` | The config schema (Pydantic models) and sensor parsing |
| `test_telemetry_pipeline.py`, `test_telemetry_contract.py` | Telemetry ingest: message shapes, calibration, rolling averages, staleness |
| `test_interpolation.py`, `test_robust_calibration.py` | Calibration fits |
| `test_mqtt_service.py`, `test_mqtt_robustness.py` | `MqttService` with a fake client: routing, hostile input, no broker |
| `test_node_stats_state.py` | `nova/stats/<node>` and `nova/state/<node>` topics, and novaSim's copy of them |
| `test_websocket_endpoint.py`, `test_websocket_manager.py`, `test_role_service.py` | The `/ws` endpoint, fan-out to clients, and roles |
| `test_recordings.py` | Starting and stopping recordings, and data-file uploads |
| `test_sound_clip_upload.py` | Staging soundboard clips for HTTP pickup |
| `test_fas_bridge_data_file.py` | Data-file naming in `tools/fas_bridge.py` |
| `test_pi_routing.py` | Per-Pi command topics for every target, and broadcasts (data saving, console) staying on `nova/command` |
| `test_pi_config_schema.py` | `tc_type`, `DI` sensors, `max_on_s` / `on_comms_loss`, `Heartbeat`, reserved and out-of-range pins |
| `test_pi_config_message.py` | `nova/config/<node>` contents and publishing, its status, mismatch, trips, `GET /api/config/pi` |
| `test_pi_telemetry.py` | Thermocouple fault values, `gpios` as `DI` readings, the `novaMock` alias, dropped unknown sources |
| `test_heartbeat.py` | `nova/heartbeat/backend` beats and the MQTT last will |
| `test_novasim_pi_config.py` | novaSim standing in for novaGround's config status and trips |
| `golden/config_hash_vectors.json` | Known inputs and their config content hashes |
| `integration/` | Real processes: Mosquitto, `tools/novaSim.py`, the backend |

## Integration tests

`tests/integration/` starts a private Mosquitto broker on a free localhost
port, never 1883, so it can't touch a station's broker. The tests are marked
`integration`:

- `test_novasim_e2e.py` runs novaSim → MQTT → backend → a websocket client
  and checks the client receives calibrated engine telemetry.
- `test_pi_config_e2e.py` checks the backend's retained `nova/config/novaGround`
  reaches novaSim and its status comes back with no mismatch.
- `test_mqtt_reconnect.py` kills the broker mid-test, brings it back on the
  same port, and checks the backend reconnects, resubscribes and publishes.

They need Mosquitto 2.x. The fixtures look for it on `PATH`, then in
`C:\Program Files\mosquitto\` and `/usr/sbin/`. Without it they're skipped,
unless `NOVA_REQUIRE_INTEGRATION` is set; then they fail. CI sets it so the
tests can't silently stop running.

The broker runs as `mosquitto -p <port>` with no config file. With only `-p`,
Mosquitto 2.x listens on loopback alone and allows anonymous clients. If it
exits or never opens its port, the failure message includes its output.

## The config hash vectors

> [!IMPORTANT]
> `golden/config_hash_vectors.json` pins the config content hash: the first
> 16 hex characters of SHA-256 of the file's raw bytes. `Get-ContentHash` in
> Nova-Collected's `ops/Nova.ps1` must produce the same values (AGENTS.md
> rule 9). Change the vectors only together with both implementations.

The vectors include CRLF and BOM inputs because the hash is over raw bytes,
and line-ending or encoding changes must change it.

## CI

`.github/workflows/pr-checks.yml` runs the whole suite on every PR, on Ubuntu
and Windows with Python 3.11, 3.12 and 3.13, with Mosquitto installed and
`NOVA_REQUIRE_INTEGRATION=1`. Each job uploads its JUnit and coverage
results as an artifact.

The job also fails if coverage of `app/` (lines and branches together) falls
below the `--cov-fail-under` floor. The floor is a ratchet: raise it as tests are added,
never lower it. A pytest-cov line saying "Required test coverage of N%
reached" means the coverage check passed; if the job still fails, look
further up for a failing test.

## Writing a test

- **Build what you need in the test file.** There's no shared top-level
  `conftest.py`. Files that need a running app make a `station` fixture: an
  `AppContext` on `tmp_path`, wrapped in FastAPI's `TestClient`.
- **Never reach a real broker, backend or Pi** outside `integration/`. Use a
  fake MQTT client, as `test_mqtt_service.py` does.
- **Don't depend on the clock's resolution.** Windows' monotonic clock ticks
  about every 15 ms, so two calls in a row can return the same value. Patch
  `time.monotonic` instead of sleeping, as
  `test_clip_store_purges_expired` does.
- **Use raw strings for Windows paths** (`r"C:\Windows\x"`). A non-raw
  `"\W"` is a `SyntaxWarning` now and an error in a future Python.
- **Start the module with a docstring** saying what the file covers, as most
  files here do.
