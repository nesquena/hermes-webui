"""Helpers for keeping internal context material out of WebUI display history."""

from __future__ import annotations

from collections.abc import Iterable
import copy


def message_text(content) -> str:
    """Return readable text from string or provider-style content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def is_context_compression_marker(message) -> bool:
    """Return true for internal context-compression handoff/reference rows."""
    if not isinstance(message, dict):
        return False
    text = message_text(message.get("content", "")).lower()
    return (
        "context compaction" in text
        or "context compression" in text
        or "context was auto-compressed" in text
        or "active task list was preserved across context compression" in text
    )


def sanitize_display_messages(messages: Iterable | None) -> list:
    """Return a display transcript with internal compaction markers removed.

    Context-compression handoff rows are model-facing recovery material. They may
    exist in context_messages or provider results, but must not become normal
    WebUI transcript rows or SSE done.session.messages entries.
    """
    if not messages:
        return []
    return [
        copy.deepcopy(message)
        for message in messages
        if isinstance(message, dict) and not is_context_compression_marker(message)
    ]
