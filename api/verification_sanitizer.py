"""Cycle-safe ``verification_evidence`` sanitizer for the Agent boundary (#6481).

The Hermes Agent stamps every terminal tool result that matches a known
verification command (pytest, lint, etc.) with a ``verification_evidence``
metadata dict in the JSON result. That field is an internal audit artifact for
the agent's verify-on-stop loop; it has no display value and must never be
persisted in the WebUI transcript or fed back to the model on subsequent turns,
where it can trigger phantom assistant messages (#6481).

This module is deliberately importable from ``api.agent_runtime`` during the
partial initialization of ``api.streaming`` (``streaming.py:631`` calls
``get_ai_agent_class()`` before the streaming module finishes loading). It must
therefore NOT import ``api.streaming`` or any other api.* module at module
level — only stdlib, plus lazy imports of the Hermes Agent inside the
installer. Keeping the installer here (instead of in ``api.streaming``) is what
makes the wrapper active on a normal cold-start server boot.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_AGENT_VERIFICATION_SANITIZER_INSTALLED = False


def _strip_verification_evidence_from_tool_content(content):
    """Strip the ``verification_evidence`` field from a terminal tool result.

    Accepts both the in-process representation (a ``{"output": ...,
    "exit_code": ..., "verification_evidence": {...}}`` dict — what the Agent's
    ``tools/terminal_tool.py`` returns and ``make_tool_result_message`` embeds
    before WebUI regains control) and the JSON-serialized string form found in
    persisted transcripts. When ``verification_evidence`` is present the field
    is removed (dict stays a dict, string is re-serialized); otherwise the
    input is returned unchanged.
    """
    if isinstance(content, dict):
        if "verification_evidence" not in content:
            return content
        return {k: v for k, v in content.items() if k != "verification_evidence"}
    if not isinstance(content, str):
        return content
    if "verification_evidence" not in content:
        return content
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return content
    if not isinstance(parsed, dict):
        return content
    if "verification_evidence" not in parsed:
        return content
    parsed.pop("verification_evidence", None)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=False)


def _correlate_tool_call_name(messages, tool_msg, tool_index=None):
    """Return the function name of the PRECEDING assistant tool_call matching a tool message.

    Legacy tool-role rows can lack the ``name``/``tool_name`` fields the Agent
    now stamps. To decide whether such a row is a terminal result without
    trusting the payload text, correlate its ``tool_call_id`` against the
    PRECEDING assistant ``tool_calls`` entries.

    Fails CLOSED (returns ``""``) on every ambiguous identity shape so a
    nameless non-terminal row can never lose legitimate top-level data:

    - missing/empty ``tool_call_id``
    - no preceding assistant tool_call with that id
    - the id also appearing in a FUTURE assistant tool_call (recovered or
      duplicate id) — correlation is not restricted to the preceding call
    - the id appearing more than once (duplicate/ambiguous)
    - conflicting function names for the same id
    """
    tid = tool_msg.get("tool_call_id") or tool_msg.get("call_id") or ""
    if not tid:
        return ""
    if tool_index is None:
        # Caller did not supply the tool row's position; locate it by identity
        # so the preceding-only scan still bounds to messages before it.
        tool_index = next(
            (i for i, m in enumerate(messages) if m is tool_msg),
            len(messages),
        )
    matched: list[tuple[int, str]] = []
    future_match = False
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id") or tc.get("call_id") or ""
            if tc_id != tid:
                continue
            fn = tc.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                name = fn.get("name") or ""
            else:
                name = tc.get("name") or ""
            if idx >= tool_index:
                # A match at-or-after the tool row is a future/recovered id.
                future_match = True
            else:
                matched.append((idx, name))
    if future_match:
        # The id is reused later in the transcript — cannot prove this row is
        # the result of the preceding call. Fail closed.
        return ""
    if not matched:
        return ""
    if len(matched) > 1:
        # Same id correlated to multiple preceding calls — ambiguous.
        return ""
    _idx, name = matched[0]
    if not name:
        return ""
    return name


def _strip_verification_from_messages(messages):
    """Strip ``verification_evidence`` from terminal tool-role message content in a message list.

    Operates on all messages in the list, modifying terminal tool-role message
    ``content`` values in-place (dict content and JSON-string content are both
    handled). Only strips from messages confirmed to be terminal tool results to
    avoid deleting legitimate data from non-terminal tools:

    - messages whose ``name`` or ``tool_name`` is ``\"terminal\"`` are stripped;
    - legacy messages lacking both fields are correlated through ``tool_call_id``
      against the PRECEDING assistant ``tool_calls`` — stripped only when the
      correlated function name is ``\"terminal\"``, preserved otherwise.

    Correlation is always run against the full list (never a singleton) so the
    preceding assistant call is visible, and fails closed on future/duplicate/
    ambiguous ids (see ``_correlate_tool_call_name``).
    """
    for idx, msg in enumerate(list(messages or [])):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        # Only strip from confirmed terminal tool results to avoid corrupting
        # legitimate JSON content from plugin/MCP/native tools that might
        # happen to use a top-level ``verification_evidence`` key.
        msg_name = msg.get("name") or msg.get("tool_name") or ""
        if msg_name:
            if msg_name != "terminal":
                continue
        else:
            # Legacy row without name/tool_name: correlate through tool_call_id
            # against the preceding assistant call (full-list context).
            if _correlate_tool_call_name(messages, msg, tool_index=idx) != "terminal":
                continue
        content = msg.get("content")
        if not isinstance(content, (str, dict)):
            continue
        cleaned = _strip_verification_evidence_from_tool_content(content)
        if cleaned != content:
            msg["content"] = cleaned


def _install_agent_verification_evidence_sanitizer() -> bool:
    """Install a WebUI-owned wrapper on the Agent's tool-result message builder.

    Closes the producer/executor boundary for #6481. The installed Hermes Agent
    stamps ``verification_evidence`` onto terminal result dicts in
    ``tools/terminal_tool.py``, and ``agent/tool_executor.py`` builds the
    tool-role message from that dict and flushes it to state.db BEFORE WebUI
    regains control of the turn. A WebUI post-run sanitizer can clean a later
    display/sidecar copy, but it cannot prevent the same-turn model request or
    the Agent's canonical SessionDB row from seeing the fresh field.

    Wrapping ``make_tool_result_message`` at the message-construction chokepoint
    strips the field before the message is appended to the live conversation and
    before the incremental SessionDB flush, so neither the same-turn provider
    payload nor the durable transcript ever contains it. The Agent's separate
    verification ledger (``record_terminal_result``) is left untouched.

    Installation is idempotent. Returns ``True`` only when BOTH the helper
    (``agent.tool_dispatch_helpers.make_tool_result_message``) AND the
    already-imported executor alias (``agent.tool_executor.make_tool_result_message``)
    are patched. If the executor module is not importable yet, the helper patch
    is retained but ``False`` is returned so a lazy retry re-attempts the alias
    patch — the wrapper must never be considered complete with only half the
    boundary covered.
    """
    global _AGENT_VERIFICATION_SANITIZER_INSTALLED
    if _AGENT_VERIFICATION_SANITIZER_INSTALLED:
        return True
    try:
        import agent.tool_dispatch_helpers as _tdh
    except Exception as exc:  # pragma: no cover - depends on agent availability
        logger.warning(
            "verification evidence sanitizer: agent tool helpers unavailable: %s",
            exc,
        )
        return False
    original = getattr(_tdh, "make_tool_result_message", None)
    if original is None:
        logger.warning(
            "verification evidence sanitizer: make_tool_result_message missing in agent.tool_dispatch_helpers"
        )
        return False
    if getattr(original, "_webui_verification_sanitized", False):
        wrapped = original
    else:
        def _sanitized_make_tool_result_message(name, content, tool_call_id, **kwargs):
            if name == "terminal":
                content = _strip_verification_evidence_from_tool_content(content)
            return original(name, content, tool_call_id, **kwargs)

        _sanitized_make_tool_result_message.__dict__["_webui_verification_sanitized"] = True
        _tdh.make_tool_result_message = _sanitized_make_tool_result_message
        wrapped = _sanitized_make_tool_result_message
    # agent.tool_executor imports the helper via ``from ... import``, so it
    # holds its own module-level reference that must be patched in place too.
    # Do NOT mark installation complete until this alias is patched — a lazy
    # retry (every Agent entry path) will re-attempt otherwise.
    try:
        import agent.tool_executor as _te
        _te.make_tool_result_message = wrapped
    except Exception as exc:  # pragma: no cover - depends on agent availability
        logger.warning(
            "verification evidence sanitizer: tool_executor alias not patched yet: %s",
            exc,
        )
        return False
    _AGENT_VERIFICATION_SANITIZER_INSTALLED = True
    return True
