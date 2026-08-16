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
import concurrent.futures
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import config  # loads .env on import
from database import db
from scraper import (
    cleaner, formatter, llm_client, llm_processor, manual, resolver, rewriter, transcript,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_CSV_COLUMNS = ["video_id", "title", "channel", "published", "url", "word_count", "md_path"]

# ── Aggregate file lock ───────────────────────────────────────────────────
# dataset.jsonl records reach ~800 KB — far above PIPE_BUF — so an append is
# not atomic against a concurrent reader, which can observe a torn line. The
# web UI runs a manual add (writer) alongside a long enrich (reader), and the
# CLI can run beside the server, so this is a *file* lock: it holds across
# processes, not just threads.
#
# flock() is per open-file-description: never nest these calls, or a shared
# holder waiting on the exclusive lock deadlocks against itself.

_AGGREGATE_LOCK_FILE = ".dataset.lock"


@contextmanager
def aggregate_lock(output_dir: str, *, exclusive: bool):
    """Serialize dataset.jsonl / index.csv access across threads and processes."""
    os.makedirs(output_dir, exist_ok=True)
    fd = os.open(
        os.path.join(output_dir, _AGGREGATE_LOCK_FILE),
        os.O_RDWR | os.O_CREAT,
        0o644,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)  # releases the lock



# ── Shared helpers ────────────────────────────────────────────────────────

def _output_paths(base_dir: str, meta: dict) -> tuple[str, str]:
    channel_dir = os.path.join(base_dir, formatter.sanitize_filename(meta["channel"] or "unknown_channel"))
    stem = formatter.sanitize_filename(meta["title"] or meta["video_id"])
    return os.path.join(channel_dir, f"{stem}.md"), os.path.join(channel_dir, f"{stem}.json")


def _write_local_aggregate(records: list[dict], output_dir: str, no_jsonl: bool, no_csv: bool) -> None:
    if not records:
        return
    with aggregate_lock(output_dir, exclusive=True):
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


def _find_staged_record(output_dir: str, video_id: str) -> dict | None:
    """Return the existing aggregate record for a video, if one is staged."""
    path = os.path.join(output_dir, "dataset.jsonl")
    if not os.path.exists(path):
        return None

    with aggregate_lock(output_dir, exclusive=False), open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("video_id") == video_id:
                return record
    return None




# ── scrape ────────────────────────────────────────────────────────────────

def _scrape_video(meta: dict, output_dir: str, lang: str, save_json: bool) -> dict:
    """Fetch and write one video. Raises transcript.TranscriptError on failure."""
    md_path, json_path = _output_paths(output_dir, meta)

    video_id = meta["video_id"]
    logger.info("Fetching: %s — %s", video_id, meta.get("title", ""))

    segments = transcript.fetch(video_id, lang=lang)

    os.makedirs(os.path.dirname(md_path), exist_ok=True)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(formatter.to_markdown(meta, segments))

    if save_json:
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_json(segments))

    logger.info("Saved raw: %s", md_path)
    return formatter.to_jsonl_record(meta, segments, md_path)


def cmd_scrape(args: argparse.Namespace) -> bool:
    output_dir = args.output

    logger.info("Resolving: %s", args.url_or_file)
    videos = resolver.resolve(args.url_or_file)
    if not videos:
        logger.error("No videos found.")
        return False

    logger.info("Found %d video(s). Raw output → %s", len(videos), output_dir)
    if not _db_available():
        logger.warning("DATABASE_URL not set — skipping PostgreSQL.")

    # Pacing lives in the transcript module now — it throttles per request, not
    # per video, so language fallbacks and retries are counted honestly.
    transcript.set_delay(getattr(args, "delay", config.DELAY_BETWEEN_REQUESTS))

    records, failures, success, skipped = [], [], 0, 0
    consecutive_blocks = 0
    aborted = False

    for meta in videos:
        if os.path.exists(_output_paths(output_dir, meta)[0]):
            skipped += 1
            logger.info("Already exists, skipping: %s", meta.get("title", meta["video_id"]))
            continue

        try:
            record = _scrape_video(meta, output_dir, args.lang, not args.no_json)
        except transcript.TranscriptError as exc:
            failures.append({
                "video_id": meta["video_id"],
                "title":    meta.get("title", ""),
                "reason":   exc.reason,
                "detail":   exc.detail,
                "transient": exc.transient,
                "at":       dt.datetime.now().isoformat(timespec="seconds"),
            })
            logger.warning("Skipping %s — %s.", meta["video_id"], exc.reason)

            # Only blocks trip the breaker. A run of videos that simply have no
            # transcript is normal; a run of blocks means YouTube is refusing us
            # and every further request digs the hole deeper.
            if isinstance(exc, transcript.TranscriptBlocked):
                consecutive_blocks += 1
                if consecutive_blocks >= config.BLOCK_ABORT_THRESHOLD:
                    logger.error(
                        "Aborting: %d consecutive videos blocked. Wait before "
                        "retrying — re-running resumes where this stopped.",
                        consecutive_blocks,
                    )
                    aborted = True
                    break
            else:
                consecutive_blocks = 0
            continue

        consecutive_blocks = 0
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
    _report_failures(failures, output_dir)

    logger.info("Done. %d saved, %d skipped, %d failed.%s",
                success, skipped, len(failures), " RUN ABORTED." if aborted else "")
    return not aborted and (success + skipped > 0)


# ── manual paste ──────────────────────────────────────────────────────────

_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
}


def _video_id_from(source: str) -> str:
    """Extract an exact video ID from supported YouTube URLs or a bare ID."""
    source = (source or "").strip()
    if _YOUTUBE_VIDEO_ID_RE.fullmatch(source):
        return source

    candidate = ""
    parsed = urlparse(source if "://" in source else f"https://{source}")
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            candidate = parts[1]

    if _YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    raise ValueError(f"Could not find a valid YouTube video id in {source!r}")


def add_manual(
    source: str,
    transcript_text: str,
    description: str = "",
    title: str = "",
    channel: str = "",
    published: str = "",
    output_dir: str = config.LOCAL_OUTPUT_DIR,
    save_json: bool = config.SAVE_JSON,
    fetch_metadata: bool = True,
) -> dict:
    """
    Ingest a hand-pasted transcript as if it had been scraped.

    Writes the same raw .md/.json and dataset.jsonl record the scraper produces,
    so clean → enrich → ingest run unchanged afterwards.

    Metadata is *not* blocked by YouTube even when transcripts are, so title,
    channel and chapters are fetched with yt-dlp when possible; anything passed
    in explicitly wins, and the whole lookup is best-effort.
    """
    video_id = _video_id_from(source)
    existing = _find_staged_record(output_dir, video_id)
    if existing is not None:
        location = existing.get("md_path") or video_id
        raise FileExistsError(f"Already staged: {location}")

    segments = manual.parse_transcript(transcript_text)

    meta = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "", "channel": "", "channel_id": "",
        "published": "", "description": "", "chapters": [],
    }

    if fetch_metadata:
        try:
            fetched = resolver.resolve(meta["url"])
            if fetched:
                meta.update(fetched[0])
                logger.info("Fetched metadata: %s — %s",
                            meta.get("channel", ""), meta.get("title", ""))
        except Exception as exc:
            logger.warning("Metadata lookup failed (%s) — using what you provided.", exc)

    # Explicit values win over anything fetched.
    for key, val in (("title", title), ("channel", channel),
                     ("published", published), ("description", description)):
        if val and val.strip():
            meta[key] = val.strip()

    if meta["published"]:
        published_text = str(meta["published"]).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_text):
            raise ValueError("published date must use YYYY-MM-DD")
        try:
            meta["published"] = dt.date.fromisoformat(published_text).isoformat()
        except ValueError:
            raise ValueError("published date must be a real date in YYYY-MM-DD format") from None

    if not meta["title"]:
        meta["title"] = video_id
    if not meta["channel"]:
        meta["channel"] = "unknown_channel"

    md_path, json_path = _output_paths(output_dir, meta)
    if os.path.exists(md_path):
        raise FileExistsError(f"Already scraped: {md_path}")

    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(formatter.to_markdown(meta, segments))
    if save_json:
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_json(segments))

    record = formatter.to_jsonl_record(meta, segments, md_path)
    logger.info("Saved raw: %s (%d words)", md_path, record["word_count"])

    _write_local_aggregate([record], output_dir, no_jsonl=False, no_csv=False)

    if _db_available():
        try:
            db.upsert_video({
                **meta,
                "word_count": record["word_count"],
                "raw_path":   record["md_path"],
                "status":     "raw",
            })
        except Exception as exc:
            logger.warning("DB upsert failed for %s: %s", video_id, exc)

    logger.info("Manual entry complete — run 'clean' next.")
    return record


def cmd_manual(args: argparse.Namespace) -> bool:
    transcript_text = _read_arg_or_file(args.transcript, args.transcript_file)
    description = _read_arg_or_file(args.description, args.description_file)

    try:
        add_manual(
            args.url,
            transcript_text,
            description=description,
            title=args.title,
            channel=args.channel,
            published=args.published,
            output_dir=args.output,
            save_json=not args.no_json,
            fetch_metadata=not args.no_metadata,
        )
        return True
    except (ValueError, FileExistsError) as exc:
        logger.error("%s", exc)
        return False


def _read_arg_or_file(inline: str, path: str) -> str:
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return inline or ""


def _report_failures(failures: list[dict], output_dir: str) -> None:
    """
    Summarise failures by cause and append them to scrape_failures.jsonl, so a
    failed video is a record you can act on rather than a number in a log line.
    Transient ones are picked up automatically on the next run (scrape skips
    videos whose .md already exists), permanent ones never need retrying.
    """
    if not failures:
        return

    by_reason: dict[str, int] = {}
    for f in failures:
        by_reason[f["reason"]] = by_reason.get(f["reason"], 0) + 1
    logger.warning("Failures by cause: %s",
                   ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))

    retryable = [f for f in failures if f["transient"]]
    if retryable:
        logger.warning("%d video(s) failed transiently — re-run the same command "
                       "later to pick them up.", len(retryable))

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "scrape_failures.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for rec in failures:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logger.info("Wrote %d failure record(s) to %s", len(failures), path)


# ── shared helpers for clean / enrich ─────────────────────────────────────

def _meta_from_record(rec: dict) -> dict:
    return {k: rec.get(k, "") for k in
            ("video_id", "url", "title", "channel", "published", "description",
             "created_at")}


def _blob_path_for(blob_dir: str, meta: dict) -> tuple[str, str]:
    channel_dir = os.path.join(
        blob_dir, formatter.sanitize_filename(meta["channel"] or "unknown_channel"))
    stem = formatter.sanitize_filename(meta["title"] or meta["video_id"])
    return channel_dir, os.path.join(channel_dir, f"{stem}.md")


_TRANSCRIPT_SUFFIX = ".transcript.md"


def _transcript_path(blob_path: str) -> str:
    """
    Verbatim companion beside the article: <stem>.transcript.md.

    The article is LLM-generated prose; this file is the unmodified cleaned
    transcript it was derived from, kept as the citation and embedding anchor.
    """
    return blob_path[: -len(".md")] + _TRANSCRIPT_SUFFIX


def _is_transcript_companion(path: str) -> bool:
    return path.endswith(_TRANSCRIPT_SUFFIX)


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

def _persist_clean_row(
    meta: dict,
    cleaned_text: str,
    *,
    requeue: bool,
) -> tuple[bool, bool]:
    """Synchronize a clean blob with PostgreSQL; return (success, queued)."""
    video_id = meta["video_id"]
    wc = len(cleaned_text.split())
    try:
        row = db.get_video(video_id)
        if row is None:
            db.upsert_video({**meta, "word_count": wc, "status": "raw"})
            row = {}
        if row.get("status") in {None, "raw"}:
            db.update_video(video_id, {"status": "cleaned", "word_count": wc})

        enrichment_status = row.get("enrichment_status")
        if requeue or enrichment_status not in {"pending", "failed", "done"}:
            if enrichment_status != "pending":
                db.set_enrichment_status(video_id, "pending")
            return True, True
        return True, enrichment_status in {"pending", "failed"}
    except Exception as exc:
        logger.warning("DB update failed for %s: %s", video_id, exc)
        return False, False


def cmd_clean(args: argparse.Namespace) -> bool:
    """
    Reads raw records from dataset.jsonl, applies the deterministic cleaning
    pipeline, and writes structured .md files to blob storage for review.

    This stage is fast and does NOT call the LLM — enrichment is a separate,
    resumable background job ('enrich'). Cleaned videos are queued for it by
    setting enrichment_status = 'pending'.
    """
    raw_dir = args.raw_dir
    blob_dir = args.blob_dir
    jsonl_path = os.path.join(raw_dir, "dataset.jsonl")

    if not os.path.exists(jsonl_path):
        logger.error("No dataset.jsonl at %s — run 'scrape' first.", jsonl_path)
        return False

    db_available = _db_available()
    if not db_available:
        logger.warning("DATABASE_URL not set — skipping PostgreSQL status updates.")

    logger.info("Reading from %s", jsonl_path)
    logger.info("Cleaned output → %s  (blob storage for review)", blob_dir)

    with aggregate_lock(raw_dir, exclusive=False), open(jsonl_path, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    saved, skipped, filtered, queued, errors = 0, 0, 0, 0, 0
    for rec in records:
        meta = _meta_from_record(rec)
        chapters = rec.get("chapters") or []
        channel_dir, blob_path = _blob_path_for(blob_dir, meta)
        blob_exists = os.path.exists(blob_path)

        if blob_exists:
            try:
                with open(blob_path, encoding="utf-8") as f:
                    existing_video_id = _extract_video_id(f.read())
            except OSError as exc:
                logger.error("Could not read existing blob %s: %s", blob_path, exc)
                errors += 1
                continue
            if existing_video_id != meta["video_id"]:
                logger.error(
                    "Blob identity mismatch at %s: expected %s, found %s",
                    blob_path, meta["video_id"], existing_video_id or "no video id",
                )
                errors += 1
                continue
            if not db_available:
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

        if blob_exists:
            skipped += 1
        else:
            os.makedirs(channel_dir, exist_ok=True)
            with open(blob_path, "w", encoding="utf-8") as f:
                f.write(formatter.to_clean_markdown(meta, cleaned_text))
            logger.info("Cleaned → %s", blob_path)
            saved += 1

        # The verbatim companion is cheap and is the only ground truth once the
        # rewrite stage replaces blob_path with generated prose. Written even
        # when the blob already exists, so older runs gain one on re-clean.
        transcript_path = _transcript_path(blob_path)
        if not os.path.exists(transcript_path):
            os.makedirs(channel_dir, exist_ok=True)
            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(formatter.to_transcript_doc(meta, cleaned_text))

        if db_available:
            persisted, is_queued = _persist_clean_row(
                meta, cleaned_text, requeue=not blob_exists,
            )
            if not persisted:
                errors += 1
            elif is_queued:
                queued += 1

    logger.info(
        "Clean done. %d written to blob, %d already existed, %d filtered.",
        saved, skipped, filtered,
    )
    if queued:
        logger.info("%d video(s) queued for enrichment.", queued)
    return errors == 0


# ── enrich (background worker) ─────────────────────────────────────────────

def _load_records_index(raw_dir: str) -> dict:
    """Map video_id → dataset.jsonl record, for fast lookup by the worker."""
    jsonl_path = os.path.join(raw_dir, "dataset.jsonl")
    index: dict = {}
    if os.path.exists(jsonl_path):
        with aggregate_lock(raw_dir, exclusive=False), open(jsonl_path, encoding="utf-8") as f:
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

    # Prefer the rewritten article when the rewrite stage has already run: it is
    # better prose than the caption text, so it yields better metadata — and
    # reading it back means enrichment never clobbers it.
    channel_dir, blob_path = _blob_path_for(blob_dir, meta)
    article_body = _read_article_body(blob_path)
    source_text = article_body or cleaned_text

    result = llm_processor.enrich(meta["title"], source_text, chapters)

    try:
        db.record_processing_run(
            video_id, result.model,
            "success" if result.ok else "failed",
            result.error, result.duration_ms,
            stage="enrich", provider=result.provider, usage=result.usage.as_dict(),
        )
    except Exception as exc:
        logger.warning("Run log failed for %s: %s", video_id, exc)

    if not result.ok:
        db.set_enrichment_status(video_id, "failed")
        return "failed"

    enrichment = result.enrichment.model_dump()
    db.update_video(video_id, {
        "summary":      enrichment.get("summary"),
        "key_concepts": enrichment.get("key_concepts") or [],
        "domains":      enrichment.get("domains") or [],
        "difficulty":   enrichment.get("difficulty"),
        "content_kind": enrichment.get("content_kind"),
        # Provenance: which model produced this metadata, and when.
        "enrichment_model":    result.model,
        "enrichment_provider": result.provider,
        "enriched_at":         dt.datetime.now().astimezone(),
    })
    db.upsert_sections(
        video_id,
        _sections_with_timestamps(enrichment.get("sections") or [], chapters),
    )

    # Both persisted metadata and the output document are required before the
    # row leaves the retry queue.
    os.makedirs(channel_dir, exist_ok=True)
    with open(blob_path, "w", encoding="utf-8") as f:
        if article_body:
            f.write(formatter.to_article_doc(meta, article_body, enrichment))
        else:
            f.write(formatter.to_knowledge_doc(meta, cleaned_text, enrichment))

    db.set_enrichment_status(video_id, "done")
    return "done"


def _read_article_body(blob_path: str) -> str | None:
    """Recover rewritten prose from an existing blob, if the rewrite stage ran."""
    try:
        with open(blob_path, encoding="utf-8") as f:
            return formatter.extract_article_body(f.read())
    except OSError:
        return None


# ── rewrite (background worker) ────────────────────────────────────────────

def _resolve_workers(args: argparse.Namespace) -> int:
    """
    How many videos to process concurrently.

    Auto (0) means 1 for a local model and 4 for a hosted one: parallel calls to
    Ollama contend for one GPU and gain nothing, while a hosted worker spends
    almost all its time waiting on the network.
    """
    requested = getattr(args, "workers", None)
    if requested is None:
        requested = config.LLM_WORKERS
    if requested and requested > 0:
        return requested
    return 1 if config.LLM_PROVIDER == "ollama" else 4


# Set by server.py. Log records are routed to a job by thread id, so a worker
# thread this module spawns is invisible to the UI until it adopts the job
# binding of the thread that started it. Outside the web UI this stays None and
# `_worker_context` is a no-op.
worker_thread_hook = None


def _worker_context():
    """Context-manager factory each pool worker enters, so its logs reach the UI."""
    if worker_thread_hook is None:
        return contextlib.nullcontext
    return worker_thread_hook()


def _run_pool(video_ids: list[str], work, workers: int) -> list[tuple[str, str]]:
    """
    Run `work(video_id)` over the queue, returning (video_id, outcome) pairs.

    Sequential when workers == 1 so the single-worker path keeps its exact
    previous behaviour and stays trivially debuggable.
    """
    adopt = _worker_context()

    def guarded(video_id: str) -> tuple[str, str]:
        with adopt():
            return video_id, work(video_id)

    if workers <= 1:
        return [guarded(vid) for vid in video_ids]

    results: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="llm") as pool:
        for future in concurrent.futures.as_completed(
                [pool.submit(guarded, vid) for vid in video_ids]):
            results.append(future.result())
    return results


def _checkpoint_path(raw_dir: str, video_id: str) -> str:
    """Where a partially finished rewrite parks its progress."""
    return os.path.join(raw_dir, ".rewrite_progress", f"{video_id}.json")


def _record_article_provenance(video_id: str, result, source_text: str,
                               transcript_path: str) -> None:
    """
    Record who generated this article and how faithful it is.

    The article is generated prose, so "which model wrote this" is part of the
    record, not an implementation detail — it is what lets you re-run only the
    documents produced by a model you stopped trusting. `content_hash` covers
    the article body alone, so it moves only when the prose actually changes and
    can drive embedding staleness independently of any timestamp.
    """
    source_words = len(source_text.split())
    article_words = len(result.article.split())
    try:
        db.update_video(video_id, {
            "article_model":           result.model,
            "article_provider":        result.provider,
            "article_prompt_version":  rewriter.PROMPT_VERSION,
            "article_generated_at":    dt.datetime.now().astimezone(),
            "article_chunks":          result.chunks,
            "article_fallback_chunks": result.fallbacks,
            "article_word_count":      article_words,
            "coverage_ratio":          (article_words / source_words) if source_words else None,
            "transcript_path":         transcript_path,
            "content_hash":            hashlib.sha256(
                result.article.encode("utf-8")).hexdigest(),
            # New prose invalidates any vector built from the old text.
            "embedding_status":        "pending",
        })
    except Exception as exc:
        logger.warning("Could not record article provenance for %s: %s", video_id, exc)


def _rewrite_one(video_id: str, rec: dict, blob_dir: str, raw_dir: str) -> str:
    """
    Rewrite one video's transcript into an article and write it to blob storage.

    Writes the verbatim companion first, so the ground truth is on disk before
    any generated prose replaces the readable file. Returns an outcome string.

    Chunk progress is checkpointed under `raw_dir`, so an interrupted rewrite
    resumes where it stopped instead of regenerating hours of prose.
    """
    meta = _meta_from_record(rec)
    chapters = rec.get("chapters") or []
    cleaned_text = cleaner.clean(rec.get("transcript_segments", []), chapters=chapters)
    if cleaned_text is None:
        db.set_rewrite_status(video_id, "done")  # nothing to rewrite
        return "no-prose"

    channel_dir, blob_path = _blob_path_for(blob_dir, meta)
    os.makedirs(channel_dir, exist_ok=True)

    transcript_path = _transcript_path(blob_path)
    if not os.path.exists(transcript_path):
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(formatter.to_transcript_doc(meta, cleaned_text))

    result = rewriter.rewrite(cleaned_text, meta["title"],
                              checkpoint_path=_checkpoint_path(raw_dir, video_id),
                              label=video_id)

    try:
        db.record_processing_run(
            video_id, result.model,
            "success" if result.ok else "failed",
            result.error, result.duration_ms,
            stage="rewrite", provider=result.provider, usage=result.usage.as_dict(),
        )
    except Exception as exc:
        logger.warning("Run log failed for %s: %s", video_id, exc)

    if not result.ok or result.article is None:
        # The checkpoint is deliberately left in place: the next attempt should
        # resume this video, not regenerate the chunks that already succeeded.
        db.set_rewrite_status(video_id, "failed")
        return "failed"

    # Preserve any enrichment already on the file so re-running rewrite after
    # enrich doesn't strip the metadata back off.
    with open(blob_path, "w", encoding="utf-8") as f:
        f.write(formatter.to_article_doc(meta, result.article))

    _record_article_provenance(video_id, result, cleaned_text, transcript_path)

    rewriter.clear_checkpoint(_checkpoint_path(raw_dir, video_id))
    db.set_rewrite_status(video_id, "done")
    # A new article invalidates metadata derived from the old text.
    db.set_enrichment_status(video_id, "pending")
    return "done"


def cmd_rewrite(args: argparse.Namespace) -> bool:
    """
    Drain the rewrite queue: turn cleaned transcripts into readable articles.

    This is by far the slowest stage — it generates roughly one token per token
    of transcript — so it is a separate resumable worker like `enrich`. Each
    video is marked done as it finishes, making interruption safe.
    """
    if not _db_available():
        logger.error("rewrite needs DATABASE_URL — it reads the queue from PostgreSQL.")
        return False
    if not config.REWRITE_ENABLED:
        logger.error("REWRITE_ENABLED=0 — nothing to do. Set it to 1 in .env to rewrite.")
        return False
    if not rewriter.is_available():
        logger.error("LLM provider is not reachable (%s) — check it and retry.",
                     llm_client.describe())
        return False

    workers = _resolve_workers(args)
    logger.info("Rewrite worker — %s, model: %s, ~%d words/chunk, %d video(s) at a time",
                llm_client.describe(), config.REWRITE_MODEL,
                config.REWRITE_CHUNK_WORDS, workers)

    processed, failures = 0, 0
    while True:
        records = _load_records_index(args.raw_dir)
        pending = db.list_pending_rewrite(limit=args.limit or None)

        if not pending:
            if not args.loop:
                break
            time.sleep(args.poll)
            continue

        logger.info("%d video(s) queued for rewrite.", len(pending))

        def rewrite_one(video_id: str) -> str:
            rec = records.get(video_id)
            if rec is None:
                logger.warning("No staged record for %s — marking failed.", video_id)
                db.set_rewrite_status(video_id, "failed")
                return "failed"
            try:
                return _rewrite_one(video_id, rec, args.blob_dir, args.raw_dir)
            except Exception as exc:
                logger.error("Rewrite failed for %s: %s", video_id, exc)
                db.set_rewrite_status(video_id, "failed")
                return "failed"

        for _, outcome in _run_pool(pending, rewrite_one, workers):
            processed += 1
            if outcome == "failed":
                failures += 1

        if args.limit or not args.loop:
            break

    logger.info("Rewrite worker done. %d processed, %d failed.", processed, failures)
    if failures:
        logger.warning("%d video(s) left queued for retry.", failures)
    return True


def _enrich_summary(processed: int, failures: int) -> bool:
    """
    Report a finished run. Individual video failures are queue state, not run
    failure: those rows stay 'failed' in PostgreSQL and the next run retries
    them. Returning False here aborted every downstream run-all stage, so with
    a >50% per-video timeout rate ingest effectively never ran.
    """
    logger.info("Enrich worker done. %d processed, %d failed.", processed, failures)
    if failures:
        logger.warning(
            "%d video(s) left queued for retry — run enrich again to pick them up.",
            failures,
        )
    return True


def cmd_enrich(args: argparse.Namespace) -> bool:
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
        return False
    if not config.LLM_ENABLED:
        logger.error("LLM_ENABLED=0 — nothing to do. Set it to 1 in .env to enrich.")
        return False
    if not llm_processor.is_available():
        logger.error("LLM provider unreachable (%s) — check it and retry.",
                     llm_client.describe())
        return False

    workers = _resolve_workers(args)
    logger.info("Enrich worker — %s, model: %s, %d video(s) at a time%s",
                llm_client.describe(), config.ENRICH_MODEL, workers,
                "  (loop mode)" if args.loop else "")
    processed = 0
    failures = 0
    while True:
        index = _load_records_index(args.raw_dir)
        pending = db.list_pending_enrichment(limit=args.limit or None)

        if not pending:
            if args.loop:
                logger.info("Queue empty — sleeping %ds.", args.poll)
                time.sleep(args.poll)
                continue
            break

        def enrich_one(video_id: str) -> str:
            rec = index.get(video_id)
            if rec is None:
                logger.warning("No dataset.jsonl record for %s — marking failed.", video_id)
                db.set_enrichment_status(video_id, "failed")
                return "failed"
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
            logger.info("  %s → %s", video_id, outcome)
            return outcome

        for _, outcome in _run_pool(pending, enrich_one, workers):
            if outcome in {"failed", "error"}:
                failures += 1
            processed += 1

        if not args.loop:
            break

    return _enrich_summary(processed, failures)


# ── benchmark ─────────────────────────────────────────────────────────────

def _tokens_per_sec(stats: list[dict]) -> float | None:
    """Generation throughput across all Ollama calls in one enrich run."""
    tokens = sum(s.get("eval_count", 0) for s in stats)
    nanos = sum(s.get("eval_duration", 0) for s in stats)
    if tokens <= 0 or nanos <= 0:
        return None
    return tokens / (nanos / 1e9)


def cmd_benchmark(args: argparse.Namespace) -> None:
    """
    Run the SAME transcript through several models and compare speed + output
    side by side, so you can pick a model empirically. Uses the real enrichment
    path (schema-constrained JSON + key_concepts fallback), so what you see is
    exactly what the pipeline would store.

    Nothing is written to the DB or blob storage — this is read-only measurement.
    """
    if not llm_processor.is_available():
        logger.error("LLM provider unreachable (%s) — check it and retry.",
                     llm_client.describe())
        return

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else [config.ENRICH_MODEL])

    index = _load_records_index(args.raw_dir)
    if not index:
        logger.error("No dataset.jsonl records in %s — scrape something first.", args.raw_dir)
        return

    if args.video:
        rec = index.get(args.video)
        if rec is None:
            logger.error("video_id %s not found in %s/dataset.jsonl.", args.video, args.raw_dir)
            return
    else:
        rec = next(iter(index.values()))

    meta = _meta_from_record(rec)
    chapters = rec.get("chapters") or []
    cleaned_text = cleaner.clean(rec.get("transcript_segments", []), chapters=chapters)
    if cleaned_text is None:
        logger.error("Couldn't clean %s — no prose to benchmark.", meta["video_id"])
        return

    words = cleaned_text.split()
    if args.words and len(words) > args.words:
        cleaned_text = " ".join(words[:args.words])
    input_words = len(cleaned_text.split())

    print(f"\nBenchmark input: {meta['title']!r} ({meta['video_id']}) — "
          f"{input_words} words fed to each model")
    print(f"Models: {', '.join(models)}\n")

    original_model = config.ENRICH_MODEL
    rows = []
    try:
        for model in models:
            config.ENRICH_MODEL = model
            logger.info("Benchmarking %s …", model)
            llm_processor.reset_call_stats()
            result = llm_processor.enrich(meta["title"], cleaned_text, chapters)
            tps = _tokens_per_sec(llm_processor.get_call_stats())
            e = result.enrichment
            rows.append({
                "model": model,
                "ok": result.ok,
                "wall_s": result.duration_ms / 1000,
                "tps": tps,
                "concepts": len(e.key_concepts) if e else 0,
                "sections": len(e.sections) if e else 0,
                "difficulty": e.difficulty if e else "-",
                "content_kind": e.content_kind if e else "-",
                "error": result.error,
                "enrichment": e,
            })
    finally:
        config.ENRICH_MODEL = original_model

    # Summary table
    print(f"\n{'model':<26}{'ok':<5}{'wall(s)':<10}{'gen tok/s':<12}"
          f"{'concepts':<10}{'sections':<10}difficulty/kind")
    print("-" * 92)
    for r in rows:
        tps = f"{r['tps']:.1f}" if r["tps"] is not None else "n/a"
        print(f"{r['model']:<26}{('yes' if r['ok'] else 'NO'):<5}"
              f"{r['wall_s']:<10.1f}{tps:<12}{r['concepts']:<10}{r['sections']:<10}"
              f"{r['difficulty']}/{r['content_kind']}")

    # Per-model output so you can judge quality, not just speed
    for r in rows:
        print(f"\n─── {r['model']} ───")
        if not r["ok"]:
            print(f"  FAILED: {r['error']}")
            continue
        e = r["enrichment"]
        print(f"  summary:      {e.summary}")
        print(f"  key_concepts: {e.key_concepts}")
        print(f"  domains:      {e.domains}")
        print(f"  sections:     " + " | ".join(s.heading for s in e.sections))

    if args.out:
        payload = {
            "video_id": meta["video_id"],
            "title": meta["title"],
            "input_words": input_words,
            "results": [{k: v for k, v in r.items() if k != "enrichment"}
                        | {"enrichment": r["enrichment"].model_dump() if r["enrichment"] else None}
                        for r in rows],
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"\nSaved results to {args.out}")


# ── ingest ────────────────────────────────────────────────────────────────

_VIDEO_ID_RE = re.compile(r'url:\s+"https://www\.youtube\.com/watch\?v=([^"]+)"')


def _extract_video_id(md_content: str) -> str | None:
    m = _VIDEO_ID_RE.search(md_content)
    return m.group(1) if m else None


def cmd_ingest(args: argparse.Namespace) -> bool:
    """
    Copies reviewed .md files from blob storage to /srv/dbdata and marks
    them as ingested in PostgreSQL. Existing destination files are not copied
    again, but their database state is repaired when necessary.

    The destination is the reviewed tier, so a document regenerated upstream
    (rewrite, re-enrich) does NOT silently overwrite it. Drift is reported and
    `--republish` closes it deliberately.

    You can delete unwanted files from blob storage before running ingest —
    only what's in the blob dir gets ingested.
    """
    blob_dir = args.blob_dir
    clean_dir = args.clean_dir

    if not os.path.isdir(blob_dir):
        logger.error("Blob directory not found: %s", blob_dir)
        return False

    db_available = _db_available()
    if not db_available:
        logger.warning("DATABASE_URL not set — skipping PostgreSQL status updates.")

    logger.info("Ingesting from %s → %s", blob_dir, clean_dir)

    # Regenerating a document does not republish it over the reviewed copy
    # unless asked; see the drift branch below.
    republish = getattr(args, "republish", False)
    stale: list[str] = []

    copied, updated, skipped, errors = 0, 0, 0, 0
    for root, _, files in os.walk(blob_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue

            src = os.path.join(root, fname)
            rel = os.path.relpath(src, blob_dir)
            dst = os.path.join(clean_dir, rel)

            try:
                with open(src, encoding="utf-8") as f:
                    content = f.read()
                video_id = _extract_video_id(content)
                if not video_id:
                    raise ValueError(f"Could not extract video_id from {src}")

                if os.path.exists(dst):
                    with open(dst, encoding="utf-8") as f:
                        published = f.read()
                    destination_video_id = _extract_video_id(published)
                    if destination_video_id != video_id:
                        raise ValueError(
                            f"Destination identity mismatch at {dst}: "
                            f"source={video_id}, destination={destination_video_id or 'no video id'}"
                        )
                    # Same video, different bytes = the document was regenerated
                    # (re-clean, rewrite, re-enrich) and what is published is
                    # now behind blob storage.
                    #
                    # The destination is the *reviewed* tier, so a regenerated
                    # document does not get to overwrite it on its own — a hand
                    # -edited copy would be destroyed with no way to notice.
                    # But staying quiet was its own failure: re-running the
                    # pipeline through a better model updated blob storage,
                    # reported success, and changed nothing a reader would ever
                    # see. So the default now *reports* the drift, and
                    # --republish is the deliberate way to close it.
                    if published == content:
                        skipped += 1
                    elif republish:
                        shutil.copy2(src, dst)
                        logger.info("Republished → %s", dst)
                        updated += 1
                    else:
                        stale.append(dst)
                        skipped += 1
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    logger.info("Ingested → %s", dst)
                    copied += 1

                if db_available:
                    current = db.get_video(video_id)
                    if current is None:
                        raise KeyError(f"No video found with video_id={video_id!r}")
                    status = current.get("status")
                    if status not in {"raw", "cleaned", "ingested", "embedded"}:
                        raise ValueError(f"Cannot ingest video {video_id} from status {status!r}")
                    updates = {}
                    if status in {"raw", "cleaned"}:
                        updates["status"] = "ingested"
                    # Both the article and its verbatim companion carry the same
                    # video_id, so only the article may own clean_path — otherwise
                    # the column flip-flops with os.walk ordering.
                    if not _is_transcript_companion(src) and current.get("clean_path") != dst:
                        updates["clean_path"] = dst
                    if updates:
                        db.update_video(video_id, updates)
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", src, exc)
                errors += 1

    logger.info(
        "Ingest done. %d new, %d republished, %d unchanged, %d errors.",
        copied, updated, skipped, errors,
    )
    if stale:
        logger.warning(
            "%d published document(s) are BEHIND blob storage — regenerated but "
            "not republished, so %s still serves the older text.",
            len(stale), clean_dir,
        )
        for path in stale[:5]:
            logger.warning("  stale: %s", path)
        if len(stale) > 5:
            logger.warning("  … and %d more", len(stale) - 5)
        logger.warning(
            "Re-run with --republish to overwrite them. This replaces the "
            "reviewed copy, so any hand edits there are lost."
        )
    return errors == 0


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

def cmd_check_llm(args: argparse.Namespace) -> bool:
    """
    Verify the configured provider actually answers, before a long run commits
    to it. Sends two tiny requests — one plain, one schema-constrained — because
    those are the two shapes the pipeline depends on and structured output is
    the one that varies between OpenAI-compatible providers.
    """
    print(f"\nProvider : {llm_client.describe()}")
    print(f"Billing  : {config.LLM_BILLING_MODE}")
    print(f"Rewrite  : {config.REWRITE_MODEL}")
    print(f"Enrich   : {config.ENRICH_MODEL}")
    if config.LLM_PROVIDER != "ollama":
        key = config.LLM_API_KEY
        print(f"API key  : {'set (' + str(len(key)) + ' chars)' if key else 'MISSING'}")
        if not key:
            logger.error("LLM_API_KEY is empty — put your key in .env and retry.")
            return False

    ok = True
    for label, model in (("rewrite", config.REWRITE_MODEL), ("enrich", config.ENRICH_MODEL)):
        print(f"\n── {label}: {model} ──")

        plain = llm_client.chat(
            model=model, system="You are terse.",
            user="Reply with exactly: PONG", temperature=0.0, max_tokens=20, timeout=60,
        )
        if plain.ok:
            print(f"  text     ✓  {plain.text.strip()[:60]!r} "
                  f"({plain.duration_ms} ms, {plain.usage.total_tokens} tokens)")
        else:
            print(f"  text     ✗  {plain.error}")
            ok = False
            continue

        schema = {"type": "object",
                  "properties": {"colour": {"type": "string"}},
                  "required": ["colour"]}
        structured = llm_client.chat(
            model=model, system="You output JSON only.",
            user="Name one primary colour as {\"colour\": \"...\"}.",
            temperature=0.0, schema=schema, max_tokens=60, timeout=60,
        )
        if structured.ok:
            print(f"  json     ✓  {structured.data} ({structured.duration_ms} ms)")
        else:
            print(f"  json     ✗  {structured.error}")
            # Enrichment needs JSON; a rewrite model that cannot do it is fine.
            if label == "enrich":
                ok = False

    print("\n" + ("All checks passed." if ok else "Some checks failed — see above.") + "\n")
    return ok


def cmd_requeue(args: argparse.Namespace) -> bool:
    """
    Put blocked or failed videos back in a stage's queue.

    The counterpart to the attempt ceiling: the ceiling exists so one broken
    video cannot starve the queue behind it, and this is how the casualties come
    back once whatever broke them is fixed.
    """
    if not _db_available():
        logger.error("requeue needs DATABASE_URL — the queue lives in PostgreSQL.")
        return False
    moved = db.requeue(args.videos, stage=args.stage)
    if moved:
        logger.info("Requeued %d video(s) for %s.", moved, args.stage)
    else:
        logger.info("Nothing to requeue for %s.", args.stage)
    return True


def cmd_usage(args: argparse.Namespace) -> bool:
    """
    Report token usage, and cost where cost is a real number.

    Subscription runs show a zero cost because that is the truth: the marginal
    cost of the call was zero. Metered runs with no matching model_pricing row
    show as unpriced rather than as free — an unknown cost and a zero cost are
    different facts and collapsing them is how usage reporting starts lying.
    """
    if not _db_available():
        logger.error("usage needs DATABASE_URL.")
        return False

    rows = db.usage_summary(days=args.days)
    if not rows:
        logger.info("No LLM runs in the last %d days.", args.days)
        return True

    print(f"\nLLM usage — last {args.days} days\n")
    print(f"{'stage':<9}{'provider':<11}{'model':<20}{'runs':>6}{'in':>12}{'out':>12}"
          f"{'billing':>14}{'cost':>12}")
    print("-" * 96)
    for r in rows:
        if r["billing_mode"] == "subscription":
            cost = "included"
        elif r["cost_usd"] is None:
            cost = "unpriced"
        else:
            cost = f"${r['cost_usd']:.4f}"
        print(f"{r['stage'] or '-':<9}{r['provider'] or '-':<11}{r['model'][:19]:<20}"
              f"{r['runs']:>6}{r['prompt_tokens'] or 0:>12,}{r['completion_tokens'] or 0:>12,}"
              f"{r['billing_mode'] or '-':>14}{cost:>12}")

    unpriced = [r for r in rows
                if r["billing_mode"] != "subscription" and r["cost_usd"] is None]
    if unpriced:
        print("\nSome runs have no price on record. Add rates to price them:")
        print("  INSERT INTO model_pricing (provider, model, input_per_mtok, output_per_mtok)")
        print("  VALUES ('openai', 'glm-4.6', 0.60, 2.20);")
    print()
    return True


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

    # manual
    mp = sub.add_parser("manual", help="Add a hand-pasted transcript (for when YouTube blocks fetching)")
    mp.add_argument("url", help="YouTube URL or bare 11-character video id")
    mp.add_argument("--transcript", help="Transcript text (or use --transcript-file)")
    mp.add_argument("--transcript-file", help="Read transcript from a UTF-8 text file")
    mp.add_argument("--description", default="", help="Video description text")
    mp.add_argument("--description-file", help="Read description from a file")
    mp.add_argument("--title",     default="", help="Overrides the fetched title")
    mp.add_argument("--channel",   default="", help="Overrides the fetched channel")
    mp.add_argument("--published", default="", help="Overrides the fetched date (YYYY-MM-DD)")
    mp.add_argument("--output",    default=config.LOCAL_OUTPUT_DIR)
    mp.add_argument("--no-json",   action="store_true", help="Skip the per-video .json segment file")
    mp.add_argument("--no-metadata", action="store_true",
                    help="Skip the yt-dlp metadata lookup (offline / fully manual)")

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
    ep.add_argument("--workers", type=int, default=None,
                    help="Videos to process at once (0 = auto: 1 local, 4 hosted). Chunks within a video stay sequential.")

    # rewrite
    rp = sub.add_parser("rewrite", help="Rewrite cleaned transcripts into readable articles (resumable)")
    rp.add_argument("--raw-dir",  default=config.LOCAL_OUTPUT_DIR)
    rp.add_argument("--blob-dir", default=config.BLOB_OUTPUT_DIR)
    rp.add_argument("--limit", type=int, default=0, help="Max videos to rewrite this run (0 = all pending)")
    rp.add_argument("--loop",  action="store_true", help="Keep polling for new work instead of exiting")
    rp.add_argument("--poll",  type=int, default=60, help="Seconds between polls in --loop mode")
    rp.add_argument("--workers", type=int, default=None,
                    help="Videos to process at once (0 = auto: 1 local, 4 hosted). Chunks within a video stay sequential.")

    # benchmark
    bp = sub.add_parser("benchmark", help="Compare models on the same transcript (speed + output)")
    bp.add_argument("--models", help="Comma-separated Ollama model tags (default: OLLAMA_MODEL)")
    bp.add_argument("--video", help="video_id to test (default: first in dataset.jsonl)")
    bp.add_argument("--raw-dir", default=config.LOCAL_OUTPUT_DIR)
    bp.add_argument("--words", type=int, default=config.LLM_MAX_INPUT_WORDS,
                    help="Cap input words for a fair, bounded comparison (0 = no cap)")
    bp.add_argument("--out", help="Optional path to save full results as JSON")

    # ingest
    ip = sub.add_parser("ingest", help="Copy approved files from blob storage → /srv/dbdata")
    ip.add_argument("--blob-dir",  default=config.BLOB_OUTPUT_DIR)
    ip.add_argument("--clean-dir", default=config.CLEAN_OUTPUT_DIR)
    ip.add_argument("--republish", action="store_true",
                    help="Overwrite published documents that regeneration has left stale. "
                         "Destroys hand edits made to the reviewed copy.")

    # setup-db
    sub.add_parser("setup-db", help="Create database and apply schema (reads DATABASE_URL from .env)")

    # check-llm
    sub.add_parser("check-llm", help="Verify the configured LLM provider answers (2 tiny calls)")

    # requeue
    qp = sub.add_parser("requeue", help="Return blocked/failed videos to a stage's queue")
    qp.add_argument("--stage", choices=("rewrite", "enrichment"), required=True)
    qp.add_argument("--video", action="append", dest="videos", metavar="VIDEO_ID",
                    help="Requeue only this video (repeatable). Default: every blocked one.")

    # usage
    up = sub.add_parser("usage", help="Token usage and derived cost by model and stage")
    up.add_argument("--days", type=int, default=30, help="Look back this many days (default 30)")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = {
        "scrape":    cmd_scrape,
        "manual":    cmd_manual,
        "clean":     cmd_clean,
        "rewrite":   cmd_rewrite,
        "enrich":    cmd_enrich,
        "benchmark": cmd_benchmark,
        "ingest":    cmd_ingest,
        "setup-db":  cmd_setup_db,
        "requeue":   cmd_requeue,
        "usage":     cmd_usage,
        "check-llm": cmd_check_llm,
    }[args.command]
    if command(args) is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
