"""Crash-safe WebUI turn journal helpers.

The journal is deliberately tiny: one JSONL file per session, append-only events,
and read helpers that tolerate malformed lines. Recovery and repair can then
reason about submitted turns without depending on in-memory stream state.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Iterable

TURN_JOURNAL_DIR_NAME = "_turn_journal"
_TERMINAL_EVENTS = {"completed", "interrupted"}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _default_session_dir() -> Path:
    from api.models import SESSION_DIR

    return Path(SESSION_DIR)


def _journal_path(session_id: str, session_dir: Path | None = None) -> Path:
    sid = str(session_id or "").strip()
    if not sid or "/" in sid or "\\" in sid or not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("invalid session_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    return root / TURN_JOURNAL_DIR_NAME / f"{sid}.jsonl"


def _make_turn_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:12]}"


def append_turn_journal_event(
    session_id: str,
    event: dict,
    *,
    session_dir: Path | None = None,
) -> dict:
    """Append one turn journal event and fsync it before returning.

    The returned event is the exact payload written, with default ``version``,
    ``session_id``, ``turn_id``, and ``created_at`` fields filled in.
    """
    if not isinstance(event, dict):
        raise TypeError("event must be a dict")
    event_name = str(event.get("event") or "").strip()
    if not event_name:
        raise ValueError("event is required")
    payload = dict(event)
    payload.setdefault("version", 1)
    payload["session_id"] = str(session_id)
    payload.setdefault("turn_id", _make_turn_id())
    payload.setdefault("created_at", time.time())

    path = _journal_path(session_id, session_dir=session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return payload


def read_turn_journal(session_id: str, *, session_dir: Path | None = None) -> dict:
    """Read a session journal, returning valid events plus malformed lines."""
    path = _journal_path(session_id, session_dir=session_dir)
    events: list[dict] = []
    malformed: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {"session_id": str(session_id), "events": [], "malformed": []}
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append({"line": line_no, "raw": raw})
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            malformed.append({"line": line_no, "raw": raw})
    return {"session_id": str(session_id), "events": events, "malformed": malformed}


def derive_turn_journal_states(events: Iterable[dict]) -> dict[str, dict]:
    """Return the latest event per ``turn_id``."""
    states: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        turn_id = str(event.get("turn_id") or "").strip()
        if not turn_id:
            continue
        previous = states.get(turn_id)
        if previous is None or float(event.get("created_at") or 0) >= float(previous.get("created_at") or 0):
            states[turn_id] = event
    return states


def _latest_turn_id_for_stream(events: Iterable[dict], stream_id: str) -> str | None:
    stream = str(stream_id or "").strip()
    if not stream:
        return None
    latest: str | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("stream_id") or "") != stream:
            continue
        turn_id = str(event.get("turn_id") or "").strip()
        if turn_id:
            latest = turn_id
    return latest


def append_turn_journal_event_for_stream(
    session_id: str,
    stream_id: str,
    event: dict,
    *,
    session_dir: Path | None = None,
) -> dict:
    """Append a lifecycle event for the turn associated with ``stream_id``."""
    payload = dict(event)
    payload["stream_id"] = str(stream_id)
    if not payload.get("turn_id"):
        journal = read_turn_journal(session_id, session_dir=session_dir)
        turn_id = _latest_turn_id_for_stream(journal.get("events") or [], stream_id)
        if turn_id:
            payload["turn_id"] = turn_id
    return append_turn_journal_event(session_id, payload, session_dir=session_dir)


def iter_turn_journal_session_ids(session_dir: Path) -> list[str]:
    journal_dir = Path(session_dir) / TURN_JOURNAL_DIR_NAME
    if not journal_dir.exists():
        return []
    return sorted(path.stem for path in journal_dir.glob("*.jsonl") if path.is_file())


def is_terminal_turn_event(event: dict) -> bool:
    return str((event or {}).get("event") or "") in _TERMINAL_EVENTS
