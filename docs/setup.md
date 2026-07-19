# Setup Guide

## Requirements

- Python 3.11+
- PostgreSQL (running in `/srv/dbdata` or accessible via `DATABASE_URL`)

## Installation

```bash
git clone https://github.com/<your-username>/yt_transcript_scraper.git
cd yt_transcript_scraper
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Create a `.env` file in the project root (never commit this):

```env
DATABASE_URL=postgresql://user:password@localhost:5432/yt_transcripts
CLEAN_OUTPUT_DIR=/srv/dbdata/markdowns/yt_transcripts_structured
```

`DATABASE_URL` is required for metadata storage. If unset, the pipeline still scrapes and saves files — it just logs a warning and skips the DB steps.

## Database setup

Run the schema against your PostgreSQL instance once:

```bash
psql $DATABASE_URL -f database/schema.sql
```

This creates the `videos` table, `video_audit_log` table, and all indexes. The script is idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

## Quick start

```bash
# Scrape a single video
python main.py scrape "https://www.youtube.com/watch?v=VIDEO_ID"

# Scrape a full channel
python main.py scrape "https://www.youtube.com/@ChannelHandle"

# Scrape a playlist
python main.py scrape "https://www.youtube.com/playlist?list=PLAYLIST_ID"

# Scrape from a file of URLs
python main.py scrape urls.txt

# Clean all raw transcripts and push to /srv/dbdata
python main.py clean
```

## Verifying output

After `scrape`:
- `./output/<Channel>/<Title>.md` — raw timestamped transcript
- `./output/<Channel>/<Title>.json` — raw segments
- `./output/dataset.jsonl` — one record per video (used by `clean`)
- `./output/index.csv` — quick-view manifest
- PostgreSQL: `SELECT video_id, title, status FROM videos;`

After `clean`:
- `/srv/dbdata/markdowns/yt_transcripts_structured/<Channel>/<Title>.md`
- PostgreSQL: `SELECT video_id, status, word_count, clean_path FROM videos;`

## Checking the metadata DB

```sql
-- Search by title / description
SELECT title, channel, published_date, status
FROM videos
WHERE to_tsvector('english', title || ' ' || COALESCE(description,''))
      @@ plainto_tsquery('english', 'machine learning');

-- Filter by channel + status
SELECT title, word_count, clean_path
FROM videos
WHERE channel = 'Andrej Karpathy' AND status = 'cleaned'
ORDER BY published_date DESC;

-- Filter by tags
SELECT title, tags FROM videos WHERE tags @> ARRAY['ml', 'transformer'];

-- View audit history for a video
SELECT field_name, old_value, new_value, changed_at
FROM video_audit_log
WHERE video_id = 'dQw4w9WgXcQ'
ORDER BY changed_at;
```
