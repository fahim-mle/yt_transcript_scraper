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
-- Content metadata — derived from the transcript by the LLM enrichment
-- step during `clean`. Added via ALTER so setup-db stays idempotent and
-- upgrades an existing table in place.
-- ─────────────────────────────────────────────
ALTER TABLE videos ADD COLUMN IF NOT EXISTS summary       TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS key_concepts  TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS domains       TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS difficulty    TEXT;   -- beginner | intermediate | advanced
ALTER TABLE videos ADD COLUMN IF NOT EXISTS content_kind  TEXT;   -- tutorial | lecture | talk | interview | explainer | ...

-- Enrichment queue state — drives the background `enrich` worker (FIFO).
--   pending → cleaned, awaiting LLM enrichment
--   done    → enriched successfully, never reprocessed
--   failed  → enrichment attempted and failed (eligible for retry)
ALTER TABLE videos ADD COLUMN IF NOT EXISTS enrichment_status TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_videos_enrichment ON videos(enrichment_status);

-- Article rewrite queue state — drives the background `rewrite` worker (FIFO).
-- Independent of enrichment: rewriting is the slow LLM pass over the whole
-- transcript, enrichment is a cheap metadata pass over the result.
--   pending → cleaned, awaiting rewrite
--   done    → article written, never reprocessed
--   failed  → rewrite attempted and failed (eligible for retry)
ALTER TABLE videos ADD COLUMN IF NOT EXISTS rewrite_status TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_videos_rewrite ON videos(rewrite_status);

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
-- Sections — per-video structural breakdown produced by enrichment.
-- Upserted by (video_id, idx); timestamps populated from YouTube chapters
-- when available, otherwise NULL.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sections (
    id          BIGSERIAL PRIMARY KEY,
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    idx         INTEGER NOT NULL,          -- 0-based order within the video
    heading     TEXT NOT NULL,
    summary     TEXT,
    start_ts    NUMERIC,                   -- seconds, from chapter marker if known
    end_ts      NUMERIC,
    UNIQUE (video_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_sections_video ON sections(video_id);

-- ─────────────────────────────────────────────
-- Processing runs — one row per LLM enrichment attempt (audit + debugging).
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS processing_runs (
    id          BIGSERIAL PRIMARY KEY,
    video_id    TEXT NOT NULL REFERENCES videos(video_id),
    model       TEXT NOT NULL,
    status      TEXT NOT NULL,             -- success | failed
    error       TEXT,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runs_video ON processing_runs(video_id);

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
