#!/usr/bin/env bash
# Dev launcher for the backend on Linux/macOS (bench work, not the ground
# station PC — that runs Windows services, see ops/Nova.ps1).
#
# Changed from the previous version: dependencies are no longer reinstalled on
# every launch. Pass --install-deps when you want them refreshed.
set -euo pipefail

BROKER="localhost"
PORT="8001"
WITH_DUMMY="false"
WITH_FAS="false"
INSTALL_DEPS="false"

print_help() {
  cat <<'EOF'
Usage: ./scripts/dev_linux.sh [options]

Options:
  --broker <localhost|hivemq|host>  MQTT broker host (default: localhost)
  --port <port>                     Uvicorn port (default: 8001, matching dev)
  --with-dummy                      Run tools/novaSystem_dummy.py alongside the API
  --with-fas                        Run tools/fas_bridge.py alongside the API
  --install-deps                    Refresh pip dependencies before starting
  -h, --help                        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --broker)       BROKER="$2"; shift 2 ;;
    --port)         PORT="$2"; shift 2 ;;
    --with-dummy)   WITH_DUMMY="true"; shift ;;
    --with-fas)     WITH_FAS="true"; shift ;;
    --install-deps) INSTALL_DEPS="true"; shift ;;
    -h|--help)      print_help; exit 0 ;;
    *)              echo "Unknown argument: $1"; print_help; exit 1 ;;
  esac
done

case "$BROKER" in
  hivemq) BROKER="broker.hivemq.com" ;;
  local)  BROKER="localhost" ;;
esac

cd "$(dirname "$0")/.."

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
  INSTALL_DEPS="true"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [[ "$INSTALL_DEPS" == "true" ]]; then
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
fi

export NOVA_MQTT_BROKER="$BROKER"
export NOVA_MQTT_PORT="1883"
export NOVA_ADMIN_PASSWORD="${NOVA_ADMIN_PASSWORD:-dev}"
export PYTHONUNBUFFERED=1

CHILD_PIDS=()

cleanup() {
  for pid in "${CHILD_PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT INT TERM

if [[ "$WITH_DUMMY" == "true" ]]; then
  python tools/novaSystem_dummy.py &
  CHILD_PIDS+=("$!")
  echo "Started novaSystem_dummy.py (PID ${CHILD_PIDS[-1]})"
fi

if [[ "$WITH_FAS" == "true" ]]; then
  python tools/fas_bridge.py \
    --broker "${BROKER}:1883" \
    --ops-url "http://127.0.0.1:${PORT}" \
    --data-dir data &
  CHILD_PIDS+=("$!")
  echo "Started fas_bridge.py (PID ${CHILD_PIDS[-1]})"
fi

echo "Backend (dev, reload) on http://0.0.0.0:${PORT} — broker ${BROKER}:1883"
python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
