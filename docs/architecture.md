# System Architecture

## Problem

Informational YouTube videos are a high-signal knowledge source but are hard to work with at scale: no search, no structure, no way to link ideas across videos or feed them into ML pipelines.

## Goal

Build a personal knowledge base from YouTube transcripts that is:
- **Searchable** — full-text and metadata-filtered
- **Clean** — structured, paragraph-formatted prose, not raw caption segments
- **ML-ready** — compatible with embedding pipelines, LLM fine-tuning datasets, and graph views
- **Durable** — metadata is never deleted, all changes are audited

## Constraints

| Resource | Capacity | Usage |
|---|---|---|
| `/srv/dbdata` | ~150 GB total, ~30 GB for markdowns | PostgreSQL, MongoDB, Qdrant, Chroma, ingested `.md` files |
| `/media/ghost/external_storage` | ~300 GB | Blob storage — cleaned files sit here for human review before ingestion |
| `./output` | Temporary, inside app | Raw scraped files (staging only) |

## Four-Stage Pipeline

```
YouTube URL / Playlist / Channel
        │
        ▼
┌───────────────┐
│  scrape stage │  yt-dlp (metadata + chapters) + youtube-transcript-api
│               │  → ./output/<Channel>/<Title>.md    (raw, timestamped)
│               │  → ./output/<Channel>/<Title>.json  (raw segments + chapters)
│               │  → ./output/dataset.jsonl            (aggregate; carries chapters)
│               │  → ./output/index.csv                (quick inspection manifest)
│               │  → PostgreSQL: status = 'raw'
└───────────────┘
        │
        ▼
┌───────────────┐
│  clean stage  │  scraper/cleaner.py
│               │  → inject YouTube chapter headings (## Heading) if available
│               │  → heuristic heading detection (fallback when no chapters)
│               │  → adaptive paragraph gap (derived from video's own pacing)
│               │  → strip vocal fillers; reject < 200 prose words
│               │  → /media/ghost/external_storage/yt_transcripts/<Channel>/<Title>.md
│               │  → PostgreSQL: status = 'cleaned', enrichment = 'pending'
└───────────────┘
        │
        ▼
┌───────────────┐
│ enrich stage  │  local Ollama model (qwen3:4b by default)
│               │  → summary, concepts, domains, difficulty, content kind
│               │  → PostgreSQL sections + processing run audit
│               │  → rewrite blob .md as a structured knowledge document
│               │  → enrichment_status = 'done'
└───────────────┘
        │  ← human reviews / edits .md files in blob storage here
        ▼
┌───────────────┐
│ ingest stage  │  shutil.copy2 (preserves folder structure)
│               │  → /srv/dbdata/markdowns/yt_transcripts_structured/<Channel>/<Title>.md
│               │  → PostgreSQL: status = 'ingested', clean_path set
└───────────────┘
        │
        ▼ (future)
┌───────────────┐
│  embed stage  │  Qdrant / Chroma
│               │  → chunk ingested .md → vector embeddings
│               │  → PostgreSQL: status = 'embedded'
└───────────────┘
```

## Design Decisions

### No API key required
Both `yt-dlp` and `youtube-transcript-api` work without a YouTube Data API key, avoiding quota limits when scraping channels or large playlists.

### Raw files kept in `./output`
Raw files act as a local cache. The `clean` command reads from `dataset.jsonl` rather than re-fetching from YouTube — faster, offline-capable, and idempotent.

### Blob storage as review buffer
Cleaned files go to `/media/ghost/external_storage/` first, not directly to `/srv/dbdata`. This allows human inspection and manual editing before any file touches the production database store. `ingest` is the explicit approval gate — delete unwanted files from blob before running it.

### Adaptive cleaning, not hardcoded rules
Different videos have wildly different pacing and structure. The cleaner derives paragraph break thresholds from each video's own segment gap distribution (80th percentile), and uses YouTube chapter markers as headings when available — falling back to heuristic detection only when the creator didn't add chapters.

### PostgreSQL for metadata
Metadata lives in the same PostgreSQL instance as other project databases in `/srv/dbdata`. This enables JOINs with future tables (embeddings index, topic graph, learning progress) and full-text search via `tsvector`.

### Immutable fields + audit log
Fields set at scrape time (video ID, URL, channel, publish date, raw path) are never updated. Any change to an updatable field (title, status, tags, topic, notes) is recorded in `video_audit_log`. Nothing is hard-deleted.

### Clean `.md` format
The final `.md` uses YAML frontmatter (for machine parsing) + paragraph prose with `## Heading` sections (for human reading and LLM ingestion). Timestamps live only in the raw `.json` — not in the clean version.

### Status lifecycle
```
raw → cleaned → ingested → embedded (future)
```
Each stage is tracked in the `videos.status` column. `ingest` is the human-controlled gate between `cleaned` and `ingested`.

### Future: embeddings + graph
The schema is designed for a future `embed` command that chunks ingested `.md` files, generates embeddings, and stores them in Qdrant or Chroma — without changing the core tables.
