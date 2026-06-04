"""Derive settled todo snapshots for session GET responses.

The browser's Todos panel currently reconstructs state by reverse-scanning
loaded tool messages. That works only when the latest todo tool result is inside
the returned message window. This helper gives ``/api/session`` a compact,
explicit ``todo_state`` sidecar derived from the full settled transcript.

The detector intentionally mirrors ``run_agent.AIAgent._hydrate_todo_store``:
walk messages newest-first and use the first tool message whose JSON content has
a ``todos`` list. Empty ``todos`` is a valid snapshot so a cleared task list does
not fall through to an older non-empty write.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

VERSION = 1
PAYLOAD_KEY = "todo_state"


def _normalize_snapshot(data: Any) -> Optional[dict]:
    """Return the canonical todo snapshot shape, or ``None`` for non-todo data."""
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
    """Parse a todo tool result JSON string or pre-parsed dict into a snapshot."""
    data: Any = function_result
    if isinstance(function_result, str):
        try:
            data = json.loads(function_result)
        except (TypeError, ValueError):
            return None
    return _normalize_snapshot(data)


def _message_ts_float(ts_raw: Any) -> float:
    """Coerce a message ``timestamp`` field to a positive float, or 0.0."""
    try:
        return float(ts_raw) if ts_raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _max_timestamp_through(messages: Sequence[Any], upto_idx: int) -> float:
    """Return the largest valid timestamp at or before ``upto_idx``."""
    best = 0.0
    end = min(upto_idx, len(messages) - 1)
    for i in range(end, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        best = max(best, _message_ts_float(msg.get("timestamp")))
    return best


def derive_todo_state(messages: Optional[Iterable[dict]]) -> Optional[dict]:
    """Derive the latest settled todo snapshot from conversation history.

    Returns ``None`` when the session has no todo writes. Malformed or
    non-string tool contents are skipped so unrelated tool results never break
    session loading.
    """
    if not messages:
        return None
    if not isinstance(messages, (list, tuple)):
        messages = list(messages)

    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or '"todos"' not in content:
            continue
        snapshot = parse_todo_tool_result(content)
        if snapshot is None:
            continue

        ts_val = _message_ts_float(msg.get("timestamp"))
        if ts_val <= 0:
            ts_val = _max_timestamp_through(messages, idx)
        if ts_val > 0:
            snapshot["ts"] = ts_val
        return snapshot
    return None


def attach_todo_state(payload: dict, messages: Optional[Iterable[dict]]) -> bool:
    """Attach ``todo_state`` to a session payload when one can be derived.

    Mutates ``payload`` in place. Errors are swallowed deliberately: a malformed
    historical tool message must not make ``/api/session`` fail.
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
