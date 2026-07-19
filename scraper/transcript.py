"""
Fetches the transcript for a single YouTube video using youtube-transcript-api.

API note: v1.x switched from class methods to instance-based usage and returns
FetchedTranscriptSnippet objects instead of dicts. We normalise to plain dicts
so the rest of the pipeline doesn't care about the library version.
"""

import logging
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

logger = logging.getLogger(__name__)


def _to_dicts(segments) -> list[dict]:
    return [{"text": s.text, "start": s.start, "duration": s.duration} for s in segments]


def fetch(video_id: str, lang: str = "en") -> list[dict] | None:
    """
    Returns a list of {text, start, duration} dicts, or None if unavailable.
    Tries the requested language first, then falls back to any available language.
    """
    api = YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        logger.warning("[%s] Transcripts are disabled for this video.", video_id)
        return None
    except Exception as exc:
        logger.warning("[%s] Could not list transcripts: %s", video_id, exc)
        return None

    # Try requested language (manual first, then auto-generated)
    try:
        return _to_dicts(transcript_list.find_transcript([lang]).fetch())
    except NoTranscriptFound:
        pass

    # Fall back to any manually created transcript
    try:
        manual_codes = list(transcript_list._manually_created_transcripts.keys())
        if manual_codes:
            t = transcript_list.find_manually_created_transcript(manual_codes)
            logger.info("[%s] Falling back to manual transcript in '%s'.", video_id, t.language_code)
            return _to_dicts(t.fetch())
    except Exception:
        pass

    # Fall back to any auto-generated transcript
    try:
        generated_codes = list(transcript_list._generated_transcripts.keys())
        if generated_codes:
            t = transcript_list.find_generated_transcript(generated_codes)
            logger.info("[%s] Falling back to auto-generated transcript in '%s'.", video_id, t.language_code)
            return _to_dicts(t.fetch())
    except Exception:
        pass

    logger.warning("[%s] No transcript found in any language.", video_id)
    return None
