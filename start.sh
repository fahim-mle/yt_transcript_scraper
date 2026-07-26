#!/usr/bin/env bash
#
# Start the web UI. Assumes ./setup.sh has already run once.
#
#   ./start.sh          launch on port 8000
#   PORT=9000 ./start.sh
#
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
VENV=".venv"

[[ -x "$VENV/bin/uvicorn" ]] || { echo "Run ./setup.sh first."; exit 1; }

echo "Web UI → http://localhost:$PORT   (Ctrl+C to stop)"
exec "$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port "$PORT"
