# Project Status & Operations

_Last updated: 2026-07-26_

A living snapshot of where `yt_transcript_scraper` stands: what works, what
needs improvement, how to deal with YouTube blocking, and where it's headed.
Read this first when returning to the project after a break.

---

## Where we are

The pipeline is four stages, end to end:

```
scrape  →  clean  →  enrich  →  ingest
```

- **scrape** — `yt-dlp` (metadata + playlist/channel discovery) + `youtube-transcript-api` (transcript text). Writes per-video `.md` + `.json` to `./output`, appends to `dataset.jsonl` and `index.csv`, records `status=raw` in PostgreSQL.
- **clean** — fast, deterministic, **no LLM**. Produces reviewable `.md` in blob storage, sets `status=cleaned`, and queues the video for enrichment (`enrichment_status=pending`).
- **enrich** — background, resumable, low-priority (`nice -n 19 ionice -c3`). A local Ollama model derives summary, key concepts, domains, difficulty, content kind, and sections; writes them to PostgreSQL + rewrites the blob `.md` as a knowledge doc. FIFO queue, marks each video `done` so it never reprocesses.
- **ingest** — copies approved `.md` from blob storage to `/srv/dbdata`, sets `status=ingested`.

A **FastAPI web UI** (`server.py`, port 8000) drives scrape/clean/ingest with live SSE logs. A **benchmark** command compares models on one transcript.

---

## What's working

- ✅ Scrape → clean → enrich → ingest pipeline, all four stages.
- ✅ PostgreSQL schema (videos, sections, processing_runs, audit log) via `python main.py setup-db`; Unix-socket peer auth (`postgresql:///yt_transcripts`).
- ✅ Aggregate outputs `dataset.jsonl` (one record/line, embed-ready) + `index.csv`.
- ✅ Web UI: paste a video/playlist URL, watch live logs, see the video table + status dots.
- ✅ Local LLM enrichment via Ollama, schema-constrained JSON, Pydantic-validated. `qwen3:4b` validated as a solid baseline; output is grounded (no hallucination) on tested inputs.
- ✅ Resumable FIFO enrichment worker — safe to interrupt; cron-friendly.
- ✅ `benchmark` command + `./benchmark.sh` — compares models on the same transcript (real gen tok/s + side-by-side output). Models installed: `qwen3:4b`, `qwen2.5:3b-instruct`, `qwen2.5:7b-instruct`.
- ✅ One-command setup (`./setup.sh`).

---

## What needs improvement

| Area | Issue | Priority |
|---|---|---|
| **GPU** | NVIDIA driver mismatch (kernel `580.159.03` vs NVML `580.173`) forces CPU-only inference (~4 tok/s). **Needs a reboot** (no sudo here). Until then, enrichment is impractically slow. | High |
| **Transcript error handling** | `scraper/transcript.py` catches every failure as a generic "Could not list transcripts" — it hides whether it was an IP block, a PoToken requirement, age restriction, or simply no transcript. No retry/backoff. | High |
| **Long transcripts** | Input is capped at `LLM_MAX_INPUT_WORDS` (5000); longer videos are truncated. A map-reduce pass over chunks is the real fix. | Medium |
| **Model choice** | Pending an empirical benchmark run *after the reboot* to pick between `qwen3:4b` / `qwen2.5:3b-instruct` / `qwen2.5:7b-instruct`. On 4 GB VRAM, 3–4B fits fully on GPU; 7B spills to CPU. | Medium |
| **key_concepts** | Ollama's grammar can't enforce a non-empty array, so a fallback second call recovers concepts when the model returns `[]`. Works, but adds a call. | Low |

---

## YouTube blocking — error catalog & how to avoid a ban

YouTube throttles/blocks the transcript endpoint per-IP. As of the last check the
home IP (`192.168.1.117`, no VPN/proxy) was **not** banned — a `list()` call
succeeded — so blocks seen were most likely **transient rate-limiting** or
**per-video** issues, not a standing ban.

`youtube-transcript-api` v1.2.4 raises a distinct exception per cause. Know which
one you hit before reacting:

| Exception | Meaning | What to do |
|---|---|---|
| `RequestBlocked` | Endpoint refused this request (usually rate/volume). | **Back off** — wait and retry with exponential delay. Slow the run down. |
| `IpBlocked` | IP flagged at a higher level. | Wait it out (often hours). If persistent, route through a **proxy**. |
| `PoTokenRequired` | Video needs a proof-of-origin token. | Fetch via `yt-dlp` (different path) or supply cookies. |
| `AgeRestricted` / `VideoUnplayable` | Gated content. | Supply authenticated **cookies**. |
| `TranscriptsDisabled` / `NoTranscriptFound` | Video genuinely has no transcript. | Skip — **not** a block. |
| `VideoUnavailable` / `InvalidVideoId` | Bad/removed video. | Skip. |

### Don't-get-banned strategy

1. **Go slow.** Keep `DELAY_BETWEEN_REQUESTS ≥ 1s` (config); raise it to 2–3s for big playlists. Bans come from bursts.
2. **Batch small.** Scrape a large playlist in chunks, not all at once. The pipeline dedupes (existing `.md` is skipped), so re-runs are cheap.
3. **Back off on block, don't hammer.** On `RequestBlocked`/`IpBlocked`, stop and wait — retrying immediately deepens the flag.
4. **Never run scrape from a datacenter/VPN IP** — those are blocked fastest. Home residential IP is best.
5. **Prefer a residential proxy over a VPN** if you must route around a persistent ban (`GenericProxyConfig` / `WebshareProxyConfig` are built into the library).
6. **Cookies for gated videos only** — export from a logged-in browser; don't use them for bulk scraping (ties activity to your account).

### Planned code fix (not yet implemented)

Harden `scraper/transcript.py`:
- Catch the specific exception classes above and **log the exact cause** instead of a generic message.
- **Retry with exponential backoff** on `RequestBlocked` / `IpBlocked` / `YouTubeRequestFailed` (e.g. 3 tries, 2⁴/2⁵/2⁶ s), then give up gracefully.
- Optional `.env`-driven **proxy** support (`GenericProxyConfig`/`WebshareProxyConfig`) and **cookie** support, off by default.

---

## Error-fix strategy (general)

- **Fail soft per video, never per run.** One bad video (no transcript, gated) should log and skip, not abort the batch. (Current behavior — keep it.)
- **Surface the real cause.** Replace broad `except Exception` with typed catches so logs say *why*.
- **Idempotent + resumable.** Enrichment already marks `done`; scrape dedupes on existing files. Keep every stage safe to re-run.
- **Distinguish transient vs permanent.** Retry transient (network, rate-limit); skip permanent (disabled/removed).

---

## Future scope

- [ ] Harden transcript fetch (typed errors, backoff, optional proxy/cookies) — _next up_.
- [ ] Map-reduce enrichment over chunks for long transcripts (removes the 5000-word cap).
- [ ] `embed` command — chunk clean transcripts → Qdrant/Chroma.
- [ ] Semantic search CLI over the knowledge base.
- [ ] Graph view of related content.
- [ ] Pick the production enrichment model after the post-reboot benchmark.

---

## Immediate next actions

1. **Reboot** to load the matching NVIDIA driver; verify with `nvidia-smi`.
2. `./benchmark.sh --models qwen3:4b,qwen2.5:7b-instruct,qwen2.5:3b-instruct` → pick a model → set `OLLAMA_MODEL` in `.env`.
3. Implement the transcript-fetch hardening above.
