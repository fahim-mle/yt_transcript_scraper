"""
Resolves any YouTube URL (video, playlist, channel) or a batch .txt file
into a flat list of video metadata dicts using yt-dlp.
"""

import os
import logging
import yt_dlp

logger = logging.getLogger(__name__)

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
}


def _fetch_entries(url: str) -> list[dict]:
    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return []

    # Single video
    if info.get("_type") != "playlist":
        return [_normalize(info)]

    # Playlist / channel — entries may be stubs; enrich each one
    results = []
    for entry in info.get("entries", []):
        if entry is None:
            continue
        video_id = entry.get("id") or entry.get("url", "").split("v=")[-1]
        if not video_id:
            continue
        try:
            full = _fetch_single(f"https://www.youtube.com/watch?v={video_id}")
            results.append(full)
        except Exception as exc:
            logger.warning("Skipping %s: %s", video_id, exc)
    return results


def _fetch_single(url: str) -> dict:
    opts = {**_YDL_OPTS, "extract_flat": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _normalize(info)


def _normalize(info: dict) -> dict:
    upload_date = info.get("upload_date") or ""
    if len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return {
        "video_id":   info.get("id", ""),
        "url":        info.get("webpage_url") or f"https://www.youtube.com/watch?v={info.get('id', '')}",
        "title":      info.get("title", ""),
        "channel":    info.get("channel") or info.get("uploader") or "",
        "channel_id": info.get("channel_id") or "",
        "published":  upload_date,
        "description": info.get("description") or "",
        # chapters: [{"title": "...", "start_time": 0.0, "end_time": 60.0}, ...]
        # Present only when the creator added YouTube chapters to the video.
        "chapters":   info.get("chapters") or [],
    }


def resolve(url_or_path: str) -> list[dict]:
    """
    Returns a list of video metadata dicts.
    Accepts a YouTube URL (video / playlist / channel) or a path to a
    .txt file containing one URL per line.
    """
    if os.path.isfile(url_or_path):
        videos = []
        with open(url_or_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        videos.extend(_fetch_entries(line))
                    except Exception as exc:
                        logger.warning("Failed to resolve %s: %s", line, exc)
        return videos

    return _fetch_entries(url_or_path)
