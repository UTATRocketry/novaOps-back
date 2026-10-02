# NovaOps Backend

NovaOps Backend is a FastAPI service that bridges frontend clients, MQTT device
traffic, and config-driven parsing and translation logic.

## Documentation map

| Document | Covers |
|---|---|
| [Backend services](backend-services.md) | Service responsibilities, parsing flow, runtime state |
| [Environment variables](environment.md) | Every variable the backend and FAS bridge read |
| [Tools](tools.md) | The FAS bridge and the simulators |
| `/docs`, `/redoc` on a running backend | The live, generated API reference |

System-wide docs (config schema, MQTT topics, releases) live in
[Nova-Collected](https://github.com/UTATRocketry/Nova-Collected/blob/main/docs/README.md).
