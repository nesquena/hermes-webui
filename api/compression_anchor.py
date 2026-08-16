"""
Shared helpers for session compression anchor metadata.

Manual compression anchoring versus automatic compression paths
===============================================================

When ``auto_compression=True`` is passed to ``visible_messages_for_anchor()``,
the function accepts a broader set of message content types (including
provider-style ``input_text`` / ``output_text`` parts) and metadata markers
(``reasoning``, ``thinking``, etc.) from any non-tool role. This enables the
streaming auto-compression path to determine which messages should anchor
compression UI metadata without being limited to the legacy manual-compression
rules.

When ``auto_compression=False`` (the default), the function applies the
historical manual-compression rules: only plain ``text`` content parts from
non-assistant roles are counted.

Why this module exists
======================

Compression anchoring needs to identify which messages in a session transcript
are semantically significant enough to seed the compression UI metadata (e.g.,
message count, token budget display). The original implementation hard-coded
these rules in multiple places. This module consolidates the logic so that:

1. Manual compression anchoring (CLI/legacy path) uses the stricter ruleset.
2. Automatic compression (streaming/agent path) can leverage the relaxed ruleset
   when it knows it is handling provider-style messages.

Callers specify ``auto_compression=True`` when the messages may originate from
an automatic/compression-aware pipeline, and ``False`` (default) for manual
compression contexts.
"""


def _content_text(content, *, part_types):
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in part_types
        ).strip()
    return str(content or "").strip()


def _content_has_part_type(content, part_types):
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") in part_types
        for part in content
    )


def compaction_summary_segment(text):
    """Return the compaction-summary portion of a marker text, or None.

    Plain markers (text starting with ``[CONTEXT COMPACTION``) are returned
    unchanged.  The agent's merge-into-tail compaction instead wraps its
    summary in a ``[PRIOR CONTEXT — for reference only…]`` envelope where the
    ``[CONTEXT COMPACTION — REFERENCE ONLY]`` marker only appears *after* the
    ``[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]`` delimiter; for that
    shape, return the segment starting at the inner compaction marker.

    Returns None when the text carries no compaction marker in either shape,
    so callers can use this as a detector as well as an extractor.  Trust
    (the ``_compressed_summary`` stamp) is intentionally NOT checked here —
    callers own that decision.
    """
    stripped = str(text or "").lstrip()
    low = stripped.lower()
    if low.startswith("[context compaction") or low.startswith("context compaction"):
        return stripped
    if not low.startswith("[prior context"):
        return None
    delim_idx = low.find("[end of prior context")
    if delim_idx == -1:
        return None
    after = stripped[delim_idx:]
    close = after.find("]")
    if close == -1:
        return None
    segment = after[close + 1:].lstrip()
    if segment.lower().startswith("[context compaction"):
        return segment
    return None


def is_context_compression_marker(message):
    """Return true for synthetic compression/reference cards, not user turns."""
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if not role or role == "tool":
        return False
    text = _content_text(
        message.get("content", ""),
        part_types={"text", "input_text", "output_text"},
    ).lower().lstrip()
    synthetic_unbracketed_marker = bool(message.get("_compressed_summary"))
    if (
        text.startswith("[context compaction")
        or (synthetic_unbracketed_marker and text.startswith("context compaction"))
        or text.startswith("[your active task list was preserved across context compression]")
        or text.startswith("[session arc summary")
    ):
        return True
    # Agent merge-into-tail compaction: the compaction marker is embedded
    # after the [PRIOR CONTEXT …] envelope. Only the synthetic
    # ``_compressed_summary`` stamp (never forgeable by pasted user text)
    # makes this shape a marker — fail closed otherwise.
    if synthetic_unbracketed_marker and compaction_summary_segment(text) is not None:
        return True
    return False


def _is_context_compression_marker(message):
    """Backward-compatible alias for callers that have not switched yet."""
    return is_context_compression_marker(message)


def visible_messages_for_anchor(messages, *, auto_compression: bool = False):
    """Return transcript messages that can anchor compression UI metadata.

    Manual compression historically only counted plain ``text`` content parts
    for non-assistant messages, while the streaming auto-compression path also
    accepted provider-style ``input_text`` / ``output_text`` parts and metadata
    markers on any non-tool role. Keep that difference explicit at the call site
    instead of carrying two near-identical helper implementations.
    """
    out = []
    text_part_types = {"text", "input_text", "output_text"} if auto_compression else {"text"}
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if not role or role == "tool":
            continue
        if _is_context_compression_marker(message):
            continue

        content = message.get("content", "")
        has_attachments = bool(message.get("attachments"))
        text = _content_text(content, part_types=text_part_types)

        if auto_compression:
            has_tool_calls = bool(
                isinstance(message.get("tool_calls"), list) and message.get("tool_calls")
            )
            has_tool_use = _content_has_part_type(content, {"tool_use"})
            has_reasoning = bool(message.get("reasoning"))
            if not text:
                has_reasoning = has_reasoning or _content_has_part_type(
                    content,
                    {"thinking", "reasoning"},
                )
            if text or has_attachments or has_tool_calls or has_tool_use or has_reasoning:
                out.append(message)
            continue

        if role == "assistant":
            has_tool_calls = bool(
                isinstance(message.get("tool_calls"), list) and message.get("tool_calls")
            )
            has_tool_use = _content_has_part_type(content, {"tool_use"})
            has_reasoning = bool(message.get("reasoning")) or _content_has_part_type(
                content,
                {"thinking", "reasoning"},
            )
            if text or has_attachments or has_tool_calls or has_tool_use or has_reasoning:
                out.append(message)
            continue

        if text or has_attachments:
            out.append(message)
    return out
