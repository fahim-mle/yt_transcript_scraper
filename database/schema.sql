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
--   blocked → too many failed attempts; skipped until explicitly requeued
ALTER TABLE videos ADD COLUMN IF NOT EXISTS rewrite_status TEXT NOT NULL DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_videos_rewrite ON videos(rewrite_status);

-- Attempt counters. A FIFO queue that re-serves 'failed' rows forever lets one
-- permanently broken video sit at the head and starve everything behind it.
-- Past QUEUE_MAX_ATTEMPTS the row moves to 'blocked' and stops being served.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS rewrite_attempts    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS enrichment_attempts INTEGER NOT NULL DEFAULT 0;

-- ─────────────────────────────────────────────
-- Generation provenance — who produced the article, with what, and how well.
-- The article is generated text; without this there is no way to tell a
-- GLM-4.6 rewrite from a 4B local one six months from now, or to re-run only
-- the documents produced by a model you no longer trust.
-- ─────────────────────────────────────────────
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_model          TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_provider       TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_prompt_version TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_generated_at   TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_chunks         INTEGER;
-- Chunks that failed the coverage guard and fell back to verbatim transcript.
-- Non-zero means parts of this "article" are unpolished caption text.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_fallback_chunks INTEGER;

-- Same, for the metadata pass — it can run on a different model than the prose.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS enrichment_model    TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS enrichment_provider TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS enriched_at         TIMESTAMPTZ;

-- ─────────────────────────────────────────────
-- Source fidelity — how much to trust this document as evidence.
-- ─────────────────────────────────────────────
ALTER TABLE videos ADD COLUMN IF NOT EXISTS transcript_path     TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS article_word_count  INTEGER;
-- article words / transcript words. Far below 1.0 means the rewrite compressed
-- the source despite the guard; a filter for retrieval quality.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS coverage_ratio      REAL;
-- Auto-generated captions carry transcription errors that no rewrite can fix.
-- NULL for rows scraped before this was recorded — unknown, not false.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS captions_auto       BOOLEAN;

-- ─────────────────────────────────────────────
-- Retrieval / embedding — written now because backfilling a content hash
-- across a corpus after the fact means re-deriving every document.
-- ─────────────────────────────────────────────
-- SHA-256 of the article body. Changes iff the prose changed, so it is the
-- signal for "this embedding is stale", independent of any timestamp.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS content_hash     TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS embedding_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS embedded_at      TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS embedding_model  TEXT;

CREATE INDEX IF NOT EXISTS idx_videos_embedding ON videos(embedding_status);
CREATE INDEX IF NOT EXISTS idx_videos_hash      ON videos(content_hash);

-- ─────────────────────────────────────────────
-- Learning workflow — your layer, not the pipeline's. Nothing writes these
-- automatically; they turn the corpus into a queue you work through.
-- ─────────────────────────────────────────────
ALTER TABLE videos ADD COLUMN IF NOT EXISTS reviewed       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS usefulness     SMALLINT;  -- 1-5, NULL = unrated
-- Knowledge-base artifact ids this transcript fed into (IDEA-…, ARTICLE-…).
-- Transcripts stay out of the KB; this is the citation link back the other way.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS extracted_to   TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS open_questions TEXT[] NOT NULL DEFAULT '{}';

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

-- Which stage the run belongs to. Both `rewrite` and `enrich` write here, and
-- without this column a 2-hour rewrite and a 30-second metadata call are
-- indistinguishable rows, which makes every throughput question unanswerable.
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS stage    TEXT;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS provider TEXT;

-- Token usage — the physical fact about a request, recorded always.
-- Deliberately NOT a cost: see the model_pricing note below.
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS prompt_tokens     INTEGER;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS completion_tokens INTEGER;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS cached_tokens     INTEGER;
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS api_calls         INTEGER;
-- 'local'        → ran on your own hardware; no marginal money cost
-- 'subscription' → covered by a flat plan; marginal money cost is zero
-- 'api'          → billed per token, priceable from model_pricing
ALTER TABLE processing_runs ADD COLUMN IF NOT EXISTS billing_mode TEXT;

CREATE INDEX IF NOT EXISTS idx_runs_stage ON processing_runs(stage, created_at DESC);

-- Backfill: every run recorded before these columns existed was a local Ollama
-- call. Labelling them 'local' keeps them out of the "unpriced, cost unknown"
-- bucket, which is reserved for metered runs whose rate is genuinely missing.
UPDATE processing_runs
   SET provider = COALESCE(provider, 'ollama'),
       billing_mode = COALESCE(billing_mode, 'local')
 WHERE provider IS NULL OR billing_mode IS NULL;

-- ─────────────────────────────────────────────
-- Model pricing — cost is DERIVED, never stored on the run.
--
-- Storing a dollar figure at generation time is wrong in both directions: on a
-- subscription plan the marginal cost is zero, so any number written there is
-- fabricated; and on metered billing, rates change, so a number written once is
-- only correct until the next price change. Recording tokens and pricing them
-- on demand keeps history accurate under both, and makes "what would the
-- corpus have cost on direct API?" a query rather than a guess.
--
-- Rates are per 1,000,000 tokens, in USD. `effective_from` makes the table
-- append-only: a price change is a new row, so historical runs stay priced at
-- the rate that was actually in force.
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_pricing (
    id              BIGSERIAL PRIMARY KEY,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_per_mtok  NUMERIC(12, 6) NOT NULL,
    output_per_mtok NUMERIC(12, 6) NOT NULL,
    cached_per_mtok NUMERIC(12, 6),
    currency        TEXT NOT NULL DEFAULT 'USD',
    effective_from  DATE NOT NULL DEFAULT CURRENT_DATE,
    note            TEXT,
    UNIQUE (provider, model, effective_from)
);

-- Cost of every run, priced at the rate in force when it ran. Runs with no
-- matching pricing row (subscription, or a model never priced) surface as NULL
-- rather than as zero — an unknown cost and a free call are not the same fact.
CREATE OR REPLACE VIEW processing_run_costs AS
SELECT
    r.id,
    r.video_id,
    r.stage,
    r.provider,
    r.model,
    r.status,
    r.billing_mode,
    r.prompt_tokens,
    r.completion_tokens,
    r.cached_tokens,
    r.duration_ms,
    r.created_at,
    CASE WHEN r.billing_mode IN ('subscription', 'local') THEN 0::NUMERIC ELSE
        ROUND(
            (COALESCE(r.prompt_tokens, 0)     / 1000000.0) * p.input_per_mtok +
            (COALESCE(r.completion_tokens, 0) / 1000000.0) * p.output_per_mtok,
            6
        )
    END AS cost_usd,
    p.effective_from AS priced_at
FROM processing_runs r
LEFT JOIN LATERAL (
    SELECT * FROM model_pricing mp
    WHERE mp.model = r.model
      AND (mp.provider = r.provider OR r.provider IS NULL)
      AND mp.effective_from <= r.created_at::date
    ORDER BY mp.effective_from DESC
    LIMIT 1
) p ON TRUE;

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
