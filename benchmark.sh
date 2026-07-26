#!/usr/bin/env bash
#
# Compare local models on the SAME transcript — speed (gen tok/s) and the
# actual metadata each one produces, side by side. Read-only: nothing is
# written to the database or blob storage. Runs at low CPU/I/O priority so it
# doesn't fight your foreground work.
#
#   ./benchmark.sh --models qwen3:4b,qwen2.5:7b-instruct,qwen2.5:3b-instruct
#   ./benchmark.sh --models qwen2.5:7b-instruct --video dQw4w9WgXcQ
#   ./benchmark.sh --models qwen2.5:3b-instruct --words 1500 --out bench.json
#
# Pick a model empirically, then set OLLAMA_MODEL=<winner> in .env.
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
[[ -x "$VENV/bin/python" ]] || { echo "Run ./setup.sh first."; exit 1; }

exec nice -n 19 ionice -c3 "$VENV/bin/python" main.py benchmark "$@"
