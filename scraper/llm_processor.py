"""
LLM enrichment for cleaned transcripts.

Sends the cleaned prose to a local Ollama model and gets back structured
content metadata: a summary, key concepts, domains, difficulty, content kind,
and a section-level breakdown. Output is constrained to a JSON schema (Ollama
structured outputs) and validated with Pydantic, so callers always receive a
well-formed object or None.

Depends only on the stdlib for HTTP (urllib) plus Pydantic for validation —
no extra Ollama client library required.
"""

import logging
import time

from pydantic import BaseModel, Field, ValidationError

import config
from scraper import llm_client

logger = logging.getLogger(__name__)


# ── Output schema ──────────────────────────────────────────────────────────

class Section(BaseModel):
    heading: str = Field(..., description="Short title for this part of the video")
    summary: str = Field(..., description="1-2 sentence summary of what this section covers")


class Enrichment(BaseModel):
    summary: str = Field(..., description="2-4 sentence overview of the whole video")
    # json_schema_extra nudges Ollama's grammar toward 3-8 items without making
    # validation hard-fail if the model still returns fewer (we'd rather keep a
    # good summary than discard the whole result over one field).
    key_concepts: list[str] = Field(
        default_factory=list,
        description="REQUIRED. 3-8 specific technical terms or named concepts from the video. Never empty.",
        json_schema_extra={"minItems": 3, "maxItems": 8},
    )
    domains: list[str] = Field(
        default_factory=list,
        description="1-4 subject areas, e.g. 'machine learning'",
        json_schema_extra={"minItems": 1, "maxItems": 4},
    )
    difficulty: str = Field(..., description="beginner, intermediate, or advanced")
    content_kind: str = Field(..., description="tutorial, lecture, talk, interview, explainer, news, or other")
    sections: list[Section] = Field(default_factory=list, description="Ordered logical sections of the video")


_DIFFICULTY = {"beginner", "intermediate", "advanced"}
_CONTENT_KINDS = {"tutorial", "lecture", "talk", "interview", "explainer", "news", "other"}


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYSTEM_BODY = (
    "You are a precise metadata extractor for a personal knowledge base built from "
    "YouTube transcripts. You read a cleaned transcript and produce structured metadata. "
    "Base everything strictly on the transcript content — do not invent facts. "
    "Respond with JSON only, matching the provided schema."
)

# The leading /no_think is qwen3's reliable soft-switch to skip chain-of-thought.
# The `think: false` API flag isn't honored on all Ollama builds, and thinking
# tokens make CPU inference impractically slow, so we disable it in the prompt
# too. Applied by model family: on anything else it is a stray literal token in
# the system prompt, which is exactly the sort of leak a shared gateway invites.
_NO_THINK_PREFIXES = ("qwen",)


def _system_prompt() -> str:
    if config.ENRICH_MODEL.lower().startswith(_NO_THINK_PREFIXES):
        return "/no_think\n" + _SYSTEM_BODY
    return _SYSTEM_BODY


_SYSTEM = _SYSTEM_BODY  # back-compat for tests that assert on the prompt text


def _build_prompt(title: str, cleaned_text: str, chapter_titles: list[str]) -> str:
    parts = [f"Video title: {title}", ""]
    if chapter_titles:
        parts.append("Creator-provided chapters (use these as section headings when sensible):")
        parts.extend(f"  - {t}" for t in chapter_titles)
        parts.append("")
    parts += [
        "Transcript:",
        '"""',
        cleaned_text,
        '"""',
        "",
        "Produce ALL of the following fields:",
        "- summary: a concise overall summary.",
        "- key_concepts: 3 to 8 specific technical terms or named concepts actually "
        "discussed (e.g. 'gradient descent', 'activation function'). This is required — "
        "never return an empty list; always extract at least 3.",
        "- domains: 1 to 4 subject areas (e.g. 'machine learning').",
        "- difficulty: beginner, intermediate, or advanced.",
        "- content_kind: tutorial, lecture, talk, interview, explainer, news, or other.",
        "- sections: an ordered list, each with a heading and a 1-2 sentence summary.",
    ]
    return "\n".join(parts)


# ── Ollama call ────────────────────────────────────────────────────────────

def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    logger.info("Transcript is %d words; capping to %d for the model.", len(words), max_words)
    return " ".join(words[:max_words])


# Per-run performance counters, populated by _call_ollama from Ollama's own
# timing fields. Used by the benchmark command to report real tokens/sec.
# A caller resets before a run and reads after; harmless in normal use.
_call_stats: list[dict] = []


def reset_call_stats() -> None:
    _call_stats.clear()


def get_call_stats() -> list[dict]:
    return list(_call_stats)


def _call_model(prompt: str, schema: dict,
                usage: llm_client.Usage | None = None) -> dict | None:
    """Ask the configured provider for JSON matching `schema`; parsed dict or None."""
    result = llm_client.chat(
        model=config.ENRICH_MODEL,
        system=_system_prompt(),
        user=prompt,
        temperature=0.2,
        schema=schema,
        num_ctx=config.OLLAMA_NUM_CTX,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    if usage is not None:
        usage.add(result.usage)

    # Throughput counters for `benchmark`. Ollama reports nanosecond durations
    # per call; hosted providers report neither, so the benchmark's tok/s is
    # only meaningful against a local model — which is what it is for.
    _call_stats.append({
        "eval_count":           result.usage.completion_tokens,
        "eval_duration":        result.duration_ms * 1_000_000,
        "prompt_eval_count":    result.usage.prompt_tokens,
        "prompt_eval_duration": 0,
        "load_duration":        0,
    })

    return result.data if result.ok else None


def _normalise(e: Enrichment) -> Enrichment:
    """Clamp free-text enums to the allowed vocabularies; leave the rest intact."""
    diff = e.difficulty.strip().lower()
    e.difficulty = diff if diff in _DIFFICULTY else "intermediate"
    kind = e.content_kind.strip().lower()
    e.content_kind = kind if kind in _CONTENT_KINDS else "other"
    e.key_concepts = [c.strip() for c in e.key_concepts if c.strip()][:8]
    e.domains = [d.strip().lower() for d in e.domains if d.strip()][:4]
    return e


class _Concepts(BaseModel):
    concepts: list[str]


def _extract_concepts(enrichment: Enrichment,
                      usage: llm_client.Usage | None = None) -> list[str]:
    """
    Fallback for the empty-key_concepts case: a JSON grammar doesn't enforce
    array minItems, so the model sometimes returns []. Here we make one small,
    fast follow-up call over the already-generated summary + section notes to
    pull out concrete concepts. Cheap even on CPU (short input and output).
    """
    notes = enrichment.summary + "\n" + "\n".join(
        f"{s.heading}: {s.summary}" for s in enrichment.sections
    )
    prompt = (
        "From the following video summary and section notes, list 3 to 8 specific "
        "technical terms or named concepts (short noun phrases, e.g. "
        "'gradient descent'). Return them as the 'concepts' array.\n\n" + notes
    )
    raw = _call_model(prompt, _Concepts.model_json_schema(), usage)
    if not raw:
        return []
    try:
        concepts = _Concepts.model_validate(raw).concepts
    except ValidationError:
        return []
    return [c.strip() for c in concepts if c.strip()][:8]


# ── Public API ─────────────────────────────────────────────────────────────

class Result:
    """Carries the enrichment plus timing/status for the processing_runs log."""
    def __init__(self, enrichment: Enrichment | None, duration_ms: int, error: str | None,
                 usage: llm_client.Usage | None = None):
        self.enrichment = enrichment
        self.duration_ms = duration_ms
        self.error = error
        self.ok = enrichment is not None
        self.model = config.ENRICH_MODEL
        self.provider = config.LLM_PROVIDER
        self.usage = usage or llm_client.Usage(calls=0)


def enrich(title: str, cleaned_text: str, chapters: list[dict] | None = None) -> Result:
    """
    Derive content metadata from a cleaned transcript.

    Always returns a Result; Result.ok is False (with .error set) when the model
    is unreachable or the output can't be validated — callers should fall back
    to writing a plain clean .md in that case.
    """
    started = time.monotonic()
    usage = llm_client.Usage(calls=0)
    chapter_titles = [c["title"].strip() for c in (chapters or []) if c.get("title")]
    prompt = _build_prompt(title, _cap_words(cleaned_text, config.LLM_MAX_INPUT_WORDS), chapter_titles)

    raw = _call_model(prompt, Enrichment.model_json_schema(), usage)

    if raw is None:
        elapsed = int((time.monotonic() - started) * 1000)
        return Result(None, elapsed, "llm_unavailable_or_invalid_json", usage)

    try:
        enrichment = _normalise(Enrichment.model_validate(raw))
    except ValidationError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning("Enrichment failed schema validation: %s", exc)
        return Result(None, elapsed, f"validation_error: {exc.error_count()} issue(s)", usage)

    # The grammar can't enforce a non-empty key_concepts, so recover it here.
    if not enrichment.key_concepts:
        logger.info("key_concepts empty — running targeted concept extraction.")
        enrichment.key_concepts = _extract_concepts(enrichment, usage)

    elapsed = int((time.monotonic() - started) * 1000)
    logger.info(
        "Enriched in %d ms — %d concepts, %d sections.",
        elapsed, len(enrichment.key_concepts), len(enrichment.sections),
    )
    return Result(enrichment, elapsed, None, usage)


def is_available() -> bool:
    """Quick reachability check for the configured LLM provider."""
    return llm_client.is_available()
