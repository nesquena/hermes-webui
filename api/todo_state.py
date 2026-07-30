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
from typing import Any, Callable, Iterable, Optional, Sequence


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

    **Empty list is a valid snapshot.** ``todos == []`` returns a normal
    snapshot (not ``None``), so the latest write wins even when it cleared
    the list. This is deliberately symmetric with the agent: its
    ``_hydrate_todo_store`` (run_agent.py) breaks at the most-recent todo
    message and, because ``if last_todo_response:`` is falsy for ``[]``,
    leaves its TodoStore empty — i.e. agent shows empty, panel shows empty.
    Do NOT reintroduce a ``len(todos) > 0`` guard here or in the frontend
    fallback (``_legacyTodosFromMessages``): that was the pre-Phase-2
    behavior that kept scanning past an empty write to an older non-empty
    list, diverging from the agent and showing a stale "cleared" list.
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
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
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
            # Carry a timestamp so the frontend can reconcile cold-load
            # vs. INFLIGHT snapshots by recency.
            #
            # Primary source: this message's own ``timestamp``. But a
            # todo tool message can lose its timestamp during context
            # compression/rebuild — the on-disk message ends up with
            # ``timestamp=None``. If we emit a snapshot with no ``ts``,
            # the frontend reads coldTs=0 and a STALE-but-timestamped
            # INFLIGHT snapshot wins the recency comparison, so the panel
            # renders a historical todo list. This is the latest-by-
            # POSITION snapshot, so it must never lose recency to an
            # earlier list. When this message has no usable timestamp,
            # fall back to the max timestamp seen anywhere at or before
            # this position — guaranteeing cold ts >= any earlier todo
            # write's ts.
            ts_val = _message_ts_float(msg.get("timestamp"))
            if ts_val <= 0:
                ts_val = _max_timestamp_through(messages, idx)
            if ts_val > 0:
                snapshot["ts"] = ts_val
            return snapshot
    return None


def _message_ts_float(ts_raw: Any) -> float:
    """Coerce a message ``timestamp`` field to a positive float, or 0.0."""
    try:
        return float(ts_raw) if ts_raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _max_timestamp_through(messages: "Sequence[Any]", upto_idx: int) -> float:
    """Largest valid ``timestamp`` among messages[0:upto_idx+1].

    Used as a recency floor when the latest todo message itself lost its
    timestamp during compression/rebuild. Scanning only up to the todo's
    position keeps the floor causally correct — it never borrows a
    timestamp from a message that came after the todo write.
    """
    best = 0.0
    end = min(upto_idx, len(messages) - 1)
    for i in range(end, -1, -1):
        m = messages[i]
        if not isinstance(m, dict):
            continue
        ts = _message_ts_float(m.get("timestamp"))
        if ts > best:
            best = ts
    return best


def _redact_snapshot(snapshot: dict) -> dict:
    """Redact credential-shaped text from a todo snapshot before it leaves the process.

    The live SSE path (:func:`emit_todo_state`) does NOT pass through
    ``redact_session_data`` (api/helpers.py) the way the cold-load session
    GET response does, so emission must redact the same content that path
    would — otherwise the live Todos panel (and the run-journal replay that
    persists every SSE event) becomes a redaction bypass for any credential
    an agent wrote into a todo item's ``content``. The live event also
    carries the FULL untruncated todos, a wider exposure surface than the
    truncated ``preview`` the sibling ``tool``/``tool_complete`` events send.

    ``_redact_value`` is imported lazily to keep the dependency direction
    one-way (helpers must never import todo_state) and to avoid paying the
    import cost on the cold-load path, which redacts via ``redact_session_data``.
    The redaction setting is read once per SSE snapshot and threaded through the
    recursive helper so nested strings do not reload settings.json individually.

    Returns a new, redacted snapshot. Raises on failure so the caller fails
    closed (no emission) rather than leaking an unredacted payload.
    """
    from typing import cast
    from api.config import load_settings
    from api.helpers import _redact_value
    _enabled = bool(load_settings().get("api_redact_enabled", True))
    # ``_redact_value`` preserves container shape (dict in → dict out); the
    # cast narrows its broad recursive union back to dict for the type checker.
    return cast(dict, _redact_value(snapshot, _enabled=_enabled))


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
    under SSE replay through the run journal. The snapshot is redacted
    before emission (see :func:`_redact_snapshot`); if redaction fails the
    event is dropped (fail-closed) rather than leaking an unredacted payload.
    """
    if name != "todo":
        return False
    try:
        snapshot = parse_todo_tool_result(function_result)
        if snapshot is None:
            return False
        snapshot = _redact_snapshot(snapshot)
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
        # Redaction failure also lands here and correctly drops the event.
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
