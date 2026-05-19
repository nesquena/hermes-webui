"""Metadata-only Capy structured progress event status and recorder.

This module exposes a bounded local taxonomy for progress/event streams. It
intentionally stores and returns only aggregate/status metadata: no raw prompts,
command bodies, generated widget bodies, renderer/source/html/script fields,
API-auth fields, credentials, or secret-looking values are read, persisted, or
echoed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EVENT_FAMILIES = [
    "run",
    "tool",
    "subagent",
    "taskboard",
    "memory.ingest",
    "space.visual_qa",
]

_SUPPORTED_EVENT_TYPES = [
    "run.started",
    "run.completed",
    "run.failed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "subagent.started",
    "subagent.completed",
    "subagent.failed",
    "taskboard.updated",
    "memory.ingest.started",
    "memory.ingest.completed",
    "memory.ingest.failed",
    "space.visual_qa.started",
    "space.visual_qa.completed",
    "space.visual_qa.failed",
]
_SUPPORTED_EVENT_SET = set(_SUPPORTED_EVENT_TYPES)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,120}$")
_UNSAFE_PUBLIC_ID_RE = re.compile(
    r"SECRET_VALUE_DO_NOT_LEAK|<\s*/?\s*script\b|<[^>]+>|bearer\b|api[ _-]?key|api[ _-]?auth|"
    r"\b(?:sk|pk)-(?:live|test)(?:[-_][A-Za-z0-9]+)*\b|gh[pousr]_[A-Za-z0-9_]+|"
    r"renderer|rendercode|generated[_ -]?code|raw\s+prompt|ignore\s+previous\s+instructions|"
    r"credential|password|secret(?!ary)|token(?!ization)|authorization|cookie|"
    r"(?:^|[._/\s])on(?:click|load|error|submit|change|mouseover|focus|blur)(?:$|[._/\s])|"
    r"(?:^|[._/\s])(?:html|script|source|data|body|code)(?:$|[._/\s])|"
    r"(?:html|script|source|data|body|code)(?:panel|widget|module|source|body)",
    re.IGNORECASE,
)
_SAFE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_MAX_STATUS_EVENTS = 200


def progress_events_log_path() -> Path:
    """Return the local progress JSONL path without creating user data in Git."""
    configured = os.getenv("CAPY_PROGRESS_LOG")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(os.getenv("CAPY_PROGRESS_ROOT") or "~/.hermes/capy-progress").expanduser().resolve()
    return root / "events.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _event_family(event_type: str) -> str:
    if event_type.startswith("space.visual_qa."):
        return "space.visual_qa"
    if event_type.startswith("memory.ingest."):
        return "memory.ingest"
    return event_type.split(".", 1)[0]


def _safe_public_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(text):
        return ""
    if _UNSAFE_PUBLIC_ID_RE.search(text):
        return ""
    return text


def _normalize_scoped_space_id(value: Any) -> str:
    raw = str(value or "").strip()
    scoped_space_id = _safe_public_id(raw) if raw else ""
    if not scoped_space_id:
        raise ValueError("Invalid progress space id")
    return scoped_space_id


def _safe_created_at(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_TIMESTAMP_RE.fullmatch(text) else ""


def _normalize_event_type(payload: dict[str, Any]) -> str:
    candidates = []
    for key in ("event_type", "eventType", "type"):
        if key not in payload:
            continue
        value = str(payload.get(key) or "").strip().lower()
        if value:
            candidates.append(value)
    if candidates and any(value != candidates[0] for value in candidates[1:]):
        raise ValueError("Conflicting progress event type aliases")
    event_type = candidates[0] if candidates else ""
    if event_type not in _SUPPORTED_EVENT_SET:
        raise ValueError("Unsupported progress event type")
    return event_type


def _normalize_run_id(payload: dict[str, Any]) -> str:
    candidates = []
    for key in ("run_id", "runId"):
        if key not in payload:
            continue
        value = _safe_public_id(payload.get(key))
        if value:
            candidates.append(value)
    if candidates and any(value != candidates[0] for value in candidates[1:]):
        raise ValueError("Conflicting progress run aliases")
    return candidates[0] if candidates else ""


def _normalize_space_id(payload: dict[str, Any]) -> str:
    candidates = []
    for key in ("space_id", "spaceId"):
        if key not in payload:
            continue
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        value = _safe_public_id(raw)
        if not value:
            raise ValueError("Invalid progress space id")
        candidates.append(value)
    if candidates and any(value != candidates[0] for value in candidates[1:]):
        raise ValueError("Conflicting progress space aliases")
    return candidates[0] if candidates else ""


def _space_id_from_record(item: dict[str, Any], run_id: str) -> str:
    space_id = _safe_public_id(item.get("space_id") or item.get("spaceId"))
    if space_id:
        return space_id
    for prefix in ("research:", "creator:"):
        if run_id.startswith(prefix):
            return _safe_public_id(run_id[len(prefix) :])
    return ""
def _read_events(space_id: str | None = None) -> list[dict[str, Any]]:
    path = progress_events_log_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    scoped_space_id = _normalize_scoped_space_id(space_id) if space_id else ""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                event_type = item.get("event_type")
                if event_type in _SUPPORTED_EVENT_SET:
                    run_id = _safe_public_id(item.get("run_id"))
                    event = {
                        "event_id": _safe_public_id(item.get("event_id")),
                        "event_type": event_type,
                        "family": _event_family(event_type),
                        "run_id": run_id,
                        "created_at": _safe_created_at(item.get("created_at")),
                    }
                    item_space_id = _space_id_from_record(item, run_id)
                    if scoped_space_id and item_space_id != scoped_space_id:
                        continue
                    if item_space_id:
                        event["space_id"] = item_space_id
                    events.append(event)
                    if len(events) > _MAX_STATUS_EVENTS:
                        events = events[-_MAX_STATUS_EVENTS:]
    except OSError:
        return []
    return events[-_MAX_STATUS_EVENTS:]


def _active_run_count(events: list[dict[str, Any]]) -> int:
    active: set[str] = set()
    for event in events:
        run_id = event.get("run_id") or ""
        if not run_id:
            continue
        event_type = event.get("event_type")
        if event_type == "run.started":
            active.add(run_id)
        elif event_type in {"run.completed", "run.failed"}:
            active.discard(run_id)
    return len(active)


def _recent_event_types(events: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for event in events:
        event_type = event.get("event_type") or ""
        if event_type in _SUPPORTED_EVENT_SET and event_type not in labels:
            labels.append(event_type)
        if len(labels) >= 6:
            break
    return labels


def _recent_family_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        family = event.get("family") or _event_family(str(event.get("event_type") or ""))
        if family not in _EVENT_FAMILIES:
            continue
        counts[family] = counts.get(family, 0) + 1
    return {family: counts[family] for family in _EVENT_FAMILIES if counts.get(family)}


def _recent_events(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    recent: list[dict[str, str]] = []
    for event in reversed(events):
        event_id = _safe_public_id(event.get("event_id"))
        event_type = str(event.get("event_type") or "")
        family = str(event.get("family") or _event_family(event_type))
        run_id = _safe_public_id(event.get("run_id"))
        created_at = _safe_created_at(event.get("created_at"))
        if not event_id or event_type not in _SUPPORTED_EVENT_SET or family not in _EVENT_FAMILIES or not created_at:
            continue
        recent.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "family": family,
                "run_id": run_id,
                "created_at": created_at,
            }
        )
        space_id = _safe_public_id(event.get("space_id"))
        if space_id:
            recent[-1]["space_id"] = space_id
        if len(recent) >= 6:
            break
    return recent


def progress_status(space_id: str | None = None) -> dict[str, Any]:
    """Return local-only progress event capability/status metadata."""
    scoped_space_id = _normalize_scoped_space_id(space_id) if space_id is not None else ""
    events = _read_events(space_id=scoped_space_id or None)
    last_event_at = events[-1]["created_at"] if events else ""
    status = {
        "available": True,
        "local_only": True,
        "metadata_only": True,
        "status": "ready",
        "active_run_count": _active_run_count(events),
        "recent_event_count": len(events),
        "recent_event_types": _recent_event_types(events),
        "recent_family_counts": _recent_family_counts(events),
        "recent_events": _recent_events(events),
        "last_event_at": last_event_at,
        "event_families": list(_EVENT_FAMILIES),
        "supported_event_types": list(_SUPPORTED_EVENT_TYPES),
        "redaction_status": "metadata_only",
    }
    if scoped_space_id:
        status["space_id"] = scoped_space_id
    return status


def record_progress_event(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Persist one allow-listed progress event as metadata-only JSONL."""
    body = payload if isinstance(payload, dict) else {}
    event_type = _normalize_event_type(body)
    run_id = _normalize_run_id(body)
    space_id = _normalize_space_id(body)
    created_at = _now_iso()
    digest_input = json.dumps(
        {"event_type": event_type, "run_id": run_id, "space_id": space_id, "created_at": created_at},
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
    record = {
        "event_id": event_id,
        "event_type": event_type,
        "family": _event_family(event_type),
        "run_id": run_id,
        "created_at": created_at,
        "redaction_status": "metadata_only",
    }
    if space_id:
        record["space_id"] = space_id
    path = progress_events_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    result = {
        "stored": True,
        "queued": True,
        "event_id": event_id,
        "event_type": event_type,
        "family": record["family"],
        "run_id": run_id,
        "created_at": created_at,
        "redaction_status": "metadata_only",
    }
    if space_id:
        result["space_id"] = space_id
    return result


__all__ = ["progress_events_log_path", "progress_status", "record_progress_event"]
