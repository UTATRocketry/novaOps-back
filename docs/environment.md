# Environment variables

Everything the backend and the FAS bridge read from the environment. On the
ground station, Nova-Collected's `ops/Nova.ps1` sets these for each service from
`ops/nova.settings.ps1`; for local development the defaults work.

## Backend

| Variable | Default | What |
|---|---|---|
| `NOVA_MQTT_BROKER` | `localhost` | MQTT broker host |
| `NOVA_MQTT_PORT` | `1883` | MQTT broker port |
| `NOVA_CONFIG_DIR` | `config/` | Live config folder. An empty folder is seeded from `config/` on first start |
| `NOVA_CONFIG_HISTORY_DIR` | `<config dir>/history` | Every config ever used, by content hash, plus `history.jsonl` |
| `NOVA_DATA_DIR` | `data/` | Recordings and their `.meta.json` |
| `NOVA_ENV` | empty | `prod` or `dev`; stamped into recordings and config history |
| `NOVA_RELEASE` | empty | Release tag, e.g. `v2026.10.04`; stamped into recordings |
| `NOVA_ADMIN_PASSWORD` | unset | Password for the `admin` role. Unset disables the role |
| `NOVA_STALE_TTL_S` | `10` | Seconds without fresh data before a sensor is dropped |
| `NOVA_STALE_CHECK_INTERVAL_S` | `2` | How often staleness is checked |
| `NOVA_PUBLIC_BASE_URL` | the requesting URL | Base URL the FAS bridge uses to download staged soundboard clips |
| `NOVA_CLIP_TTL_S` | `900` | Seconds a staged clip is kept if nobody fetches it |
| `NOVA_SOUND_CLIP_MAX_BYTES` | 8 MiB | Cap on a transcoded clip |
| `NOVA_SOUND_SOURCE_MAX_BYTES` | 64 MiB | Cap on an uploaded source file |

> [!WARNING]
> Never commit a real `NOVA_ADMIN_PASSWORD`. On the station it comes from the
> untracked `ops/nova.settings.ps1`.

## FAS bridge

| Variable | Default | What |
|---|---|---|
| `NOVA_FAS_PORT` | empty | Serial port; same as `--port`. Empty starts the bridge idle until a port is chosen from the UI |
| `NOVA_OPS_URL` | empty | Backend URL for clip downloads; same as `--ops-url` |

What each config folder holds, and how its history works:
[configs.md](https://github.com/UTATRocketry/Nova-Collected/blob/main/docs/architecture/configs.md).
