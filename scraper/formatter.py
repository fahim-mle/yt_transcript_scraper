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
    return name[:max_len] or "untitled"


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
