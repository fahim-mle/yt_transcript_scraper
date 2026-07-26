# yt_transcript_scraper

A three-stage pipeline for scraping, cleaning, and structuring YouTube transcripts into a personal knowledge base — designed for ML pipelines, semantic search, and long-term learning. Comes with a CLI and a local web UI.

## What it does

1. **Scrape** — given any YouTube URL (video, playlist, or channel), downloads transcripts and metadata without an API key. Saves raw files to a local staging directory and indexes metadata into PostgreSQL.
2. **Clean** — merges caption segments into readable paragraphs, strips vocal fillers, rejects low-quality transcripts, and writes structured `.md` files to blob storage for review. Fast and deterministic; queues each video for enrichment. No LLM in this stage.
3. **Enrich** *(background worker)* — drains the queue of cleaned videos, using a local LLM (via Ollama) to derive a summary, key concepts, domains, difficulty, content kind, and a section-level breakdown. Resumable and idempotent: each video is marked done when finished, so it's safe to interrupt and re-run. Designed to run at low priority (or via cron) so it never slows your foreground work.
4. **Ingest** — copies reviewed `.md` files into structured production storage (`/srv/dbdata`) and marks them ingested.

Future stage (not yet built): **embed** — chunk clean transcripts, generate vector embeddings, store in Qdrant/Chroma for semantic search and graph view.

## Tech stack

| Concern | Library |
|---|---|
| Transcript fetching | `youtube-transcript-api` |
| Metadata + playlist/channel discovery | `yt-dlp` |
| Metadata + content storage + search | PostgreSQL (`psycopg2`) |
| Content enrichment (local LLM) | Ollama (`qwen3:4b` by default) |
| Web UI | FastAPI + `uvicorn` |
| Future: semantic search | Qdrant / Chroma |

No YouTube Data API key required. Enrichment is optional — if Ollama isn't running, `clean` still produces structured `.md`, just without the LLM metadata.

## Quick start (one command)

```bash
./setup.sh
```

This creates the virtualenv, installs dependencies, applies the database schema, ensures the Ollama enrichment model is pulled, and launches the web UI at **http://localhost:8000**. It's idempotent — safe to re-run.

- Setup without launching the server: `./setup.sh --no-web`
- Just start the UI later (after setup): `./start.sh`
- Custom port: `PORT=9000 ./start.sh`

### Using the web UI

Open http://localhost:8000, paste a YouTube URL (single video, playlist, or channel), and click **Scrape**. Pipeline output streams live into the log panel. Use the **Clean** and **Ingest** buttons to run the later stages.

### Manual / CLI setup

If you prefer to drive it yourself:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure environment (edit DATABASE_URL to a real connection string)
cp .env.example .env

# Apply the database schema (reads DATABASE_URL from .env automatically)
python main.py setup-db

# Optional: pull the enrichment model
ollama pull qwen3:4b

# Run the pipeline
python main.py scrape "https://www.youtube.com/@ChannelHandle"
python main.py clean       # fast, no LLM — queues videos for enrichment
./enrich.sh                # background LLM enrichment (low priority, resumable)
python main.py ingest

# Or launch the web UI
uvicorn server:app --reload --port 8000
```

### Background enrichment

LLM enrichment is slow (especially on CPU), so it's decoupled from `clean` and runs as its own resumable worker. `clean` just marks each video `enrichment_status = 'pending'`; the worker drains that queue FIFO and marks each `done`.

```bash
./enrich.sh              # enrich all pending, then exit (good for cron)
./enrich.sh --loop       # stay running, pick up new work as it's cleaned
./enrich.sh --limit 5    # stop after 5 videos
```

`enrich.sh` runs the worker at the lowest CPU/IO priority (`nice -n 19 ionice -c3`), so scraping a big playlist and enriching it won't slow down whatever else you're doing. Because every video is marked `done` on completion, the worker never reprocesses the same file — interrupt it any time (Ctrl+C, reboot) and the next run resumes where it left off.

Run it overnight via cron:

```cron
# enrich nightly at 2am
0 2 * * *  /path/to/yt_transcript_scraper/enrich.sh >> /tmp/yt_enrich.log 2>&1
```

To skip enrichment entirely (e.g. until you've picked a model), set `LLM_ENABLED=0` in `.env` — the rest of the pipeline is unaffected.

### Database connection

The default `.env` uses a placeholder. On a local PostgreSQL with peer auth (the Ubuntu default), the simplest working value is:

```env
DATABASE_URL=postgresql:///yt_transcripts
```

The three slashes mean "connect via the Unix socket as your OS user" — no password needed. `setup-db` reads this from `.env` automatically (the shell does **not**, which is why `psql $DATABASE_URL` alone fails).

See [docs/setup.md](docs/setup.md) for full installation instructions.

## Output structure

```text
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

PostgreSQL tracks every video through the pipeline:

- **`videos`** — one row per video: bibliographic metadata (title, channel, date, URL, word count, status) plus LLM-derived content metadata (summary, key concepts, domains, difficulty, content kind).
- **`sections`** — per-video section breakdown (heading + summary, with timestamps when the video has YouTube chapters).
- **`processing_runs`** — one row per enrichment attempt, for auditing the LLM step.
- **`video_audit_log`** — append-only history of every metadata change. Nothing is ever hard-deleted.

## Documentation

- [Architecture](docs/architecture.md) — system design and decisions
- [Data schema](docs/data-schema.md) — PostgreSQL tables, indexes, file formats
- [Pipeline reference](docs/pipeline.md) — scrape / clean stages in detail
- [Setup guide](docs/setup.md) — installation, env vars, example queries

## Roadmap

- [x] LLM content enrichment (summary, concepts, domains, difficulty, sections)
- [x] Web UI for scrape / clean / ingest with live logs
- [ ] `embed` command — chunk clean transcripts → Qdrant/Chroma
- [ ] Semantic search CLI
- [ ] Graph view of related content
