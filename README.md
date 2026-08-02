# yt_transcript_scraper

A four-stage pipeline that turns YouTube transcripts into a structured personal
knowledge base — designed for ML pipelines, semantic search, and long-term
learning. Driven from a CLI or a local web UI.

```text
scrape  →  clean  →  enrich  →  ingest
```

No YouTube Data API key is required, and nothing leaves your machine: metadata
comes from `yt-dlp`, transcripts from `youtube-transcript-api`, and enrichment
from a local Ollama model.

---

## What it covers

| Stage | What it does | Where output lands |
|---|---|---|
| **scrape** | Resolves any video / playlist / channel URL (or a `.txt` file of URLs), downloads transcripts + metadata + chapters. | `./output/<Channel>/<Title>.md` and `.json`, `dataset.jsonl`, `index.csv`, PostgreSQL `status='raw'` |
| **manual** *(fallback)* | Accepts a hand-pasted transcript when YouTube blocks transcript delivery, producing byte-identical downstream records. | same as `scrape` |
| **clean** | Deterministic, no LLM: merges caption segments into paragraphs, injects YouTube chapters as headings, strips fillers, rejects short transcripts. | blob storage `.md` for review, `status='cleaned'`, `enrichment_status='pending'` |
| **enrich** | Background, resumable worker: a local LLM derives summary, key concepts, domains, difficulty, content kind, and sections, then rewrites the review `.md` as a knowledge document. | PostgreSQL content metadata + `sections` + `processing_runs`, `enrichment_status='done'` |
| **ingest** | Copies reviewed `.md` into production storage — the explicit human approval gate. | `/srv/dbdata/markdowns/yt_transcripts_structured/`, `status='ingested'` |

Blob storage sits between `clean` and `ingest` on purpose: you read, edit, or
delete files there before anything reaches production storage.

Planned but **not built**: `embed` — chunk clean transcripts into Qdrant/Chroma
for semantic search and a graph view.

---

## Features

**Scraping**

- Any YouTube URL form: single video, playlist, channel, or a batch `.txt` file.
- Transcript language fallback chain (manual in your language → any manual →
  auto-generated → any auto-generated).
- Typed transcript failures (`blocked`, `network`, `no_transcript`,
  `video_unavailable`, `auth_required`, `failed`) instead of silent `None`.
- Per-request pacing with jitter, exponential backoff on transient blocks, and a
  circuit breaker that aborts the run after repeated blocks rather than digging
  the hole deeper.
- Every failure is appended to `output/scrape_failures.jsonl` with a
  `transient` flag, so you know exactly what is worth re-running.
- Optional proxy (`YT_PROXY_HTTP(S)`, Webshare) and Netscape cookie file for
  gated videos.

**Manual fallback**

- Accepts four paste formats: `0:00 text`, a timestamp on its own line,
  SRT/WebVTT cues, or plain prose with no timestamps at all.
- Real timestamps are preserved (chapter headings depend on them); prose gets a
  synthetic cadence that keeps your paragraph breaks.
- Video ID is extracted strictly — only real YouTube hosts and URL shapes are
  accepted, so a look-alike URL can never be recorded as a different video.
- Metadata is fetched with `yt-dlp` (best effort) and anything you pass
  explicitly wins.

**Cleaning**

- YouTube chapter markers become `## Heading`s; heuristic heading detection is
  the fallback when the creator added none.
- Paragraph break threshold is derived per video from its own gap distribution
  (80th percentile), not hardcoded.
- Filler removal (`um`, `uh`, `hmm`, …); `like` and `you know` are deliberately
  left alone.
- Transcripts below `MIN_WORD_COUNT` prose words are rejected.

**Enrichment**

- Local Ollama model, schema-constrained JSON, Pydantic-validated.
- Resumable FIFO queue — interrupt any time; the next run resumes.
- Runs at lowest CPU/IO priority so it never slows your foreground work.
- `benchmark` command compares models on the *same* transcript (real generation
  tok/s plus side-by-side output quality).

**Storage and state**

- PostgreSQL tracks every video through the lifecycle, with immutable
  scrape-time fields and an append-only audit log — nothing is hard-deleted.
- Aggregate `dataset.jsonl` (one embed-ready record per line) and `index.csv`.

**Operability**

- Every stage is idempotent and re-runnable. Re-running repairs database state
  for files that already exist instead of duplicating work.
- File identity is validated before any recovery write: a blob or destination
  holding a different video's content is rejected, never silently repointed.
- Each stage reports success or failure. The CLI exits non-zero on failure, and
  the web UI marks the job errored.
- FastAPI web UI with live SSE logs, single-flight job locking, and a **Run All**
  that stops at the first failed stage instead of ingesting incomplete work.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10+ | Developed and tested on 3.12 |
| PostgreSQL | Optional for file output, **required** for the enrichment queue and status tracking |
| Ollama | Optional — only needed for the `enrich` stage |
| Disk | Raw staging in `./output`, review buffer in blob storage, production copies in `/srv/dbdata` |

Python dependencies are pinned in `requirements.txt`: `yt-dlp`,
`youtube-transcript-api`, `psycopg2-binary`, `python-dotenv`, `pydantic`,
`fastapi`, `uvicorn[standard]`.

---

## How to run

### Quick start (one command)

```bash
./setup.sh
```

Creates the virtualenv, installs dependencies, writes `.env` from the example,
applies the database schema, checks Ollama and the enrichment model, then
launches the web UI at **http://localhost:8000**. Idempotent — safe to re-run.

- Setup without launching the server: `./setup.sh --no-web`
- Start the UI later: `./start.sh` (custom port: `PORT=9000 ./start.sh`)

### Manual setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit DATABASE_URL and the storage paths
python main.py setup-db       # reads DATABASE_URL from .env automatically
ollama pull qwen3:4b          # optional — only for enrichment
```

### Web UI

Open http://localhost:8000, paste a YouTube URL, and click **Scrape**. Output
streams live into the log panel.

| Button | What it runs |
|---|---|
| **Scrape** | Fetches transcripts + metadata for the URL in the box |
| **Clean** | Merges segments into paragraphs, writes `.md` to blob storage, queues enrichment |
| **Enrich** | Drains the enrichment queue with the local LLM at lowest CPU priority |
| **Ingest** | Copies reviewed `.md` files into production storage |
| **Run All** | scrape → clean → enrich → ingest in one job (scrape only when a URL is in the box) |

Only one job runs at a time; the other buttons disable themselves while it is in
flight, and a second concurrent submission is rejected with HTTP 409. **Run All**
stops at the first failed stage — ingest never runs after a failed clean or
enrich. A failed job ends the log stream with an error, and the manual form keeps
whatever you pasted so nothing is lost.

Below the buttons, *Paste transcript manually* opens the blocked-transcript
fallback.

### CLI

```bash
python main.py scrape "https://www.youtube.com/@ChannelHandle"
python main.py manual "https://youtu.be/VIDEO_ID" --transcript-file t.txt
python main.py clean
./enrich.sh                  # or: python main.py enrich
python main.py ingest
```

| Command | Purpose | Key flags |
|---|---|---|
| `scrape <url_or_file>` | Download raw transcripts | `--output`, `--lang`, `--delay`, `--no-json`, `--no-jsonl`, `--no-csv` |
| `manual <url_or_id>` | Add a hand-pasted transcript | `--transcript(-file)`, `--description(-file)`, `--title`, `--channel`, `--published`, `--no-metadata` |
| `clean` | Clean raw transcripts into blob storage | `--raw-dir`, `--blob-dir` |
| `enrich` | Drain the enrichment queue | `--limit`, `--loop`, `--poll`, `--raw-dir`, `--blob-dir` |
| `benchmark` | Compare models on one transcript | `--models`, `--video`, `--words`, `--out` |
| `ingest` | Copy approved files to production | `--blob-dir`, `--clean-dir` |
| `setup-db` | Create the database and apply the schema | — |

Commands exit non-zero when the stage fails, so they compose safely in scripts
and cron.

### Pasting a transcript by hand

YouTube periodically rate-limits transcript endpoints per IP (`IpBlocked` /
HTTP 429), which stops `scrape` even though the text is still readable in a
browser. Copy it out of the "Show transcript" panel and paste it in — everything
downstream runs exactly as usual.

In the **web UI**, description and transcript are separate boxes on purpose: the
description is metadata and goes to frontmatter, the transcript is content and
becomes the timed segments.

From the **CLI**:

```bash
python main.py manual "https://www.youtube.com/watch?v=VIDEO_ID" \
  --transcript-file transcript.txt \
  --description-file description.txt
```

Only the URL and the transcript are required — a bare 11-character video ID
works in place of the URL, and `--transcript`/`--description` take text inline
instead of a file.

**What happens:** the video ID is extracted and validated, the pasted text is
parsed into `{text, start, duration}` segments identical in shape to what the
API returns, `yt-dlp` fills in title/channel/date/description/chapters
(best effort), your explicit values override anything fetched, and the raw
files + aggregate rows + a `status='raw'` database row are written. Then run
`clean` → `enrich` → `ingest` as normal; there is no manual-specific path after
this point.

Notes:

- **Metadata is not blocked** even when transcripts are, so the lookup usually
  succeeds and you rarely need to paste the description. Use `--no-metadata` for
  a fully offline run.
- `--published` must be a real `YYYY-MM-DD` date; it is validated before
  anything is written.
- Re-adding a video ID already present in `dataset.jsonl` fails rather than
  creating duplicate rows. Use a separate `--output` directory for an alternate
  version, or remove the existing aggregate rows and staged files first.

### Background enrichment

Enrichment is slow, so it is decoupled from `clean` and runs as its own
resumable worker. `clean` marks each video `enrichment_status='pending'`; the
worker drains that queue FIFO and marks each `done`.

```bash
./enrich.sh              # drain the queue, then exit (good for cron)
./enrich.sh --loop       # keep polling for new work
./enrich.sh --limit 5    # stop after 5 videos
```

```cron
0 2 * * *  /path/to/yt_transcript_scraper/enrich.sh >> /tmp/yt_enrich.log 2>&1
```

Set `LLM_ENABLED=0` in `.env` to skip enrichment entirely; the rest of the
pipeline is unaffected.

### Choosing a model

```bash
./benchmark.sh --models qwen3:4b,qwen2.5:7b-instruct,qwen2.5:3b-instruct
```

Runs the same transcript through each model via the real enrichment path and
prints a side-by-side table (generation tok/s, wall time, concept/section
counts) plus each model's actual output — so you compare quality, not just
speed. Read-only: nothing is written to the database or blob storage.

Model choice is VRAM-bound. On a 4 GB card a 3–4B model at Q4 fits fully; a 7–8B
model spills to CPU and is much slower. Benchmark before committing.

---

## Configuration

`.env` (copied from `.env.example`) drives everything. The most relevant knobs:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | PostgreSQL connection; `postgresql:///yt_transcripts` uses the Unix socket with peer auth |
| `BLOB_OUTPUT_DIR` | `/media/ghost/external_storage/yt_transcripts` | Review buffer for cleaned `.md` |
| `CLEAN_OUTPUT_DIR` | `/srv/dbdata/markdowns/yt_transcripts_structured` | Production storage |
| `DELAY_BETWEEN_REQUESTS` | `2` | Base pause per transcript **request** (not per video) |
| `REQUEST_JITTER` | `0.6` | Randomizes the delay so it is not metronomic |
| `TRANSCRIPT_MAX_ATTEMPTS` | `4` | Retries for transient block/network failures |
| `BLOCK_ABORT_THRESHOLD` | `3` | Consecutive blocked videos before the run aborts |
| `YT_PROXY_HTTP(S)`, `YT_WEBSHARE_*`, `YT_COOKIE_FILE` | unset | Optional routing around a persistent block, or cookies for gated videos |
| `LLM_ENABLED` | `1` | Set to `0` to skip enrichment |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | `http://localhost:11434` / `qwen3:4b` | Enrichment backend |
| `OLLAMA_NUM_CTX` | `8192` | Context window; must hold transcript + prompt + output |
| `LLM_MAX_INPUT_WORDS` | `5000` | Transcript words sent to the model; longer input is truncated |

The storage defaults match the original author's machine — override
`BLOB_OUTPUT_DIR` and `CLEAN_OUTPUT_DIR` for your own layout.

---

## Output structure

```text
./output/                                          # staging (temp)
  <Channel>/
    <Title>.md                                     # raw timestamped transcript
    <Title>.json                                   # raw segments (for clean stage)
  dataset.jsonl                                    # one record per video (ML-ready)
  index.csv                                        # quick manifest
  scrape_failures.jsonl                            # per-failure records with cause

<BLOB_OUTPUT_DIR>/                                 # human review buffer
  <Channel>/<Title>.md                             # cleaned, then enriched in place

/srv/dbdata/markdowns/yt_transcripts_structured/   # production
  <Channel>/<Title>.md                             # approved transcript
```

PostgreSQL tables:

- **`videos`** — one row per video: bibliographic metadata plus LLM-derived
  content metadata and lifecycle status.
- **`sections`** — per-video section breakdown, with timestamps when the video
  has YouTube chapters.
- **`processing_runs`** — one row per enrichment attempt, for auditing.
- **`video_audit_log`** — append-only history of every metadata change.

---

## Testing

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

The suite is offline and deterministic — no YouTube, Ollama, PostgreSQL, or
production storage is touched. It covers transcript parsing across all supported
paste formats, video ID and date validation, output-path confinement, duplicate
rejection, job locking and SSE status, Run All stop-on-failure, stage exit
codes, and the idempotent clean/ingest recovery paths.

---

## Limitations

- **YouTube blocks transcript delivery per IP.** Metadata keeps working, so a
  run looks healthy until the transcript itself fails. Backoff and the circuit
  breaker keep you from making it worse, but the only real fixes are waiting,
  a residential proxy, or the manual paste fallback.
- **Enrichment input is capped** at `LLM_MAX_INPUT_WORDS` (5000). Longer
  transcripts are truncated; map-reduce over chunks is not implemented yet.
- **Enrichment requires PostgreSQL** — the queue lives there. Without
  `DATABASE_URL` the file pipeline still works, but nothing is tracked or
  queued.
- **Ollama is not managed by this project.** If the server is not running,
  `enrich` fails fast and the rest of the pipeline continues without LLM
  metadata.
- **One job at a time in the web UI.** There is no queue: a second request while
  a job runs is rejected with HTTP 409.
- **The web UI has no authentication.** Bind it to localhost; do not expose it.
- **File paths are derived from channel + title.** Two different videos that
  sanitize to the same path are rejected rather than merged or renamed, so you
  resolve the collision by hand.
- **Duplicate manual entries are rejected, not versioned.** Redoing a video
  means removing its staged files and aggregate rows first.
- **Scraping is deliberately fail-soft per video.** One blocked or
  transcript-less video is logged and skipped rather than aborting the batch, so
  a "successful" playlist run can still be partial — check
  `scrape_failures.jsonl`.
- **No embeddings, semantic search, or graph view yet** — see the roadmap.
- **Enrichment quality is model- and VRAM-bound**, and small models sometimes
  return empty `domains`/`key_concepts`, which costs a fallback call.

---

## Documentation

- [Status & operations](docs/status.md) — current state, YouTube blocking + don't-get-banned strategy, error-fix strategy, future scope
- [Architecture](docs/architecture.md) — system design and decisions
- [Data schema](docs/data-schema.md) — PostgreSQL tables, indexes, file formats
- [Pipeline reference](docs/pipeline.md) — scrape / manual / clean / enrich / ingest stages in detail
- [Setup guide](docs/setup.md) — installation, env vars, example queries

---

## Roadmap

- [x] LLM content enrichment (summary, concepts, domains, difficulty, sections)
- [x] Web UI for scrape / manual / clean / enrich / ingest with live logs
- [x] Model benchmark command for empirical model selection
- [x] Harden transcript fetch — typed errors, backoff, circuit breaker
- [x] Manual transcript paste (CLI + web UI) for when YouTube blocks fetching
- [x] Optional proxy / cookie support for transcript fetching
- [x] Explicit stage outcomes + idempotent clean/ingest recovery
- [ ] Map-reduce enrichment for long transcripts (remove input cap)
- [ ] `embed` command — chunk clean transcripts → Qdrant/Chroma
- [ ] Semantic search CLI
- [ ] Graph view of related content
