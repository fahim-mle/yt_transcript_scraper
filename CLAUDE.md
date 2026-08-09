# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
./setup.sh                # venv + deps + .env + schema + Ollama check, then launch UI
./setup.sh --no-web       # same, without starting the server
./start.sh                # UI only (PORT=9000 ./start.sh to change port)

.venv/bin/python -m unittest discover -s tests -p 'test_*.py'          # full suite (40 tests, ~0.5s)
.venv/bin/python -m unittest tests.test_pipeline_stages.CleanStageOutcomeTests            # one class
.venv/bin/python -m unittest tests.test_pipeline_stages.CleanStageOutcomeTests.test_name  # one test

.venv/bin/python main.py setup-db
.venv/bin/python main.py scrape <url|playlist|channel|urls.txt>
.venv/bin/python main.py manual <url|video_id> --transcript-file t.txt
.venv/bin/python main.py clean
.venv/bin/python main.py rewrite [--loop|--limit N]   # slow: hours for a long video
./enrich.sh [--loop|--limit N]          # = main.py enrich under nice -n 19 / ionice -c3
.venv/bin/python main.py ingest
./benchmark.sh --models a,b,c           # read-only model comparison on one transcript
```

There is no linter or formatter configured. Tests are offline and deterministic — they touch no YouTube, Ollama, PostgreSQL, or production storage, and must stay that way (`_FakeDb` + `tempfile` + `unittest.mock.patch` are the existing conventions).

Full user-facing docs live in `README.md` and `docs/` (`architecture.md`, `data-schema.md`, `pipeline.md`, `setup.md`, `status.md`). Prefer updating those over duplicating them here.

## Architecture

Five stages — `scrape → clean → rewrite → enrich → ingest` — each idempotent, re-runnable, and exposed identically through `main.py` (CLI) and `server.py` (FastAPI UI). The UI calls the same `cmd_*` functions rather than reimplementing anything.

### Two artifacts per video, not one

The blob and production directories hold a pair of files per video:

- `<Title>.md` — the **article**: LLM-rewritten prose. What you read.
- `<Title>.transcript.md` — the **verbatim companion**: unmodified cleaned caption text. The citation and embedding anchor.

The article is generated text, so it must never become the only record. `clean` writes the companion, `rewrite` writes the article, `enrich` layers metadata on top of the article. Both files carry the same `video_id`, so **only the article may own `videos.clean_path`** — `_is_transcript_companion` guards that in `cmd_ingest`, otherwise the column flip-flops with `os.walk` ordering.

### `dataset.jsonl` is the content source of truth

`./output/dataset.jsonl` — not the raw `.md` — is what every downstream stage reads. `cmd_clean` parses it directly; `cmd_enrich` builds a `video_id → record` index from it (`_load_records_index`). Neither stage re-fetches from YouTube, so `clean` and `enrich` are fully offline. The raw `./output/<Channel>/<Title>.md` is human-facing only; nothing consumes it.

### `cleaner.clean()` runs twice and must stay deterministic

`cmd_clean` calls it to write the blob `.md`. `_enrich_one` calls it **again** on the same segments to rebuild the prose it sends to the LLM and then rewrites that same blob file as a knowledge doc. Same input must yield byte-identical output, or enrichment silently replaces a reviewed file with different prose. Anything stateful, random, or time-dependent in `scraper/cleaner.py` breaks this invariant.

The blob file is therefore written twice: `formatter.to_clean_markdown` at clean time, overwritten by `formatter.to_knowledge_doc` at enrich time.

### `created_at` is stamped once, at the source

`published` is a fact about the video; `created_at` is when it entered the knowledge base, and it's the clock staleness is measured on. `formatter.to_jsonl_record` stamps it when a video first reaches `dataset.jsonl` — every later stage reads it back through `_meta_from_record` rather than re-stamping. **Never regenerate it at render time**: `clean`, `rewrite` and `enrich` each rewrite the document, so a `now()` there would churn the frontmatter on every run and break their idempotency. Records staged before the field existed omit it; `_frontmatter_lines` leaves a missing stamp missing rather than inventing one.

All five document builders route through `_frontmatter_lines`, so a new frontmatter field is added in exactly one place.

### Frontmatter `url:` is load-bearing

Before overwriting any existing blob or ingest destination, `_extract_video_id` parses the video ID out of the file's YAML `url:` field and rejects the write on mismatch — a path collision is never silently repointed. Changing the frontmatter shape in `scraper/formatter.py` breaks the clean and ingest recovery paths.

### Two independent status columns

`videos.status` (`raw → cleaned → ingested`) and `videos.enrichment_status` (`pending`/`failed`/`done`) advance separately. A video the LLM fails on stays queued but is still ingestable — enrichment failure is queue state, not a stage failure, which is why **Run All** does not abort on it.

### Database is optional in some paths, required in others

The file pipeline (`scrape`/`manual`/`clean`/`ingest`) degrades gracefully without `DATABASE_URL` — `_db_available()` only checks the env var. `enrich` hard-requires PostgreSQL because the FIFO queue lives in `list_pending_enrichment`. Note that without a DB, `clean` skips existing blobs entirely rather than repairing state.

### Cross-process aggregate lock

`aggregate_lock()` in `main.py` is an `flock` on `output/.dataset.lock`, held across processes (CLI beside server; manual add beside a long enrich). `dataset.jsonl` records reach ~800 KB, well past `PIPE_BUF`, so appends are not atomic against readers. `flock` is per open-file-description — **never nest these calls**, or a shared holder waiting to upgrade deadlocks against itself.

### Web job model (`server.py`)

Two admission classes: `HEAVY` (all pipeline stages, single-flight — a second request gets HTTP 409) and `LIGHT` (manual transcript add). That split is why pasting a transcript works during a multi-hour **Run All**.

Run All order is `scrape → clean → rewrite → enrich → ingest`, with `rewrite` gated on `REWRITE_ENABLED` and `enrich` on `LLM_ENABLED`. **Rewrite must stay before enrich** — enrich reads the article body back out of the blob. When adding a stage here, patch its `cmd_*` in every run-all test: an unpatched LLM stage makes the offline suite call a real model and hang.

Log streaming is replayable: every line carries a monotonic sequence number, `_Job.subscribe(after)` atomically snapshots the backlog and registers for live lines, and finished jobs are retained (`_MAX_JOBS = 20`) so a page reload after completion replays rather than reporting a phantom failure. Reattachment is driven by the `Last-Event-ID` header.

Job output is captured via a `logging.Handler` plus a thread-ident → job-id map (`_thread_job_map`). **Stages must emit progress through `logging`, not `print`**, or it never reaches the UI. Enrichment jobs call `_low_priority()` (`os.nice(19)`, per-thread on Linux) so the server stays responsive.

### Article rewrite (`scraper/rewriter.py`)

The slowest stage by far — it generates roughly one token per transcript token, so a long video is hours, not minutes. Measured on the target box: ~5.9 tok/s under load with `gemma4:e4b`.

Two invariants hold the design together:

- **Chunked, never one-shot.** Chunks break on paragraph and chapter boundaries so no sentence is split, and each carries the tail of the previous chunk's *output* as context. A heading forces a chunk boundary, so a heavily chaptered video yields many small chunks regardless of `REWRITE_CHUNK_WORDS`. A multi-chunk section emits its `##` heading only once.
- **Coverage is enforced, not hoped for.** `_coverage_failure` rejects a rewrite whose word ratio leaves the band or whose distinctive content words did not survive (`_STOPWORDS` keeps frequent words out of the denominator so rewording isn't punished). Failure retries at a lower temperature, then **falls back to the verbatim chunk**. The article is therefore never quietly shorter than the transcript — a bad chunk degrades to unpolished prose, never to missing prose. Do not "simplify" this fallback into an exception.

`rewrite` sets `enrichment_status = 'pending'` on success, because new prose invalidates metadata derived from the old text.

### LLM enrichment (`scraper/llm_processor.py`)

Metadata extraction only — the LLM does **not** rewrite transcript prose; cleaning is pure regex/heuristics in `scraper/cleaner.py`. Output is constrained by Ollama's `format` (JSON schema from Pydantic) and re-validated with Pydantic, so callers always get a valid `Enrichment` or `None`.

Things to know before changing it:

- Input is truncated at `LLM_MAX_INPUT_WORDS` (default 5000) with no map-reduce, so long transcripts are summarized from their opening only. Chapter titles for the *whole* video are still injected into the prompt, so the model can emit headings for content it never read.
- `OLLAMA_NUM_CTX` (default 8192) must comfortably hold the capped transcript (~1.3 tokens/word) plus prompt and output, or Ollama truncates further.
- The `_SYSTEM` prompt starts with `/no_think` and the payload sets `"think": False` — both are qwen3-specific workarounds and are inert or literal text for other model families.
- Ollama's grammar does not enforce array `minItems`, so `key_concepts` can come back empty; `_extract_concepts` is the recovery call.
- `_sections_with_timestamps` only attaches real timestamps when chapter count exactly equals section count; otherwise they stay NULL.

Model choice is VRAM-bound — the target machine has 4 GB. Use `./benchmark.sh` to compare empirically rather than assuming.

## Configuration

Everything is env-driven through `config.py` (loaded from `.env` via `python-dotenv` at the top of `main.py`). Add new knobs there with an `os.getenv` default rather than reading the environment at the point of use, and mirror them in `.env.example`.
