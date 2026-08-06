"""Durable session-scoped decisions produced by external authorization tools."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path


SCHEMA_VERSION = "superset-pending-decision/v2"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _session_dir(session_dir: Path | None) -> Path:
    if session_dir is not None:
        return Path(session_dir).resolve()
    from api.models import SESSION_DIR

    return Path(SESSION_DIR).resolve()


def _identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def _root(session_dir: Path | None) -> Path:
    sessions = _session_dir(session_dir)
    root = (sessions / "_pending_decisions").resolve()
    root.relative_to(sessions)
    return root


def _decision_path(session_id: str, decision_id: str, session_dir: Path | None) -> Path:
    root = _root(session_dir)
    path = (
        root
        / _identifier(session_id, "session_id")
        / f"{_identifier(decision_id, 'decision_id')}.json"
    ).resolve()
    path.relative_to(root)
    return path


def _resolution_path(path: Path) -> Path:
    return path.with_name(path.stem + ".resolution.json")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate(payload: dict) -> dict:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported pending decision schema")
    _identifier(payload.get("session_id"), "session_id")
    _identifier(payload.get("decision_id"), "decision_id")
    _identifier(payload.get("task_id"), "task_id")
    if payload.get("state") != "waiting_for_user":
        raise ValueError("pending decision must be waiting_for_user")
    if not str(payload.get("question") or "").strip():
        raise ValueError("pending decision question is required")
    options = payload.get("options")
    if not isinstance(options, list) or len({str(item) for item in options}) < 2:
        raise ValueError("pending decision requires distinct options")
    if "expires_at" in payload or "timeout_seconds" in payload:
        raise ValueError("mutation authorization decisions cannot expire by wall clock")
    return dict(payload)


def create_pending_decision(payload: dict, *, session_dir: Path | None = None) -> dict:
    normalized = _validate(payload)
    path = _decision_path(normalized["session_id"], normalized["decision_id"], session_dir)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != normalized:
            raise ValueError("pending decision id already exists with different content")
        return existing
    _atomic_write(path, normalized)
    return normalized


def get_pending_decision(
    session_id: str, decision_id: str, *, session_dir: Path | None = None
) -> dict | None:
    path = _decision_path(session_id, decision_id, session_dir)
    try:
        payload = _validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    resolution_path = _resolution_path(path)
    if resolution_path.exists():
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        payload["state"] = "resolved"
        payload["resolution"] = resolution
    return payload


def list_pending_decisions(session_id: str, *, session_dir: Path | None = None) -> list[dict]:
    sid = _identifier(session_id, "session_id")
    directory = _root(session_dir) / sid
    if not directory.exists():
        return []
    pending: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".resolution.json"):
            continue
        item = get_pending_decision(sid, path.stem, session_dir=session_dir)
        if item and item.get("state") == "waiting_for_user":
            pending.append(item)
    return pending


def mark_pending_decision_resolved(
    session_id: str,
    decision_id: str,
    *,
    option: str,
    event_id: str,
    turn_id: str,
    session_dir: Path | None = None,
) -> dict:
    path = _decision_path(session_id, decision_id, session_dir)
    pending = get_pending_decision(session_id, decision_id, session_dir=session_dir)
    if not pending or pending.get("state") != "waiting_for_user":
        raise ValueError("pending decision is not active")
    selected = str(option or "").strip()
    if selected not in pending["options"]:
        raise ValueError("resolution must select an offered option")
    resolution = {
        "option": selected,
        "event_id": _identifier(event_id, "event_id"),
        "turn_id": _identifier(turn_id, "turn_id"),
        "resolved_at": time.time(),
    }
    resolution_path = _resolution_path(path)
    if resolution_path.exists():
        raise ValueError("pending decision is not active")
    _atomic_write(resolution_path, resolution)
    return {**pending, "state": "resolved", "resolution": resolution}
