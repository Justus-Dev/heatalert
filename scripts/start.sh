#!/usr/bin/env bash
# Start the ring-sandbox emulator and the HeatWatch backend together.
# Usage: ./scripts/start.sh            then open http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

.venv/bin/ring-sandbox serve --port 8787 &
EMULATOR_PID=$!
trap 'kill $EMULATOR_PID 2>/dev/null || true' EXIT

# wait for the emulator before the backend registers its webhook
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8787/_sandbox/health >/dev/null && break
  sleep 0.25
done

echo
echo "  HeatWatch:          http://127.0.0.1:8000"
echo "  Emulator API docs:  http://127.0.0.1:8787/_sandbox/docs"
echo
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
