#!/usr/bin/env python3
"""
Web UI server for YouTube Transcript Scraper.

Run:
    uvicorn server:app --reload --port 8000
    # or
    python server.py

Then open: http://localhost:8000
"""

import argparse
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from typing import Callable, Generator

from dotenv import load_dotenv
load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
# Configure root logger before importing pipeline modules so that
# logging.basicConfig() calls in main.py become no-ops (already have handlers).

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(logging.StreamHandler())  # terminal output
_root.handlers[-1].setFormatter(_fmt)

# Thread ident → job id, set by the runner thread so every record it emits
# (including from the pipeline modules it calls) lands in that job's buffer.
_thread_job_map: dict[int, str] = {}
_tjm_lock = threading.Lock()


class _JobLogHandler(logging.Handler):
    """Routes log records into the owning job's buffer, keyed by thread ID."""
    def emit(self, record: logging.LogRecord) -> None:
        tid = threading.current_thread().ident
        with _tjm_lock:
            job_id = _thread_job_map.get(tid)
        if job_id is None:
            return
        job = _jobs.get(job_id)
        if job is not None:
            job.log(self.format(record))


_job_handler = _JobLogHandler()
_job_handler.setFormatter(_fmt)
_root.addHandler(_job_handler)

# Pipeline imports (safe now that logging is configured)
import config
import main as pipeline

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="YT Transcript Scraper", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)

# ── Job management ─────────────────────────────────────────────────────────────
#
# Jobs are admitted per *class*, not globally.
#
#   heavy — the pipeline stages. Mutually exclusive: they hand work down a
#           shared queue, and enrich alone averages ~6 minutes per video, so a
#           run-all holds this slot for hours.
#   light — a manual transcript add: two file writes, an append, one row
#           upsert. It used to queue behind that multi-hour run-all for no
#           reason. The single real collision, the dataset.jsonl append, is
#           serialized by pipeline.aggregate_lock instead of by a job slot.

HEAVY = "heavy"
LIGHT = "light"

_MAX_LOG_LINES = 2000   # per-job ring buffer; doubles as the SSE replay window
_MAX_JOBS = 20          # finished jobs retained so a reloaded page can reattach


def _offer(q: "queue.Queue", item, *, force: bool = False) -> None:
    """Non-blocking put. A slow reader loses old lines, never the terminal event."""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    if not force:
        return
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


class _Job:
    """A background pipeline run plus its replayable log buffer."""

    def __init__(self, job_id: str, label: str, klass: str) -> None:
        self.id = job_id
        self.label = label
        self.klass = klass
        self.status = "running"
        self.started = time.time()
        self._lock = threading.Lock()
        self._seq = 0
        self._lines: deque[tuple[int, str]] = deque(maxlen=_MAX_LOG_LINES)
        self._subscribers: set[queue.Queue] = set()

    # ── producer side (the job thread) ──

    def log(self, line: str) -> None:
        with self._lock:
            self._seq += 1
            item = (self._seq, line)
            self._lines.append(item)
            subscribers = list(self._subscribers)
        for q in subscribers:
            _offer(q, item)

    def finish(self, status: str) -> None:
        with self._lock:
            self.status = status
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for q in subscribers:
            _offer(q, ("done", status), force=True)

    # ── consumer side (SSE threads) ──

    def subscribe(self, after: int):
        """
        Atomically snapshot the log past `after` and register for live lines.
        Returns (queue, backlog, status). The queue is None for a finished job —
        the backlog is then everything the caller will ever receive.
        """
        with self._lock:
            backlog = [item for item in self._lines if item[0] > after]
            if self.status != "running":
                return None, backlog, self.status
            q: queue.Queue = queue.Queue(maxsize=_MAX_LOG_LINES + 8)
            self._subscribers.add(q)
            return q, backlog, self.status

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)


_jobs: dict[str, _Job] = {}         # job id → job, oldest first
_active_jobs: dict[str, _Job] = {}  # class → most recent job of that class
_jobs_lock = threading.Lock()


def _reap() -> None:
    """Drop the oldest finished jobs. Caller holds _jobs_lock."""
    for job_id in list(_jobs):
        if len(_jobs) <= _MAX_JOBS:
            return
        job = _jobs[job_id]
        if job.status == "running" or _active_jobs.get(job.klass) is job:
            continue
        del _jobs[job_id]


def _launch(label: str, fn: Callable, *args, klass: str = HEAVY) -> str | None:
    """Start `fn` on a background thread. Returns None when that class is busy."""
    with _jobs_lock:
        active = _active_jobs.get(klass)
        if active is not None and active.status == "running":
            return None
        job = _Job(uuid.uuid4().hex[:8], label, klass)
        _jobs[job.id] = job
        _active_jobs[klass] = job
        _reap()

    def run() -> None:
        tid = threading.current_thread().ident
        with _tjm_lock:
            _thread_job_map[tid] = job.id
        status = "done"
        try:
            fn(*args)
        except Exception as exc:
            status = "error"
            logger.error("Job %s failed: %s", job.id, exc)
        finally:
            with _tjm_lock:
                _thread_job_map.pop(tid, None)
            job.finish(status)

    threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
    return job.id


def _accepted(label: str, fn: Callable, *args, klass: str = HEAVY, **extra):
    """Launch a job, or report the class busy. The label is what the UI shows."""
    job_id = _launch(label, fn, *args, klass=klass)
    if job_id is None:
        return _busy(klass)
    return {"job_id": job_id, "label": label, **extra}


def _running_jobs() -> list[dict]:
    with _jobs_lock:
        running = [j for j in _jobs.values() if j.status == "running"]
    return [
        {"id": j.id, "label": j.label, "class": j.klass,
         "elapsed": int(time.time() - j.started)}
        for j in running
    ]


def _busy(klass: str) -> JSONResponse:
    with _jobs_lock:
        active = _active_jobs.get(klass)
        label = active.label if active is not None else "a job"
    if klass == LIGHT:
        message = f"A manual add is already running ({label})"
    else:
        message = (f"Pipeline is busy running '{label}' — "
                   "adding a transcript manually still works")
    return JSONResponse({"error": message}, status_code=409)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return open(path, encoding="utf-8").read()


class ScrapeBody(BaseModel):
    url: str = ""


@app.post("/api/scrape")
def api_scrape(body: ScrapeBody):
    url = body.url.strip()
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)

    def run():
        ns = argparse.Namespace(
            url_or_file=url,
            output=config.LOCAL_OUTPUT_DIR,
            lang=config.DEFAULT_LANG,
            delay=config.DELAY_BETWEEN_REQUESTS,
            no_json=False, no_jsonl=False, no_csv=False,
        )
        if not pipeline.cmd_scrape(ns):
            raise RuntimeError("Scrape did not complete successfully; see the job log")

    return _accepted("scraping", run)


class ManualBody(BaseModel):
    url: str = ""
    transcript: str = ""
    description: str = ""
    title: str = ""
    channel: str = ""
    published: str = ""
    fetch_metadata: bool = True


@app.post("/api/manual")
def api_manual(body: ManualBody):
    """
    Add a hand-pasted transcript. Description and transcript stay separate —
    the description goes to frontmatter, the transcript becomes segments.

    Runs in the 'light' job class so it is admitted while the pipeline grinds.
    """
    if not body.url.strip():
        return JSONResponse({"error": "url or video id required"}, status_code=400)
    if not body.transcript.strip():
        return JSONResponse({"error": "transcript is required"}, status_code=400)

    # Validate cheaply up front so mistakes come back in the response rather
    # than disappearing into a background job's log.
    try:
        video_id = pipeline._video_id_from(body.url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def run():
        pipeline.add_manual(
            body.url,
            body.transcript,
            description=body.description,
            title=body.title,
            channel=body.channel,
            published=body.published,
            output_dir=config.LOCAL_OUTPUT_DIR,
            fetch_metadata=body.fetch_metadata,
        )

    return _accepted("adding transcript", run, klass=LIGHT, video_id=video_id)


@app.post("/api/clean")
def api_clean():

    def run():
        ns = argparse.Namespace(
            raw_dir=config.LOCAL_OUTPUT_DIR,
            blob_dir=config.BLOB_OUTPUT_DIR,
        )
        if not pipeline.cmd_clean(ns):
            raise RuntimeError("Clean did not complete successfully; see the job log")

    return _accepted("cleaning", run)


@app.post("/api/ingest")
def api_ingest():

    def run():
        ns = argparse.Namespace(
            blob_dir=config.BLOB_OUTPUT_DIR,
            clean_dir=config.CLEAN_OUTPUT_DIR,
        )
        if not pipeline.cmd_ingest(ns):
            raise RuntimeError("Ingest did not complete successfully; see the job log")

    return _accepted("ingesting", run)


def _low_priority() -> None:
    """
    Drop the current job thread to the lowest CPU priority — the in-process
    equivalent of enrich.sh's `nice -n 19`. On Linux nice is per-thread, so the
    web server itself stays responsive while enrichment grinds away.
    """
    try:
        os.nice(19)
    except OSError as exc:
        logger.warning("Could not lower job priority: %s", exc)


def _enrich_ns(limit: int = 0):
    return argparse.Namespace(
        raw_dir=config.LOCAL_OUTPUT_DIR,
        blob_dir=config.BLOB_OUTPUT_DIR,
        limit=limit,
        loop=False,      # the UI never runs the polling worker
        poll=60,
    )


def _rewrite_ns(limit: int = 0):
    return argparse.Namespace(
        raw_dir=config.LOCAL_OUTPUT_DIR,
        blob_dir=config.BLOB_OUTPUT_DIR,
        limit=limit,
        loop=False,      # the UI never runs the polling worker
        poll=60,
    )


class EnrichBody(BaseModel):
    limit: int = Field(default=0, ge=0)  # 0 = drain the whole queue


@app.post("/api/enrich")
def api_enrich(body: EnrichBody | None = None):

    limit = body.limit if body else 0

    def run():
        _low_priority()
        if not pipeline.cmd_enrich(_enrich_ns(limit)):
            raise RuntimeError("Enrichment could not run; see the job log")

    return _accepted("enriching", run)


class RewriteBody(BaseModel):
    limit: int = Field(default=0, ge=0)  # 0 = drain the whole queue


@app.post("/api/rewrite")
def api_rewrite(body: RewriteBody | None = None):
    """
    Rewrite cleaned transcripts into readable articles.

    The slowest thing the pipeline does — roughly one generated token per
    transcript token, so a long video is hours. Runs at lowest priority like
    enrichment, and is resumable: closing the page does not lose progress.
    """
    limit = body.limit if body else 0

    def run():
        _low_priority()
        if not pipeline.cmd_rewrite(_rewrite_ns(limit)):
            raise RuntimeError("Rewrite could not run; see the job log")

    return _accepted("rewriting", run)


class RunAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = ""        # optional — scrape this first


@app.post("/api/run-all")
def api_run_all(body: RunAllBody | None = None):
    """
    Run the whole pipeline back to back in one job: scrape (only when a URL is
    given) → clean → enrich → ingest. A failed stage stops every dependent
    downstream stage.

    Individual videos that fail enrichment are not stage failures — they stay
    queued for the next run, and ingest still publishes everything that did
    make it through.
    """
    url = (body.url if body else "").strip()

    def run():
        def run_enrich():
            _low_priority()
            return pipeline.cmd_enrich(_enrich_ns(0))

        def run_rewrite():
            _low_priority()
            return pipeline.cmd_rewrite(_rewrite_ns(0))

        stages = []
        if url:
            stages.append(("scrape", lambda: pipeline.cmd_scrape(argparse.Namespace(
                url_or_file=url,
                output=config.LOCAL_OUTPUT_DIR,
                lang=config.DEFAULT_LANG,
                delay=config.DELAY_BETWEEN_REQUESTS,
                no_json=False, no_jsonl=False, no_csv=False,
            ))))
        stages.append(("clean", lambda: pipeline.cmd_clean(argparse.Namespace(
            raw_dir=config.LOCAL_OUTPUT_DIR,
            blob_dir=config.BLOB_OUTPUT_DIR,
        ))))
        # Rewrite must precede enrich: enrich reads the article body back out
        # of the blob and derives its metadata from that.
        if config.REWRITE_ENABLED:
            stages.append(("rewrite", run_rewrite))
        if config.LLM_ENABLED:
            stages.append(("enrich", run_enrich))
        stages.append(("ingest", lambda: pipeline.cmd_ingest(argparse.Namespace(
            blob_dir=config.BLOB_OUTPUT_DIR,
            clean_dir=config.CLEAN_OUTPUT_DIR,
        ))))

        if not config.REWRITE_ENABLED:
            logger.info("REWRITE_ENABLED=0 — skipping the article rewrite.")
        if not config.LLM_ENABLED:
            logger.info("LLM_ENABLED=0 — skipping enrichment, publishing plain clean files.")

        for i, (name, fn) in enumerate(stages, 1):
            logger.info("──── [%d/%d] %s ────", i, len(stages), name)
            if fn() is not True:
                raise RuntimeError(
                    f"Stage '{name}' did not complete successfully; "
                    "downstream stages were not started"
                )
        logger.info("──── run-all complete ────")

    return _accepted(
        f"{'scrape → ' if url else ''}clean → "
        f"{'rewrite → ' if config.REWRITE_ENABLED else ''}"
        f"{'enrich → ' if config.LLM_ENABLED else ''}ingest",
        run,
    )


def _line_event(seq: int, line: str) -> str:
    safe = line.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return f"id: {seq}\ndata: {safe}\n\n"


def _last_seq(value) -> int:
    """
    Last-Event-ID as a sequence number. Anything unparseable — junk header, or
    the unresolved Header() default when this function is called directly —
    replays the whole buffer, which is always safe.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@app.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str, last_event_id: str | None = Header(default=None)):
    """
    Stream a job's log as SSE.

    Every line carries its sequence number as the SSE event id, so a browser
    reconnect (which replays Last-Event-ID) resumes exactly where it dropped:
    no duplicated lines, and no phantom failure when the connection blips. A
    page reload reattaches the same way and gets the whole buffer back.
    """
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)

    q, backlog, status = job.subscribe(_last_seq(last_event_id))

    def generate() -> Generator[str, None, None]:
        try:
            yield "retry: 3000\n\n"
            for seq, line in backlog:
                yield _line_event(seq, line)
            if q is None:
                yield f"event: done\ndata: {status}\n\n"
                return
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield "event: ping\ndata: \n\n"
                    continue
                if item[0] == "done":
                    yield f"event: done\ndata: {item[1]}\n\n"
                    return
                yield _line_event(item[0], item[1])
        finally:
            if q is not None:
                job.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/videos")
def api_videos():
    jsonl_path = os.path.join(config.LOCAL_OUTPUT_DIR, "dataset.jsonl")
    if not os.path.exists(jsonl_path):
        return []
    videos = []
    try:
        with pipeline.aggregate_lock(config.LOCAL_OUTPUT_DIR, exclusive=False), \
                open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                videos.append({
                    "video_id": rec.get("video_id", ""),
                    "title":    rec.get("title", ""),
                    "channel":  rec.get("channel", ""),
                    "published": rec.get("published", ""),
                    "word_count": rec.get("word_count", 0),
                    "url":      rec.get("url", ""),
                })
    except Exception:
        pass
    return list(reversed(videos))  # newest first


@app.get("/api/status")
def api_status():
    db_url = os.environ.get("DATABASE_URL", "")
    db_ok = bool(db_url and "user:password" not in db_url)
    return {
        "database":     "configured" if db_ok else "not configured",
        "blob_storage": "mounted"    if os.path.isdir(config.BLOB_OUTPUT_DIR)  else "not found",
        "clean_storage": "available" if os.path.isdir(config.CLEAN_OUTPUT_DIR) else "not found",
        "jobs":         _running_jobs(),
    }


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
