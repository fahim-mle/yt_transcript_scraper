"""
Fetches the transcript for a single YouTube video using youtube-transcript-api.

API note: v1.x switched from class methods to instance-based usage and returns
FetchedTranscriptSnippet objects instead of dicts. We normalise to plain dicts
so the rest of the pipeline doesn't care about the library version.

Failure handling is the point of this module. The caller has to distinguish
"YouTube is throttling us" (back off, retry the video later) from "this video
has no transcript" (skip it forever) — collapsing both into None makes a run
look successful while it quietly loses videos, and keeps hammering an endpoint
that is already refusing us. So every failure raises a typed TranscriptError
carrying a stable `reason`, and every outbound request goes through _request(),
which paces calls and retries the transient ones with exponential backoff.
"""

import http.cookiejar
import logging
import random
import threading
import time

import requests
from youtube_transcript_api import (
    AgeRestricted,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

import config

logger = logging.getLogger(__name__)


# ── Typed failures ─────────────────────────────────────────────────────────
# `reason` is a short stable slug — it lands in logs and the failure report,
# and is what you filter on when deciding which videos are worth re-running.

class TranscriptError(Exception):
    """Base for every fetch failure."""
    reason = "error"
    transient = False

    def __init__(self, video_id: str, detail: str = ""):
        self.video_id = video_id
        self.detail = detail
        super().__init__(f"[{video_id}] {self.reason}: {detail}" if detail
                         else f"[{video_id}] {self.reason}")


class TranscriptBlocked(TranscriptError):
    """Rate-limited or IP-blocked, and retries are exhausted. Retry later."""
    reason = "blocked"
    transient = True


class TranscriptNetworkError(TranscriptError):
    """Network/transport failure that survived every retry."""
    reason = "network"
    transient = True


class TranscriptUnavailable(TranscriptError):
    """Video genuinely has no transcript. Permanent — do not retry."""
    reason = "no_transcript"


class TranscriptGone(TranscriptError):
    """Video is removed, private, or the ID is malformed. Permanent."""
    reason = "video_unavailable"


class TranscriptAuthRequired(TranscriptError):
    """Age-gated, unplayable, or needs a PoToken. Retrying as-is won't help."""
    reason = "auth_required"


class TranscriptFailed(TranscriptError):
    """Anything we didn't anticipate. Logged with the real exception type."""
    reason = "failed"


# Transport-level errors worth retrying. The library wraps some of these in
# YouTubeRequestFailed, but a dropped connection surfaces raw.
_RETRYABLE = (RequestBlocked, IpBlocked, YouTubeRequestFailed,
              requests.exceptions.ConnectionError, requests.exceptions.Timeout)


# ── Request pacing ─────────────────────────────────────────────────────────
# A single lock serialises every request this process makes, so the interval
# holds no matter how many call sites there are.

_pace_lock = threading.Lock()
_last_request_at = 0.0
_delay = config.DELAY_BETWEEN_REQUESTS


def set_delay(seconds: float) -> None:
    """Override the inter-request delay (CLI --delay wins over config)."""
    global _delay
    _delay = max(0.0, float(seconds))


def _wait_turn() -> None:
    global _last_request_at
    with _pace_lock:
        jitter = 1.0 + random.uniform(-config.REQUEST_JITTER, config.REQUEST_JITTER)
        gap = max(0.0, _delay * jitter) - (time.monotonic() - _last_request_at)
        if gap > 0:
            time.sleep(gap)
        _last_request_at = time.monotonic()


# ── API client ─────────────────────────────────────────────────────────────

_api_instance = None


def _proxy_config():
    if config.YT_WEBSHARE_USERNAME and config.YT_WEBSHARE_PASSWORD:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        logger.info("Routing transcript requests through Webshare residential proxy.")
        return WebshareProxyConfig(
            proxy_username=config.YT_WEBSHARE_USERNAME,
            proxy_password=config.YT_WEBSHARE_PASSWORD,
        )
    if config.YT_PROXY_HTTP or config.YT_PROXY_HTTPS:
        from youtube_transcript_api.proxies import GenericProxyConfig
        logger.info("Routing transcript requests through configured proxy.")
        return GenericProxyConfig(
            http_url=config.YT_PROXY_HTTP or None,
            https_url=config.YT_PROXY_HTTPS or None,
        )
    return None


def _http_client():
    """A session carrying cookies, for gated videos. None when unconfigured."""
    if not config.YT_COOKIE_FILE:
        return None
    jar = http.cookiejar.MozillaCookieJar(config.YT_COOKIE_FILE)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        logger.warning("Could not load cookie file %s: %s — continuing without cookies.",
                       config.YT_COOKIE_FILE, exc)
        return None
    session = requests.Session()
    session.cookies = jar
    logger.info("Loaded cookies from %s.", config.YT_COOKIE_FILE)
    return session


def _api() -> YouTubeTranscriptApi:
    global _api_instance
    if _api_instance is None:
        _api_instance = YouTubeTranscriptApi(
            proxy_config=_proxy_config(),
            http_client=_http_client(),
        )
    return _api_instance


# ── Request wrapper ────────────────────────────────────────────────────────

def _request(video_id: str, what: str, call):
    """
    Run one outbound request: paced, and retried with exponential backoff when
    the failure is transient. Permanent failures propagate untouched for the
    caller to translate — retrying them is pure waste.
    """
    delay = config.TRANSCRIPT_BACKOFF_BASE
    last_exc = None

    for attempt in range(1, config.TRANSCRIPT_MAX_ATTEMPTS + 1):
        _wait_turn()
        try:
            return call()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt == config.TRANSCRIPT_MAX_ATTEMPTS:
                break
            logger.warning(
                "[%s] %s failed (%s), attempt %d/%d — backing off %.0fs.",
                video_id, what, type(exc).__name__, attempt,
                config.TRANSCRIPT_MAX_ATTEMPTS, delay,
            )
            time.sleep(delay)
            delay = min(delay * config.TRANSCRIPT_BACKOFF_FACTOR,
                        config.TRANSCRIPT_BACKOFF_CAP)

    kind = type(last_exc).__name__
    logger.error("[%s] %s gave up after %d attempts (%s).",
                 video_id, what, config.TRANSCRIPT_MAX_ATTEMPTS, kind)
    if isinstance(last_exc, (requests.exceptions.ConnectionError,
                             requests.exceptions.Timeout)):
        raise TranscriptNetworkError(video_id, kind) from last_exc
    raise TranscriptBlocked(video_id, kind) from last_exc


def _to_dicts(segments) -> list[dict]:
    return [{"text": s.text, "start": s.start, "duration": s.duration} for s in segments]


def _select(transcript_list, video_id: str, lang: str):
    """Requested language first, then any manual transcript, then generated."""
    try:
        return transcript_list.find_transcript([lang])
    except NoTranscriptFound:
        pass

    available = list(transcript_list)
    for wanted_generated in (False, True):
        for t in available:
            if t.is_generated is wanted_generated:
                logger.info("[%s] No '%s' transcript — falling back to %s '%s'.",
                            video_id, lang,
                            "auto-generated" if t.is_generated else "manual",
                            t.language_code)
                return t
    return None


# ── Public API ─────────────────────────────────────────────────────────────

def fetch(video_id: str, lang: str = config.DEFAULT_LANG) -> list[dict]:
    """
    Returns a list of {text, start, duration} dicts.

    Raises a TranscriptError subclass on failure — check `.reason` to decide
    whether the video is worth retrying (`.transient`) or should be skipped.
    """
    api = _api()

    try:
        transcript_list = _request(video_id, "list", lambda: api.list(video_id))
    except TranscriptsDisabled as exc:
        raise TranscriptUnavailable(video_id, "transcripts disabled") from exc
    except (AgeRestricted, VideoUnplayable, PoTokenRequired) as exc:
        raise TranscriptAuthRequired(video_id, type(exc).__name__) from exc
    except (VideoUnavailable, InvalidVideoId) as exc:
        raise TranscriptGone(video_id, type(exc).__name__) from exc
    except TranscriptError:
        raise
    except Exception as exc:
        raise TranscriptFailed(video_id, f"{type(exc).__name__}: {exc}") from exc

    chosen = _select(transcript_list, video_id, lang)
    if chosen is None:
        raise TranscriptUnavailable(video_id, "no transcript in any language")

    try:
        return _to_dicts(_request(video_id, "fetch", chosen.fetch))
    except (AgeRestricted, VideoUnplayable, PoTokenRequired) as exc:
        raise TranscriptAuthRequired(video_id, type(exc).__name__) from exc
    except (VideoUnavailable, InvalidVideoId) as exc:
        raise TranscriptGone(video_id, type(exc).__name__) from exc
    except TranscriptError:
        raise
    except Exception as exc:
        raise TranscriptFailed(video_id, f"{type(exc).__name__}: {exc}") from exc
