"""WebUI runtime route handlers.

Exposes stable runtime endpoints for run status, event replay, and controls.
Delegates to ``api/runtime_journal.py`` for persistence and
``api/runtime_adapter.py`` for adapter-mode detection.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs

from api.runtime_adapter import (
    runtime_adapter_enabled,
    runtime_adapter_mode,
    runtime_adapter_agent_runs_enabled,
)
from api.runtime_journal import RuntimeJournal

_RUNTIME_JOURNAL = None


def _journal() -> RuntimeJournal:
    global _RUNTIME_JOURNAL
    if _RUNTIME_JOURNAL is None:
        _RUNTIME_JOURNAL = RuntimeJournal()
    return _RUNTIME_JOURNAL


def _adapter():
    from api.runtime_adapters import get_runtime_adapter

    return get_runtime_adapter()


def _reset_journal_for_test():
    global _RUNTIME_JOURNAL
    _RUNTIME_JOURNAL = None
    from api.runtime_adapters import _reset_adapter_instance_for_test

    _reset_adapter_instance_for_test()


def handle_runtime_capabilities(handler, parsed):
    """GET /api/runtime/capabilities"""
    from api.helpers import j as json_response

    mode = runtime_adapter_mode()
    is_journal = runtime_adapter_enabled()
    is_agent_runs = runtime_adapter_agent_runs_enabled()
    has_resumable = is_journal or is_agent_runs
    payload = {
        "api_version": "2026-07-02",
        "runtime_adapter": mode,
        "supports": {
            "resumable_events": has_resumable,
            "last_event_id": has_resumable,
            "cancel": is_journal or is_agent_runs,
            "approval": is_agent_runs,
            "clarify": is_agent_runs,
        },
    }
    return json_response(handler, payload)


def handle_active_run(handler, parsed):
    """GET /api/sessions/{session_id}/active-run"""
    from api.helpers import j as json_response

    path = str(parsed.path)
    prefix = "/api/sessions/"
    suffix = "/active-run"
    if not path.startswith(prefix) or not path.endswith(suffix):
        from api.helpers import bad

        return bad(handler, "invalid route", 404)
    session_id = path[len(prefix) : -len(suffix)]
    if not session_id:
        from api.helpers import bad

        return bad(handler, "session_id is required", 400)
    jrn = _journal()
    active = jrn.get_active_run_for_session(session_id)
    if active is None:
        return json_response(handler, {"active": False, "run": None})
    run_dict = {
        "run_id": active.run_id,
        "session_id": active.session_id,
        "status": active.status,
        "last_event_id": active.last_event_id,
        "last_seq": active.last_seq,
        "terminal": active.terminal,
        "controls": ["observe", "cancel"] if not active.terminal else [],
    }
    return json_response(handler, {"active": True, "run": run_dict})


def handle_run_status(handler, parsed):
    """GET /api/runs/{run_id}"""
    from api.helpers import j as json_response, bad

    path = str(parsed.path)
    prefix = "/api/runs/"
    if not path.startswith(prefix):
        return bad(handler, "invalid route", 404)
    run_id = path[len(prefix):]
    if not run_id:
        return bad(handler, "run_id is required", 400)
    if "/" in run_id or "\\" in run_id:
        return bad(handler, "invalid route", 404)
    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                status=502,
            )
        status = adapter.get_run(run_id)
        d = {
            "run_id": status.run_id,
            "session_id": status.session_id,
            "status": status.status,
            "last_event_id": status.last_event_id,
            "last_seq": None,
            "terminal": status.status in ("completed", "failed", "cancelled", "expired"),
            "controls": status.active_controls if not (status.status in ("completed", "failed", "cancelled", "expired")) else [],
            "pending_approval_ids": [status.pending_approval_id] if status.pending_approval_id else [],
            "pending_clarify_ids": [status.pending_clarify_id] if status.pending_clarify_id else [],
            "error": None,
            "result": None,
        }
        return json_response(handler, d)
    jrn = _journal()
    status = jrn.get_status(run_id)
    if status is None:
        return json_response(handler, {"error": "not_found"}, status=404)
    d = status.to_dict()
    d.setdefault("controls", ["observe"] if not status.terminal else [])
    return json_response(handler, d)

def handle_run_events(handler, parsed):
    """GET /api/runs/{run_id}/events"""
    from api.helpers import j as json_response, bad

    path = str(parsed.path)
    prefix = "/api/runs/"
    suffix = "/events"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return bad(handler, "invalid route", 404)
    run_id = path[len(prefix):-len(suffix)]
    if not run_id:
        return bad(handler, "run_id is required", 400)
    if "/" in run_id or "\\" in run_id:
        return bad(handler, "invalid run_id", 400)
    params = parse_qs(parsed.query)
    after_seq_raw = params.get("after_seq", [None])[0]
    limit_raw = params.get("limit", [None])[0]
    after_seq = _parse_int(after_seq_raw)
    limit = _parse_int(limit_raw)
    accept_header = (handler.headers.get("Accept") or "").lower()
    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                status=502,
            )
        cursor = str(after_seq) if after_seq is not None else None
        stream = adapter.observe_run(run_id, cursor=cursor)
        events = stream.events
        if limit is not None and limit >= 0 and len(events) > limit:
            events = events[:limit]
        if "text/event-stream" in accept_header:
            return _sse_stream_agent_events(handler, run_id, events)
        return json_response(
            handler,
            {
                "run_id": stream.run_id,
                "events": events,
            },
        )
    jrn = _journal()
    events = jrn.read_events(run_id, after_seq=after_seq, limit=limit)
    if events is None:
        return json_response(handler, {"error": "not_found"}, status=404)
    if "text/event-stream" in accept_header:
        return _sse_stream_run_events(handler, run_id, events)
    return json_response(
        handler,
        {
            "run_id": run_id,
            "events": [e.to_dict() for e in (events or [])],
        },
    )

def _parse_int(value):
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _sse_stream_run_events(handler, run_id, events):
    from api.sse_chunked import end_sse_headers as _end_sse_headers

    _end_sse_headers(handler)
    try:
        for ev in events:
            d = ev.to_dict()
            handler.wfile.write(f"id: {ev.event_id}\r\n".encode("utf-8"))
            handler.wfile.write(f"event: {ev.type}\r\n".encode("utf-8"))
            handler.wfile.write(f"data: {json.dumps(d)}\r\n\r\n".encode("utf-8"))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    return True


def _sse_stream_agent_events(handler, run_id, events):
    from api.sse_chunked import end_sse_headers as _end_sse_headers

    _end_sse_headers(handler)
    try:
        for ev_d in events:
            event_id = str(ev_d.get("event_id") or "")
            event_type = str(ev_d.get("type") or "")
            handler.wfile.write(f"id: {event_id}\r\n".encode("utf-8"))
            handler.wfile.write(f"event: {event_type}\r\n".encode("utf-8"))
            handler.wfile.write(f"data: {json.dumps(ev_d)}\r\n\r\n".encode("utf-8"))
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    return True


def handle_run_cancel(handler, body):
    """POST /api/runs/{run_id}/cancel"""
    from api.helpers import j as json_response, bad

    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        return bad(handler, "run_id is required", 400)
    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                status=502,
            )
        result = adapter.cancel_run(run_id)
        return json_response(
            handler,
            {"ok": result.accepted, "run_id": run_id, "status": result.status, "message": result.safe_message},
            status=200 if result.accepted else (501 if result.status == "not_supported" else 502),
        )
    if not runtime_adapter_enabled():
        return json_response(
            handler,
            {
                "error": "not_supported",
                "message": "Cancel is not supported by the current runtime adapter.",
            },
            501,
        )
    try:
        from api.streaming import cancel_stream

        result = cancel_stream(run_id)
        return json_response(handler, {"ok": True, "cancelled": result, "run_id": run_id})
    except Exception as exc:
        return json_response(
            handler,
            {
                "error": "not_supported",
                "message": str(exc) or "Cancel is not supported by the current runtime adapter.",
            },
            501,
        )


def handle_run_approval(handler, body):
    """POST /api/runs/{run_id}/approval"""
    from api.helpers import j as json_response

    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                status=502,
            )
        run_id = str(body.get("run_id") or "").strip()
        approval_id = str(body.get("approval_id") or "").strip()
        choice = str(body.get("choice") or "accept").strip()
        result = adapter.respond_approval(run_id, approval_id, choice)
        return _control_result_response(handler, result, "approval", run_id)
    return json_response(
        handler,
        {
            "error": "not_supported",
            "message": "Approval is not supported by the current runtime adapter.",
        },
        501,
    )


def handle_run_clarify(handler, body):
    """POST /api/runs/{run_id}/clarify"""
    from api.helpers import j as json_response

    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                status=502,
            )
        run_id = str(body.get("run_id") or "").strip()
        clarify_id = str(body.get("clarify_id") or "").strip()
        response = str(body.get("response") or body.get("answer") or "").strip()
        result = adapter.respond_clarify(run_id, clarify_id, response)
        return _control_result_response(handler, result, "clarify", run_id)
    return json_response(
        handler,
        {
            "error": "not_supported",
            "message": "Clarify is not supported by the current runtime adapter.",
        },
        501,
    )


def _control_result_response(handler, result, action_type, run_id):
    from api.helpers import j as json_response

    status_map = {
        "not_found": 404,
        "conflict": 409,
        "not_supported": 501,
        "error": 502,
    }
    status_code = status_map.get(result.status, 200 if result.accepted else 502)
    response = {
        "ok": result.accepted,
        "status": result.status,
        "message": result.safe_message,
        "run_id": run_id,
    }
    return json_response(handler, response, status=status_code)
