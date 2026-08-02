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
PARAGRAPH_GAP_SECONDS = 2.0 # silence gap that forces a paragraph break

# ── LLM enrichment (clean stage) ───────────────────────────────────────────
# Content metadata (summary, key concepts, domains, difficulty, sections) is
# derived from the cleaned transcript by a local Ollama model during `clean`.
# Set LLM_ENABLED=0 to skip enrichment and produce plain clean .md files.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
LLM_ENABLED = os.getenv("LLM_ENABLED", "1") == "1"
# Context window for the model. Must comfortably hold the capped transcript
# (~1.3 tokens/word) plus prompt and JSON output, or the input gets truncated.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# Cap prose sent to the model. ~5000 words ≈ 6500 tokens, leaving room in an
# 8192 context for the prompt and structured output. Longer transcripts are
# truncated (a map-reduce pass over chunks is a future improvement).
LLM_MAX_INPUT_WORDS = int(os.getenv("LLM_MAX_INPUT_WORDS", "5000"))
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "600"))

# ── Database ───────────────────────────────────────────────────────────────
# Set DATABASE_URL in your .env file, e.g.:
#   DATABASE_URL=postgresql:///yt_transcripts        (Unix socket, peer auth)
#   DATABASE_URL=postgresql://user:pass@host:5432/yt_transcripts
DATABASE_URL = os.getenv("DATABASE_URL", "")
