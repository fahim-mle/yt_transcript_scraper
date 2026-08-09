import os

# ── Directories ────────────────────────────────────────────────────────────
# Stage 1 output — raw scraped files land here before cleaning.
LOCAL_OUTPUT_DIR = "./output"

# Stage 2 output — cleaned .md files written here for human review.
# Mounted blob storage; reviewed files are ingested from here.
BLOB_OUTPUT_DIR = os.getenv(
    "BLOB_OUTPUT_DIR",
    "/media/ghost/external_storage/yt_transcripts",
)

# Stage 3 output — approved, ingested .md files stored here permanently.
CLEAN_OUTPUT_DIR = os.getenv(
    "CLEAN_OUTPUT_DIR",
    "/srv/dbdata/markdowns/yt_transcripts_structured",
)

# ── Scraping ───────────────────────────────────────────────────────────────
DEFAULT_LANG = "en"
SAVE_JSON = True          # save raw segment .json alongside .md in ./output

# Pacing. Enforced per *request*, not per video — one video costs a list() plus
# a fetch(), so the real request rate is 2-3x the video rate. Slow scraping is
# fine; getting the IP flagged is not. Raise to 3-5s for large playlists.
DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS", "2"))
# Every delay is multiplied by 1 ± this. A metronomic interval reads as a bot;
# 0.6 turns a 2s delay into a 0.8-3.2s spread.
REQUEST_JITTER = float(os.getenv("REQUEST_JITTER", "0.6"))

# Retry/backoff for transient failures (rate limits, IP blocks, network).
# Delays grow BASE * FACTOR^n: 5s, 15s, 45s, then capped.
TRANSCRIPT_MAX_ATTEMPTS = int(os.getenv("TRANSCRIPT_MAX_ATTEMPTS", "4"))
TRANSCRIPT_BACKOFF_BASE = float(os.getenv("TRANSCRIPT_BACKOFF_BASE", "5"))
TRANSCRIPT_BACKOFF_FACTOR = float(os.getenv("TRANSCRIPT_BACKOFF_FACTOR", "3"))
TRANSCRIPT_BACKOFF_CAP = float(os.getenv("TRANSCRIPT_BACKOFF_CAP", "300"))

# Circuit breaker: abort the run after this many consecutive videos lost to
# blocking. Pushing on while flagged deepens the block and burns the playlist.
# Nothing is lost — scrape dedupes on existing files, so a re-run resumes.
BLOCK_ABORT_THRESHOLD = int(os.getenv("BLOCK_ABORT_THRESHOLD", "3"))

# Optional routing around a persistent block. Off by default: a datacenter or
# VPN exit is blocked faster than a home IP, so only reach for this when the
# residential IP is genuinely flagged. Residential proxies work best.
#   YT_PROXY_HTTPS=http://user:pass@host:port
#   YT_WEBSHARE_USERNAME / YT_WEBSHARE_PASSWORD  (rotating residential)
YT_PROXY_HTTP = os.getenv("YT_PROXY_HTTP", "")
YT_PROXY_HTTPS = os.getenv("YT_PROXY_HTTPS", "")
YT_WEBSHARE_USERNAME = os.getenv("YT_WEBSHARE_USERNAME", "")
YT_WEBSHARE_PASSWORD = os.getenv("YT_WEBSHARE_PASSWORD", "")
# Netscape-format cookie file, for age-restricted/gated videos only. Ties the
# requests to your account — don't use it for bulk scraping.
YT_COOKIE_FILE = os.getenv("YT_COOKIE_FILE", "")

# ── Cleaning pipeline ──────────────────────────────────────────────────────
MIN_WORD_COUNT = 200        # reject transcripts shorter than this after cleaning
PARAGRAPH_MIN_WORDS = 60    # minimum words before a paragraph break is allowed
PARAGRAPH_GAP_SECONDS = 2.0  # silence gap that forces a paragraph break

# ── LLM enrichment (clean stage) ───────────────────────────────────────────
# Content metadata (summary, key concepts, domains, difficulty, sections) is
# derived from the cleaned transcript by a local Ollama model during `clean`.
# Set LLM_ENABLED=0 to skip enrichment and produce plain clean .md files.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
LLM_ENABLED = os.getenv("LLM_ENABLED", "1") == "1"
# Context window for the model. Must comfortably hold the capped transcript
# (~1.3 tokens/word) plus prompt and JSON output, or the input gets truncated.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "32768"))
# Cap prose sent to the model. ~25000 words ≈ 32500 tokens, leaving room in an
# 32768 context for the prompt and structured output. Longer transcripts are
# truncated (a map-reduce pass over chunks is a future improvement).
LLM_MAX_INPUT_WORDS = int(os.getenv("LLM_MAX_INPUT_WORDS", "25000"))
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "600"))

# ── Article rewrite (rewrite stage) ────────────────────────────────────────
# Turns cleaned caption prose into a readable article. Unlike enrichment this
# is a *lossless* transform: every chunk is rewritten, never summarised, and a
# coverage guard rejects output that dropped content (see scraper/rewriter.py).
REWRITE_ENABLED = os.getenv("REWRITE_ENABLED", "1") == "1"
# Deliberately independent of OLLAMA_MODEL: rewriting rewards a strong writer,
# enrichment only needs a competent JSON filler. Running a bigger model here
# and a faster one for enrichment is the intended split.
REWRITE_MODEL = os.getenv("REWRITE_MODEL", "gemma4:e4b")
# Words of source prose per LLM call. Chunks break on paragraph and chapter
# boundaries, so the real size varies around this. Small chunks keep the KV
# cache short (decode stays fast) and keep the model from drifting into
# summarising, which is what long one-shot rewrites degrade into.
REWRITE_CHUNK_WORDS = int(os.getenv("REWRITE_CHUNK_WORDS", "900"))
# Words of the previous chunk's *output* replayed as context, so prose flows
# across seams instead of restarting each chunk. This is context only — the
# model is told to continue from it, not to rewrite it — so nothing is
# duplicated in the article. Overlapping the *source* instead would rewrite the
# same sentences twice and put them in the output twice.
REWRITE_CONTEXT_WORDS = int(os.getenv("REWRITE_CONTEXT_WORDS", "100"))
# A chunk plus prompt and output fits in ~3k tokens; 8192 is comfortable.
REWRITE_NUM_CTX = int(os.getenv("REWRITE_NUM_CTX", "8192"))
REWRITE_TIMEOUT_SECONDS = int(os.getenv("REWRITE_TIMEOUT_SECONDS", "900"))
# ── Coverage guard ──
# Output/input word ratio must land in this band. Below the floor means the
# model summarised; above the ceiling means it padded or hallucinated.
REWRITE_MIN_RATIO = float(os.getenv("REWRITE_MIN_RATIO", "0.55"))
REWRITE_MAX_RATIO = float(os.getenv("REWRITE_MAX_RATIO", "1.60"))
# Fraction of the chunk's distinctive content words that must survive. This is
# the check that actually enforces "covers every point" — ratio alone passes a
# fluent paraphrase that quietly dropped a third of the material.
REWRITE_MIN_RETENTION = float(os.getenv("REWRITE_MIN_RETENTION", "0.72"))
# Retries at lower temperature before falling back to the verbatim chunk.
REWRITE_MAX_ATTEMPTS = int(os.getenv("REWRITE_MAX_ATTEMPTS", "2"))

# ── Database ───────────────────────────────────────────────────────────────
# Set DATABASE_URL in your .env file, e.g.:
#   DATABASE_URL=postgresql:///yt_transcripts        (Unix socket, peer auth)
#   DATABASE_URL=postgresql://user:pass@host:5432/yt_transcripts
DATABASE_URL = os.getenv("DATABASE_URL", "")
