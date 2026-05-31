"""Project append-only run-journal events into graph-card data.

The journal is the source of truth.  This module does not invent demo nodes, does
not inspect live process state, and does not mutate journal files.  It is the
read-only bridge between the durable SSE journal and a future WebUI "run graph"
view.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any, Iterable

from api.run_journal import latest_run_summary, read_run_events

_TERMINAL_EVENTS = {"done", "stream_end", "cancel", "apperror", "error"}
_TOOL_START_EVENTS = {"tool"}
_TOOL_END_EVENTS = {"tool_complete"}
_ASSISTANT_EVENTS = {"token", "interim_assistant"}
_REASONING_EVENTS = {"reasoning"}
_COMPRESSION_EVENTS = {"compressing", "compressed"}
_METERING_EVENTS = {"metering"}
_WARNING_EVENTS = {"warning"}


def _safe_str(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _event_seq(event: dict) -> int:
    try:
        return int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _event_time(event: dict) -> float | None:
    try:
        raw = event.get("created_at")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _event_name(event: dict) -> str:
    return str(event.get("event") or event.get("type") or "").strip()


def _payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _hash_key(parts: Iterable[Any]) -> str:
    raw = "|".join(str(p) for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _payload_summary(event_name: str, payload: dict) -> dict[str, Any]:
    """Return bounded metadata for cards without dumping raw logs/tokens."""
    summary: dict[str, Any] = {}
    for key in ("name", "tool", "type", "status", "phase", "message", "hint"):
        if key in payload and payload.get(key) not in (None, ""):
            summary[key] = _safe_str(payload.get(key))
    if event_name in _ASSISTANT_EVENTS or event_name in _REASONING_EVENTS:
        text = payload.get("text") or payload.get("content") or payload.get("delta") or ""
        summary["text_chars"] = len(str(text))
    if event_name in _TOOL_START_EVENTS:
        args = payload.get("args") or payload.get("arguments") or {}
        if isinstance(args, dict):
            summary["arg_keys"] = sorted(str(k) for k in args.keys())[:20]
        elif args:
            summary["args_chars"] = len(str(args))
    if event_name in _TOOL_END_EVENTS:
        result = payload.get("result") or payload.get("output") or payload.get("content") or ""
        if result:
            summary["result_chars"] = len(str(result))
        if "ok" in payload:
            summary["ok"] = bool(payload.get("ok"))
    if event_name in _METERING_EVENTS:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            summary["usage"] = {
                key: usage.get(key)
                for key in ("input_tokens", "output_tokens", "estimated_cost", "cache_hit_percent")
                if key in usage
            }
    return summary


def _status_from_terminal(event_name: str, payload: dict, terminal_state: str | None = None) -> str | None:
    if event_name == "done":
        return "succeeded"
    if event_name == "stream_end":
        return "succeeded" if terminal_state == "completed" else "completed"
    if event_name == "cancel":
        return "interrupted"
    if event_name in {"apperror", "error"}:
        err_type = str(payload.get("type") or terminal_state or "").lower()
        if err_type in {"cancelled", "canceled", "interrupted", "interrupted-by-user", "lost-worker-bookkeeping"}:
            return "interrupted"
        return "failed"
    return None


def _new_node(node_id: str, kind: str, label: str, parent_id: str, event: dict, status: str = "running") -> dict[str, Any]:
    seq = _event_seq(event)
    created_at = _event_time(event)
    return {
        "id": node_id,
        "kind": kind,
        "label": label,
        "status": status,
        "parent_id": parent_id,
        "first_seq": seq,
        "last_seq": seq,
        "event_count": 0,
        "started_at": created_at,
        "finished_at": None,
        "events": [],
        "latest": {},
    }


def _touch_node(node: dict[str, Any], event: dict, status: str | None = None) -> None:
    seq = _event_seq(event)
    event_name = _event_name(event)
    payload = _payload(event)
    node["last_seq"] = max(int(node.get("last_seq") or 0), seq)
    node["event_count"] = int(node.get("event_count") or 0) + 1
    node.setdefault("events", []).append({
        "seq": seq,
        "event": event_name,
        "event_id": event.get("event_id"),
        "created_at": _event_time(event),
    })
    node["latest"] = _payload_summary(event_name, payload)
    if status:
        node["status"] = status
    if node.get("status") in {"succeeded", "failed", "interrupted", "completed", "skipped"}:
        node["finished_at"] = _event_time(event) or node.get("finished_at")


def _tool_node_key(event: dict, fallback_index: int) -> str:
    payload = _payload(event)
    key = (
        payload.get("tool_call_id")
        or payload.get("call_id")
        or payload.get("id")
        or payload.get("name")
        or payload.get("tool")
    )
    if key:
        return _safe_str(key, 80)
    return f"tool-{fallback_index}"


def _tool_label(event: dict) -> str:
    payload = _payload(event)
    return _safe_str(payload.get("name") or payload.get("tool") or payload.get("id") or "Tool call")


def build_run_graph_from_events(session_id: str, run_id: str, events: Iterable[dict]) -> dict[str, Any]:
    """Build graph-card data from already-read journal events."""
    ordered_events = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: (_event_seq(event), _event_time(event) or 0.0),
    )
    root_id = f"run:{run_id}"
    root = {
        "id": root_id,
        "kind": "run",
        "label": "Run",
        "status": "running" if ordered_events else "unknown",
        "parent_id": None,
        "first_seq": _event_seq(ordered_events[0]) if ordered_events else 0,
        "last_seq": _event_seq(ordered_events[-1]) if ordered_events else 0,
        "event_count": len(ordered_events),
        "started_at": _event_time(ordered_events[0]) if ordered_events else None,
        "finished_at": None,
        "events": [],
        "latest": {},
    }

    nodes: "OrderedDict[str, dict[str, Any]]" = OrderedDict([(root_id, root)])
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    tool_index = 0

    def ensure_edge(source: str, target: str, kind: str = "contains") -> None:
        key = (source, target, kind)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({"source": source, "target": target, "kind": kind})

    def ensure_node(node_id: str, kind: str, label: str, event: dict, status: str = "running") -> dict[str, Any]:
        node = nodes.get(node_id)
        if node is None:
            node = _new_node(node_id, kind, label, root_id, event, status=status)
            nodes[node_id] = node
            ensure_edge(root_id, node_id)
        return node

    active_tool_by_key: dict[str, str] = {}

    for event in ordered_events:
        event_name = _event_name(event)
        payload = _payload(event)
        terminal_state = event.get("terminal_state")

        if event_name in _REASONING_EVENTS:
            node = ensure_node(f"{root_id}:reasoning", "model_reasoning", "Reasoning", event)
            _touch_node(node, event)
        elif event_name in _ASSISTANT_EVENTS:
            node = ensure_node(f"{root_id}:assistant", "assistant_output", "Assistant output", event)
            _touch_node(node, event)
        elif event_name in _TOOL_START_EVENTS:
            tool_index += 1
            tool_key = _tool_node_key(event, tool_index)
            node_id = f"{root_id}:tool:{_hash_key([tool_key])}"
            active_tool_by_key[tool_key] = node_id
            node = ensure_node(node_id, "tool_call", _tool_label(event), event)
            _touch_node(node, event, status="running")
        elif event_name in _TOOL_END_EVENTS:
            tool_key = _tool_node_key(event, tool_index + 1)
            node_id = active_tool_by_key.get(tool_key) or f"{root_id}:tool:{_hash_key([tool_key])}"
            node = ensure_node(node_id, "tool_call", _tool_label(event), event)
            ok = payload.get("ok")
            status = "succeeded" if ok is not False else "failed"
            _touch_node(node, event, status=status)
        elif event_name in _COMPRESSION_EVENTS:
            node = ensure_node(f"{root_id}:compression", "compression", "Context compression", event)
            status = "succeeded" if event_name == "compressed" else "running"
            _touch_node(node, event, status=status)
        elif event_name in _METERING_EVENTS:
            node = ensure_node(f"{root_id}:metering", "metering", "Metering", event)
            _touch_node(node, event, status="running")
        elif event_name in _WARNING_EVENTS:
            node_id = f"{root_id}:warning:{_hash_key([_event_seq(event), payload.get('type'), payload.get('message')])}"
            node = ensure_node(node_id, "warning", "Warning", event, status="succeeded")
            _touch_node(node, event, status="succeeded")
        elif event_name in _TERMINAL_EVENTS:
            status = _status_from_terminal(event_name, payload, terminal_state) or "completed"
            node_id = f"{root_id}:terminal"
            node = ensure_node(node_id, "terminal", _safe_str(event_name or "terminal"), event, status=status)
            _touch_node(node, event, status=status)
            root["status"] = status
            root["finished_at"] = _event_time(event) or root.get("finished_at")
        else:
            node_id = f"{root_id}:event:{_hash_key([event_name, _event_seq(event)])}"
            node = ensure_node(node_id, "event", _safe_str(event_name or "Event"), event, status="succeeded")
            _touch_node(node, event, status="succeeded")

    if root["status"] == "running":
        # If the journal stored an explicit terminal state but the terminal event
        # did not make it through the branch above, preserve that status rather
        # than guessing a success path.
        terminal = next((event for event in reversed(ordered_events) if event.get("terminal")), None)
        if terminal:
            status = _status_from_terminal(_event_name(terminal), _payload(terminal), terminal.get("terminal_state"))
            if status:
                root["status"] = status
                root["finished_at"] = _event_time(terminal)

    return {
        "version": 1,
        "session_id": str(session_id),
        "run_id": str(run_id),
        "status": root["status"],
        "event_count": len(ordered_events),
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def build_run_graph(session_id: str, run_id: str, *, after_seq: int | None = None) -> dict[str, Any]:
    """Read journal events and project them into graph-card data."""
    journal = read_run_events(session_id, run_id, after_seq=after_seq)
    graph = build_run_graph_from_events(session_id, run_id, journal.get("events") or [])
    graph["malformed"] = journal.get("malformed") or []
    try:
        graph["summary"] = latest_run_summary(session_id, run_id)
    except Exception:
        graph["summary"] = None
    return graph
