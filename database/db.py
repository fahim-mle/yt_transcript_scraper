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
