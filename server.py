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
from pydantic import BaseModel

app = FastAPI(title="YT Transcript Scraper", docs_url=None, redoc_url=None)
logger = logging.getLogger(__name__)

# ── Job management ─────────────────────────────────────────────────────────────

_active_job: dict | None = None
_active_lock = threading.Lock()
_job_queues: dict[str, queue.Queue] = {}


def _is_running() -> bool:
    with _active_lock:
        return _active_job is not None and _active_job["status"] == "running"


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
        try:
            fn(*args)
            with _active_lock:
                _active_job["status"] = "done"
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc)
            with _active_lock:
                _active_job["status"] = "error"
        finally:
            with _tqm_lock:
                _thread_queue_map.pop(tid, None)
            q.put_nowait(None)  # sentinel — stream ends

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
    if _is_running():
        return JSONResponse({"error": "A job is already running"}, status_code=409)

    def run():
        ns = argparse.Namespace(
            url_or_file=url,
            output=config.LOCAL_OUTPUT_DIR,
            lang=config.DEFAULT_LANG,
            delay=config.DELAY_BETWEEN_REQUESTS,
            no_json=False, no_jsonl=False, no_csv=False,
        )
        pipeline.cmd_scrape(ns)

    job_id = _launch("scrape", run)
    return {"job_id": job_id}


@app.post("/api/clean")
def api_clean():
    if _is_running():
        return JSONResponse({"error": "A job is already running"}, status_code=409)

    def run():
        ns = argparse.Namespace(
            raw_dir=config.LOCAL_OUTPUT_DIR,
            blob_dir=config.BLOB_OUTPUT_DIR,
        )
        pipeline.cmd_clean(ns)

    job_id = _launch("clean", run)
    return {"job_id": job_id}


@app.post("/api/ingest")
def api_ingest():
    if _is_running():
        return JSONResponse({"error": "A job is already running"}, status_code=409)

    def run():
        ns = argparse.Namespace(
            blob_dir=config.BLOB_OUTPUT_DIR,
            clean_dir=config.CLEAN_OUTPUT_DIR,
        )
        pipeline.cmd_ingest(ns)

    job_id = _launch("ingest", run)
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
                if msg is None:
                    yield "event: done\ndata: \n\n"
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
