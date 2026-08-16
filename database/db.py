"""
PostgreSQL interface for the transcript metadata store.

All writes go through upsert_video() or update_video().
Hard deletes are not supported — nothing in this module issues DELETE.
Immutable fields are enforced at the application layer; any attempt
to update them raises ValueError.
"""

import json
import logging
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)

# Fields that must never be changed after the initial insert.
IMMUTABLE_FIELDS = frozenset({
    "id", "video_id", "url", "channel", "channel_id",
    "published_date", "scraped_at", "language", "raw_path", "created_at",
})

# Fields that callers are allowed to update.
UPDATABLE_FIELDS = frozenset({
    "title", "description", "word_count",
    "status", "clean_path",
    "topic", "tags", "notes",
    # Content metadata derived by LLM enrichment during `clean`
    "summary", "key_concepts", "domains", "difficulty", "content_kind",
    # Generation provenance — which model produced the article/metadata
    "article_model", "article_provider", "article_prompt_version",
    "article_generated_at", "article_chunks", "article_fallback_chunks",
    "enrichment_model", "enrichment_provider", "enriched_at",
    # Source fidelity
    "transcript_path", "article_word_count", "coverage_ratio", "captions_auto",
    # Retrieval / embedding
    "content_hash", "embedding_status", "embedded_at", "embedding_model",
    # Learning workflow — owned by you, never written by the pipeline
    "reviewed", "usefulness", "extracted_to", "open_questions",
})


@contextmanager
def _conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set.")
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_video(meta: dict) -> None:
    """
    Insert a new video record, or update updatable fields if video_id already exists.
    `meta` should contain at minimum: video_id, url, title, channel.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO videos (
                    video_id, url, title, channel, channel_id,
                    published_date, language, description, word_count,
                    raw_path, status, topic, tags
                ) VALUES (
                    %(video_id)s, %(url)s, %(title)s, %(channel)s, %(channel_id)s,
                    %(published_date)s, %(language)s, %(description)s, %(word_count)s,
                    %(raw_path)s, %(status)s, %(topic)s, %(tags)s
                )
                ON CONFLICT (video_id) DO UPDATE SET
                    title        = EXCLUDED.title,
                    description  = EXCLUDED.description,
                    word_count   = EXCLUDED.word_count,
                    raw_path     = COALESCE(videos.raw_path, EXCLUDED.raw_path),
                    status       = EXCLUDED.status,
                    updated_at   = NOW()
            """, {
                "video_id":      meta.get("video_id"),
                "url":           meta.get("url"),
                "title":         meta.get("title", ""),
                "channel":       meta.get("channel", ""),
                "channel_id":    meta.get("channel_id"),
                "published_date": meta.get("published") or None,
                "language":      meta.get("language", "en"),
                "description":   meta.get("description"),
                "word_count":    meta.get("word_count"),
                "raw_path":      meta.get("raw_path"),
                "status":        meta.get("status", "raw"),
                "topic":         meta.get("topic"),
                "tags":          meta.get("tags", []),
            })


def update_video(video_id: str, updates: dict) -> None:
    """
    Update specific fields for a video. Only UPDATABLE_FIELDS are allowed.
    All changes are written to video_audit_log.
    """
    bad = set(updates) - UPDATABLE_FIELDS
    if bad:
        # Check if any of these are immutable (give a clearer error)
        immutable_attempted = bad & IMMUTABLE_FIELDS
        if immutable_attempted:
            raise ValueError(f"Cannot update immutable field(s): {immutable_attempted}")
        raise ValueError(f"Unknown field(s): {bad}")

    if not updates:
        return

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch current values for audit log
            cur.execute(
                f"SELECT {', '.join(updates.keys())} FROM videos WHERE video_id = %s",
                (video_id,)
            )
            current = cur.fetchone()
            if current is None:
                raise KeyError(f"No video found with video_id={video_id!r}")

            # Build dynamic UPDATE
            set_clauses = ", ".join(f"{k} = %({k})s" for k in updates)
            cur.execute(
                f"UPDATE videos SET {set_clauses}, updated_at = NOW() WHERE video_id = %(video_id)s",
                {**updates, "video_id": video_id},
            )

            # Write audit rows
            for field, new_val in updates.items():
                old_val = current.get(field)
                cur.execute("""
                    INSERT INTO video_audit_log (video_id, field_name, old_value, new_value)
                    VALUES (%s, %s, %s, %s)
                """, (
                    video_id,
                    field,
                    _to_audit_str(old_val),
                    _to_audit_str(new_val),
                ))


def upsert_sections(video_id: str, sections: list[dict]) -> None:
    """
    Replace the section breakdown for a video. Sections are derived (reproducible)
    so re-running enrichment overwrites by (video_id, idx) rather than appending.
    Each section dict: {heading, summary, start_ts?, end_ts?}.
    """
    if not sections:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            for idx, s in enumerate(sections):
                cur.execute("""
                    INSERT INTO sections (video_id, idx, heading, summary, start_ts, end_ts)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (video_id, idx) DO UPDATE SET
                        heading  = EXCLUDED.heading,
                        summary  = EXCLUDED.summary,
                        start_ts = EXCLUDED.start_ts,
                        end_ts   = EXCLUDED.end_ts
                """, (
                    video_id, idx,
                    s.get("heading", ""),
                    s.get("summary"),
                    s.get("start_ts"),
                    s.get("end_ts"),
                ))


def record_processing_run(
    video_id: str,
    model: str,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
    *,
    stage: str | None = None,
    provider: str | None = None,
    usage: dict | None = None,
) -> None:
    """
    Append one LLM attempt to processing_runs (audit / debugging / accounting).

    Token counts are stored; cost is not. See the model_pricing note in
    schema.sql — pricing is derived through the processing_run_costs view so
    that subscription runs and metered runs stay honestly distinguishable.
    """
    usage = usage or {}
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processing_runs (
                    video_id, model, status, error, duration_ms,
                    stage, provider, prompt_tokens, completion_tokens,
                    cached_tokens, api_calls, billing_mode
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                video_id, model, status, error, duration_ms,
                stage, provider,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                usage.get("cached_tokens"), usage.get("calls"),
                config.LLM_BILLING_MODE,
            ))


def list_pending_enrichment(limit: int | None = None) -> list[str]:
    """
    Return video_ids that are cleaned but not yet enriched, oldest first (FIFO).

    'failed' rows are included so a later run retries them — but only while
    they are under the attempt ceiling. Without that bound one video that fails
    every time sits at the head of the queue forever and nothing behind it is
    ever reached.
    """
    sql = (
        "SELECT video_id FROM videos "
        "WHERE status IN ('cleaned', 'ingested') "
        "AND enrichment_status IN ('pending', 'failed') "
        "AND enrichment_attempts < %s "
        "ORDER BY created_at ASC"
    )
    params: list = [config.QUEUE_MAX_ATTEMPTS]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]


def _set_queue_status(video_id: str, status: str, status_col: str, attempts_col: str) -> None:
    """
    Move a video through a queue's state machine, maintaining its attempt count.

    `failed` increments the counter and promotes to `blocked` once the ceiling
    is reached, so a permanently broken video leaves the queue instead of
    blocking it. `done` and `pending` both reset the counter — a success or an
    explicit requeue means the next failure starts a fresh budget.

    Transient operational state, so this is a plain UPDATE, not audit-logged.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            if status == "failed":
                cur.execute(
                    f"UPDATE videos SET {attempts_col} = {attempts_col} + 1, "
                    f"{status_col} = CASE WHEN {attempts_col} + 1 >= %s "
                    f"THEN 'blocked' ELSE 'failed' END "
                    f"WHERE video_id = %s RETURNING {status_col}, {attempts_col}",
                    (config.QUEUE_MAX_ATTEMPTS, video_id),
                )
                row = cur.fetchone()
                if row and row[0] == "blocked":
                    logger.warning(
                        "%s blocked after %d failed attempt(s) — it will be skipped "
                        "until requeued.", video_id, row[1],
                    )
            elif status in {"done", "pending"}:
                cur.execute(
                    f"UPDATE videos SET {status_col} = %s, {attempts_col} = 0 "
                    f"WHERE video_id = %s",
                    (status, video_id),
                )
            else:
                cur.execute(
                    f"UPDATE videos SET {status_col} = %s WHERE video_id = %s",
                    (status, video_id),
                )


def set_enrichment_status(video_id: str, status: str) -> None:
    """Set the enrichment queue state (pending | done | failed | blocked)."""
    _set_queue_status(video_id, status, "enrichment_status", "enrichment_attempts")


def list_pending_rewrite(limit: int | None = None) -> list[str]:
    """
    Return video_ids that are cleaned but not yet rewritten, oldest first (FIFO).

    'failed' rows are retried only while under the attempt ceiling — see
    list_pending_enrichment for why the bound exists.
    """
    sql = (
        "SELECT video_id FROM videos "
        "WHERE status IN ('cleaned', 'ingested') "
        "AND rewrite_status IN ('pending', 'failed') "
        "AND rewrite_attempts < %s "
        "ORDER BY created_at ASC"
    )
    params: list = [config.QUEUE_MAX_ATTEMPTS]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]


def set_rewrite_status(video_id: str, status: str) -> None:
    """Set the rewrite queue state (pending | done | failed | blocked)."""
    _set_queue_status(video_id, status, "rewrite_status", "rewrite_attempts")


def requeue(video_ids: list[str] | None = None, *, stage: str) -> int:
    """
    Return blocked/failed videos to 'pending' and clear their attempt budget.

    The escape hatch for the attempt ceiling: once you have fixed whatever was
    breaking (a model, a timeout, a bad key), this puts the casualties back in
    line. With no ids, requeues every blocked video for that stage.
    """
    if stage not in {"rewrite", "enrichment"}:
        raise ValueError(f"unknown stage {stage!r}")
    status_col, attempts_col = f"{stage}_status", f"{stage}_attempts"

    sql = (f"UPDATE videos SET {status_col} = 'pending', {attempts_col} = 0 "
           f"WHERE {status_col} IN ('blocked', 'failed')")
    params: list = []
    if video_ids:
        sql += " AND video_id = ANY(%s)"
        params.append(list(video_ids))

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def usage_summary(days: int = 30) -> list[dict]:
    """
    Token totals and derived cost per (stage, provider, model, billing_mode).

    Reads processing_run_costs rather than processing_runs so pricing stays a
    view concern — nothing here knows what a token costs.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT stage, provider, model, billing_mode,
                       COUNT(*)                    AS runs,
                       SUM(prompt_tokens)          AS prompt_tokens,
                       SUM(completion_tokens)      AS completion_tokens,
                       SUM(cost_usd)               AS cost_usd
                FROM processing_run_costs
                WHERE created_at >= NOW() - make_interval(days => %s)
                GROUP BY stage, provider, model, billing_mode
                ORDER BY SUM(completion_tokens) DESC NULLS LAST
            """, (days,))
            return [dict(r) for r in cur.fetchall()]


def get_sections(video_id: str) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT idx, heading, summary, start_ts, end_ts "
                "FROM sections WHERE video_id = %s ORDER BY idx",
                (video_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_video(video_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM videos WHERE video_id = %s", (video_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def search_videos(
    query: str | None = None,
    channel: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """
    Search videos. `query` runs against the full-text index (title + description).
    All other filters are ANDed together.
    """
    conditions, params = [], []

    if query:
        conditions.append(
            "to_tsvector('english', COALESCE(title,'') || ' ' || COALESCE(description,'')) "
            "@@ plainto_tsquery('english', %s)"
        )
        params.append(query)
    if channel:
        conditions.append("channel ILIKE %s")
        params.append(f"%{channel}%")
    if topic:
        conditions.append("topic = %s")
        params.append(topic)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if tags:
        conditions.append("tags @> %s")
        params.append(tags)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM videos {where} ORDER BY scraped_at DESC LIMIT %s OFFSET %s",
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def _to_audit_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return json.dumps(val)
    return str(val)
