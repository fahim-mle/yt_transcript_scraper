"""
Rewrites cleaned caption prose into a readable article — losslessly.

This is the opposite of summarisation. `llm_processor` compresses a transcript
into metadata; this module keeps every point and only changes *how* it reads:
disfluencies and false starts repaired, punctuation restored, speaker turns
attributed, enumerations turned into lists, headings inserted where the talk
actually changes subject.

Two design constraints drive everything here:

  1. **Chunked, never one-shot.** A 60k-word transcript would need the model to
     emit 80k+ tokens in a single response, where quality collapses and the KV
     cache stops fitting in memory. Chunks break on paragraph and chapter
     boundaries so no sentence is ever split, and each chunk sees the tail of
     the previous chunk's output so prose flows across the seam.

  2. **Coverage is enforced, not hoped for.** Every rewritten chunk is checked
     against its source before being accepted: the word ratio must land in a
     band, and the chunk's distinctive content words must survive. A chunk that
     fails is retried cooler, then falls back to its verbatim source text. The
     article is therefore never quietly shorter than the transcript — a failed
     chunk degrades to unpolished prose, never to missing prose.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)


# ── Chunking ───────────────────────────────────────────────────────────────

def _split_blocks(cleaned_text: str) -> list[tuple[str, str]]:
    """Split cleaned markdown into ('heading'|'para', text) blocks."""
    blocks: list[tuple[str, str]] = []
    for raw in cleaned_text.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        blocks.append(("heading", block.lstrip("#").strip()) if block.startswith("#")
                      else ("para", block))
    return blocks


class Chunk:
    """A unit of work: one heading's worth of prose, or a slice of a long run."""

    def __init__(self, heading: str | None, paragraphs: list[str]) -> None:
        self.heading = heading
        self.paragraphs = paragraphs
        self.text = "\n\n".join(paragraphs)
        self.words = len(self.text.split())


def chunk_blocks(blocks: list[tuple[str, str]], target_words: int) -> list[Chunk]:
    """
    Group blocks into chunks of roughly `target_words`, never splitting a
    paragraph and always starting fresh at a heading. A heading's prose that
    runs long is split across several chunks that all carry that heading —
    the assembler emits the heading once.
    """
    chunks: list[Chunk] = []
    heading: str | None = None
    pending: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal pending, count
        if pending:
            chunks.append(Chunk(heading, pending))
            pending = []
            count = 0

    for kind, text in blocks:
        if kind == "heading":
            flush()
            heading = text
            continue
        pending.append(text)
        count += len(text.split())
        if count >= target_words:
            flush()

    flush()
    return chunks


# ── Coverage guard ─────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")

# Frequent words carry no signal about whether content survived, so they are
# excluded from retention. Deliberately short: the goal is to keep rare,
# topic-bearing terms in the denominator, not to build a real stoplist.
_STOPWORDS = frozenset("""
about above after again against all also and any are because been before being
below between both but can did does doing down during each few for from further
had has have having here how into its itself just more most not now off once
only other our out over own same should some such than that the their them then
there these they this those through too under until very was were what when
where which while who whom why will with would you your yours are our
like really actually basically thing things going get got make makes made say
says said know think see look kind sort lot want need use used using way ways
""".split())


def _content_words(text: str) -> set[str]:
    """Distinctive lowercase tokens — the vocabulary a rewrite must preserve."""
    return {w for w in _WORD_RE.findall(text.lower())
            if len(w) > 3 and w not in _STOPWORDS}


def coverage(source: str, output: str) -> tuple[float, float]:
    """Return (word_ratio, content_word_retention) for a rewritten chunk."""
    src_words = len(source.split())
    ratio = len(output.split()) / src_words if src_words else 0.0

    src_terms = _content_words(source)
    if not src_terms:
        return ratio, 1.0
    kept = src_terms & _content_words(output)
    return ratio, len(kept) / len(src_terms)


def _coverage_failure(source: str, output: str) -> str | None:
    """Return a reason string when a rewrite is not acceptable, else None."""
    if not output.strip():
        return "empty output"
    ratio, retention = coverage(source, output)
    if ratio < config.REWRITE_MIN_RATIO:
        return f"too short (ratio {ratio:.2f} < {config.REWRITE_MIN_RATIO})"
    if ratio > config.REWRITE_MAX_RATIO:
        return f"too long (ratio {ratio:.2f} > {config.REWRITE_MAX_RATIO})"
    if retention < config.REWRITE_MIN_RETENTION:
        return f"dropped content (retention {retention:.2f} < {config.REWRITE_MIN_RETENTION})"
    return None


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are an editor who turns raw speech-to-text transcripts into clean, readable "
    "written prose for a technical knowledge base.\n\n"
    "Your single hard rule: PRESERVE ALL CONTENT. You are rewriting, not summarising. "
    "Every claim, example, number, name, caveat and aside in the input must survive in "
    "your output. If the input takes 400 words to make a point, your output takes about "
    "400 words to make the same point — better written, not shorter.\n\n"
    "What to change:\n"
    "- Repair disfluencies, false starts, stutters and mid-sentence restarts.\n"
    "- Restore sentence boundaries and punctuation; fix obvious transcription garble "
    "when the intended words are unambiguous from context.\n"
    "- Break run-on speech into paragraphs at real topic shifts.\n"
    "- When the speaker enumerates, format it as a markdown list.\n"
    "- Format code, commands, filenames and identifiers as `inline code` or fenced blocks.\n"
    "- Attribute speaker turns. Audience questions become **Q:** and answers **A:**.\n"
    "- Replace transcription markers like [INAUDIBLE] with nothing, and repair the "
    "sentence around them if the meaning is clear; otherwise leave the sentence as is.\n"
    "- Insert `### ` subheadings where the subject genuinely changes.\n\n"
    "What NOT to do:\n"
    "- Do not summarise, compress, or skip repetitive passages.\n"
    "- Do not add facts, opinions, examples or conclusions that are not in the input.\n"
    "- Do not add a preamble, a closing summary, or commentary about the transcript.\n"
    "- Do not write 'the speaker says' — write the content directly, in their voice.\n\n"
    "Output only the rewritten markdown. No preamble, no explanation."
)

# qwen's reliable soft-switch for skipping chain-of-thought. The `think: false`
# API flag is not honoured on every Ollama build, and on a rewrite pass thinking
# tokens are catastrophic: the model spends minutes reasoning about prose it
# should simply be rewriting, turning a 40-second chunk into a 10-minute one.
# Applied by model family so other models don't get a stray literal token.
_NO_THINK_PREFIXES = ("qwen",)


def _system_prompt() -> str:
    if config.REWRITE_MODEL.lower().startswith(_NO_THINK_PREFIXES):
        return "/no_think\n" + _SYSTEM
    return _SYSTEM


def _build_prompt(title: str, heading: str | None, context_tail: str, chunk_text: str) -> str:
    parts = [f"Source video: {title}"]
    if heading:
        parts.append(f"Current section: {heading}")
    if context_tail:
        parts += [
            "",
            "The article so far ends with the text below. Continue naturally from it — "
            "do not repeat it and do not re-introduce the topic.",
            "<<<", context_tail, ">>>",
        ]
    parts += [
        "",
        "Rewrite this transcript excerpt as clean prose, preserving every point:",
        "<<<", chunk_text, ">>>",
    ]
    return "\n".join(parts)


# ── Ollama call ────────────────────────────────────────────────────────────

def _output_ceiling(source_words: int) -> int:
    """
    Hard cap on generated tokens for one chunk.

    A rewrite that has already exceeded the coverage guard's upper ratio is
    going to be rejected anyway, so there is no reason to keep paying for it.
    Without this a rambling model burns the entire request timeout per attempt
    — observed with a thinking model that ignored both `think: false` and the
    `/no_think` switch. Truncated output fails the guard and falls back, which
    is the correct outcome; the point is to fail in seconds, not in minutes.
    """
    return int(source_words * config.REWRITE_MAX_RATIO * 1.4) + 128


def _call(prompt: str, temperature: float, source_words: int) -> str | None:
    payload = {
        "model": config.REWRITE_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        # Rewriting is a transform, not a reasoning task. Thinking tokens would
        # double the cost of the slowest stage in the pipeline for no gain.
        "think": False,
        "options": {
            "temperature": temperature,
            "num_ctx": config.REWRITE_NUM_CTX,
            "num_predict": _output_ceiling(source_words),
        },
    }
    req = urllib.request.Request(
        f"{config.OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.REWRITE_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        logger.warning("Rewrite request failed (%s). Is Ollama running with %s pulled?",
                       exc, config.REWRITE_MODEL)
        return None
    except (TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Rewrite response problem: %s", exc)
        return None

    return _strip_fences((body.get("message") or {}).get("content", "").strip())


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```$", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Models sometimes wrap the whole answer in a markdown fence. Unwrap it."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


# ── Public API ─────────────────────────────────────────────────────────────

class Result:
    """The rewritten article plus per-run stats for logging and the run log."""

    def __init__(self, article: str | None, *, chunks: int, fallbacks: int,
                 duration_ms: int, error: str | None) -> None:
        self.article = article
        self.chunks = chunks
        self.fallbacks = fallbacks
        self.duration_ms = duration_ms
        self.error = error
        self.ok = article is not None
        self.model = config.REWRITE_MODEL


def rewrite_chunk(chunk: Chunk, title: str, context_tail: str) -> tuple[str, bool]:
    """
    Rewrite one chunk. Returns (text, used_fallback).

    Falls back to the verbatim source text when the model is unreachable or
    every attempt fails the coverage guard — an unpolished paragraph is a far
    better outcome than a silently missing one.
    """
    prompt = _build_prompt(title, chunk.heading, context_tail, chunk.text)

    for attempt in range(config.REWRITE_MAX_ATTEMPTS):
        # Cool down on retry: the usual failure is a chatty model padding or
        # summarising, and lower temperature makes it track the source harder.
        output = _call(prompt, 0.3 if attempt == 0 else 0.15, chunk.words)
        if output is None:
            break
        failure = _coverage_failure(chunk.text, output)
        if failure is None:
            return output, False
        logger.warning("Chunk rejected (attempt %d/%d): %s",
                       attempt + 1, config.REWRITE_MAX_ATTEMPTS, failure)

    logger.warning("Keeping verbatim text for a %d-word chunk after %d failed attempt(s).",
                   chunk.words, config.REWRITE_MAX_ATTEMPTS)
    return chunk.text, True


def rewrite(cleaned_text: str, title: str) -> Result:
    """
    Rewrite a whole cleaned transcript into article markdown.

    Always returns a Result. `ok` is False only when there was nothing to do;
    individual chunk failures degrade to verbatim text and are counted in
    `fallbacks` rather than failing the run.
    """
    started = time.monotonic()

    chunks = chunk_blocks(_split_blocks(cleaned_text), config.REWRITE_CHUNK_WORDS)
    if not chunks:
        return Result(None, chunks=0, fallbacks=0,
                      duration_ms=int((time.monotonic() - started) * 1000),
                      error="no_prose_to_rewrite")

    total_words = sum(c.words for c in chunks)
    logger.info("Rewriting %d words in %d chunk(s) with %s.",
                total_words, len(chunks), config.REWRITE_MODEL)

    parts: list[str] = []
    context_tail = ""
    fallbacks = 0
    last_heading: str | None = None

    for i, chunk in enumerate(chunks, 1):
        text, used_fallback = rewrite_chunk(chunk, title, context_tail)
        fallbacks += used_fallback

        # A long section spans several chunks; emit its heading only once.
        if chunk.heading and chunk.heading != last_heading:
            parts.append(f"## {chunk.heading}")
            last_heading = chunk.heading
        parts.append(text)

        context_tail = " ".join(text.split()[-config.REWRITE_CONTEXT_WORDS:])
        logger.info("  chunk %d/%d — %d words in, %d out%s",
                    i, len(chunks), chunk.words, len(text.split()),
                    " (verbatim fallback)" if used_fallback else "")

    elapsed = int((time.monotonic() - started) * 1000)
    article = "\n\n".join(parts)
    logger.info("Rewrote %s in %.1f min — %d chunk(s), %d fallback(s), %d → %d words.",
                title, elapsed / 60000, len(chunks), fallbacks,
                total_words, len(article.split()))
    return Result(article, chunks=len(chunks), fallbacks=fallbacks,
                  duration_ms=elapsed, error=None)


def is_available() -> bool:
    """Reachability check for the Ollama server (shared host with enrichment)."""
    try:
        req = urllib.request.Request(f"{config.OLLAMA_HOST}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False
