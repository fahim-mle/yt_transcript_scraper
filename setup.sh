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
# Model names are NOT set here. They come from config.py (which reads .env), so
# there is exactly one place to change one. See ask_config() below.

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

# ── 5. Ollama models ───────────────────────────────────────────────────────
# Read the effective settings from config.py, which applies .env on top of its
# own defaults. Nothing about a model is duplicated in this script.
ask_config() { "$VPY" -c "import config; print(getattr(config, '$1'))" 2>/dev/null; }

ensure_model() {  # ensure_model <tag> <purpose>
    local tag="$1" purpose="$2"
    [[ -n "$tag" ]] || { warn "no model configured for $purpose — skipping"; return; }
    # Match the full tag, not the family prefix: "qwen3" alone also matches
    # "qwen3.5:9b", which reports a model as present when it is not.
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$tag"; then
        ok "$purpose model '$tag' available"
    else
        warn "pulling '$tag' for $purpose (several GB, one time)…"
        ollama pull "$tag" && ok "model pulled" \
            || warn "pull failed — the $purpose stage will be skipped until it is."
    fi
}

say "LLM models (Ollama)"
if command -v ollama >/dev/null; then
    OLLAMA_HOST_URL="$(ask_config OLLAMA_HOST)"
    if curl -sf "${OLLAMA_HOST_URL:-http://localhost:11434}/api/tags" >/dev/null 2>&1; then
        ensure_model "$(ask_config OLLAMA_MODEL)"  "enrichment"
        [[ "$(ask_config REWRITE_ENABLED)" == "True" ]] \
            && ensure_model "$(ask_config REWRITE_MODEL)" "rewrite"
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
