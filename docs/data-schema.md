# Data Schema

## PostgreSQL — `videos` table

Core metadata record. One row per YouTube video.

| Column | Type | Immutable | Description |
|---|---|---|---|
| `id` | BIGSERIAL | ✓ | Internal primary key |
| `video_id` | TEXT | ✓ | YouTube video ID (e.g. `dQw4w9WgXcQ`) |
| `url` | TEXT | ✓ | Full YouTube URL |
| `channel` | TEXT | ✓ | Channel name at time of scraping |
| `channel_id` | TEXT | ✓ | YouTube channel ID if available |
| `published_date` | DATE | ✓ | Video publish date |
| `scraped_at` | TIMESTAMPTZ | ✓ | When this row was first created |
| `language` | TEXT | ✓ | Transcript language code |
| `raw_path` | TEXT | ✓ | Path to raw `.md` in `./output` |
| `created_at` | TIMESTAMPTZ | ✓ | Row creation timestamp |
| `title` | TEXT | | Video title (may be corrected) |
| `description` | TEXT | | Video description |
| `word_count` | INTEGER | | Updated after cleaning |
| `status` | TEXT | | `raw` → `cleaned` → `embedded` |
| `clean_path` | TEXT | | Path to clean `.md` in `/srv/dbdata` |
| `topic` | TEXT | | User-assigned topic / project |
| `tags` | TEXT[] | | User-assigned tags |
| `notes` | TEXT | | Free-form notes |
| `updated_at` | TIMESTAMPTZ | | Last update timestamp |

### `status` lifecycle

```
raw → cleaned → embedded
```

- `raw`: transcript scraped, raw `.md` in `./output`, not yet cleaned
- `cleaned`: clean `.md` written to `/srv/dbdata`, word count finalised
- `embedded`: chunks embedded into Qdrant/Chroma (future)

### Immutability

Immutable fields are enforced at the application layer in `database/db.py`. The `IMMUTABLE_FIELDS` frozenset prevents any `update_video()` call from touching these columns. They are also excluded from `ON CONFLICT DO UPDATE` in `upsert_video()`.

---

## PostgreSQL — `video_audit_log` table

Append-only record of every change to an updatable field.

| Column | Type | Description |
|---|---|---|
| `id` | BIGSERIAL | Primary key |
| `video_id` | TEXT | FK → `videos.video_id` |
| `field_name` | TEXT | Which field changed |
| `old_value` | TEXT | Previous value (serialised) |
| `new_value` | TEXT | New value (serialised) |
| `changed_at` | TIMESTAMPTZ | When the change was made |

---

## File outputs

### `./output/<Channel>/<Title>.md` — raw transcript
YAML frontmatter with video metadata + `[MM:SS]` timestamped transcript body.
Temporary staging file; can be deleted after cleaning if disk space is needed.

### `./output/<Channel>/<Title>.json` — raw segments
Raw list of `{"text": "...", "start": 0.0, "duration": 2.5}` dicts.
Source of truth for the clean stage.

### `./output/dataset.jsonl` — aggregate (ML-ready)
One JSON record per line. Contains all metadata + `transcript_text` (clean joined text) + `transcript_segments` + `word_count`. Used as input to the `clean` command. Append-only across runs.

### `./output/index.csv` — manifest
One row per video: `video_id, title, channel, published, url, word_count, md_path`.
Useful for `pd.read_csv()` inspection and filtering.

### `/srv/dbdata/markdowns/yt_transcripts_structured/<Channel>/<Title>.md` — clean transcript
Final output. YAML frontmatter + paragraph-structured prose (no timestamps).
This is what gets fed into embedding pipelines and used as a knowledge source.

---

## Indexes (PostgreSQL)

| Index | Type | Supports |
|---|---|---|
| `idx_videos_channel` | B-tree | Filter by channel |
| `idx_videos_status` | B-tree | Filter by pipeline stage |
| `idx_videos_topic` | B-tree | Filter by topic |
| `idx_videos_language` | B-tree | Filter by language |
| `idx_videos_published` | B-tree (DESC) | Date range queries |
| `idx_videos_scraped` | B-tree (DESC) | Recency sorting |
| `idx_videos_tags` | GIN | `tags @> ARRAY['ml']` containment |
| `idx_videos_fts` | GIN (tsvector) | Full-text search on title + description |
