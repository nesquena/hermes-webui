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


# Compaction carrier vocabulary (single owner for the WebUI server).
#
# When context compaction folds the summary into the first retained tail row,
# the Agent prepends this header and appends the summary behind the delimiter,
# setting ``_compressed_summary`` on the row. The authentic retained text of the
# row sits BETWEEN the header and the delimiter (#8709).
COMPACTION_HANDOFF_HEADER_PREFIX = "[prior context"
COMPACTION_HANDOFF_DELIMITER_PREFIX = "[end of prior context"
COMPACTION_PROMPT_CARD_PREFIX = "[context compaction"


def _marker_text(message):
    """Return the lower-cased, left-trimmed text of a transcript message."""
    return _content_text(
        message.get("content", ""),
        part_types={"text", "input_text", "output_text"},
    ).lower().lstrip()


def is_context_compression_marker(message):
    """Return true for synthetic compression/reference cards, not user turns.

    This is the ONE owner of the compaction-prefix vocabulary for the WebUI
    server; ``api/models.py`` and ``static/ui.js`` mirror it instead of keeping
    their own copies. ``[PRIOR CONTEXT ...]`` is the merged-carrier header the
    Agent prepends when it folds the summary into a retained tail row (#8709);
    the row also carries ``_compressed_summary``, which is the flag the state.db
    reader now selects so this predicate receives the signal.
    """
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if not role or role == "tool":
        return False
    text = _marker_text(message)
    synthetic_unbracketed_marker = bool(message.get("_compressed_summary"))
    return (
        text.startswith(COMPACTION_PROMPT_CARD_PREFIX)
        or (synthetic_unbracketed_marker and text.startswith("context compaction"))
        or text.startswith(COMPACTION_HANDOFF_HEADER_PREFIX)
        or text.startswith("[your active task list was preserved across context compression]")
        or text.startswith("[session arc summary")
    )


def is_prompt_compaction_marker(message):
    """Return true only for prompt-compaction summary cards.

    Compression-adjacent summaries (session arc, preserved task list) share the
    generic marker predicate but are not prompt compaction summaries. Callers
    that make fail-closed replay decisions must use this narrower predicate;
    it replaces the duplicated ``[context compaction`` prefix test that used to
    live in ``api/models.py``.
    """
    if not is_context_compression_marker(message):
        return False
    text = _marker_text(message)
    return text.startswith(COMPACTION_PROMPT_CARD_PREFIX) or text.startswith("context compaction")


def is_compaction_handoff_carrier(message):
    """Return true when *message* carries a synthetic compaction handoff blob.

    Durable shapes produced by the Agent compressor:

    * standalone summary card - content starts with ``[context compaction`` (the
      unbracketed form is accepted when ``_compressed_summary`` is set);
    * merged carrier - the summary was folded into the first retained tail row,
      whose content starts with the ``[PRIOR CONTEXT ...]`` header, keeps the
      authentic row text, then reaches the summary behind the delimiter. The
      row still carries ``_compressed_summary``.
    """
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    if not role or role == "tool":
        return False
    text = _marker_text(message)
    if text.startswith(COMPACTION_PROMPT_CARD_PREFIX) or text.startswith(
        COMPACTION_HANDOFF_HEADER_PREFIX
    ):
        return True
    if not message.get("_compressed_summary"):
        return False
    return text.startswith("context compaction") or COMPACTION_HANDOFF_DELIMITER_PREFIX in text


def compaction_handoff_remainder(message):
    """Return the authentic retained text of a merged handoff carrier.

    The summary (and with it the handoff header/delimiter envelope) is removed;
    whatever the retained row said before the fold survives. An empty result
    means the row is synthetic only, with no authentic turn to render.
    """
    if not is_compaction_handoff_carrier(message):
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    text = content.strip()
    low = text.lower()
    if low.startswith(COMPACTION_PROMPT_CARD_PREFIX) or low.startswith("context compaction"):
        return ""
    delimiter = low.find(COMPACTION_HANDOFF_DELIMITER_PREFIX)
    if delimiter < 0:
        return ""
    head = text[:delimiter].lstrip()
    if head.lower().startswith(COMPACTION_HANDOFF_HEADER_PREFIX):
        close = head.find("]")
        if close >= 0:
            head = head[close + 1:]
    return head.strip()


_CARRIER_NOT_A_CARRIER = "not_carrier"
_CARRIER_PROVEN_SYNTHETIC = "proven_synthetic"
_CARRIER_AUTHENTIC_REMAINDER = "authentic_remainder"


def _compaction_carrier_envelope_decision(content_text: str) -> tuple[str, str]:
    """Three-outcome decision on compaction carrier envelope evidence.

    Outcomes:
    - (_CARRIER_NOT_A_CARRIER, ""): not a carrier or missing/truncated envelope.
    - (_CARRIER_PROVEN_SYNTHETIC, ""): proven pure synthetic carrier only.
    - (_CARRIER_AUTHENTIC_REMAINDER, authentic_head): merged carrier with authentic text.
    """
    if not isinstance(content_text, str):
        return _CARRIER_NOT_A_CARRIER, ""

    text = content_text.strip()
    low = text.lower()
    if not low.startswith(COMPACTION_HANDOFF_HEADER_PREFIX):
        return _CARRIER_NOT_A_CARRIER, ""

    close = text.find("]")
    if close < 0:
        return _CARRIER_NOT_A_CARRIER, ""

    after_close = text[close + 1:]
    rest = after_close.strip()
    low_after = after_close.lower()

    delimiter_idx = low_after.find(COMPACTION_HANDOFF_DELIMITER_PREFIX)
    if delimiter_idx >= 0:
        head = after_close[:delimiter_idx].strip()
        if not head:
            return _CARRIER_PROVEN_SYNTHETIC, ""
        low_head = head.lower()
        if low_head.startswith(COMPACTION_PROMPT_CARD_PREFIX) or low_head.startswith("context compaction"):
            return _CARRIER_PROVEN_SYNTHETIC, ""
        return _CARRIER_AUTHENTIC_REMAINDER, head

    low_rest = rest.lower()
    if low_rest.startswith(COMPACTION_PROMPT_CARD_PREFIX) or low_rest.startswith("context compaction"):
        return _CARRIER_PROVEN_SYNTHETIC, ""

    return _CARRIER_NOT_A_CARRIER, ""


def project_compaction_carrier_for_display(message):
    """Display-only projection: drop the handoff, keep the authentic row.

    Model-facing history must keep the full carrier (the Agent needs the
    summary for replay), so this never mutates the input and belongs only at
    the WebUI display boundary.

    Only MERGED carriers and proven synthetic carriers are rewritten.
    Standalone summary cards (``[context compaction ...]``) are returned
    untouched - the transcript renderer already treats them as a compaction
    card and needs the summary text for that card.
    """
    if not isinstance(message, dict):
        return message
    role = message.get("role")
    if role not in ("user", "assistant"):
        return message

    content = message.get("content")
    if isinstance(content, str):
        decision, remainder = _compaction_carrier_envelope_decision(content)
        if decision == _CARRIER_NOT_A_CARRIER:
            return message
        projected = dict(message)
        projected["content"] = remainder
        # The flag describes the handoff we just removed; keeping it would let a
        # downstream mirror treat the authentic remainder as a synthetic card.
        projected.pop("_compressed_summary", None)
        return projected

    if isinstance(content, list):
        has_carrier_block = False
        new_content = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"text", "input_text", "output_text"} or (
                    part_type is None and ("text" in part or "content" in part)
                ):
                    text_key = "text" if "text" in part else ("content" if "content" in part else None)
                    if text_key and isinstance(part[text_key], str):
                        raw_text = part[text_key]
                        decision, remainder = _compaction_carrier_envelope_decision(raw_text)
                        if decision in (
                            _CARRIER_PROVEN_SYNTHETIC,
                            _CARRIER_AUTHENTIC_REMAINDER,
                        ):
                            has_carrier_block = True
                            new_part = dict(part)
                            new_part[text_key] = remainder
                            new_content.append(new_part)
                            continue
            elif isinstance(part, str):
                decision, remainder = _compaction_carrier_envelope_decision(part)
                if decision in (_CARRIER_PROVEN_SYNTHETIC, _CARRIER_AUTHENTIC_REMAINDER):
                    has_carrier_block = True
                    new_content.append(remainder)
                    continue
            new_content.append(part)

        if not has_carrier_block:
            return message
        projected = dict(message)
        projected["content"] = new_content
        projected.pop("_compressed_summary", None)
        return projected

    return message


def project_compaction_carriers_for_display(messages):
    """Return *messages* with every compaction handoff carrier stripped."""
    if not isinstance(messages, list):
        return messages
    return [project_compaction_carrier_for_display(message) for message in messages]


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
