import os

# ── Directories ────────────────────────────────────────────────────────────
# Stage 1 output — raw scraped files land here before cleaning.
LOCAL_OUTPUT_DIR = "./output"

# Stage 2 output — cleaned .md files written here for human review.
# Mounted blob storage; reviewed files are ingested from here.
BLOB_OUTPUT_DIR = os.getenv(
    "BLOB_OUTPUT_DIR",
    "/media/ghost/Blob Storage/yt_transcripts",
)

# Stage 3 output — approved, ingested .md files stored here permanently.
CLEAN_OUTPUT_DIR = os.getenv(
    "CLEAN_OUTPUT_DIR",
    "/srv/dbdata/markdowns/yt_transcripts_structured",
)

# ── Scraping ───────────────────────────────────────────────────────────────
DEFAULT_LANG = "en"
SAVE_JSON = True          # save raw segment .json alongside .md in ./output
DELAY_BETWEEN_REQUESTS = 1  # seconds between transcript API calls

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
