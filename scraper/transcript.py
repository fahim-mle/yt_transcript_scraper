"""
Fetches the transcript for a single YouTube video using youtube-transcript-api.
"""

import logging
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

logger = logging.getLogger(__name__)


def fetch(video_id: str, lang: str = "en") -> list[dict] | None:
    """
    Returns a list of {text, start, duration} dicts, or None if unavailable.
    Tries the requested language first, then falls back to any available language.
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except TranscriptsDisabled:
        logger.warning("[%s] Transcripts are disabled for this video.", video_id)
        return None
    except Exception as exc:
        logger.warning("[%s] Could not list transcripts: %s", video_id, exc)
        return None

    # Try requested language (manual first, then auto-generated)
    try:
        transcript = transcript_list.find_transcript([lang])
        return transcript.fetch()
    except NoTranscriptFound:
        pass

    # Fall back to any manually created transcript
    try:
        transcript = transcript_list.find_manually_created_transcript(
            transcript_list._manually_created_transcripts.keys()
        )
        logger.info("[%s] Falling back to manual transcript in '%s'.", video_id, transcript.language_code)
        return transcript.fetch()
    except Exception:
        pass

    # Fall back to any auto-generated transcript
    try:
        transcript = transcript_list.find_generated_transcript(
            transcript_list._generated_transcripts.keys()
        )
        logger.info("[%s] Falling back to auto-generated transcript in '%s'.", video_id, transcript.language_code)
        return transcript.fetch()
    except Exception:
        pass

    logger.warning("[%s] No transcript found in any language.", video_id)
    return None
