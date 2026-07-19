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
| `--delay SECS` | `1` | Pause between transcript API calls |
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

## Stage 2: clean

```bash
python main.py clean [options]
```

Reads `dataset.jsonl`, applies the cleaning pipeline to each video's raw segments, and writes reviewed `.md` files to blob storage for human inspection. Does **not** touch `/srv/dbdata`.

**What gets saved:**

| Path | Contents |
|---|---|
| `/media/ghost/Blog Storage/yt_transcripts/<Channel>/<Title>.md` | Clean, paragraph-formatted transcript |
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
| `--blob-dir DIR` | `/media/ghost/Blog Storage/yt_transcripts` | Where to write clean `.md` files |

---

## Human review step

After `clean`, open blob storage and inspect the `.md` files:
- Read through the transcript for formatting issues
- Edit headings, fix paragraph breaks, correct obvious errors
- **Delete** any file you don't want ingested

No action is needed for files you're happy with — just leave them.

---

## Stage 3: ingest

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
| `--blob-dir DIR` | `/media/ghost/Blog Storage/yt_transcripts` | Source |
| `--clean-dir DIR` | `/srv/dbdata/markdowns/yt_transcripts_structured` | Destination |

---

## Stage 4: embed (future)

Planned: `python main.py embed`

Chunks ingested `.md` files → generates embeddings → stores in Qdrant or Chroma → sets `status = 'embedded'` in PostgreSQL.
