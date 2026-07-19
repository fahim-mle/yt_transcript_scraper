import os

# ── Directories ────────────────────────────────────────────────────────────
# Stage 1 output — raw scraped files land here before cleaning.
LOCAL_OUTPUT_DIR = "./output"

# Stage 2 output — cleaned .md files written here for human review.
# Mounted blob storage; reviewed files are ingested from here.
BLOB_OUTPUT_DIR = os.getenv(
    "BLOB_OUTPUT_DIR",
    "/media/ghost/Blog Storage/yt_transcripts",
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

# ── Database ───────────────────────────────────────────────────────────────
# Set DATABASE_URL in your .env file, e.g.:
#   DATABASE_URL=postgresql://user:password@localhost:5432/yt_transcripts
DATABASE_URL = os.getenv("DATABASE_URL", "")
