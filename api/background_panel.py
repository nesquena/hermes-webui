"""Background task panel: list and stop the processes a chat session spawned."""

from __future__ import annotations

import logging

from api.helpers import bad, j, require

logger = logging.getLogger(__name__)

_FINISHED_LIMIT = 40


def _registry():
    from tools.process_registry import process_registry

    return process_registry


def _summarize(entry: dict) -> dict:
    return {
        "process_id": entry.get("session_id"),
        "command": (entry.get("command") or "")[:160],
        "pid": entry.get("pid"),
        "status": entry.get("status"),
        "uptime_seconds": entry.get("uptime_seconds"),
        "started_at": entry.get("started_at"),
        "exit_code": entry.get("exit_code"),
        "completion_reason": entry.get("completion_reason"),
        "output_preview": (entry.get("output_preview") or "")[-400:],
    }


def _entries_for_session(session_id: str) -> list[dict]:
    registry = _registry()
    entries = registry.list_sessions(session_key=session_id)
    running = [_summarize(e) for e in entries if e.get("status") == "running"]
    finished = [_summarize(e) for e in entries if e.get("status") != "running"]
    finished.sort(key=lambda e: e.get("uptime_seconds") or 0, reverse=True)
    return running + finished[:_FINISHED_LIMIT]


def handle_background_processes(handler, query: dict) -> None:
    session_id = (query.get("session_id") or [""])[0]
    if not session_id:
        return bad(handler, "Missing session_id")
    try:
        entries = _entries_for_session(session_id)
    except Exception as exc:
        logger.warning("background panel listing failed: %s", exc)
        return j(handler, {"running": [], "finished": [], "error": str(exc)})
    running = [e for e in entries if e["status"] == "running"]
    finished = [e for e in entries if e["status"] != "running"]
    return j(handler, {"running": running, "finished": finished})


def handle_background_process_kill(handler, body: dict) -> None:
    try:
        require(body, "process_id")
    except ValueError as exc:
        return bad(handler, str(exc))
    process_id = str(body["process_id"]).strip()
    if not process_id:
        return bad(handler, "process_id is required")
    try:
        result = _registry().kill_process(process_id, source="webui.panel")
    except Exception as exc:
        logger.warning("background panel kill failed for %s: %s", process_id, exc)
        return bad(handler, f"kill failed: {exc}", 500)
    if not result or result.get("not_found"):
        return bad(handler, "Process not found", 404)
    return j(handler, {"ok": True, "process_id": process_id})
