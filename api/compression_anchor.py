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
    return (
        text.startswith("[context compaction")
        or (synthetic_unbracketed_marker and text.startswith("context compaction"))
        or text.startswith("[your active task list was preserved across context compression]")
        or text.startswith("[session arc summary")
    )


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


# ── User-originated-turn classification (mirror of the agent's predicate) ──────
#
# The agent (:file:`agent/context_compressor.py``) exposes ``is_user_originated_turn`` /
# ``user_originated_turn_view`` as the authoritative way to tell a genuine
# human-authored user row from synthetic scaffolding that the agent persists with
# ``role='user'`` (compression summaries, the continuation marker, process‑wakeup
# notifications, async‑delegation / hidden display rows, blank echoes, the
# max‑iterations request, TODO re‑injection, ...). The sidebar's two user‑turn
# counters — the in‑memory sidecar walk and the state.db SQL aggregate — must
# classify a transcript identically or they drift (e.g. state.db says 4 while the
# agent says 2 for the same session).
#
# Because WebUI can run without the agent package importable (separate venv /
# embedded appliance), we carry a faithful local mirror and fall back to the
# agent's own predicate when it IS importable. The content rules are data, so the
# SQL aggregate (api/agent_sessions.py) can be generated from the same lists below
# and the two producers can never diverge.

# Stripped, case‑insensitive content prefixes that mark a user‑role row as
# internal scaffolding rather than a human ask. Kept lowercase; the classifier and
# the SQL aggregate both compare against ``LOWER(LTRIM(content))``.
USER_TURN_INTERNAL_PREFIXES = (
    "[context compaction",
    "[context summary",   # legacy "[CONTEXT SUMMARY]:"
    "[session arc summary",
    "[your active task list was preserved across context compression]",
    "[prior context",     # merged "[PRIOR CONTEXT — ...] !" carrier header
    "[important: background process ",  # process wake‑up notification
)

# Whole‑op internal markers (stripped, lowercased content equals one of these).
USER_TURN_INTERNAL_EXACT = (
    "continue from the compressed conversation context above. "
    "this marker exists because no human user turn was available.",
    "continue from the compressed conversation context above. "
    "this marker exists because the compacted transcript contained no preserved user turn.",
    "you've reached the maximum number of tool-calling iterations allowed. "
    "please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools.",
)

# ``display_kind`` values that still represent genuine human input. Everything
# else (``hidden`` scaffolding, async‑delegation rows, operational notices) is not.
_USER_STEER_DISPLAY_KIND = "steer"


def _sql_literal(value: str) -> str:
    """Render a Python string as a SQLite single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _message_search_text(message) -> str:
    """Lowercased, lstrip‑trimmed text view of a row's content for classification."""
    if not isinstance(message, dict):
        return ""
    return _content_text(
        message.get("content", ""),
        part_types={"text", "input_text", "output_text"},
    ).lower().lstrip()


def _is_internal_user_row(message) -> bool:
    """True when *message* is synthetic scaffolding despite ``role='user'``.

    Covers compaction/context-summary carriers, the continuation marker, the
    process‑wakeup notification, the max‑iterations request, the TODO
    re‑injection header, the merged prior-context header, and blank echoes.
    """
    if not isinstance(message, dict):
        return True
    text = _message_search_text(message)
    if not text:
        return True  # blank echo — no human ask
    if any(text.startswith(prefix) for prefix in USER_TURN_INTERNAL_PREFIXES):
        return True
    if text in USER_TURN_INTERNAL_EXACT:
        return True
    return False


_AGENT_USER_PREDICATE = None
_AGENT_USER_PREDICATE_ATTEMPTED = False
_AGENT_SPLIT_PREDICATE = None
_AGENT_SPLIT_PREDICATE_ATTEMPTED = False


def _agent_user_originated_turn(message):
    """Return the agent's verdict if its predicate is importable, else ``None``."""
    global _AGENT_USER_PREDICATE, _AGENT_USER_PREDICATE_ATTEMPTED
    if not _AGENT_USER_PREDICATE_ATTEMPTED:
        _AGENT_USER_PREDICATE_ATTEMPTED = True
        try:
            from agent.context_compressor import is_user_originated_turn

            _AGENT_USER_PREDICATE = is_user_originated_turn
        except Exception:
            _AGENT_USER_PREDICATE = None
    if _AGENT_USER_PREDICATE is not None:
        try:
            return bool(_AGENT_USER_PREDICATE(message))
        except Exception:
            pass
    return None


def is_user_originated_turn(message) -> bool:
    """Return true only for genuine human-authored user turns.

    Uses the Hermes agent's own ``is_user_originated_turn`` predicate when it is
    importable; otherwise the self-contained mirror below. Either path excludes
    every synthetic ``role='user'`` scaffolding shape so the WebUI turn counter
    agrees with the agent's lineage view.
    """
    agent_verdict = _agent_user_originated_turn(message)
    if agent_verdict is not None:
        return agent_verdict
    if not isinstance(message, dict):
        return False
    if message.get("role") != "user":
        return False
    display_kind = message.get("display_kind")
    if display_kind and display_kind not in ("", _USER_STEER_DISPLAY_KIND):
        # #7681 finding 2: 'hidden' is the one non-empty display_kind that can
        # still wrap a live human ask (the legacy compaction wrapper). The
        # agent's own split keeps that ask as a user turn; mirror it instead of
        # rejecting every non-empty kind.
        if display_kind != "hidden":
            return False
        if not _summary_carrier_has_live_user_ask(message):
            return False
        return True
    if _is_internal_user_row(message):
        return False
    if message.get("_compressed_summary"):
        # A summary carrier whose display_kind is empty still carries its
        # handoff, and the agent counts a live ask embedded in it.
        return not _summary_carrier_has_live_user_ask(message)
    return True


def _summary_carrier_has_live_user_ask(message: dict) -> bool:
    """True when a compaction-summary row still embeds a live human ask.

    Mirrors the agent's ``_strip_context_summary_handoff_message``: a summary
    carrier splits at the merged-prior-context delimiter (or the legacy end
    marker), and whatever remains after the summary block is the live ask. A
    standalone summary with nothing after it is pure scaffolding.

    Without this, a merged summary carrier is invisible to the sidecar counter
    while the agent still counts it (#7681 finding 2).
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    split = _agent_split_user_originated_turn()
    if split is not None:
        try:
            return split(message)[1] is not None
        except Exception:
            pass
    content = message.get("content")
    if not isinstance(content, str):
        return False
    if _MERGED_SUMMARY_DELIMITER in content:
        # Merged form: the delimiter separates the reference-only prior context
        # from the summary block; anything after the SUMMARY is the live ask.
        tail = content.split(_MERGED_SUMMARY_DELIMITER, 1)[1]
        for marker in (_SUMMARY_END_MARKER,):
            if marker in tail:
                tail = tail.split(marker, 1)[1]
                break
        return bool(tail.strip())
    if _SUMMARY_END_MARKER in content:
        # Legacy form: the live ask follows the end marker directly.
        tail = content.split(_SUMMARY_END_MARKER, 1)[1]
        return bool(tail.strip())
    return False


# Shapes copied from agent/context_compressor.py so the mirror does not drift.
_SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"


def _agent_split_user_originated_turn():
    """The agent's splitter, or ``None`` when it is not importable."""
    global _AGENT_SPLIT_PREDICATE, _AGENT_SPLIT_PREDICATE_ATTEMPTED
    if not _AGENT_SPLIT_PREDICATE_ATTEMPTED:
        _AGENT_SPLIT_PREDICATE_ATTEMPTED = True
        try:
            from agent.context_compressor import split_user_originated_turn

            _AGENT_SPLIT_PREDICATE = split_user_originated_turn
        except Exception:
            _AGENT_SPLIT_PREDICATE = None
    return _AGENT_SPLIT_PREDICATE


def user_turn_sql_exclusions(content_expr: str) -> str:
    """SQL boolean fragment excluding synthetic internal rows from a user count.

    ``content_expr`` is a SQL expression naming the message content column
    (already lowercased / LTRIM'd by the caller). Built from the same
    ``USER_TURN_INTERNAL_PREFIXES`` / ``USER_TURN_INTERNAL_EXACT`` data as the
    Python classifier so the sidecar producer and the state.db SQL aggregate
    can never classify a transcript differently.
    """
    conditions = []
    for prefix in USER_TURN_INTERNAL_PREFIXES:
        conditions.append(f"INSTR({content_expr}, {_sql_literal(prefix)}) = 1")
    for exact in USER_TURN_INTERNAL_EXACT:
        conditions.append(f"{content_expr} = {_sql_literal(exact)}")
    return " OR ".join(conditions)
