"""Clean-room Capy Memory Tree primitives.

This module stores and exposes only bounded, sanitized summaries. Retrieved
memory is advisory context; it must not bypass Spaces safety gates, prompt
injection checks, approval gates, or rollback/recovery controls.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

_MAX_SCAN_DEPTH = 24
_MAX_SCAN_NODES = 2_000
_MAX_TEXT_LEN = 500

_UNSAFE_KEY_RE = re.compile(
    r"renderer|html|script|source|data|code|body|generated[_-]?code|generatedbody|rendercode|widgetbody|"
    r"api[_-]?key|apiauth|authorization|bearer|token|secret|password|credential|"
    r"^on[a-z]+$",
    re.IGNORECASE,
)

_UNSAFE_VALUE_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|bearer\s+placeholder|raw\s+prompt|"
    r"system\s+prompt|developer\s+prompt|prompt\s+injection|ignore\s+previous\s+instructions|"
    r"<[^>]+\bon[a-z]+\s*=",
    re.IGNORECASE,
)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def _safe_text(value: Any, *, limit: int = _MAX_TEXT_LEN) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if _UNSAFE_VALUE_RE.search(text):
        return ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _safe_id(value: Any, *, fallback: str = "unknown") -> str:
    text = _safe_text(value, limit=160)
    text = _SAFE_ID_RE.sub("-", text).strip("-._:")
    return text or fallback


def _scan_for_unsafe(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> int:
    """Return count of unsafe fields/values and fail closed on complex input."""
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if depth > _MAX_SCAN_DEPTH or nodes[0] > _MAX_SCAN_NODES:
        raise ValueError("source metadata is too deep or too complex to canonicalize safely")

    dropped = 0
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if _UNSAFE_KEY_RE.search(key_text):
                dropped += 1
                continue
            dropped += _scan_for_unsafe(item, depth=depth + 1, nodes=nodes)
        return dropped
    if isinstance(value, (list, tuple)):
        for item in value:
            dropped += _scan_for_unsafe(item, depth=depth + 1, nodes=nodes)
        return dropped
    if isinstance(value, str) and _UNSAFE_VALUE_RE.search(value):
        return 1
    return 0


def _frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key in sorted(data):
        val = _safe_text(data[key], limit=1_000)
        lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _append_line(lines: list[str], label: str, value: Any) -> None:
    safe = _safe_text(value)
    if safe:
        lines.append(f"- {label}: {safe}")


def canonicalize_space_manifest(space: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, metadata-only memory record for a Space manifest."""
    if not isinstance(space, dict):
        raise ValueError("space manifest must be a mapping")

    dropped_field_count = _scan_for_unsafe(space)
    space_id = _safe_id(space.get("space_id") or space.get("id"), fallback="space")
    name = _safe_text(space.get("name") or space_id, limit=200) or space_id
    description = _safe_text(space.get("description"), limit=700)
    template = _safe_text(space.get("template"), limit=160)
    revision = _safe_text(space.get("revision_event_id"), limit=160)

    body_lines: list[str] = [f"# {name}", "", "## Space metadata"]
    _append_line(body_lines, "space_id", space_id)
    _append_line(body_lines, "name", name)
    _append_line(body_lines, "description", description)
    _append_line(body_lines, "template", template)
    if space.get("metadata_only") is not None:
        body_lines.append(f"- metadata_only: {bool(space.get('metadata_only'))}")
    _append_line(body_lines, "revision_event_id", revision)

    widgets = space.get("widgets") if isinstance(space.get("widgets"), list) else []
    if widgets:
        body_lines.extend(["", "## Widgets"])
        for raw_widget in widgets[:50]:
            if not isinstance(raw_widget, dict):
                dropped_field_count += 1
                continue
            widget_id = _safe_id(raw_widget.get("id"), fallback="widget")
            title = _safe_text(raw_widget.get("title"), limit=200) or widget_id
            kind = _safe_text(raw_widget.get("kind"), limit=120)
            parts = [widget_id, title]
            if kind:
                parts.append(kind)
            body_lines.append("- " + " | ".join(parts))

    body = "\n".join(body_lines).strip() + "\n"
    content_sha256 = _sha256(body)
    source_id_seed = f"space_manifest:{space_id}:{content_sha256}"
    source_id = "cmt-src-" + _sha256(source_id_seed)[:24]
    chunk_id = "cmt-chunk-" + _sha256(f"{source_id}:0:{content_sha256}")[:24]
    redaction_status = "dropped_fields" if dropped_field_count else "none"
    frontmatter = _frontmatter(
        {
            "source_id": source_id,
            "source_type": "space_manifest",
            "origin_uri": f"capy-space://{space_id}",
            "space_id": space_id,
            "content_sha256": content_sha256,
            "redaction_status": redaction_status,
        }
    )
    markdown = frontmatter + "\n\n" + body

    return {
        "source_id": source_id,
        "chunk_id": chunk_id,
        "source_type": "space_manifest",
        "origin_uri": f"capy-space://{space_id}",
        "space_id": space_id,
        "content_sha256": content_sha256,
        "redaction_status": redaction_status,
        "dropped_field_count": dropped_field_count,
        "markdown": markdown,
    }


__all__ = ["canonicalize_space_manifest"]
