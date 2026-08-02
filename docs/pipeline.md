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
| `/media/ghost/external_storage/yt_transcripts/<Channel>/<Title>.md` | Clean, paragraph-formatted transcript |
| PostgreSQL `videos` | `status = 'cleaned'`, `word_count` updated |

Already-existing files in blob storage are skipped.

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

## Stage 3: enrich

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

The web UI runs enrichment at the lowest CPU priority. **Run All** stops before ingest if enrichment prerequisites or any enrichment item fail.

---

## Human review step

After `clean`, open blob storage and inspect the `.md` files:
- Read through the transcript for formatting issues
- Edit headings, fix paragraph breaks, correct obvious errors
- **Delete** any file you don't want ingested

No action is needed for files you're happy with — just leave them.

---

## Stage 4: ingest

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

## Stage 5: embed (future)

Planned: `python main.py embed`

Chunks ingested `.md` files → generates embeddings → stores in Qdrant or Chroma → sets `status = 'embedded'` in PostgreSQL.
