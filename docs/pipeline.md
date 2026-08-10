# Pipeline Reference

## Stage 1: scrape

```bash
python main.py scrape <URL_OR_FILE> [options]
```

Resolves any YouTube URL (single video, playlist, channel) or a `.txt` file of URLs into individual videos, downloads transcripts and metadata, and stages everything locally.

**What gets saved:**

| Path | Contents |
|---|---|
| `./output/<Channel>/<Title>.md` | Raw transcript with `[MM:SS]` timestamps |
| `./output/<Channel>/<Title>.json` | Raw segments + chapter list |
| `./output/dataset.jsonl` | One full record per video (used by `clean`) |
| `./output/index.csv` | Quick-view manifest |
| PostgreSQL `videos` | Metadata row with `status = 'raw'` |

Already-existing files are skipped (idempotent).

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--output DIR` | `./output` | Raw staging directory |
| `--lang CODE` | `en` | Preferred transcript language |
| `--delay SECS` | `2` | Base pause between transcript API requests (jittered; retries back off separately) |
| `--no-json` | off | Skip per-video `.json` segment files |
| `--no-jsonl` | off | Skip `dataset.jsonl` |
| `--no-csv` | off | Skip `index.csv` |

**Transcript language fallback order:**
1. Manual transcript in `--lang`
2. Any manual transcript (any language)
3. Auto-generated transcript in `--lang`
4. Any auto-generated transcript
5. Skip video with a warning

---

## Manual fallback: paste a transcript

```bash
python main.py manual <YOUTUBE_URL_OR_VIDEO_ID> --transcript-file transcript.txt [options]
```

Use this path when YouTube blocks transcript delivery but the text remains available in the browser. It accepts timestamp-prefixed lines, timestamps followed by text, SRT/WebVTT cues, or plain prose. The parser emits the same `{text, start, duration}` segments as the transcript API; metadata lookup remains best-effort, and explicit title/channel/date/description values win.

The command writes the same raw Markdown, segment JSON, `dataset.jsonl`, `index.csv`, and PostgreSQL `status = 'raw'` record as `scrape`. A video ID already present in `dataset.jsonl` is rejected to prevent duplicate aggregate records. Afterward, use the normal `clean → enrich → ingest` stages.

---

## Stage 2: clean

```bash
python main.py clean [options]
```

Reads `dataset.jsonl`, applies the cleaning pipeline to each video's raw segments, and writes reviewed `.md` files to blob storage for human inspection. Does **not** touch `/srv/dbdata`.

**What gets saved:**

| Path | Contents |
|---|---|
| `<blob>/<Channel>/<Title>.md` | Clean, paragraph-formatted transcript |
| `<blob>/<Channel>/<Title>.transcript.md` | Verbatim companion — same text, kept as ground truth once `rewrite` replaces the file above |
| PostgreSQL `videos` | `status = 'cleaned'`, `word_count` updated, queued for rewrite and enrichment |

Already-existing files in blob storage are skipped, but a missing verbatim
companion is written even then, so videos cleaned before that file existed gain
one on a re-run.

**Cleaning pipeline (applied in order):**
1. HTML entity decoding (`&amp;` → `&`, `&#39;` → `'`, etc.)
2. Filler word removal — `um`, `uh`, `hmm`, `hm`, `mhm`, `err` as standalone words only. `like` and `you know` are deliberately left alone.
3. **Chapter injection** — if the video has YouTube chapter markers, they are inserted as `## Heading` at the correct timestamps. This is the most reliable source of structure.
4. **Heuristic heading detection** (fallback when no chapters) — short (≤7 words), title-cased, unpunctuated segments preceded by a long pause are promoted to `## Heading`.
5. **Adaptive paragraph merging** — break threshold is computed per-video as the 80th-percentile inter-segment gap, capped between 1–8 s. A paragraph break fires when the gap exceeds the threshold AND the paragraph has accumulated ≥60 words.
6. **Word count filter** — transcripts with < 200 prose words (headings excluded) are rejected and not written to blob.

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--raw-dir DIR` | `./output` | Where to read `dataset.jsonl` |
| `--blob-dir DIR` | `/media/ghost/external_storage/yt_transcripts` | Where to write clean `.md` files |

---

## Stage 3: rewrite

```bash
python main.py rewrite [options]
```

Turns cleaned caption prose into a readable article. Background, resumable, and
driven from PostgreSQL's pending/failed `rewrite_status` queue, FIFO. The web UI
exposes it as the **Rewrite** button and runs it inside **Run All**.

This is a *lossless* transform, the opposite of enrichment: nothing is
summarised or dropped. Disfluencies and false starts are repaired, punctuation
restored, speaker turns attributed (`**Q:**` / `**A:**`), enumerations become
lists, and `###` subheadings are inserted where the subject changes.

**How a video is processed:**

1. **Chunked on paragraph boundaries.** Chunks are built from whole paragraphs,
   so a chunk never ends mid-sentence. A chapter heading forces a boundary, so a
   heavily chaptered video yields more, smaller chunks than `REWRITE_CHUNK_WORDS`
   suggests. A paragraph longer than the target is kept whole, never sliced.
2. **Seams flow.** Each chunk is shown the last `REWRITE_CONTEXT_WORDS` of the
   *previous chunk's output* and told to continue from it. That is context, not
   input — it is never rewritten twice, so nothing is duplicated in the article.
   (Overlapping the *source* would put the same sentences in the output twice.)
3. **Coverage is checked before acceptance.** The output/input word ratio must
   land inside a band, and the chunk's distinctive content words must survive.
   Frequent words are excluded from that measure so rewording is not punished.
4. **Failure degrades, never deletes.** A rejected chunk retries at a lower
   temperature, then falls back to its verbatim source text. The article is
   never quietly shorter than the transcript — a bad chunk becomes unpolished
   prose, never missing prose.

**What gets saved:**

| Path | Contents |
|---|---|
| `<blob>/<Channel>/<Title>.md` | The article, replacing the clean transcript |
| `<blob>/<Channel>/<Title>.transcript.md` | Untouched verbatim companion |
| PostgreSQL `videos` | `rewrite_status = 'done'`, `enrichment_status` reset to `'pending'` |
| PostgreSQL `processing_runs` | One row per attempt, with model and duration |

Enrichment is re-queued on purpose: new prose invalidates metadata derived from
the old text.

**Cost.** The slowest stage by far — it generates roughly one token per
transcript token. Budget hours per long video and run it overnight or from cron.
Throughput is RAM-bound; see `status.md` for measured rates.

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--limit N` | all | Stop after N videos |
| `--loop` | off | Poll continuously instead of exiting when empty |
| `--poll SECS` | `60` | Sleep interval in loop mode |
| `--raw-dir DIR` | `./output` | Where to read `dataset.jsonl` |
| `--blob-dir DIR` | blob storage | Where to write articles |

Set `REWRITE_ENABLED=0` to skip the stage entirely; the rest of the pipeline is
unaffected and publishes plain clean transcripts.

---

## Stage 4: enrich

```bash
python main.py enrich [options]
# or: ./enrich.sh
```

Drains PostgreSQL's pending/failed enrichment queue FIFO. The local Ollama model derives summary, key concepts, domains, difficulty, content kind, and sections; successful results update PostgreSQL and rewrite the blob-storage Markdown as a structured knowledge document. Each completed video is marked `enrichment_status = 'done'`, so interruption and restart are safe.

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--raw-dir DIR` | `./output` | Source `dataset.jsonl` used to recover transcript records |
| `--blob-dir DIR` | `/media/ghost/external_storage/yt_transcripts` | Review-buffer Markdown to rewrite |
| `--limit N` | `0` | Maximum videos for this run; `0` drains the queue |
| `--loop` | off | Poll continuously instead of exiting when empty |
| `--poll SECS` | `60` | Sleep interval in loop mode |

The web UI runs enrichment at the lowest CPU priority. A run reports failure
only when it cannot start at all — no `DATABASE_URL`, `LLM_ENABLED=0`, or an
unreachable Ollama — and **Run All** stops before ingest in that case. Videos
that fail individually are marked `failed` and retried by the next run; they do
not stop the pipeline, so everything that did enrich still gets ingested.

---

## Human review step

After `clean`, open blob storage and inspect the `.md` files:
- Read through the transcript for formatting issues
- Edit headings, fix paragraph breaks, correct obvious errors
- **Delete** any file you don't want ingested

No action is needed for files you're happy with — just leave them.

---

## Stage 5: ingest

```bash
python main.py ingest [options]
```

Copies every `.md` file still in blob storage to `/srv/dbdata` and updates PostgreSQL. Files already present at the destination are skipped.

**What gets saved:**

| Path | Contents |
|---|---|
| `/srv/dbdata/markdowns/yt_transcripts_structured/<Channel>/<Title>.md` | Approved transcript |
| PostgreSQL `videos` | `status = 'ingested'`, `clean_path` set |

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--blob-dir DIR` | `/media/ghost/external_storage/yt_transcripts` | Source |
| `--clean-dir DIR` | `/srv/dbdata/markdowns/yt_transcripts_structured` | Destination |

---

## Stage 6: embed (future)

Planned: `python main.py embed`

Chunks ingested `.md` files → generates embeddings → stores in Qdrant or Chroma → sets `status = 'embedded'` in PostgreSQL.
