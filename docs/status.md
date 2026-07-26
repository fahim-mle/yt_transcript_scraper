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
| **IP currently blocked** | As of 2026-07-26 the home IP gets `IpBlocked` on the transcript **fetch** endpoint (`list()` still succeeds). Transient, but scraping is dead until it clears. Wait it out — hours, not minutes. | High |
| **Ollama systemd service** | The unit is misconfigured and **stopped**; Ollama only runs if you start `ollama serve` manually. See "Ollama service" below. Won't survive a reboot. | High |
| **Long transcripts** | Input is capped at `LLM_MAX_INPUT_WORDS` (5000); longer videos are truncated. A map-reduce pass over chunks is the real fix. | Medium |
| **Storage split** | 18 GB of models in `/home` (78% full) while the 373 GB partition holds an older 8.3 GB store. Consolidate. | Low |
| **key_concepts** | Ollama's grammar can't enforce a non-empty array, so a fallback second call recovers concepts when the model returns `[]`. Works, but adds a call. Both small models hit it in the benchmark. | Low |

### Resolved

- ~~**GPU driver mismatch**~~ — fixed by the reboot. Driver `580.173.02` loads, CUDA detected, 3.2 GiB usable.
- ~~**Transcript error handling**~~ — implemented, see below.
- ~~**Model choice**~~ — benchmarked, `qwen3:4b` wins. See "Model benchmark".

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

### Probed alternatives (2026-07-26, while IP-blocked)

Don't re-derive these — all were tested against a live block:

| Path | Result |
|---|---|
| Watch page HTML / player API / yt-dlp metadata | **200 OK** — never blocked |
| `youtube-transcript-api` → `timedtext` | `IpBlocked` |
| `yt-dlp --write-auto-subs` → `timedtext` | `HTTP 429` |
| `yt-dlp --js-runtimes node` (JS challenges solved) | still `HTTP 429` |
| `youtubei/v1/get_transcript` via `requests` | `400 FAILED_PRECONDITION` |

The block targets **transcript content delivery only** — metadata is unaffected,
which is why a run looks healthy until the transcript itself fails.

`get_transcript` was tried six ways (hand-built protobuf params, the page's own
`getTranscriptEndpoint.params`, full and minimal `INNERTUBE_CONTEXT`, with and
without `key=`/`prettyPrint=false`, plus `X-YouTube-Client-*` and
`X-Goog-Visitor-Id` headers). All returned the same gateway-level 400, which is
*not* a rate limit — the endpoint requires browser session state (likely
SAPISIDHASH auth or a JS-minted token) that a plain HTTP client can't supply.
**BeautifulSoup is a dead end**: the watch page has no transcript text, only
`captionTracks[].baseUrl` pointing back at the blocked `timedtext`.

Untried levers, cheapest first: cookies from a logged-in browser (may satisfy
the `get_transcript` precondition — `YT_COOKIE_FILE` is already wired);
`curl_cffi` for TLS impersonation in yt-dlp; Playwright driving the real
transcript panel.

### Implemented (2026-07-26)

`scraper/transcript.py` now raises **typed errors** instead of returning `None`.
Each carries a stable `reason` slug and a `transient` flag:

| Exception | `reason` | Transient? |
|---|---|---|
| `TranscriptBlocked` | `blocked` | yes — retry later |
| `TranscriptNetworkError` | `network` | yes |
| `TranscriptUnavailable` | `no_transcript` | no |
| `TranscriptGone` | `video_unavailable` | no |
| `TranscriptAuthRequired` | `auth_required` | no — needs cookies |
| `TranscriptFailed` | `failed` | no |

- **Pacing is per request, not per video** (`transcript._wait_turn()`), since one
  video costs a `list()` plus a `fetch()`. Default `DELAY_BETWEEN_REQUESTS` is
  now **2 s**, multiplied by `1 ± REQUEST_JITTER` (0.6) so the interval isn't
  metronomic. `--delay` still overrides via `transcript.set_delay()`.
- **Exponential backoff** on `RequestBlocked` / `IpBlocked` /
  `YouTubeRequestFailed` / connection errors: 4 attempts at 5 s, 15 s, 45 s
  (`TRANSCRIPT_BACKOFF_*`, capped at 300 s). Permanent failures are never
  retried.
- **Circuit breaker** — `BLOCK_ABORT_THRESHOLD` (3) consecutive blocked videos
  aborts the run. Only blocks trip it; a stretch of transcript-less videos does
  not. Nothing is lost: scrape dedupes on existing `.md`, so a re-run resumes.
- **Failure report** — every failure is appended to
  `<output>/scrape_failures.jsonl` with `video_id`, `reason`, `detail` and
  `transient`, plus an end-of-run summary by cause. Filter on `transient: true`
  to see what's worth re-running.
- **Optional proxy/cookies**, off by default: `YT_PROXY_HTTP(S)`,
  `YT_WEBSHARE_USERNAME`/`_PASSWORD`, `YT_COOKIE_FILE` (Netscape format).

---

## Error-fix strategy (general)

- **Fail soft per video, never per run.** One bad video (no transcript, gated) should log and skip, not abort the batch. (Current behavior — keep it.)
- **Surface the real cause.** Replace broad `except Exception` with typed catches so logs say *why*.
- **Idempotent + resumable.** Enrichment already marks `done`; scrape dedupes on existing files. Keep every stage safe to re-run.
- **Distinguish transient vs permanent.** Retry transient (network, rate-limit); skip permanent (disabled/removed).

---

## Future scope

- [x] Harden transcript fetch (typed errors, backoff, optional proxy/cookies).
- [x] Pick the production enrichment model after the post-reboot benchmark.
- [ ] Fix the Ollama systemd unit so it survives a reboot.
- [ ] Map-reduce enrichment over chunks for long transcripts (removes the 5000-word cap).
- [ ] `embed` command — chunk clean transcripts → Qdrant/Chroma.
- [ ] Semantic search CLI over the knowledge base.
- [ ] Graph view of related content.

---

## Model benchmark (2026-07-26)

3401 words of "But what is a neural network?" fed to each model, GTX 1050 Ti,
3.2 GiB usable VRAM:

| model | wall | gen tok/s | GPU layers | concepts | sections | domains |
|---|---|---|---|---|---|---|
| **qwen3:4b** | 199.5 s | 5.5 | 30/37 | 8 | **12** | 3 |
| qwen2.5:3b-instruct | **137.4 s** | **7.9** | **37/37** | 8 | 8 | **`[]`** |
| qwen2.5:7b-instruct | 361.2 s | 2.2 | 12/29 | 6 | 11 | 1 |

**`qwen3:4b` is the pick** — best coverage, and already the `config.py` default,
so no `.env` change is needed. The 3B is fastest (the only one to fully offload)
but returned empty `domains` and dropped the tail sections. The 7B spilled to
CPU as expected and was worst on both axes.

Untested idea: `qwen3:4b` spills 7 layers with `OLLAMA_NUM_CTX=8192`. A smaller
context or quantized KV cache may fit all 37 and close the speed gap.

---

## Ollama service

The systemd unit is **stopped and misconfigured**. Two separate faults:

1. `/var/lib/docker` is `0710 root:root` — Docker re-asserts this on daemon
   start — so `User=ollama` couldn't traverse it and the service crash-looped.
   Worked around with an ACL: `setfacl -m u:ollama:x /var/lib/docker` (survives
   Docker's re-`chmod`, since mode `0710` leaves the mask at `--x`).
2. `OLLAMA_MODELS` in `/etc/systemd/system/ollama.service.d/override.conf`
   points at `/var/lib/docker/ollama-models`, but that store's `blobs/` and
   `manifests/` are one level deeper in `.../ollama-models/models`. That store
   holds `llama3.2`/`nomic-embed-text`/`qwen3.5` — **not** the qwen benchmark
   models, which live in `/home/ghost/.ollama/models` (18 GB).

Current workaround: run `ollama serve` as your own user (defaults to
`~/.ollama/models`, binds `127.0.0.1:11434`). To fix properly, either repoint
the override one level deeper and re-pull the qwen models (~9 GB), or point it
at the home store and grant the `ollama` user read access.

---

## Immediate next actions

1. **Wait out the IP block**, then re-run the scrape — it resumes automatically.
2. Fix the Ollama systemd unit (above) so enrichment survives a reboot.
3. Drain the 13 pending enrichments (~45 min at ~200 s/video) and investigate
   the 1 `failed` row.
