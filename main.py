#!/usr/bin/env python3
"""
YouTube Transcript Scraper — three-stage pipeline

  scrape    →  raw files in ./output  +  PostgreSQL (status=raw)
  clean     →  reviewed .md in blob storage  +  PostgreSQL (status=cleaned)
  ingest    →  approved .md pushed to /srv/dbdata  +  PostgreSQL (status=ingested)
  setup-db  →  create database (if needed) and apply schema.sql

Usage:
    python main.py scrape <URL_OR_FILE> [options]
    python main.py clean  [options]
    python main.py ingest [options]
    python main.py setup-db

Examples:
    python main.py scrape "https://www.youtube.com/@ChannelHandle"
    python main.py scrape urls.txt
    python main.py clean
    python main.py ingest
    python main.py setup-db
"""

import argparse
import csv
import json
import logging
import os
import re
import shutil
import time

from dotenv import load_dotenv
load_dotenv()

import config
from database import db
from scraper import cleaner, formatter, llm_processor, resolver, transcript

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_CSV_COLUMNS = ["video_id", "title", "channel", "published", "url", "word_count", "md_path"]


# ── Shared helpers ────────────────────────────────────────────────────────

def _output_paths(base_dir: str, meta: dict) -> tuple[str, str]:
    channel_dir = os.path.join(base_dir, formatter.sanitize_filename(meta["channel"] or "unknown_channel"))
    stem = formatter.sanitize_filename(meta["title"] or meta["video_id"])
    return os.path.join(channel_dir, f"{stem}.md"), os.path.join(channel_dir, f"{stem}.json")


def _write_local_aggregate(records: list[dict], output_dir: str, no_jsonl: bool, no_csv: bool) -> None:
    if not records:
        return
    os.makedirs(output_dir, exist_ok=True)

    if not no_jsonl:
        path = os.path.join(output_dir, "dataset.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info("Appended %d record(s) to %s", len(records), path)

    if not no_csv:
        path = os.path.join(output_dir, "index.csv")
        is_new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerows(records)
        logger.info("Appended %d row(s) to %s", len(records), path)


def _db_available() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


# ── scrape ────────────────────────────────────────────────────────────────

def _scrape_video(meta: dict, output_dir: str, lang: str, save_json: bool) -> dict | None:
    md_path, json_path = _output_paths(output_dir, meta)
    if os.path.exists(md_path):
        return None

    video_id = meta["video_id"]
    logger.info("Fetching: %s — %s", video_id, meta.get("title", ""))

    segments = transcript.fetch(video_id, lang=lang)
    if segments is None:
        logger.warning("No transcript for %s, skipping.", video_id)
        return None

    os.makedirs(os.path.dirname(md_path), exist_ok=True)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(formatter.to_markdown(meta, segments))

    if save_json:
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_json(segments))

    logger.info("Saved raw: %s", md_path)
    return formatter.to_jsonl_record(meta, segments, md_path)


def cmd_scrape(args: argparse.Namespace) -> None:
    output_dir = args.output

    logger.info("Resolving: %s", args.url_or_file)
    videos = resolver.resolve(args.url_or_file)
    if not videos:
        logger.error("No videos found.")
        return

    logger.info("Found %d video(s). Raw output → %s", len(videos), output_dir)
    if not _db_available():
        logger.warning("DATABASE_URL not set — skipping PostgreSQL.")

    records, success, skipped, failed = [], 0, 0, 0
    for i, meta in enumerate(videos):
        if i > 0:
            time.sleep(args.delay)

        if os.path.exists(_output_paths(output_dir, meta)[0]):
            skipped += 1
            logger.info("Already exists, skipping: %s", meta.get("title", meta["video_id"]))
            continue

        record = _scrape_video(meta, output_dir, args.lang, not args.no_json)
        if record is None:
            failed += 1
            continue

        success += 1
        records.append(record)

        if _db_available():
            try:
                db.upsert_video({
                    **meta,
                    "word_count": record["word_count"],
                    "raw_path":   record["md_path"],
                    "status":     "raw",
                })
            except Exception as exc:
                logger.warning("DB upsert failed for %s: %s", meta["video_id"], exc)

    _write_local_aggregate(records, output_dir, args.no_jsonl, args.no_csv)
    logger.info("Done. %d saved, %d skipped, %d failed.", success, skipped, failed)


# ── shared helpers for clean / enrich ─────────────────────────────────────

def _meta_from_record(rec: dict) -> dict:
    return {k: rec.get(k, "") for k in
            ("video_id", "url", "title", "channel", "published", "description")}


def _blob_path_for(blob_dir: str, meta: dict) -> tuple[str, str]:
    channel_dir = os.path.join(
        blob_dir, formatter.sanitize_filename(meta["channel"] or "unknown_channel"))
    stem = formatter.sanitize_filename(meta["title"] or meta["video_id"])
    return channel_dir, os.path.join(channel_dir, f"{stem}.md")


def _sections_with_timestamps(sections: list[dict], chapters: list[dict]) -> list[dict]:
    """
    Attach real timestamps to LLM sections when the video's chapter count
    matches the section count (order-aligned). Otherwise timestamps stay NULL.
    """
    aligned = bool(chapters) and len(chapters) == len(sections)
    out = []
    for i, s in enumerate(sections):
        item = {"heading": s.get("heading", ""), "summary": s.get("summary")}
        if aligned:
            item["start_ts"] = chapters[i].get("start_time")
            item["end_ts"] = chapters[i].get("end_time")
        out.append(item)
    return out


# ── clean ─────────────────────────────────────────────────────────────────

def _persist_clean_row(meta: dict, cleaned_text: str) -> None:
    """Ensure the video row exists, mark it cleaned and pending enrichment."""
    video_id = meta["video_id"]
    wc = len(cleaned_text.split())
    try:
        if db.get_video(video_id) is None:
            db.upsert_video({**meta, "word_count": wc, "status": "raw"})
        db.update_video(video_id, {"status": "cleaned", "word_count": wc})
        db.set_enrichment_status(video_id, "pending")
    except Exception as exc:
        logger.warning("DB update failed for %s: %s", video_id, exc)


def cmd_clean(args: argparse.Namespace) -> None:
    """
    Reads raw records from dataset.jsonl, applies the deterministic cleaning
    pipeline, and writes structured .md files to blob storage for review.

    This stage is fast and does NOT call the LLM — enrichment is a separate,
    resumable background job ('enrich'). Cleaned videos are queued for it by
    setting enrichment_status = 'pending'.
    """
    raw_dir  = args.raw_dir
    blob_dir = args.blob_dir
    jsonl_path = os.path.join(raw_dir, "dataset.jsonl")

    if not os.path.exists(jsonl_path):
        logger.error("No dataset.jsonl at %s — run 'scrape' first.", jsonl_path)
        return

    if not _db_available():
        logger.warning("DATABASE_URL not set — skipping PostgreSQL status updates.")

    logger.info("Reading from %s", jsonl_path)
    logger.info("Cleaned output → %s  (blob storage for review)", blob_dir)

    with open(jsonl_path, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    saved, skipped, filtered = 0, 0, 0
    for rec in records:
        meta = _meta_from_record(rec)
        chapters = rec.get("chapters") or []
        channel_dir, blob_path = _blob_path_for(blob_dir, meta)

        if os.path.exists(blob_path):
            skipped += 1
            continue

        segments = rec.get("transcript_segments", [])
        if not segments:
            logger.warning("No segments for %s, skipping.", meta["video_id"])
            filtered += 1
            continue

        cleaned_text = cleaner.clean(segments, chapters=chapters)
        if cleaned_text is None:
            logger.info("Below word threshold, filtered: %s", meta.get("title", meta["video_id"]))
            filtered += 1
            continue

        os.makedirs(channel_dir, exist_ok=True)
        with open(blob_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_clean_markdown(meta, cleaned_text))
        logger.info("Cleaned → %s", blob_path)
        saved += 1

        if _db_available():
            _persist_clean_row(meta, cleaned_text)

    logger.info(
        "Clean done. %d written to blob, %d already existed, %d filtered.",
        saved, skipped, filtered,
    )
    if saved and _db_available():
        logger.info("Queued %d for enrichment. Run './enrich.sh' (or 'python main.py enrich').", saved)


# ── enrich (background worker) ─────────────────────────────────────────────

def _load_records_index(raw_dir: str) -> dict:
    """Map video_id → dataset.jsonl record, for fast lookup by the worker."""
    jsonl_path = os.path.join(raw_dir, "dataset.jsonl")
    index: dict = {}
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("video_id"):
                        index[rec["video_id"]] = rec
    return index


def _enrich_one(video_id: str, rec: dict, blob_dir: str) -> str:
    """
    Enrich a single video end-to-end: LLM → DB (content, sections, run log) →
    rewrite the blob .md as a knowledge doc. Returns an outcome string.
    """
    meta = _meta_from_record(rec)
    chapters = rec.get("chapters") or []
    cleaned_text = cleaner.clean(rec.get("transcript_segments", []), chapters=chapters)
    if cleaned_text is None:
        db.set_enrichment_status(video_id, "done")  # nothing to enrich
        return "no-prose"

    result = llm_processor.enrich(meta["title"], cleaned_text, chapters)

    try:
        db.record_processing_run(
            video_id, result.model,
            "success" if result.ok else "failed",
            result.error, result.duration_ms,
        )
    except Exception as exc:
        logger.warning("Run log failed for %s: %s", video_id, exc)

    if not result.ok:
        db.set_enrichment_status(video_id, "failed")
        return "failed"

    enrichment = result.enrichment.model_dump()
    try:
        db.update_video(video_id, {
            "summary":      enrichment.get("summary"),
            "key_concepts": enrichment.get("key_concepts") or [],
            "domains":      enrichment.get("domains") or [],
            "difficulty":   enrichment.get("difficulty"),
            "content_kind": enrichment.get("content_kind"),
        })
        db.upsert_sections(
            video_id,
            _sections_with_timestamps(enrichment.get("sections") or [], chapters),
        )
    except Exception as exc:
        logger.warning("DB write failed for %s: %s", video_id, exc)

    # Rewrite the review .md with the enriched frontmatter + structure.
    channel_dir, blob_path = _blob_path_for(blob_dir, meta)
    try:
        os.makedirs(channel_dir, exist_ok=True)
        with open(blob_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_knowledge_doc(meta, cleaned_text, enrichment))
    except Exception as exc:
        logger.warning("Blob rewrite failed for %s: %s", video_id, exc)

    db.set_enrichment_status(video_id, "done")
    return "done"


def cmd_enrich(args: argparse.Namespace) -> None:
    """
    Drain the enrichment queue (cleaned videos with enrichment_status pending
    or failed), oldest first. Idempotent and resumable: each video is marked
    'done' when finished, so re-runs never reprocess it. Interrupt any time;
    the next run picks up where it left off.

    Default: process everything pending, then exit (ideal for cron).
    --loop:  keep polling for new work forever (a low-priority background svc).
    """
    if not _db_available():
        logger.error("enrich needs DATABASE_URL — it reads the queue from PostgreSQL.")
        return
    if not config.LLM_ENABLED:
        logger.error("LLM_ENABLED=0 — nothing to do. Set it to 1 in .env to enrich.")
        return
    if not llm_processor.is_available():
        logger.error("Ollama unreachable at %s — start it (`ollama serve`) and retry.",
                     config.OLLAMA_HOST)
        return

    logger.info("Enrich worker — model: %s%s", config.OLLAMA_MODEL,
                "  (loop mode)" if args.loop else "")
    processed = 0
    while True:
        index = _load_records_index(args.raw_dir)
        pending = db.list_pending_enrichment(limit=args.limit or None)

        if not pending:
            if args.loop:
                logger.info("Queue empty — sleeping %ds.", args.poll)
                time.sleep(args.poll)
                continue
            break

        for video_id in pending:
            rec = index.get(video_id)
            if rec is None:
                logger.warning("No dataset.jsonl record for %s — marking failed.", video_id)
                db.set_enrichment_status(video_id, "failed")
                continue
            logger.info("Enriching %s — %s", video_id, rec.get("title", ""))
            try:
                outcome = _enrich_one(video_id, rec, args.blob_dir)
            except Exception as exc:
                logger.error("Enrichment crashed for %s: %s", video_id, exc)
                try:
                    db.set_enrichment_status(video_id, "failed")
                except Exception:
                    pass
                outcome = "error"
            logger.info("  → %s", outcome)
            processed += 1
            if args.limit and processed >= args.limit:
                logger.info("Reached --limit %d.", args.limit)
                logger.info("Enrich worker done. %d processed.", processed)
                return

        if not args.loop:
            break

    logger.info("Enrich worker done. %d processed.", processed)


# ── ingest ────────────────────────────────────────────────────────────────

_VIDEO_ID_RE = re.compile(r'url:\s+"https://www\.youtube\.com/watch\?v=([^"]+)"')


def _extract_video_id(md_content: str) -> str | None:
    m = _VIDEO_ID_RE.search(md_content)
    return m.group(1) if m else None


def cmd_ingest(args: argparse.Namespace) -> None:
    """
    Copies reviewed .md files from blob storage to /srv/dbdata and marks
    them as ingested in PostgreSQL. Only copies files that have not already
    been ingested (i.e. don't already exist at the destination).

    You can delete unwanted files from blob storage before running ingest —
    only what's in the blob dir gets ingested.
    """
    blob_dir  = args.blob_dir
    clean_dir = args.clean_dir

    if not os.path.isdir(blob_dir):
        logger.error("Blob directory not found: %s", blob_dir)
        return

    if not _db_available():
        logger.warning("DATABASE_URL not set — skipping PostgreSQL status updates.")

    logger.info("Ingesting from %s → %s", blob_dir, clean_dir)

    copied, skipped, errors = 0, 0, 0
    for root, _, files in os.walk(blob_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue

            src = os.path.join(root, fname)
            # Preserve channel subfolder structure
            rel = os.path.relpath(src, blob_dir)
            dst = os.path.join(clean_dir, rel)

            if os.path.exists(dst):
                skipped += 1
                continue

            try:
                content = open(src, encoding="utf-8").read()
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                logger.info("Ingested → %s", dst)
                copied += 1

                if _db_available():
                    video_id = _extract_video_id(content)
                    if video_id:
                        try:
                            db.update_video(video_id, {
                                "status":     "ingested",
                                "clean_path": dst,
                            })
                        except Exception as exc:
                            logger.warning("DB update failed for %s: %s", video_id, exc)
                    else:
                        logger.warning("Could not extract video_id from %s", src)

            except Exception as exc:
                logger.error("Failed to ingest %s: %s", src, exc)
                errors += 1

    logger.info(
        "Ingest done. %d copied to /srv/dbdata, %d already existed, %d errors.",
        copied, skipped, errors,
    )


# ── setup-db ──────────────────────────────────────────────────────────────

def cmd_setup_db(args: argparse.Namespace) -> None:
    """
    Create the yt_transcripts database (if it doesn't exist) and apply schema.sql.
    Reads DATABASE_URL from .env automatically.
    """
    import psycopg2
    from urllib.parse import urlparse

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url or "user:password" in db_url:
        logger.error(
            "DATABASE_URL is not configured. Edit .env and set a real connection string, e.g.:\n"
            "  DATABASE_URL=postgresql://ghost@localhost:5432/yt_transcripts"
        )
        return

    schema_path = os.path.join(os.path.dirname(__file__), "database", "schema.sql")
    schema_sql = open(schema_path, encoding="utf-8").read()

    parsed = urlparse(db_url)
    db_name = parsed.path.lstrip("/")

    # Try connecting to target DB directly
    conn = None
    try:
        conn = psycopg2.connect(db_url)
    except psycopg2.OperationalError as exc:
        if "does not exist" in str(exc):
            # Database doesn't exist yet — try creating it via the postgres maintenance DB
            maint_url = db_url.replace(parsed.path, "/postgres")
            try:
                mconn = psycopg2.connect(maint_url)
                mconn.autocommit = True
                with mconn.cursor() as cur:
                    cur.execute(f'CREATE DATABASE "{db_name}"')
                mconn.close()
                logger.info("Created database: %s", db_name)
                conn = psycopg2.connect(db_url)
            except Exception as ce:
                logger.error(
                    "Could not create database '%s': %s\n"
                    "Create it manually first:  createdb %s",
                    db_name, ce, db_name,
                )
                return
        else:
            logger.error("Cannot connect: %s\nCheck DATABASE_URL in .env", exc)
            return

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        logger.info("Schema applied to '%s'. Tables: videos, video_audit_log.", db_name)
    except Exception as exc:
        logger.error("Failed to apply schema: %s", exc)
    finally:
        conn.close()


# ── CLI wiring ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube transcript scraper — scrape → clean → ingest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scrape
    sp = sub.add_parser("scrape", help="Download raw transcripts to ./output")
    sp.add_argument("url_or_file", help="YouTube URL or .txt file of URLs")
    sp.add_argument("--output",   default=config.LOCAL_OUTPUT_DIR)
    sp.add_argument("--lang",     default=config.DEFAULT_LANG)
    sp.add_argument("--delay",    type=float, default=config.DELAY_BETWEEN_REQUESTS)
    sp.add_argument("--no-json",  action="store_true", help="Skip per-video .json segment files")
    sp.add_argument("--no-jsonl", action="store_true", help="Skip dataset.jsonl")
    sp.add_argument("--no-csv",   action="store_true", help="Skip index.csv")

    # clean
    cp = sub.add_parser("clean", help="Clean raw transcripts → blob storage for review")
    cp.add_argument("--raw-dir",  default=config.LOCAL_OUTPUT_DIR)
    cp.add_argument("--blob-dir", default=config.BLOB_OUTPUT_DIR)

    # enrich
    ep = sub.add_parser("enrich", help="Background LLM enrichment of cleaned videos (resumable)")
    ep.add_argument("--raw-dir",  default=config.LOCAL_OUTPUT_DIR)
    ep.add_argument("--blob-dir", default=config.BLOB_OUTPUT_DIR)
    ep.add_argument("--limit", type=int, default=0, help="Max videos to enrich this run (0 = all pending)")
    ep.add_argument("--loop",  action="store_true", help="Keep polling for new work instead of exiting")
    ep.add_argument("--poll",  type=int, default=60, help="Seconds between polls in --loop mode")

    # ingest
    ip = sub.add_parser("ingest", help="Copy approved files from blob storage → /srv/dbdata")
    ip.add_argument("--blob-dir",  default=config.BLOB_OUTPUT_DIR)
    ip.add_argument("--clean-dir", default=config.CLEAN_OUTPUT_DIR)

    # setup-db
    sub.add_parser("setup-db", help="Create database and apply schema (reads DATABASE_URL from .env)")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    {
        "scrape":    cmd_scrape,
        "clean":     cmd_clean,
        "enrich":    cmd_enrich,
        "ingest":    cmd_ingest,
        "setup-db":  cmd_setup_db,
    }[args.command](args)


if __name__ == "__main__":
    main()
