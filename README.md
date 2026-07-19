# yt_transcript_scraper

A two-stage pipeline for scraping, cleaning, and structuring YouTube transcripts into a personal knowledge base — designed for ML pipelines, semantic search, and long-term learning.

## What it does

1. **Scrape** — given any YouTube URL (video, playlist, or channel), downloads transcripts and metadata without an API key. Saves raw files to a local staging directory and indexes metadata into PostgreSQL.
2. **Clean** — merges caption segments into readable paragraphs, strips vocal fillers, rejects low-quality transcripts, and writes clean `.md` files to a structured storage path. Updates pipeline status in PostgreSQL.

Future stage (not yet built): **embed** — chunk clean transcripts, generate vector embeddings, store in Qdrant/Chroma for semantic search and graph view.

## Tech stack

| Concern | Library |
|---|---|
| Transcript fetching | `youtube-transcript-api` |
| Metadata + playlist/channel discovery | `yt-dlp` |
| Metadata storage + search | PostgreSQL (`psycopg2`) |
| Future: semantic search | Qdrant / Chroma |

No YouTube Data API key required.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in your environment variables
cp .env.example .env

# Apply the database schema
psql $DATABASE_URL -f database/schema.sql

# Scrape a channel
python main.py scrape "https://www.youtube.com/@ChannelHandle"

# Clean and push to structured storage
python main.py clean
```

See [docs/setup.md](docs/setup.md) for full installation instructions.

## Output structure

```
./output/                                          # staging (temp)
  <Channel>/
    <Title>.md                                     # raw timestamped transcript
    <Title>.json                                   # raw segments (for clean stage)
  dataset.jsonl                                    # one record per video (ML-ready)
  index.csv                                        # quick manifest

/srv/dbdata/markdowns/yt_transcripts_structured/   # production
  <Channel>/
    <Title>.md                                     # clean paragraph-formatted transcript
```

PostgreSQL `videos` table tracks every video through the pipeline with full audit history of any metadata changes.

## Documentation

- [Architecture](docs/architecture.md) — system design and decisions
- [Data schema](docs/data-schema.md) — PostgreSQL tables, indexes, file formats
- [Pipeline reference](docs/pipeline.md) — scrape / clean stages in detail
- [Setup guide](docs/setup.md) — installation, env vars, example queries

## Roadmap

- [ ] `embed` command — chunk clean transcripts → Qdrant/Chroma
- [ ] Semantic search CLI
- [ ] Graph view of related content
- [ ] LLM-assisted topic tagging
