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
#
# #7681 finding 3: this table is the mirror of the agent's
# ``_is_synthetic_compression_user_turn`` exclusion set. Every recovery nudge the
# agent excludes must appear here or the SQL aggregate over-reports — the 23-shape
# reproduction reported 7 against the agent's 6 because the length-continuation
# and tool-call nudges were missing.
USER_TURN_INTERNAL_PREFIXES = (
    "[context compaction",
    "[context summary",   # legacy "[CONTEXT SUMMARY]:"
    "[session arc summary",
    "[your active task list was preserved across context compression]",
    "[prior context",     # merged "[PRIOR CONTEXT — ...] !" carrier header
    "[important: background process ",  # process wake‑up notification
    # ── agent/conversation_loop.py recovery nudges (#7681 finding 3) ──
    "[system: the previous response was cut off by a network error",
    "[system: your previous response was truncated by the output length limit",
    "[system: your previous tool call ",  # dropped-tools variant, tool names interpolated
    "[system: your previous response contained only internal reasoning",
    "[system: continue now. execute the required tool calls",
    "[system: your previous message ended the turn with a fragment",
)

# Whole‑op internal markers (stripped, lowercased content equals one of these).
# Mirrors the agent's exact-match set; a recovery nudge that is not one of these
# still counts as a user turn.
USER_TURN_INTERNAL_EXACT = (
    "continue from the compressed conversation context above. "
    "this marker exists because no human user turn was available.",
    "continue from the compressed conversation context above. "
    "this marker exists because the compacted transcript contained no preserved user turn.",
    "you've reached the maximum number of tool-calling iterations allowed. "
    "please provide a final response summarizing what you've found and accomplished so far, "
    "without calling any more tools.",
    # ── agent/conversation_loop.py recovery nudges (#7681 finding 3) ──
    "your previous turn indicated a tool call but none was included. do not narrate a plan or "
    "restate intent — issue the actual tool call now to continue the task.",
    "you just executed tool calls but returned an empty response. please process the tool "
    "results above and continue with the task.",
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


def _is_context_summary_message(message) -> bool:
    """True for summary handoff rows, by metadata or by content.

    Mirrors the agent's ``ContextCompressor._is_context_summary_message``: the
    durable flag, the merged prior-context delimiter, the legacy end marker, or
    the legacy ``[CONTEXT SUMMARY]:`` prefix. #7681 finding 3 needs this split
    because such a row can still carry a live ask, and classifying from the
    carrier's presence alone is what under-counted them.
    """
    if not isinstance(message, dict):
        return False
    if message.get("_compressed_summary"):
        return True
    return _is_context_summary_content(message.get("content"))


def _is_context_summary_content(content) -> bool:
    """Content-side summary markers (str or list payload)."""
    text = _content_text(content, part_types={"text", "input_text", "output_text"})
    if not text:
        return False
    lowered = text.lower().lstrip()
    return (
        _MERGED_SUMMARY_DELIMITER.lower() in lowered
        or _SUMMARY_END_MARKER.lower() in lowered
        or lowered.startswith("[context summary")
        or lowered.startswith("[context compaction")
    )


def _is_blank_user_turn(message) -> bool:
    """True for an empty, non-summary ``role='user'`` echo.

    Mirrors the agent's ``_is_blank_user_turn``: a blank echo is not a human
    turn. List payloads count as blank only when every text part is blank.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if _is_context_summary_message(message):
        return False
    content = message.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if not isinstance(content, list):
        return False

    def _blank_part(part) -> bool:
        if isinstance(part, str):
            return not part.strip()
        if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
            return not str(part.get("text") or "").strip()
        # Non-text parts (images, tool results) are real payload, not blank.
        return False

    return bool(content) and all(_blank_part(part) for part in content)


def _is_internal_user_row(message) -> bool:
    """True when *message* is synthetic scaffolding despite ``role='user'``.

    Covers compaction/context-summary carriers, the continuation marker, the
    process‑wakeup notification, the max‑iterations request, the TODO
    re‑injection header, the merged prior-context header, and blank echoes.

    #7681 finding 5: a row can carry one of these prefixes AND still embed a live
    human ask (the merged prior-context carrier is exactly that shape). The agent
    unwraps it and keeps the ask, so a prefix alone must not condemn the row —
    otherwise every embedded ask is rejected and the sidecar under-counts. The
    live-ask check runs first for summary carriers.
    """
    if not isinstance(message, dict):
        return True
    text = _message_search_text(message)
    if not text:
        # #7681 media-only finding: no extracted TEXT is not the same as no
        # payload. An image-only row has real content the Agent counts, and
        # ``_is_blank_user_turn`` already accepts non-text payloads as
        # non-blank — this classifier must agree instead of condemning every
        # media-only turn as internal. Only a genuinely blank row (no text and
        # no non-text payload) is scaffolding here.
        content = message.get("content")
        if _content_has_part_type(content, {"image", "input_image", "document", "file"}):
            return False
        return True  # blank echo — no human ask
    if any(text.startswith(prefix) for prefix in USER_TURN_INTERNAL_PREFIXES):
        # A merged prior-context carrier is reference-only scaffolding in front of
        # the delimiter and a live ask after it is not what these prefixes match;
        # they match the HEADER. Ask the carrier splitter before condemning.
        if _summary_carrier_has_live_user_ask(message):
            return False
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

    #7681 finding 3: the agent's predicate is
    ``split_user_originated_turn(message)[1] is not None``, which the WebUI must
    mirror EXACTLY — including that a summary carrier with a live payload IS a
    user turn. Order of the mirrors below follows the agent's own chain:

      1. ``display_kind`` that is neither empty nor ``steer`` -> not a turn,
         EXCEPT ``hidden``, which is the legacy compaction wrapper and can still
         wrap a live ask (the agent keeps it).
      2. a context-summary message -> unwrap it and keep the live payload;
         ``None`` (standalone) -> not a turn.
      3. a blank echo -> not a turn.
      4. anything else -> a real human turn.
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
    # #7681 finding 3: a summary carrier is NOT automatically scaffolding. The
    # agent unwraps it and keeps the live payload in front of the merged
    # delimiter, so the mirror must classify from the UNWRAPPED payload, not from
    # the carrier's mere presence. The old blanket rejection is what made the
    # aggregate under-report carriers the agent counted.
    if _is_context_summary_message(message):
        return _summary_carrier_has_live_user_ask(message)
    # Blank echoes are not human turns (the agent's `_is_blank_user_turn`).
    if _is_blank_user_turn(message):
        return False
    if _is_internal_user_row(message):
        return False
    if message.get("_compressed_summary"):
        # A summary carrier whose display_kind is empty still carries its
        # handoff, and the agent counts a live ask embedded in it. The verdict is
        # the SAME direction as the agent's `user_originated_turn_view(...) is
        # not None`: a carrier with a live payload IS a user turn. The previous
        # `not` inverted it, so a flagged standalone summary counted as a turn
        # while a real embedded ask was rejected (#7681 finding 5).
        return _summary_carrier_has_live_user_ask(message)
    return True


def _summary_carrier_has_live_user_ask(message: dict) -> bool:
    """True when a compaction-summary row still embeds a live human ask.

    Mirrors the agent's ``_strip_context_summary_handoff_message``: a summary
    carrier is split at the merged-prior-context delimiter (or the legacy end
    marker) and the LIVE payload is the part the agent keeps — which is what
    comes BEFORE the delimiter once the reference-only prior context is stripped
    off. A standalone summary with nothing left over is pure scaffolding.

    Direction matters. The agent splits with ``content.split(delim, 1)[0]`` and
    then drops the prior-context header; the previous mirror read
    ``split(delim, 1)[1]``, i.e. the SUMMARY block itself, so a flagged standalone
    summary read as a live ask and a real embedded ask read as scaffolding
    (#7681 finding 5).

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
    candidate = _unwrap_carrier_candidate(message)
    if candidate is None:
        return False
    return _classify_unwrapped_candidate(candidate)


def _unwrap_carrier_candidate(message: dict):
    """Unwrap a summary carrier into a fresh candidate, or ``None``.

    Mirrors the agent's ``_strip_context_summary_handoff_message``
    (context_compressor.py:4639-4683): the merged form takes
    ``content.split(delim, 1)[0].strip()`` and then drops the reference-only
    prior-context header (:4641-4643); the legacy form takes whatever follows
    the end marker, left-stripped (:4645). The durable summary flag and
    ``display_kind`` are popped from the candidate exactly as the agent does
    before classifying it (:4636, :5849).
    """
    content = message.get("content")

    def _candidate(new_content):
        if not new_content:
            return None
        unwrapped = {**message, "content": new_content}
        unwrapped.pop("_compressed_summary", None)
        unwrapped.pop("display_kind", None)
        unwrapped.pop("display_metadata", None)
        return unwrapped

    if isinstance(content, str):
        if _MERGED_SUMMARY_DELIMITER in content:
            # Merged form: the live payload sits BEFORE the delimiter, after the
            # reference-only prior-context header is stripped.
            payload = _live_payload_after_prior_context(
                content.split(_MERGED_SUMMARY_DELIMITER, 1)[0]
            )
        elif _SUMMARY_END_MARKER in content:
            # Legacy form: the live ask follows the end marker directly.
            payload = content.split(_SUMMARY_END_MARKER, 1)[1].lstrip()
        else:
            return None
        if payload:
            return _candidate(payload)
        # #7681 media-only finding: a string carrier whose text after the
        # marker is empty can still carry its live payload as non-text blocks
        # in the sibling ``parts`` field (the agent's reader consults it for
        # exactly this shape). Keep those blocks instead of returning None.
        parts = message.get("parts")
        if isinstance(parts, list):
            media_blocks = [
                item
                for item in parts
                if isinstance(item, dict)
                and item.get("type") not in (None, "text", "input_text", "output_text")
            ]
            if media_blocks:
                return _candidate(media_blocks)
        return None
    if isinstance(content, list):
        # The agent walks the parts, keeps everything before the delimiter part,
        # and stops at the part carrying it. Joining all parts first would fold
        # the summary block back into the pre-delimiter slice and make every
        # standalone carrier look live (#7681 finding 5).
        # #7681 media-only finding: the kept slice must preserve NON-TEXT
        # blocks, not just their text. Joining text only dropped an image-only
        # live ask and left an empty payload, which then read as scaffolding.
        before: list = []
        found_delimiter = False
        for item in content:
            if _MERGED_SUMMARY_DELIMITER in _part_text(item):
                found_delimiter = True
                break
            before.append(item)
        if found_delimiter:
            payload = _live_payload_after_prior_context(_join_parts(before))
            # The live payload can be pure media: keep the non-text blocks so
            # the candidate is not condemned as blank by the classifier.
            media_blocks = [
                item
                for item in before
                if isinstance(item, dict)
                and item.get("type") not in (None, "text", "input_text", "output_text")
            ]
            if not payload and media_blocks:
                return _candidate(media_blocks)
        else:
            # Legacy list form: the live ask follows the end marker in whichever
            # part carries it.
            joined = _join_parts(content)
            if _SUMMARY_END_MARKER not in joined:
                return None
            tail: list = []
            seen_marker = False
            for item in content:
                if seen_marker:
                    tail.append(item)
                elif _SUMMARY_END_MARKER in _part_text(item):
                    seen_marker = True
            payload = _join_parts_text([_part_text(item) for item in tail]).lstrip()
            media_blocks = [
                item
                for item in tail
                if isinstance(item, dict)
                and item.get("type") not in (None, "text", "input_text", "output_text")
            ]
            if not payload and media_blocks:
                return _candidate(media_blocks)
        return _candidate(payload)
    return None


def _live_payload_after_prior_context(before_delimiter: str):
    """The live payload in front of the merged delimiter, or ``None``.

    The agent strips the pre-delimiter slice FIRST
    (``content.split(delim, 1)[0].strip()``, context_compressor.py:4641), then
    removes the reference-only ``[PRIOR CONTEXT ...]`` header and left-strips
    what remains (:4642-4643). The old mirror checked for the header BEFORE
    stripping, so a leading tab/newline followed by only the header was never
    recognized as an empty payload and counted as a live ask (#7681 finding 5).
    """
    payload = before_delimiter.strip()
    if _MERGED_PRIOR_CONTEXT_HEADER and payload.startswith(_MERGED_PRIOR_CONTEXT_HEADER):
        payload = payload[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
    return payload or None


def _classify_unwrapped_candidate(candidate) -> bool:
    """Classify an unwrapped carrier candidate with the complete local predicate.

    The candidate is a fresh row that is no longer a summary carrier (the
    durable flag and ``display_kind`` were popped by the unwrap), so the carrier
    branches are omitted — running ``is_user_originated_turn`` here would
    re-enter the same row's split. Mirrors the agent applying
    ``_is_real_user_turn`` to the unwrapped candidate
    (context_compressor.py:5860-5863), which rejects the exact continuation
    marker and the other synthetic exact/prefix shapes (:4444-4452).
    """
    if not isinstance(candidate, dict):
        return False
    if candidate.get("role") != "user":
        return False
    if _is_blank_user_turn(candidate):
        return False
    if _is_internal_user_row(candidate):
        return False
    return True


def _part_text(item) -> str:
    """Text of one content part, tolerating the str / {text:...} shapes."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        text = item.get("text")
        if isinstance(text, str):
            return text
    return ""


def _join_parts(content: list) -> str:
    return _join_parts_text([_part_text(item) for item in content])


def _join_parts_text(parts: list[str]) -> str:
    return "".join(part for part in parts if isinstance(part, str))


# Shapes copied from agent/context_compressor.py so the mirror does not drift.
_SUMMARY_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"


def _flagged_carrier_has_live_payload_sql(content_expr: str) -> str:
    """SQL predicate: does a flagged summary carrier still embed a live ask?

    The SQL twin of ``_summary_carrier_has_live_user_ask`` above, so the state.db
    aggregate and the sidecar classifier cannot disagree (#7681 finding 3).
    ``content_expr`` must already be lowercased/LTRIM'd, so the literals below are
    lowercased too — ``INSTR`` is case-sensitive and a lowered column compared
    against a mixed-case needle silently never matches.

    The unwrapped payload is CLASSIFIED, not just tested for nonblankness: the
    agent strips the pre-delimiter slice before removing the reference-only
    header (``split(delim, 1)[0].strip()`` then header then lstrip,
    context_compressor.py:4641-4643) and then rejects a candidate whose payload
    is a synthetic exact marker (:4444-4446). Both are mirrored here:

    * the slice in front of the merged delimiter is full-whitespace TRIM'd
      BEFORE the header comparison, so a leading tab/newline followed by only
      the header leaves nothing;
    * a payload that equals an exact internal marker (the continuation markers,
      the max-iterations request, the recovery nudges) — or starts with an
      internal prefix — is scaffolding.

    Merged form — the agent keeps ``content.split(delim, 1)[0]`` and then strips
    the reference-only prior-context header. Legacy form — the live ask follows
    the end marker, so anything non-blank after it counts, classified the same
    way.
    """

    def _not_synthetic(expr: str) -> str:
        conditions = (
            [f"INSTR({expr}, {_sql_literal(p)}) = 1" for p in USER_TURN_INTERNAL_PREFIXES]
            + [f"{expr} = {_sql_literal(e)}" for e in USER_TURN_INTERNAL_EXACT]
        )
        return f"NOT ({' OR '.join(conditions)})"

    delim = _sql_literal(_MERGED_SUMMARY_DELIMITER.lower())
    header = _sql_literal(_MERGED_PRIOR_CONTEXT_HEADER.lower())
    end_marker = _sql_literal(_SUMMARY_END_MARKER.lower())
    ws = "' ' || char(9) || char(10) || char(13)"
    before = (
        f"CASE WHEN INSTR({content_expr}, {delim}) > 0"
        f" THEN SUBSTR({content_expr}, 1, INSTR({content_expr}, {delim}) - 1)"
        f" ELSE {content_expr} END"
    )
    trimmed_before = f"TRIM({before}, {ws})"
    without_header = (
        f"CASE WHEN SUBSTR({trimmed_before}, 1, LENGTH({header})) = {header}"
        f" THEN TRIM(SUBSTR({trimmed_before}, LENGTH({header}) + 1), {ws})"
        f" ELSE {trimmed_before} END"
    )
    payload = f"TRIM({without_header}, {ws})"
    merged_live = (
        f"(INSTR({content_expr}, {delim}) > 0"
        f" AND {payload} != ''"
        f" AND {_not_synthetic(payload)})"
    )
    legacy_tail = (
        f"TRIM(SUBSTR({content_expr},"
        f" INSTR({content_expr}, {end_marker}) + LENGTH({end_marker})), {ws})"
    )
    legacy_live = (
        f"(INSTR({content_expr}, {delim}) = 0"
        f" AND INSTR({content_expr}, {end_marker}) > 0"
        f" AND {legacy_tail} != ''"
        f" AND {_not_synthetic(legacy_tail)})"
    )
    return f"({merged_live} OR {legacy_live})"


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

    #7681 finding 3 (second half): the prefix rules are the SQL twin of
    ``_is_internal_user_row``, so they must carry the same escape the Python side
    gained in finding 5 — a row that starts with a scaffolding prefix AND still
    embeds a live ask (the merged prior-context carrier) is a user turn. Without
    the escape the aggregate rejected every carrier while the agent counted it,
    which is the 7-vs-6 disagreement.
    """
    conditions = []
    for prefix in USER_TURN_INTERNAL_PREFIXES:
        conditions.append(
            f"(INSTR({content_expr}, {_sql_literal(prefix)}) = 1"
            f" AND NOT {_flagged_carrier_has_live_payload_sql(content_expr)})"
        )
    for exact in USER_TURN_INTERNAL_EXACT:
        conditions.append(f"{content_expr} = {_sql_literal(exact)}")
    return " OR ".join(conditions)
