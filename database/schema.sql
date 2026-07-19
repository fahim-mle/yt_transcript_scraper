-- YouTube Transcript Scraper — PostgreSQL Schema
-- Run once against the target database:
--   psql $DATABASE_URL -f database/schema.sql

-- ─────────────────────────────────────────────
-- Core table
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS videos (

    -- Identity — immutable after insert
    id              BIGSERIAL PRIMARY KEY,
    video_id        TEXT UNIQUE NOT NULL,   -- YouTube video ID (e.g. dQw4w9WgXcQ)
    url             TEXT NOT NULL,
    channel         TEXT NOT NULL,          -- channel name at time of scraping
    channel_id      TEXT,                   -- YouTube channel ID if available
    published_date  DATE,
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    language        TEXT NOT NULL DEFAULT 'en',
    raw_path        TEXT,                   -- path in ./output (set once at scrape time)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Content — updatable (title/description may be corrected by user)
    title           TEXT NOT NULL,
    description     TEXT,
    word_count      INTEGER,

    -- Pipeline state — updatable
    status          TEXT NOT NULL DEFAULT 'raw',
        -- raw       → scraped, sitting in ./output
        -- cleaned   → clean .md written to blob storage, awaiting review
        -- ingested  → reviewed and approved, .md copied to /srv/dbdata
        -- embedded  → vector embedding stored in Qdrant/Chroma (future)
    clean_path      TEXT,   -- path in /srv/dbdata/markdowns/... (set after ingestion)

    -- User annotations — updatable
    topic           TEXT,
    tags            TEXT[]  NOT NULL DEFAULT '{}',
    notes           TEXT,

    updated_at      TIMESTAMPTZ
);

-- ─────────────────────────────────────────────
-- Audit log — append-only, no deletes
-- Every change to an updatable field is recorded here.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS video_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    field_name  TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────
-- Indexes — optimised for the expected query patterns
-- ─────────────────────────────────────────────

-- Equality / filter queries
CREATE INDEX IF NOT EXISTS idx_videos_channel       ON videos(channel);
CREATE INDEX IF NOT EXISTS idx_videos_status        ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_topic         ON videos(topic);
CREATE INDEX IF NOT EXISTS idx_videos_language      ON videos(language);

-- Range / sort queries
CREATE INDEX IF NOT EXISTS idx_videos_published     ON videos(published_date DESC);
CREATE INDEX IF NOT EXISTS idx_videos_scraped       ON videos(scraped_at DESC);

-- Array containment (tags @> ARRAY['ml','python'])
CREATE INDEX IF NOT EXISTS idx_videos_tags          ON videos USING GIN(tags);

-- Full-text search over title + description
CREATE INDEX IF NOT EXISTS idx_videos_fts ON videos
    USING GIN(
        to_tsvector('english',
            COALESCE(title, '') || ' ' || COALESCE(description, '')
        )
    );
