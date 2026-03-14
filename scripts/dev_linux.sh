#!/usr/bin/env bash
set -euo pipefail

BROKER="localhost"
PORT="8000"
WITH_DUMMY="false"

print_help() {
  cat <<'EOF'
Usage: ./scripts/run_dev_linux.sh [options]

Options:
  --broker <localhost|hivemq|host>  MQTT broker host (default: localhost)
  --port <port>                     Uvicorn port (default: 8000)
  --with-dummy                      Run tools/novaGround_dummy.py alongside API
  -h, --help                        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --broker)
      BROKER="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --with-dummy)
      WITH_DUMMY="true"
      shift
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      print_help
      exit 1
      ;;
  esac
done

if [[ "$BROKER" == "hivemq" ]]; then
  BROKER="broker.hivemq.com"
elif [[ "$BROKER" == "local" ]]; then
  BROKER="localhost"
fi

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

export NOVA_MQTT_BROKER="$BROKER"
export NOVA_MQTT_PORT="1883"

DUMMY_PID=""
if [[ "$WITH_DUMMY" == "true" ]]; then
  python tools/novaGround_dummy.py &
  DUMMY_PID=$!
  echo "Started novaGround_dummy.py (PID $DUMMY_PID)"
fi

cleanup() {
  if [[ -n "$DUMMY_PID" ]]; then
    kill "$DUMMY_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting backend with NOVA_MQTT_BROKER=$NOVA_MQTT_BROKER on port $PORT"
python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
