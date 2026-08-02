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
import uuid
from typing import Generator

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

_thread_queue_map: dict[int, queue.Queue] = {}
_tqm_lock = threading.Lock()


class _JobLogHandler(logging.Handler):
    """Routes log records into the active job's queue, keyed by thread ID."""
    def emit(self, record: logging.LogRecord) -> None:
        tid = threading.current_thread().ident
        with _tqm_lock:
            q = _thread_queue_map.get(tid)
        if q is not None:
            try:
                q.put_nowait(self.format(record))
            except queue.Full:
                pass


_job_handler = _JobLogHandler()
_job_handler.setFormatter(_fmt)
_root.addHandler(_job_handler)

# Pipeline imports (safe now that logging is configured)
import config
import main as pipeline

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="YT Transcript Scraper", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)

# ── Job management ─────────────────────────────────────────────────────────────

_active_job: dict | None = None
_active_lock = threading.Lock()
_job_queues: dict[str, queue.Queue] = {}




def _launch(label: str, fn, *args) -> str | None:
    with _active_lock:
        global _active_job
        if _active_job is not None and _active_job["status"] == "running":
            return None
        job_id = uuid.uuid4().hex[:8]
        q: queue.Queue = queue.Queue(maxsize=2000)
        _job_queues[job_id] = q
        _active_job = {"id": job_id, "label": label, "status": "running"}

    def run() -> None:
        tid = threading.current_thread().ident
        with _tqm_lock:
            _thread_queue_map[tid] = q
        status = "done"
        try:
            fn(*args)
        except Exception as exc:
            status = "error"
            logger.error("Job %s failed: %s", job_id, exc)
        finally:
            with _tqm_lock:
                _thread_queue_map.pop(tid, None)
            with _active_lock:
                if _active_job is not None and _active_job["id"] == job_id:
                    _active_job["status"] = status
            terminal = ("done", status)
            try:
                q.put_nowait(terminal)
            except queue.Full:
                # Preserve completion even when a verbose unattended job filled
                # the bounded queue: sacrifice one old log line for the sentinel.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait(terminal)

    threading.Thread(target=run, daemon=True, name=f"job-{job_id}").start()
    return job_id


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

    job_id = _launch("scrape", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id}


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

    job_id = _launch("manual", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id, "video_id": video_id}


@app.post("/api/clean")
def api_clean():

    def run():
        ns = argparse.Namespace(
            raw_dir=config.LOCAL_OUTPUT_DIR,
            blob_dir=config.BLOB_OUTPUT_DIR,
        )
        if not pipeline.cmd_clean(ns):
            raise RuntimeError("Clean did not complete successfully; see the job log")

    job_id = _launch("clean", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id}


@app.post("/api/ingest")
def api_ingest():

    def run():
        ns = argparse.Namespace(
            blob_dir=config.BLOB_OUTPUT_DIR,
            clean_dir=config.CLEAN_OUTPUT_DIR,
        )
        if not pipeline.cmd_ingest(ns):
            raise RuntimeError("Ingest did not complete successfully; see the job log")

    job_id = _launch("ingest", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id}


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


class EnrichBody(BaseModel):
    limit: int = Field(default=0, ge=0)  # 0 = drain the whole queue


@app.post("/api/enrich")
def api_enrich(body: EnrichBody | None = None):

    limit = body.limit if body else 0

    def run():
        _low_priority()
        if not pipeline.cmd_enrich(_enrich_ns(limit)):
            raise RuntimeError("Enrichment did not complete successfully; see the job log")

    job_id = _launch("enrich", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id}


class RunAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = ""        # optional — scrape this first


@app.post("/api/run-all")
def api_run_all(body: RunAllBody | None = None):
    """
    Run the whole pipeline back to back in one job: scrape (only when a URL is
    given) → clean → enrich → ingest. A failed stage stops every dependent
    downstream stage.
    """
    url = (body.url if body else "").strip()

    def run():
        def run_enrich():
            _low_priority()
            return pipeline.cmd_enrich(_enrich_ns(0))

        stages = []
        if url:
            stages.append(("scrape", lambda: pipeline.cmd_scrape(argparse.Namespace(
                url_or_file=url,
                output=config.LOCAL_OUTPUT_DIR,
                lang=config.DEFAULT_LANG,
                delay=config.DELAY_BETWEEN_REQUESTS,
                no_json=False, no_jsonl=False, no_csv=False,
            ))))
        stages += [
            ("clean", lambda: pipeline.cmd_clean(argparse.Namespace(
                raw_dir=config.LOCAL_OUTPUT_DIR,
                blob_dir=config.BLOB_OUTPUT_DIR,
            ))),
            ("enrich", run_enrich),
            ("ingest", lambda: pipeline.cmd_ingest(argparse.Namespace(
                blob_dir=config.BLOB_OUTPUT_DIR,
                clean_dir=config.CLEAN_OUTPUT_DIR,
            ))),
        ]

        for i, (name, fn) in enumerate(stages, 1):
            logger.info("──── [%d/%d] %s ────", i, len(stages), name)
            if fn() is not True:
                raise RuntimeError(
                    f"Stage '{name}' did not complete successfully; "
                    "downstream stages were not started"
                )
        logger.info("──── run-all complete ────")

    job_id = _launch("run-all", run)
    if job_id is None:
        return JSONResponse({"error": "A job is already running"}, status_code=409)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str):
    q = _job_queues.get(job_id)
    if q is None:
        return JSONResponse({"error": "job not found"}, status_code=404)

    def generate() -> Generator[str, None, None]:
        while True:
            try:
                msg = q.get(timeout=30)
                if isinstance(msg, tuple) and msg[0] == "done":
                    _job_queues.pop(job_id, None)
                    yield f"event: done\ndata: {msg[1]}\n\n"
                    return
                safe = msg.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                yield f"data: {safe}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: \n\n"

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
        with open(jsonl_path, encoding="utf-8") as f:
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
    with _active_lock:
        job = dict(_active_job) if _active_job else None
    return {
        "database":     "configured" if db_ok else "not configured",
        "blob_storage": "mounted"    if os.path.isdir(config.BLOB_OUTPUT_DIR)  else "not found",
        "clean_storage": "available" if os.path.isdir(config.CLEAN_OUTPUT_DIR) else "not found",
        "current_job":  job,
    }


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
