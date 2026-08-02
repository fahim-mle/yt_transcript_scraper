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
import datetime as dt
import json
import logging
import os
import re
import shutil
import time
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
load_dotenv()

import config
from database import db
from scraper import cleaner, formatter, llm_processor, manual, resolver, transcript

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


def _find_staged_record(output_dir: str, video_id: str) -> dict | None:
    """Return the existing aggregate record for a video, if one is staged."""
    path = os.path.join(output_dir, "dataset.jsonl")
    if not os.path.exists(path):
        return None

    with open(path, encoding="utf-8") as f:
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

    with open(jsonl_path, encoding="utf-8") as f:
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

    # Both persisted metadata and the knowledge document are required before
    # the row leaves the retry queue.
    channel_dir, blob_path = _blob_path_for(blob_dir, meta)
    os.makedirs(channel_dir, exist_ok=True)
    with open(blob_path, "w", encoding="utf-8") as f:
        f.write(formatter.to_knowledge_doc(meta, cleaned_text, enrichment))

    db.set_enrichment_status(video_id, "done")
    return "done"


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
        logger.error("Ollama unreachable at %s — start it (`ollama serve`) and retry.",
                     config.OLLAMA_HOST)
        return False

    logger.info("Enrich worker — model: %s%s", config.OLLAMA_MODEL,
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

        for video_id in pending:
            rec = index.get(video_id)
            if rec is None:
                logger.warning("No dataset.jsonl record for %s — marking failed.", video_id)
                db.set_enrichment_status(video_id, "failed")
                failures += 1
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
            if outcome in {"failed", "error"}:
                failures += 1
            processed += 1
            if args.limit and processed >= args.limit:
                logger.info("Reached --limit %d.", args.limit)
                logger.info("Enrich worker done. %d processed.", processed)
                return failures == 0

        if not args.loop:
            break

    logger.info("Enrich worker done. %d processed.", processed)
    return failures == 0


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
        logger.error("Ollama unreachable at %s — start it (`ollama serve`) and retry.",
                     config.OLLAMA_HOST)
        return

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else [config.OLLAMA_MODEL])

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

    original_model = config.OLLAMA_MODEL
    rows = []
    try:
        for model in models:
            config.OLLAMA_MODEL = model
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
        config.OLLAMA_MODEL = original_model

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

    copied, skipped, errors = 0, 0, 0
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
                        destination_video_id = _extract_video_id(f.read())
                    if destination_video_id != video_id:
                        raise ValueError(
                            f"Destination identity mismatch at {dst}: "
                            f"source={video_id}, destination={destination_video_id or 'no video id'}"
                        )
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
                    if current.get("clean_path") != dst:
                        updates["clean_path"] = dst
                    if updates:
                        db.update_video(video_id, updates)
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", src, exc)
                errors += 1

    logger.info(
        "Ingest done. %d copied to /srv/dbdata, %d already existed, %d errors.",
        copied, skipped, errors,
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

    # setup-db
    sub.add_parser("setup-db", help="Create database and apply schema (reads DATABASE_URL from .env)")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = {
        "scrape":    cmd_scrape,
        "manual":    cmd_manual,
        "clean":     cmd_clean,
        "enrich":    cmd_enrich,
        "benchmark": cmd_benchmark,
        "ingest":    cmd_ingest,
        "setup-db":  cmd_setup_db,
    }[args.command]
    if command(args) is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
