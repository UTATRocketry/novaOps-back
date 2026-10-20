# NovaOps Backend

The FastAPI server at the centre of the Nova ground station. It receives
telemetry from novaGround and the flight computer over MQTT, serves a REST and
WebSocket API to the operator UI, checks every command against roles and safety
rules, and owns the station config and recordings. It also contains
`tools/fas_bridge.py`, the serial link to the flight computer.

> [!CAUTION]
> This service decides which commands reach hardware. Changes to command
> handling, safety rules or roles need a lead's review and the `hardware-risk`
> label ([CONTRIBUTING.md](CONTRIBUTING.md)).

## Repo structure

```
app/
├── main.py, context.py   app factory and the object that owns every service
├── routers/              API endpoints, one file per area
├── services/             the logic: parsing, commands, MQTT, roles, config, recordings
└── models.py             Pydantic models: config schema and request bodies
config/                   published default configs (system.yaml, launch.yaml)
tools/                    fas_bridge.py and the simulators
scripts/                  foreground launchers for Windows and Linux
tests/                    pytest
docs/                     backend docs (mkdocs)
```

## Getting started

### Set up

Python 3.11+ and an MQTT broker (Mosquitto) on `localhost:1883`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt pytest httpx
```

### Run

With a simulator standing in for all the hardware:

```powershell
.\scripts\dev_windows.ps1 -Broker localhost -WithMock
```

On Linux or macOS, start the backend and the simulator separately:

```bash
./scripts/dev_linux.sh --broker localhost
python tools/novaMock.py --broker localhost
```

Open `http://localhost:8000/docs` for the live API reference. Run the tests:

```bash
python -m pytest -q
```

> [!NOTE]
> On the ground station the backend runs as a Windows service managed by
> Nova-Collected's `ops/Nova.ps1`, not by these scripts. `Dockerfile`,
> `docker-compose.yml` and `initial_config.sh` are left from an older setup.

### Troubleshoot

| Symptom | Fix |
|---|---|
| No telemetry | Is the broker running, and is a simulator or novaGround publishing? Watch `http://localhost:8000/view-mqtt` |
| Config fails to load | The error names the field. Duplicate sensor addresses and actuator names are rejected ([config rules](https://github.com/UTATRocketry/Nova-Collected/blob/main/docs/architecture/configs.md#what-the-backend-rejects)) |
| Commands rejected | Your WebSocket client is a `viewer`, or the novaLock lockout is on (`GET /api/flags/lockout`) |
| Tests fail before your change | Compare against a clean checkout of `dev` first |

More: [Nova SUPPORT.md](https://github.com/UTATRocketry/Nova-Collected/blob/main/SUPPORT.md).

## More information

| To... | Read |
|---|---|
| Understand the services and parsing | [docs/backend-services.md](docs/backend-services.md) |
| Set environment variables | [docs/environment.md](docs/environment.md) |
| Use the FAS bridge and simulators | [docs/tools.md](docs/tools.md) |
| Write a config (sensors, actuators, safety rules) | [configs.md](https://github.com/UTATRocketry/Nova-Collected/blob/main/docs/architecture/configs.md) |
| Look up REST, WebSocket and MQTT contracts | [Nova API docs](https://github.com/UTATRocketry/Nova-Collected/tree/main/docs/api) |
| See what changed in each release | [CHANGELOG.md](CHANGELOG.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See the whole system | [Nova-Collected](https://github.com/UTATRocketry/Nova-Collected) |
