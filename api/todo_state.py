"""Derive ``todo_state`` snapshots from tool results and settled session messages.

The ``todo`` tool's in-memory store lives on the per-session AIAgent. The
WebUI bridge needs to mirror that state to the browser in two situations:

1. **Live**: when the agent calls ``todo`` mid-stream, ``api.streaming``
   emits a dedicated ``todo_state`` SSE event so the Todos panel updates
   without waiting for the turn to finish. See :func:`emit_todo_state`.

2. **Cold-load**: when the browser opens a session (no live stream), the
   session GET handler attaches ``todo_state`` derived from the most
   recent ``role='tool'`` message whose JSON content carries a ``todos``
   list. See :func:`attach_todo_state`.

Both paths normalize through :func:`_normalize_snapshot` so the frontend
has a single deserialization contract:

    {
        "todos":   [{"id": ..., "content": ..., "status": ...}, ...],
        "summary": {"total": N, "pending": N, "in_progress": N,
                    "completed": N, "cancelled": N},
        "version": 1,
    }

Live SSE payloads add ``session_id``, ``stream_id``, ``source`` and ``ts``
on top so the frontend can filter cross-session events and ignore
out-of-order replays.

**Detection symmetry with the agent.** The cold-load helper deliberately
uses the same loose detector as ``run_agent.AIAgent._hydrate_todo_store``
(``role='tool'`` + JSON content with ``todos: list``). If a future change
tightens or relaxes that detector, mirror it here so the WebUI panel
never disagrees with the agent's in-memory ``TodoStore``.

**Multimodal tool results.** Some tools return content as a list of
OpenAI/Anthropic content parts rather than a JSON string. The ``todo``
tool always returns a JSON string, so list-shaped content cannot be a
todo write — :func:`derive_todo_state` skips them by design.

This module is **side-effect free** by design — it only parses data and
calls a caller-supplied ``put`` callable for SSE. Routing/event-shape
decisions live here so the call sites stay one-liners.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Iterable, Optional


logger = logging.getLogger(__name__)


# Bumped when the on-wire payload shape changes in a non-additive way.
# Additive fields (e.g. timestamps, tags) keep VERSION at 1.
VERSION = 1

# Single source of truth for the SSE event name and the session GET
# payload key. Any current or future caller must reuse these so a
# rename only happens in one place.
EVENT_NAME = "todo_state"
PAYLOAD_KEY = "todo_state"


def _normalize_snapshot(data: Any) -> Optional[dict]:
    """Return a normalized snapshot dict, or ``None`` if the payload is invalid.

    Accepts the canonical ``{"todos": [...], "summary": {...}}`` shape
    produced by ``tools.todo_tool.todo_tool``. Anything else returns
    ``None`` so callers can fall through to legacy paths or skip
    emission.

    The detector is intentionally loose so it stays symmetric with the
    agent's hydration logic — see the module docstring.
    """
    if not isinstance(data, dict):
        return None
    todos = data.get("todos")
    if not isinstance(todos, list):
        return None
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    return {
        "todos": todos,
        "summary": summary,
        "version": VERSION,
    }


def parse_todo_tool_result(function_result: Any) -> Optional[dict]:
    """Parse a fresh ``todo`` tool call result into a snapshot dict.

    The agent's ``todo`` handler returns a JSON string; this helper
    accepts either that string or an already-parsed dict (defensive —
    future callers may deserialize earlier in the pipeline).

    Returns ``None`` on any parse/shape failure so the caller can
    swallow the error without breaking the tool delivery path.
    """
    data: Any = function_result
    if isinstance(function_result, str):
        try:
            data = json.loads(function_result)
        except (ValueError, TypeError):
            return None
    return _normalize_snapshot(data)


def derive_todo_state(messages: Optional[Iterable[dict]]) -> Optional[dict]:
    """Derive the latest todo snapshot from settled conversation history.

    Mirrors the agent-side ``_hydrate_todo_store`` logic: walk messages
    in reverse, return the first ``role='tool'`` message whose JSON
    content carries a ``todos`` list. Returns ``None`` when no such
    message is found (fresh session, or a session that never invoked
    ``todo``).

    Multimodal tool results — ``content`` as a list of content parts
    rather than a JSON string — are skipped intentionally. The ``todo``
    tool always returns a string, so list-shaped content cannot be a
    todo write; non-string ``content`` is therefore correct to ignore.

    The fast-path string check (``'"todos"' in content``) avoids parsing
    JSON for every tool result — most sessions have many non-todo tool
    calls but at most a handful of todo writes.
    """
    if not messages:
        return None
    # ``reversed`` works on ``list`` and ``tuple`` natively; for any
    # other iterable (e.g. a generator) we materialize once. Routes
    # always pass a list, so this branch is normally a no-op.
    if not isinstance(messages, (list, tuple)):
        messages = list(messages)
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or '"todos"' not in content:
            continue
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            continue
        snapshot = _normalize_snapshot(data)
        if snapshot is not None:
            # Carry the source message timestamp so the frontend can
            # reconcile cold-load vs. INFLIGHT snapshots by recency.
            ts_raw = msg.get("timestamp")
            try:
                ts_val = float(ts_raw) if ts_raw is not None else 0.0
            except (TypeError, ValueError):
                ts_val = 0.0
            if ts_val > 0:
                snapshot["ts"] = ts_val
            return snapshot
    return None


def emit_todo_state(
    put: Callable[[str, dict], Any],
    *,
    name: Optional[str],
    function_result: Any,
    session_id: Optional[str],
    stream_id: Optional[str],
    source: str = "tool",
) -> bool:
    """Emit a ``todo_state`` SSE event when ``name == 'todo'``.

    Returns ``True`` if an event was emitted, ``False`` otherwise.
    Always swallows internal errors — emission must never break tool
    delivery, which is the caller's primary contract.

    Args:
        put: streaming queue callback; signature ``put(event, data)``.
        name: tool name from the callback. Skipped when not ``'todo'``.
        function_result: raw tool result (JSON string or dict).
        session_id: tag so the frontend can filter cross-session events.
        stream_id: tag so SSE replay can dedupe by stream.
        source: emission origin tag. ``'tool'`` for live tool calls;
                future callers may use ``'compression-refresh'`` etc.

    The full snapshot is always sent — idempotent re-application is safe
    under SSE replay through the run journal.
    """
    if name != "todo":
        return False
    try:
        snapshot = parse_todo_tool_result(function_result)
        if snapshot is None:
            return False
        put(EVENT_NAME, {
            "session_id": session_id,
            "stream_id": stream_id,
            "source": source,
            "ts": time.time(),
            **snapshot,
        })
        return True
    except Exception:
        # Per-call debug logging — a flood would mean the queue is
        # broken, in which case the rest of the stream is already dead.
        logger.debug("todo_state emit failed (name=%s)", name, exc_info=True)
        return False


def attach_todo_state(
    payload: dict,
    messages: Optional[Iterable[dict]],
) -> bool:
    """Attach a derived ``todo_state`` snapshot to a session GET response.

    Mutates ``payload`` in place when a snapshot can be derived.
    Returns ``True`` if attached, ``False`` otherwise. Always swallows
    errors — a malformed sidecar must never break the session GET
    response.

    The caller is responsible for any higher-level gating
    (e.g. ``load_messages``); this helper is a no-op on empty/``None``
    ``messages`` so callers can hand it whatever message list they have.
    """
    if not messages:
        return False
    try:
        snapshot = derive_todo_state(messages)
        if snapshot is None:
            return False
        payload[PAYLOAD_KEY] = snapshot
        return True
    except Exception:
        logger.debug("todo_state attach failed", exc_info=True)
        return False
