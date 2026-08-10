#!/usr/bin/env bash
#
# One-shot setup for yt_transcript_scraper.
#
#   ./setup.sh          full setup, then launch the web UI
#   ./setup.sh --no-web setup only, don't start the server
#
# Idempotent — safe to re-run. It creates the venv, installs deps, prepares
# the PostgreSQL schema, checks Ollama + the enrichment model, then (unless
# --no-web) starts the UI at http://localhost:8000.

set -euo pipefail
cd "$(dirname "$0")"

# ── pretty output ──────────────────────────────────────────────────────────
c_reset=$'\033[0m'; c_blue=$'\033[34m'; c_green=$'\033[32m'
c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_bold=$'\033[1m'
say()  { printf "%s▸%s %s\n" "$c_blue" "$c_reset" "$1"; }
ok()   { printf "%s✓%s %s\n" "$c_green" "$c_reset" "$1"; }
warn() { printf "%s!%s %s\n" "$c_yellow" "$c_reset" "$1"; }
die()  { printf "%s✗%s %s\n" "$c_red" "$c_reset" "$1" >&2; exit 1; }

SERVE=1
[[ "${1:-}" == "--no-web" ]] && SERVE=0

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV=".venv"
PORT="${PORT:-8000}"
# Kept in step with config.py's default. Setup only checks/pulls the enrichment
# model; the rewrite model (REWRITE_MODEL) is pulled on demand by that stage.
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:9b}"

printf "\n%sYouTube Transcript Scraper — setup%s\n\n" "$c_bold" "$c_reset"

# ── 1. Python virtualenv ───────────────────────────────────────────────────
say "Python environment"
command -v "$PYTHON_BIN" >/dev/null || die "python3 not found on PATH"
if [[ ! -d "$VENV" ]]; then
    "$PYTHON_BIN" -m venv "$VENV"
    ok "created $VENV"
else
    ok "$VENV exists"
fi
VPY="$VENV/bin/python"

# ── 2. Dependencies ────────────────────────────────────────────────────────
say "Dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt
ok "requirements installed"

# ── 3. Environment file ────────────────────────────────────────────────────
say "Configuration (.env)"
if [[ ! -f .env ]]; then
    cp .env.example .env
    warn "created .env from template — review DATABASE_URL before heavy use"
else
    ok ".env present"
fi

# Load DATABASE_URL from .env for the checks below.
DB_URL="$(grep -E '^DATABASE_URL=' .env | tail -1 | cut -d= -f2- || true)"

# ── 4. PostgreSQL schema ───────────────────────────────────────────────────
say "Database"
if [[ -z "$DB_URL" || "$DB_URL" == *"user:password"* ]]; then
    warn "DATABASE_URL not configured in .env — skipping schema setup."
    warn "  Set e.g.  DATABASE_URL=postgresql:///yt_transcripts  then re-run."
elif ! command -v psql >/dev/null && ! "$VPY" -c "import psycopg2" 2>/dev/null; then
    warn "PostgreSQL client not available — skipping schema setup."
else
    if "$VPY" main.py setup-db; then
        ok "schema ready"
    else
        warn "schema setup failed — check DATABASE_URL and that PostgreSQL is running."
    fi
fi

# ── 5. Ollama enrichment model ─────────────────────────────────────────────
say "LLM enrichment (Ollama)"
if command -v ollama >/dev/null; then
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        # Match the full tag, not the family prefix: "qwen3" alone also matches
        # "qwen3.5:9b", which reports a model as present when it is not.
        if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$OLLAMA_MODEL"; then
            ok "model '$OLLAMA_MODEL' available"
        else
            warn "pulling '$OLLAMA_MODEL' (several GB, one time)…"
            ollama pull "$OLLAMA_MODEL" && ok "model pulled" \
                || warn "pull failed — clean will still run, without enrichment."
        fi
    else
        warn "Ollama installed but server not responding — start it with:  ollama serve"
        warn "  (clean still works without it; it just skips enrichment.)"
    fi
else
    warn "Ollama not installed — enrichment will be skipped."
    warn "  Install from https://ollama.com to enable content metadata."
fi

# ── Done ───────────────────────────────────────────────────────────────────
printf "\n%s✓ Setup complete.%s\n\n" "$c_green$c_bold" "$c_reset"

if [[ "$SERVE" -eq 1 ]]; then
    say "Starting web UI at http://localhost:$PORT  (Ctrl+C to stop)"
    echo
    exec "$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port "$PORT"
else
    echo "Next steps:"
    echo "  • Start the web UI:   ./start.sh"
    echo "  • Or use the CLI:     $VENV/bin/python main.py scrape \"<url>\""
    echo
fi
