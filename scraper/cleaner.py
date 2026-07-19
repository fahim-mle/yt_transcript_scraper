"""
Cleans raw transcript segments into structured, readable Markdown.

Handles two sources of structure:
  1. YouTube chapters (preferred) — injected as ## headings at chapter timestamps
  2. Heuristic heading detection (fallback) — short, title-case segments after a pause

Paragraph merging uses an adaptive gap threshold derived from the video's own
segment pacing, so fast-cut educational videos and slow lecture videos are
treated differently without manual tuning.

Cleaning steps applied to each segment:
  1. HTML entity decoding
  2. Filler word removal (um, uh, hmm — conservative, semantic words left intact)
  3. Whitespace normalisation
"""

import html
import re
import statistics

import config

_FILLER_PATTERN = re.compile(
    r"\b(um+|uh+|hmm+|hm+|mhm|uh-huh|err+)\b[,]?",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?]['\"]?\s*$")


# ── Text normalisation ────────────────────────────────────────────────────

def _decode(text: str) -> str:
    return html.unescape(text).replace("\n", " ").strip()


def _strip_fillers(text: str) -> str:
    cleaned = _FILLER_PATTERN.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _clean_text(text: str) -> str:
    return _strip_fillers(_decode(text))


# ── Adaptive gap threshold ────────────────────────────────────────────────

def _adaptive_gap_threshold(segments: list[dict]) -> float:
    """
    Derive a paragraph-break gap from the video's own pacing.
    Uses the 80th-percentile inter-segment gap, capped between 1.0 and 8.0 s.
    Falls back to config default when there aren't enough segments to measure.
    """
    if len(segments) < 10:
        return config.PARAGRAPH_GAP_SECONDS

    gaps = []
    for i in range(1, len(segments)):
        prev = segments[i - 1]
        gap = segments[i]["start"] - (prev["start"] + prev.get("duration", 0))
        if gap > 0:
            gaps.append(gap)

    if not gaps:
        return config.PARAGRAPH_GAP_SECONDS

    gaps.sort()
    p80 = gaps[int(len(gaps) * 0.80)]
    return max(1.0, min(8.0, p80))


# ── Chapter injection ─────────────────────────────────────────────────────

def _inject_chapters(segments: list[dict], chapters: list[dict]) -> list[dict]:
    """
    Inserts synthetic heading segments at chapter start times.
    The heading marker carries _is_heading=True so the merger can treat it specially.
    """
    if not chapters:
        return segments

    markers = sorted(
        [{"start": c["start_time"], "title": c["title"]}
         for c in chapters if c.get("title") and c.get("start_time") is not None],
        key=lambda x: x["start"],
    )

    result: list[dict] = []
    marker_idx = 0

    for seg in segments:
        while marker_idx < len(markers) and markers[marker_idx]["start"] <= seg["start"]:
            m = markers[marker_idx]
            result.append({
                "text":       f"## {m['title'].strip()}",
                "start":      m["start"],
                "duration":   0,
                "_is_heading": True,
            })
            marker_idx += 1
        result.append(seg)

    # Flush any remaining chapter markers that come after all segments
    while marker_idx < len(markers):
        m = markers[marker_idx]
        result.append({"text": f"## {m['title'].strip()}", "start": m["start"],
                       "duration": 0, "_is_heading": True})
        marker_idx += 1

    return result


# ── Heuristic heading detection (no chapters available) ───────────────────

def _looks_like_heading(text: str, gap_before: float, threshold: float) -> bool:
    """
    Returns True when a segment is short, title-cased, unpunctuated,
    and preceded by a pause notably longer than the adaptive threshold.
    Deliberately conservative to avoid false positives.
    """
    words = text.split()
    if not (1 <= len(words) <= 7):
        return False
    if text[-1] in ".!?,;:":
        return False
    if not text[0].isupper():
        return False
    # Require a pause meaningfully longer than a normal paragraph break
    return gap_before >= threshold * 2.0


# ── Paragraph / heading merger ────────────────────────────────────────────

def _build_blocks(
    segments: list[dict],
    gap_threshold: float,
    min_words: int,
) -> list[str]:
    """
    Returns a list of markdown blocks — either '## Heading' strings or
    paragraph strings. Blocks are later joined with '\n\n'.
    """
    blocks: list[str] = []
    current: list[str] = []
    word_count = 0

    def flush():
        nonlocal current, word_count
        if current:
            blocks.append(" ".join(current))
            current = []
            word_count = 0

    for i, seg in enumerate(segments):
        # Explicit heading injected by chapter logic
        if seg.get("_is_heading"):
            flush()
            blocks.append(seg["text"])
            continue

        text = _clean_text(seg["text"])
        if not text:
            continue

        # Compute gap from previous segment (skip first)
        gap = 0.0
        if i > 0:
            prev = segments[i - 1]
            gap = seg["start"] - (prev["start"] + prev.get("duration", 0))

        # Heuristic heading (only when no chapters were injected)
        if i > 0 and _looks_like_heading(text, gap, gap_threshold):
            flush()
            blocks.append(f"## {text}")
            continue

        # Paragraph break on gap + word count
        ends_sentence = bool(_SENTENCE_END.search(current[-1])) if current else False
        long_enough = word_count >= min_words

        if i > 0 and long_enough and (gap >= gap_threshold or ends_sentence):
            flush()

        current.append(text)
        word_count += len(text.split())

    flush()
    return blocks


# ── Public API ────────────────────────────────────────────────────────────

def clean(segments: list[dict], chapters: list[dict] | None = None) -> str | None:
    """
    Returns cleaned, structured Markdown text or None if below MIN_WORD_COUNT.

    - If `chapters` is provided and non-empty, uses them as section headings.
    - Otherwise falls back to heuristic heading detection.
    - Paragraph gap threshold is derived adaptively from the video's pacing.
    """
    if not segments:
        return None

    gap_threshold = _adaptive_gap_threshold(segments)

    # Prefer explicit chapters; heuristic detection runs inside _build_blocks
    # when no heading segments are injected.
    enriched = _inject_chapters(segments, chapters or [])

    blocks = _build_blocks(enriched, gap_threshold, config.PARAGRAPH_MIN_WORDS)

    # Filter out empty blocks and join
    text = "\n\n".join(b for b in blocks if b.strip())

    # Count only prose words (exclude heading lines)
    prose = " ".join(b for b in blocks if not b.startswith("##"))
    if len(prose.split()) < config.MIN_WORD_COUNT:
        return None

    return text
