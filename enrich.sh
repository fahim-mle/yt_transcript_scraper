#!/usr/bin/env bash
#
# Background LLM enrichment worker — drains the queue of cleaned-but-not-yet-
# enriched videos. Runs at the lowest CPU and I/O priority so it only uses
# spare cycles and won't slow down your foreground work.
#
#   ./enrich.sh                 enrich everything pending, then exit (good for cron)
#   ./enrich.sh --loop          keep running, picking up new work as it appears
#   ./enrich.sh --limit 5       stop after 5 videos
#
# It's resumable: each video is marked done when finished, so interrupting it
# (Ctrl+C, shutdown) is safe — the next run continues where it left off.
#
# Example cron entry (enrich nightly at 2am):
#   0 2 * * *  /home/ghost/workspace/personal_projects/yt_transcript_scraper/enrich.sh >> /tmp/yt_enrich.log 2>&1
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
[[ -x "$VENV/bin/python" ]] || { echo "Run ./setup.sh first."; exit 1; }

# nice -n 19  : lowest CPU scheduling priority
# ionice -c3  : idle I/O class (only when disk is otherwise free)
# OLLAMA_* honoured from .env via python-dotenv inside main.py
exec nice -n 19 ionice -c3 "$VENV/bin/python" main.py enrich "$@"
