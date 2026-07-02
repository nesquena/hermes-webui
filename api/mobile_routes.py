"""Hermex-compatible mobile API layer.

Stable versioned endpoints for the Hermex iOS app to consume.
Delegates to runtime adapters and the runtime journal for data.
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs

from api.runtime_adapter import (
    runtime_adapter_mode,
    runtime_adapter_enabled,
    runtime_adapter_agent_runs_enabled,
)
from api.runtime_journal import RuntimeJournal

_API_VERSION = "2026-07-02"
_HERMEX_IOS_MIN = "1.3.0"

_MOBILE_JOURNAL = None


def _journal():
    global _MOBILE_JOURNAL
    if _MOBILE_JOURNAL is None:
        _MOBILE_JOURNAL = RuntimeJournal()
    return _MOBILE_JOURNAL


def _reset_journal_for_test():
    global _MOBILE_JOURNAL
    _MOBILE_JOURNAL = None


def _adapter():
    from api.runtime_adapters import get_runtime_adapter

    return get_runtime_adapter()


def _current_activity_from_status(status: str | None) -> str | None:
    if status == "awaiting_approval":
        return "awaiting_approval"
    if status == "awaiting_clarify":
        return "awaiting_clarify"
    if status == "running":
        return "running"
    return None


def _build_mobile_run_dict(entry: dict, *, now: float) -> dict:
    status = entry.get("status", "unknown")
    terminal = entry.get("terminal", False)
    elapsed = None
    created = entry.get("created_at")
    if created is not None:
        elapsed = max(0.0, now - float(created))
    return {
        "run_id": entry["run_id"],
        "session_id": entry["session_id"],
        "title": entry.get("title"),
        "status": status,
        "current_activity": _current_activity_from_status(status),
        "model": entry.get("model"),
        "profile": entry.get("profile"),
        "workspace": entry.get("workspace"),
        "elapsed_seconds": elapsed,
        "last_event_id": entry.get("last_event_id"),
        "last_seq": entry.get("last_seq"),
        "terminal": bool(terminal),
        "controls": entry.get("controls", []) if not terminal else [],
    }


def _pending_action_from_entry(entry: dict) -> dict | None:
    for action_id in entry.get("pending_approval_ids", []):
        return {
            "action_id": str(action_id),
            "type": "approval",
            "run_id": entry["run_id"],
            "session_id": entry["session_id"],
            "summary": "Command requires approval",
            "choices": [],
            "created_at": None,
        }
    for action_id in entry.get("pending_clarify_ids", []):
        return {
            "action_id": str(action_id),
            "type": "clarify",
            "run_id": entry["run_id"],
            "session_id": entry["session_id"],
            "summary": "Clarification needed",
            "choices": [],
            "created_at": None,
        }
    return None


def handle_mobile_capabilities(handler, parsed):
    from api.helpers import j as json_response

    mode = runtime_adapter_mode()
    is_journal = runtime_adapter_enabled()
    is_agent_runs = runtime_adapter_agent_runs_enabled()
    has_resumable = is_journal or is_agent_runs
    payload = {
        "server": "hermes-webui",
        "api_version": _API_VERSION,
        "compatible_clients": {
            "hermex_ios_min": _HERMEX_IOS_MIN,
        },
        "runtime": {
            "adapter": mode,
            "resumable_events": has_resumable,
            "last_event_id": has_resumable,
        },
        "features": {
            "sessions": True,
            "resumable_runs": has_resumable,
            "run_dashboard": True,
            "approvals": is_agent_runs,
            "clarify": is_agent_runs,
            "workspace_search": False,
            "deployment_health": True,
            "file_uploads": True,
            "voice_metadata": False,
        },
    }
    return json_response(handler, payload)


def handle_mobile_runs(handler, parsed):
    from api.helpers import j as json_response

    now = time.time()
    active_runs: list[dict] = []
    pending_actions: list[dict] = []

    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "active_runs": [], "pending_actions": []},
                status=502,
            )
        jrn = _journal()
        entries = jrn.list_active_runs()
        if entries:
            for entry in entries:
                mobile_entry = _build_mobile_run_dict(entry, now=now)
                try:
                    agent_status = adapter.get_run(entry["run_id"])
                    mobile_entry["status"] = agent_status.status
                    mobile_entry["terminal"] = agent_status.status in (
                        "completed", "failed", "cancelled", "expired",
                    )
                    mobile_entry["controls"] = (
                        agent_status.active_controls
                        if not mobile_entry["terminal"]
                        else []
                    )
                    mobile_entry["current_activity"] = _current_activity_from_status(
                        agent_status.status
                    )
                    if agent_status.pending_approval_id:
                        pending_actions.append({
                            "action_id": agent_status.pending_approval_id,
                            "type": "approval",
                            "run_id": agent_status.run_id,
                            "session_id": agent_status.session_id,
                            "summary": "Command requires approval",
                            "choices": [],
                            "created_at": None,
                        })
                    if agent_status.pending_clarify_id:
                        pending_actions.append({
                            "action_id": agent_status.pending_clarify_id,
                            "type": "clarify",
                            "run_id": agent_status.run_id,
                            "session_id": agent_status.session_id,
                            "summary": "Clarification needed",
                            "choices": [],
                            "created_at": None,
                        })
                except Exception:
                    pass
                active_runs.append(mobile_entry)
    else:
        jrn = _journal()
        entries = jrn.list_active_runs()
        for entry in entries:
            if entry.get("terminal"):
                continue
            mobile_entry = _build_mobile_run_dict(entry, now=now)
            active_runs.append(mobile_entry)
            pa = _pending_action_from_entry(entry)
            if pa is not None:
                pending_actions.append(pa)

    payload = {
        "active_runs": active_runs,
        "pending_actions": pending_actions,
    }
    return json_response(handler, payload)


def handle_mobile_pending_actions(handler, parsed):
    from api.helpers import j as json_response

    pending_actions: list[dict] = []

    if runtime_adapter_agent_runs_enabled():
        adapter = _adapter()
        if adapter is None:
            return json_response(
                handler,
                {"error": "agent_runtime_unreachable", "pending_actions": []},
                status=502,
            )
        jrn = _journal()
        entries = jrn.list_active_runs()
        for entry in entries:
            try:
                agent_status = adapter.get_run(entry["run_id"])
                if agent_status.pending_approval_id:
                    pending_actions.append({
                        "action_id": agent_status.pending_approval_id,
                        "type": "approval",
                        "run_id": agent_status.run_id,
                        "session_id": agent_status.session_id,
                        "summary": "Command requires approval",
                        "choices": [],
                        "created_at": None,
                    })
                if agent_status.pending_clarify_id:
                    pending_actions.append({
                        "action_id": agent_status.pending_clarify_id,
                        "type": "clarify",
                        "run_id": agent_status.run_id,
                        "session_id": agent_status.session_id,
                        "summary": "Clarification needed",
                        "choices": [],
                        "created_at": None,
                    })
            except Exception:
                pass
    else:
        jrn = _journal()
        entries = jrn.list_active_runs()
        for entry in entries:
            pa = _pending_action_from_entry(entry)
            if pa is not None:
                pending_actions.append(pa)

    return json_response(handler, {"pending_actions": pending_actions})


def handle_mobile_resolve_action(handler, parsed, body):
    from api.helpers import j as json_response, bad

    path = str(parsed.path)
    prefix = "/api/mobile/pending-actions/"
    suffix = "/resolve"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return bad(handler, "invalid route", 404)
    action_id = path[len(prefix):-len(suffix)]
    if not action_id:
        return bad(handler, "action_id is required", 400)

    action_type = str(body.get("type") or "").strip()
    run_id = str(body.get("run_id") or "").strip()

    if not run_id:
        return bad(handler, "run_id is required", 400)

    if action_type not in ("approval", "clarify"):
        return bad(handler, "Invalid action type; expected 'approval' or 'clarify'", 400)

    if action_type == "approval":
        choice = str(body.get("choice") or "approve").strip()
        if runtime_adapter_agent_runs_enabled():
            adapter = _adapter()
            if adapter is None:
                return json_response(
                    handler,
                    {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                    status=502,
                )
            result = adapter.respond_approval(run_id, action_id, choice)
            if not result.accepted and result.status == "not_supported":
                return json_response(
                    handler,
                    {"error": "not_supported", "message": "Approval is not supported."},
                    status=501,
                )
            return json_response(
                handler,
                {
                    "ok": result.accepted,
                    "run_id": run_id,
                    "action_id": action_id,
                    "type": "approval",
                    "message": result.safe_message,
                },
                status=200 if result.accepted else 502,
            )
        return json_response(
            handler,
            {"error": "not_supported", "message": "Approval is not supported by the current runtime adapter."},
            status=501,
        )

    if action_type == "clarify":
        answer = str(body.get("answer") or body.get("response") or "").strip()
        if runtime_adapter_agent_runs_enabled():
            adapter = _adapter()
            if adapter is None:
                return json_response(
                    handler,
                    {"error": "agent_runtime_unreachable", "message": "agent-runs adapter is not configured."},
                    status=502,
                )
            result = adapter.respond_clarify(run_id, action_id, answer)
            if not result.accepted and result.status == "not_supported":
                return json_response(
                    handler,
                    {"error": "not_supported", "message": "Clarify is not supported."},
                    status=501,
                )
            return json_response(
                handler,
                {
                    "ok": result.accepted,
                    "run_id": run_id,
                    "action_id": action_id,
                    "type": "clarify",
                    "message": result.safe_message,
                },
                status=200 if result.accepted else 502,
            )
        return json_response(
            handler,
            {"error": "not_supported", "message": "Clarify is not supported by the current runtime adapter."},
            status=501,
        )

    return bad(handler, "unknown action type", 400)


def handle_mobile_reconnect(handler, parsed):
    from api.helpers import j as json_response

    path = str(parsed.path)
    prefix = "/api/mobile/reconnect/"
    if not path.startswith(prefix):
        return json_response(handler, {"error": "invalid route"}, status=404)
    session_id = path[len(prefix):]
    if not session_id:
        return json_response(handler, {"error": "session_id is required"}, status=400)

    jrn = _journal()
    active = jrn.get_active_run_for_session(session_id)
    if active is None:
        return json_response(handler, {"active": False, "run": None})

    return json_response(
        handler,
        {
            "active": True,
            "run": {
                "run_id": active.run_id,
                "session_id": active.session_id,
                "status": active.status,
                "last_event_id": active.last_event_id,
                "last_seq": active.last_seq,
                "terminal": active.terminal,
            },
        },
    )
