"""
Parses a hand-pasted transcript into the same segment shape the API returns.

Used when YouTube's transcript endpoints are blocked but the text is still
readable in a browser: copy it out of the "Show transcript" panel, paste it in,
and the rest of the pipeline (clean → enrich → ingest) runs unchanged.

Accepts what people actually end up with on the clipboard:

  1. Timestamped lines   "0:00 intro text"  /  "[00:00] intro text"
  2. Timestamp on its own line, text on the next  (YouTube's panel does this)
  3. SRT / WebVTT cue blocks with "00:00:01,000 --> 00:00:04,000"
  4. Plain prose with no timestamps at all

Timing matters downstream: cleaner.py derives paragraph breaks from the gaps
between segments, and chapter headings are injected by start time. So real
timestamps are preserved exactly, and prose without them gets a synthetic
cadence that reproduces the paragraph structure of what was pasted.
"""

import logging
import re

logger = logging.getLogger(__name__)

# 1:23, 01:23, 1:23:45, 00:00:01.500, 00:00:01,500
_TS = r"(?:\d{1,3}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_CUE_RE = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
_LINE_TS_RE = re.compile(rf"^\s*\[?\(?({_TS})\)?\]?\s*(.*)$")

# Speaking pace used to synthesise timings for un-timestamped prose.
_WORDS_PER_SECOND = 2.5
# Silence inserted at a blank line, so cleaner.py breaks the paragraph there.
_PARAGRAPH_GAP = 4.0
_WORDS_PER_CHUNK = 22


def _to_seconds(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _finalise(pairs: list[tuple[float, str]], end_times: list[float] | None = None) -> list[dict]:
    """Turn (start, text) pairs into segments, deriving durations from the next start."""
    segments = []
    for i, (start, text) in enumerate(pairs):
        text = text.strip()
        if not text:
            continue
        if end_times and end_times[i] > start:
            duration = end_times[i] - start
        elif i + 1 < len(pairs):
            duration = max(0.1, pairs[i + 1][0] - start)
        else:
            duration = 3.0
        segments.append({"text": text, "start": round(start, 3),
                         "duration": round(duration, 3)})
    return segments


def _parse_cues(lines: list[str]) -> list[dict]:
    """SRT / WebVTT: a timing line followed by one or more text lines."""
    pairs, ends, buf, start, end = [], [], [], None, None

    def flush():
        if start is not None and buf:
            pairs.append((start, " ".join(buf)))
            ends.append(end if end is not None else 0.0)

    for line in lines:
        cue = _CUE_RE.match(line)
        if cue:
            flush()
            buf = []
            start, end = _to_seconds(cue.group(1)), _to_seconds(cue.group(2))
        elif start is not None and not line.strip():
            # A blank line closes an SRT/VTT cue. Numeric lines inside an open
            # cue are caption text; numeric indices occur between closed cues.
            flush()
            buf, start, end = [], None, None
        elif start is not None:
            buf.append(line.strip())
    flush()
    return _finalise(pairs, ends)


def _parse_timestamped(lines: list[str]) -> list[dict]:
    """Timestamp-prefixed lines, or a timestamp line followed by its text."""
    pairs, pending_start, buf = [], None, []

    def flush():
        if pending_start is not None and buf:
            pairs.append((pending_start, " ".join(buf)))

    for line in lines:
        if not line.strip():
            continue
        m = _LINE_TS_RE.match(line)
        if m:
            flush()
            buf = []
            pending_start = _to_seconds(m.group(1))
            rest = m.group(2).strip()
            if rest:
                buf.append(rest)
        elif pending_start is not None:
            buf.append(line.strip())
    flush()
    return _finalise(pairs)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _parse_prose(text: str) -> list[dict]:
    """
    No timestamps: chunk into segments at a plausible speaking pace, and open a
    real gap at each blank line so the pasted paragraph structure survives
    cleaning rather than collapsing into one block.
    """
    segments: list[dict] = []
    clock = 0.0

    for para_idx, para in enumerate(p for p in re.split(r"\n\s*\n", text) if p.strip()):
        if para_idx:
            clock += _PARAGRAPH_GAP

        para = " ".join(para.split())
        chunks, current = [], []
        for sentence in _split_sentences(para) or [para]:
            words = sentence.split()
            # A long sentence becomes several segments; short ones accumulate.
            if len(words) > _WORDS_PER_CHUNK:
                if current:
                    chunks.append(" ".join(current))
                    current = []
                for i in range(0, len(words), _WORDS_PER_CHUNK):
                    chunks.append(" ".join(words[i:i + _WORDS_PER_CHUNK]))
            else:
                current.extend(words)
                if len(current) >= _WORDS_PER_CHUNK:
                    chunks.append(" ".join(current))
                    current = []
        if current:
            chunks.append(" ".join(current))

        for chunk in chunks:
            duration = max(1.0, len(chunk.split()) / _WORDS_PER_SECOND)
            segments.append({"text": chunk, "start": round(clock, 3),
                             "duration": round(duration, 3)})
            clock += duration

    return segments


def parse_transcript(text: str) -> list[dict]:
    """
    Returns [{text, start, duration}] — the same shape scraper.transcript.fetch
    produces. Raises ValueError when nothing usable can be parsed.
    """
    if not text or not text.strip():
        raise ValueError("transcript is empty")

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [ln for ln in lines if ln.strip().upper() != "WEBVTT"]

    if any(_CUE_RE.match(ln) for ln in lines):
        segments = _parse_cues(lines)
        style = "SRT/VTT cues"
    elif any(_LINE_TS_RE.match(ln) and _LINE_TS_RE.match(ln).group(1) for ln in lines
             if ln.strip()):
        segments = _parse_timestamped(lines)
        style = "timestamped lines"
    else:
        segments = _parse_prose("\n".join(lines))
        style = "prose (synthetic timings)"

    if not segments:
        raise ValueError("no transcript segments could be parsed from the pasted text")

    words = sum(len(s["text"].split()) for s in segments)
    logger.info("Parsed %d segment(s), %d words — %s.", len(segments), words, style)
    return segments
