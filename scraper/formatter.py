"""
Converts video metadata + transcript segments into .md and .json output.
"""

import html
import json
import re


def format_timestamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"[{h:02d}:{m:02d}:{sec:02d}]"
    return f"[{m:02d}:{sec:02d}]"


def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = name[:max_len]
    return "untitled" if name in {"", ".", ".."} else name


def to_markdown(metadata: dict, segments: list[dict]) -> str:
    title = metadata.get("title", "")
    channel = metadata.get("channel", "")
    published = metadata.get("published", "")
    url = metadata.get("url", "")
    description = (metadata.get("description") or "").strip()

    # YAML frontmatter — indent multi-line description
    desc_yaml = _yaml_block_scalar(description) if description else '""'

    lines = [
        "---",
        f'title: "{_escape_yaml(title)}"',
        f'channel: "{_escape_yaml(channel)}"',
        f'published: "{published}"',
        f'url: "{url}"',
        f"description: {desc_yaml}",
        "---",
        "",
        "## Transcript",
        "",
    ]

    for seg in segments:
        ts = format_timestamp(seg["start"])
        text = seg["text"].replace("\n", " ").strip()
        lines.append(f"{ts} {text}")

    lines.append("")
    return "\n".join(lines)


def to_clean_markdown(metadata: dict, cleaned_text: str) -> str:
    """Produces a clean .md with YAML frontmatter and paragraph-structured body."""
    title = metadata.get("title", "")
    channel = metadata.get("channel", "")
    published = metadata.get("published", "")
    url = metadata.get("url", "")
    description = (metadata.get("description") or "").strip()
    desc_yaml = _yaml_block_scalar(description) if description else '""'

    lines = [
        "---",
        f'title: "{_escape_yaml(title)}"',
        f'channel: "{_escape_yaml(channel)}"',
        f'published: "{published}"',
        f'url: "{url}"',
        f"description: {desc_yaml}",
        "---",
        "",
        "## Transcript",
        "",
        cleaned_text,
        "",
    ]
    return "\n".join(lines)


def to_knowledge_doc(metadata: dict, cleaned_text: str, enrichment: dict | None) -> str:
    """
    Rich knowledge document: enriched YAML frontmatter + structured body
    (summary, key concepts, section breakdown) followed by the clean transcript.

    Falls back to a plain clean .md when `enrichment` is None (LLM unavailable).
    """
    if enrichment is None:
        return to_clean_markdown(metadata, cleaned_text)

    title = metadata.get("title", "")
    channel = metadata.get("channel", "")
    published = metadata.get("published", "")
    url = metadata.get("url", "")
    description = (metadata.get("description") or "").strip()
    desc_yaml = _yaml_block_scalar(description) if description else '""'

    summary = (enrichment.get("summary") or "").strip()
    key_concepts = enrichment.get("key_concepts") or []
    domains = enrichment.get("domains") or []
    difficulty = enrichment.get("difficulty") or ""
    content_kind = enrichment.get("content_kind") or ""
    sections = enrichment.get("sections") or []
    summary_yaml = _yaml_block_scalar(summary) if summary else '""'

    lines = [
        "---",
        f'title: "{_escape_yaml(title)}"',
        f'channel: "{_escape_yaml(channel)}"',
        f'published: "{published}"',
        f'url: "{url}"',
        f'difficulty: "{difficulty}"',
        f'content_kind: "{content_kind}"',
        f"domains: {_yaml_flow_list(domains)}",
        f"key_concepts: {_yaml_flow_list(key_concepts)}",
        f"summary: {summary_yaml}",
        f"description: {desc_yaml}",
        "---",
        "",
    ]

    if summary:
        lines += ["## Summary", "", summary, ""]

    if key_concepts:
        lines += ["## Key Concepts", ""]
        lines += [f"- {c}" for c in key_concepts]
        lines.append("")

    if sections:
        lines += ["## Sections", ""]
        for s in sections:
            heading = (s.get("heading") or "").strip()
            s_summary = (s.get("summary") or "").strip()
            if not heading:
                continue
            lines.append(f"### {heading}")
            if s_summary:
                lines += ["", s_summary]
            lines.append("")

    lines += ["## Transcript", "", cleaned_text, ""]
    return "\n".join(lines)


# Marks the start of rewritten prose. `enrich` reads the body back out from
# here, so it can add metadata without paying for the rewrite a second time.
ARTICLE_MARKER = "## Article"

_ARTICLE_BODY_RE = re.compile(rf"^{re.escape(ARTICLE_MARKER)}\s*$", re.MULTILINE)


def extract_article_body(md_content: str) -> str | None:
    """Return the article prose from a rendered article doc, or None."""
    m = _ARTICLE_BODY_RE.search(md_content)
    if not m:
        return None
    body = md_content[m.end():].strip()
    return body or None


def to_article_doc(metadata: dict, article_body: str, enrichment: dict | None = None) -> str:
    """
    The primary artifact: enriched frontmatter, then the rewritten article.

    The verbatim transcript is NOT included — it lives beside this file as
    `<stem>.transcript.md` so the readable version stays readable and the
    ground truth stays citable.
    """
    lines = ["---"] + _frontmatter_lines(metadata, enrichment) + ["---", ""]

    if enrichment:
        summary = (enrichment.get("summary") or "").strip()
        key_concepts = enrichment.get("key_concepts") or []
        if summary:
            lines += ["## Summary", "", summary, ""]
        if key_concepts:
            lines += ["## Key Concepts", ""] + [f"- {c}" for c in key_concepts] + [""]

    lines += [ARTICLE_MARKER, "", article_body, ""]
    return "\n".join(lines)


def to_transcript_doc(metadata: dict, cleaned_text: str) -> str:
    """The verbatim companion — unmodified cleaned prose, for citation and embedding."""
    return to_clean_markdown(metadata, cleaned_text)


def _frontmatter_lines(metadata: dict, enrichment: dict | None) -> list[str]:
    """Shared frontmatter body (no fences) for the article and knowledge docs."""
    description = (metadata.get("description") or "").strip()
    lines = [
        f'title: "{_escape_yaml(metadata.get("title", ""))}"',
        f'channel: "{_escape_yaml(metadata.get("channel", ""))}"',
        f'published: "{metadata.get("published", "")}"',
        f'url: "{metadata.get("url", "")}"',
    ]
    if enrichment:
        summary = (enrichment.get("summary") or "").strip()
        lines += [
            f'difficulty: "{enrichment.get("difficulty") or ""}"',
            f'content_kind: "{enrichment.get("content_kind") or ""}"',
            f"domains: {_yaml_flow_list(enrichment.get('domains') or [])}",
            f"key_concepts: {_yaml_flow_list(enrichment.get('key_concepts') or [])}",
            f"summary: {_yaml_block_scalar(summary) if summary else '\"\"'}",
        ]
    lines.append(f"description: {_yaml_block_scalar(description) if description else '\"\"'}")
    return lines


def to_json(segments: list[dict]) -> str:
    return json.dumps(segments, ensure_ascii=False, indent=2)


def transcript_to_text(segments: list[dict]) -> str:
    """Clean plain-text transcript — HTML entities decoded, segments joined."""
    parts = [html.unescape(seg["text"]).replace("\n", " ").strip() for seg in segments]
    return " ".join(p for p in parts if p)


def to_jsonl_record(metadata: dict, segments: list[dict], md_path: str) -> dict:
    text = transcript_to_text(segments)
    return {
        "video_id":           metadata.get("video_id", ""),
        "url":                metadata.get("url", ""),
        "title":              metadata.get("title", ""),
        "channel":            metadata.get("channel", ""),
        "channel_id":         metadata.get("channel_id", ""),
        "published":          metadata.get("published", ""),
        "description":        metadata.get("description", ""),
        "chapters":           metadata.get("chapters", []),
        "transcript_text":    text,
        "transcript_segments": segments,
        "word_count":         len(text.split()),
        "md_path":            md_path,
    }


def _escape_yaml(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_block_scalar(s: str) -> str:
    indented = "\n".join(f"  {line}" for line in s.splitlines())
    return f"|\n{indented}"


def _yaml_flow_list(items: list[str]) -> str:
    """Render a list as a YAML flow sequence, e.g. ["ml", "python"]."""
    if not items:
        return "[]"
    quoted = ", ".join(f'"{_escape_yaml(str(i))}"' for i in items)
    return f"[{quoted}]"
