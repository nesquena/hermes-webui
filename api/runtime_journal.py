"""Durable WebUI runtime journal for replay after restart or backgrounding.

Wires ``api/runtime_contract.py`` ``RuntimeEvent`` and ``RuntimeStatus``
types into append-only run event storage with an active-session index.

Storage layout (under ``STATE_DIR / "runs"`` by default)::

  runs/
    run_<id>.jsonl   -- one JSONL file per run (redacted event dicts)
    _index.json       -- active-session mapping + per-run status snapshots

This module does not import ``api/streaming.py``, ``api/routes.py``, or
any live runtime globals. It is dependency-light and safe to use from
recovery, replay, or reconnection paths.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from api.runtime_contract import (
    RuntimeEvent,
    RuntimeStatus,
    make_event,
    make_status,
    is_valid_status,
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_RUNS_DIR_NAME = "runs"

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})


def _default_base_dir() -> Path:
    from api.config import STATE_DIR

    return Path(STATE_DIR) / _RUNS_DIR_NAME


def _validate_id(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or not _SAFE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"invalid {field}")
    return cleaned


def _make_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def _run_file_path(run_id: str, base_dir: Path) -> Path:
    rid = _validate_id(run_id, "run_id")
    return base_dir / f"{rid}.jsonl"


def _index_path(base_dir: Path) -> Path:
    return base_dir / "_index.json"


def _lock_for_run(run_id: str, locks: dict[str, threading.Lock], guard: threading.Lock) -> threading.Lock:
    with guard:
        lock = locks.get(run_id)
        if lock is None:
            lock = threading.Lock()
            locks[run_id] = lock
        return lock


def _read_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return events
    for raw in lines:
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _dict_to_runtime_event(d: dict) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=str(d.get("event_id", "")),
        seq=int(d.get("seq", 0)),
        run_id=str(d.get("run_id", "")),
        session_id=str(d.get("session_id", "")),
        type=str(d.get("type", "")),
        created_at=float(d.get("created_at", 0.0)),
        terminal=bool(d.get("terminal", False)),
        payload=dict(d.get("payload", {})),
    )


def _load_index(index_path_obj: Path) -> dict:
    try:
        raw = index_path_obj.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active_sessions", {})
    data.setdefault("runs", {})
    return data


def _atomic_write_json(
    target: Path,
    data: dict,
    lock: threading.Lock,
) -> None:
    tmp = target.with_suffix(
        f".tmp.{os.getpid()}.{threading.current_thread().ident}"
    )
    with lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


class RuntimeJournal:
    """Durable append-only run event store.

    Stores run events as JSONL and maintains an index of session-to-run
    mappings. Thread-safe for single-process use.

    Usage::

        journal = RuntimeJournal()
        status = journal.create_run("session_1", metadata={"model": "sonnet"})
        event = make_event(run_id=status.run_id, session_id="session_1", seq=1, type="token.delta")
        journal.append_event(event)
        journal.get_status(status.run_id)
        journal.mark_terminal(status.run_id, "completed")

    Unknown run behaviour: ``get_status()``, ``read_events()``,
    ``get_active_run_for_session()``, and ``mark_terminal()`` return ``None``
    for unknown runs. ``append_event()`` raises ``ValueError`` for unknown
    runs.
    """

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._index_file = _index_path(self._base_dir)
        self._index_lock = threading.Lock()
        self._event_locks: dict[str, threading.Lock] = {}
        self._event_locks_guard = threading.Lock()

    def create_run(
        self,
        session_id: str,
        metadata: dict | None = None,
        run_id: str | None = None,
    ) -> RuntimeStatus:
        sid = _validate_id(session_id, "session_id")
        run_id = _validate_id(run_id, "run_id") if run_id else _make_run_id()
        _ = metadata
        status = make_status(
            run_id=run_id,
            session_id=sid,
            status="queued",
            controls=["cancel"],
        )
        now = time.time()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        index = _load_index(self._index_file)
        index["active_sessions"][sid] = run_id
        index["runs"][run_id] = {
            "run_id": run_id,
            "session_id": sid,
            "status": "queued",
            "last_event_id": None,
            "last_seq": None,
            "terminal": False,
            "controls": ["cancel"],
            "pending_approval_ids": [],
            "pending_clarify_ids": [],
            "error": None,
            "result": None,
            "created_at": now,
        }
        _atomic_write_json(self._index_file, index, self._index_lock)
        return status

    def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        rid = _validate_id(event.run_id, "run_id")
        index = _load_index(self._index_file)
        if rid not in index["runs"]:
            raise ValueError(f"unknown run_id {rid!r}")
        path = _run_file_path(rid, self._base_dir)
        event_lock = _lock_for_run(rid, self._event_locks, self._event_locks_guard)
        with event_lock:
            line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            created_file = not path.exists()
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                if event.terminal:
                    os.fsync(fh.fileno())
            if created_file:
                try:
                    dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
        run_info = index["runs"][rid]
        run_info["last_event_id"] = event.event_id
        run_info["last_seq"] = int(event.seq)
        if run_info.get("status") == "queued":
            run_info["status"] = "running"
        if event.terminal:
            run_info["terminal"] = True
            sid = run_info["session_id"]
            if index["active_sessions"].get(sid) == rid:
                index["active_sessions"].pop(sid, None)
        index["runs"][rid] = run_info
        _atomic_write_json(self._index_file, index, self._index_lock)
        return event

    def read_events(
        self,
        run_id: str,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[RuntimeEvent] | None:
        rid = _validate_id(run_id, "run_id")
        path = _run_file_path(rid, self._base_dir)
        if not path.exists():
            return None
        raw = _read_jsonl(path)
        events = [_dict_to_runtime_event(d) for d in raw]
        if after_seq is not None:
            events = [e for e in events if int(e.seq) > int(after_seq)]
        if limit is not None and limit >= 0:
            events = events[: int(limit)]
        return events

    def get_status(self, run_id: str) -> RuntimeStatus | None:
        rid = _validate_id(run_id, "run_id")
        index = _load_index(self._index_file)
        entry = index["runs"].get(rid)
        if entry is None:
            return None
        return make_status(
            run_id=entry["run_id"],
            session_id=entry["session_id"],
            status=entry["status"],
            last_event_id=entry.get("last_event_id"),
            last_seq=entry.get("last_seq"),
            terminal=bool(entry.get("terminal", False)),
            controls=entry.get("controls", []),
            pending_approval_ids=entry.get("pending_approval_ids", []),
            pending_clarify_ids=entry.get("pending_clarify_ids", []),
            error=entry.get("error"),
            result=entry.get("result"),
        )

    def get_active_run_for_session(self, session_id: str) -> RuntimeStatus | None:
        sid = _validate_id(session_id, "session_id")
        index = _load_index(self._index_file)
        active_run_id = index["active_sessions"].get(sid)
        if active_run_id is None:
            return None
        return self.get_status(active_run_id)

    def list_active_runs(self) -> list[dict]:
        index = _load_index(self._index_file)
        active = index["active_sessions"]
        runs = index["runs"]
        results: list[dict] = []
        for sid, rid in active.items():
            entry = runs.get(rid)
            if entry is None:
                continue
            results.append({
                "run_id": entry["run_id"],
                "session_id": entry["session_id"],
                "status": entry.get("status", "unknown"),
                "terminal": bool(entry.get("terminal", False)),
                "last_event_id": entry.get("last_event_id"),
                "last_seq": entry.get("last_seq"),
                "controls": entry.get("controls", []),
                "pending_approval_ids": entry.get("pending_approval_ids", []),
                "pending_clarify_ids": entry.get("pending_clarify_ids", []),
                "error": entry.get("error"),
                "result": entry.get("result"),
                "created_at": entry.get("created_at"),
            })
        return results

    def mark_terminal(
        self,
        run_id: str,
        status: str,
        result: dict | None = None,
        error: dict | None = None,
    ) -> RuntimeStatus | None:
        rid = _validate_id(run_id, "run_id")
        if not is_valid_status(status):
            raise ValueError(f"invalid terminal status {status!r}")
        index = _load_index(self._index_file)
        entry = index["runs"].get(rid)
        if entry is None:
            return None
        entry["status"] = str(status)
        entry["terminal"] = True
        if result is not None:
            entry["result"] = dict(result)
        if error is not None:
            entry["error"] = str(error.get("message", error))
        index["runs"][rid] = entry
        sid = entry["session_id"]
        if index["active_sessions"].get(sid) == rid:
            index["active_sessions"].pop(sid, None)
        _atomic_write_json(self._index_file, index, self._index_lock)
        return make_status(
            run_id=entry["run_id"],
            session_id=entry["session_id"],
            status=entry["status"],
            last_event_id=entry.get("last_event_id"),
            last_seq=entry.get("last_seq"),
            terminal=True,
            controls=entry.get("controls", []),
            pending_approval_ids=entry.get("pending_approval_ids", []),
            pending_clarify_ids=entry.get("pending_clarify_ids", []),
            error=entry.get("error"),
            result=entry.get("result"),
        )
