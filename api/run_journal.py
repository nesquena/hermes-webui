"""Append-only WebUI run event journal helpers.

This is the first #1925 journal/replay slice.  It mirrors SSE events emitted by
the existing in-process streaming path without changing execution ownership.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterable

RUN_JOURNAL_DIR_NAME = "_run_journal"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WRITER_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()
# Next-seq to assign per run-journal file path, kept in memory so repeat appends
# to the same run do not re-parse the whole file on every call. The per-path
# ``_lock_for(path)`` serializes same-path reserve→append so seqs stay monotonic
# and file order matches; ``_SEQ_CACHE_LOCK`` (below) additionally guards every
# *structural* access to the dict (reserve/note/evict) so ``delete_run_journal``
# can iterate + drop keys while a concurrent append on ANOTHER path inserts one,
# without a ``dictionary changed size during iteration`` crash. See
# ``_reserve_next_seq`` and ``delete_run_journal`` (which evicts stale entries).
_SEQ_CACHE: dict[str, int] = {}
_SEQ_CACHE_LOCK = threading.Lock()
_TERMINAL_SSE_EVENTS = {"done", "cancel", "apperror", "error", "stream_end"}
_FSYNC_MODE_ENV = "HERMES_WEBUI_RUN_JOURNAL_FSYNC"
_FSYNC_MODE_EAGER = "eager"
_FSYNC_MODE_TERMINAL_ONLY = "terminal-only"


def _default_session_dir() -> Path:
    from api.models import SESSION_DIR

    return Path(SESSION_DIR)


def _validate_id(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or not _SAFE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"invalid {field}")
    return cleaned


def _run_path(session_id: str, run_id: str, session_dir: Path | None = None) -> Path:
    sid = _validate_id(session_id, "session_id")
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    return root / RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"


def _lock_for(path: Path) -> threading.Lock:
    key = (str(path.parent), path.name, str(os.getpid()))
    with _WRITER_LOCKS_GUARD:
        lock = _WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITER_LOCKS[key] = lock
        return lock


def _read_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    malformed: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return events, malformed
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append({"line": line_no, "raw": raw})
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed.append({"line": line_no, "raw": raw})
    return events, malformed


def _next_seq(path: Path) -> int:
    events, _malformed = _read_jsonl(path)
    seqs = [int(event.get("seq") or 0) for event in events if isinstance(event.get("seq"), int)]
    return (max(seqs) + 1) if seqs else 1


def _reserve_next_seq(path: Path) -> int:
    """Reserve and return the next seq for ``path``, advancing the in-memory cache.

    Callers MUST hold ``_lock_for(path)``. The first append per path in this
    process seeds the cache from ``_next_seq(path)`` (one file read); every later
    append is a pure in-memory increment, avoiding the O(n) re-parse that
    re-reading the whole journal on every append caused (O(n^2) over a run).
    Because ``RunJournalWriter`` and the free ``append_run_event`` share this one
    cache under the same per-path lock, their seqs stay monotonic and gapless
    even when both write the same path. ``_SEQ_CACHE_LOCK`` additionally makes the
    dict get+set atomic against a concurrent cross-path eviction.
    """
    key = str(path)
    with _SEQ_CACHE_LOCK:
        nxt = _SEQ_CACHE.get(key)
        if nxt is not None:
            _SEQ_CACHE[key] = nxt + 1
            return nxt
    # Cache miss: seed from disk WITHOUT holding the module-global lock, so a
    # slow first-access file read for one path can't block every other path's
    # cache ops. The caller holds the per-path lock, so only one thread per path
    # can reach this branch — no double-seed, and no same-path writer can race
    # the value in between.
    seeded = _next_seq(path)
    with _SEQ_CACHE_LOCK:
        _SEQ_CACHE[key] = seeded + 1
        return seeded


def _note_assigned_seq(path: Path, seq: int) -> None:
    """Keep the cache at least one past an explicitly-supplied ``seq``.

    Callers MUST hold ``_lock_for(path)``. When an append carries a caller-chosen
    ``seq`` rather than drawing from the cache, advance the cache so a later
    cache-based append on the same path cannot re-issue an already-used seq.
    """
    key = str(path)
    nxt = int(seq) + 1
    with _SEQ_CACHE_LOCK:
        if _SEQ_CACHE.get(key, 0) < nxt:
            _SEQ_CACHE[key] = nxt


def _terminal_state_for_event(event_name: str, payload) -> str | None:
    name = str(event_name or "")
    if name == "done" or name == "stream_end":
        if isinstance(payload, dict):
            explicit_state = str(payload.get("terminal_state") or "").strip().lower()
            if explicit_state in {"tool_limit_reached"}:
                return explicit_state
        return "completed"
    if name == "cancel":
        return "interrupted-by-user"
    if name in {"apperror", "error"}:
        err_type = str((payload or {}).get("type") or "").strip().lower() if isinstance(payload, dict) else ""
        if err_type == "tool_limit_reached":
            return "tool_limit_reached"
        if err_type in {"cancelled", "canceled"}:
            return "interrupted-by-user"
        if err_type == "interrupted":
            return "interrupted-by-crash"
        return "errored"
    return None


def _run_journal_fsync_mode() -> str:
    raw = os.environ.get(_FSYNC_MODE_ENV, _FSYNC_MODE_TERMINAL_ONLY)
    mode = str(raw or "").strip().lower()
    if mode in {_FSYNC_MODE_EAGER, _FSYNC_MODE_TERMINAL_ONLY}:
        return mode
    return _FSYNC_MODE_TERMINAL_ONLY


def _should_fsync_event(terminal_state: str | None) -> bool:
    if _run_journal_fsync_mode() == _FSYNC_MODE_EAGER:
        return True
    return bool(terminal_state)


def _fsync_parent_dir(path: Path) -> None:
    try:
        dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def append_run_event(
    session_id: str,
    run_id: str,
    event_name: str,
    payload=None,
    *,
    session_dir: Path | None = None,
    seq: int | None = None,
    created_at: float | None = None,
) -> dict:
    """Append one durable run event and fsync it according to the journal policy."""
    path = _run_path(session_id, run_id, session_dir=session_dir)
    payload = payload if payload is not None else {}
    event_name = str(event_name or "").strip()
    if not event_name:
        raise ValueError("event_name is required")
    with _lock_for(path):
        if seq is not None:
            assigned_seq = int(seq)
            _note_assigned_seq(path, assigned_seq)
        else:
            assigned_seq = _reserve_next_seq(path)
        terminal_state = _terminal_state_for_event(event_name, payload)
        event = {
            "version": 1,
            "event_id": f"{run_id}:{assigned_seq}",
            "seq": assigned_seq,
            "run_id": str(run_id),
            "session_id": str(session_id),
            "event": event_name,
            "type": event_name,
            "created_at": float(created_at if created_at is not None else time.time()),
            "terminal": bool(terminal_state),
            "terminal_state": terminal_state,
            "payload": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        created_file = not path.exists()
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            if _should_fsync_event(terminal_state):
                os.fsync(fh.fileno())
        if created_file:
            _fsync_parent_dir(path)
        return event


class RunJournalWriter:
    """Stateful writer for one WebUI stream/run."""

    def __init__(self, session_id: str, run_id: str, *, session_dir: Path | None = None):
        self.session_id = _validate_id(session_id, "session_id")
        self.run_id = _validate_id(run_id, "run_id")
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self._path = _run_path(self.session_id, self.run_id, session_dir=self.session_dir)
        self._lock = _lock_for(self._path)

    def append_sse_event(self, event_name: str, payload=None) -> dict:
        # Draw from the shared module-level seq cache under the per-path lock so
        # this writer and any direct append_run_event() call on the same path
        # agree on one monotonic, gapless sequence.
        with self._lock:
            seq = _reserve_next_seq(self._path)
        return append_run_event(
            self.session_id,
            self.run_id,
            event_name,
            payload or {},
            session_dir=self.session_dir,
            seq=seq,
        )


def read_run_events(
    session_id: str,
    run_id: str,
    *,
    after_seq: int | None = None,
    max_seq: int | None = None,
    session_dir: Path | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    events, malformed = _read_jsonl(path)
    if after_seq is not None:
        events = [event for event in events if int(event.get("seq") or 0) > int(after_seq)]
    if max_seq is not None:
        events = [event for event in events if int(event.get("seq") or 0) <= int(max_seq)]
    return {
        "session_id": str(session_id),
        "run_id": str(run_id),
        "events": events,
        "malformed": malformed,
    }


def _summary_from_events(session_id: str, run_id: str, events: Iterable[dict]) -> dict:
    ordered = [event for event in events if isinstance(event, dict)]
    last = ordered[-1] if ordered else None
    terminal_events = [event for event in ordered if event.get("terminal")]
    terminal = next(
        (event for event in reversed(terminal_events) if event.get("event") != "stream_end"),
        terminal_events[-1] if terminal_events else None,
    )
    status = terminal.get("terminal_state") if terminal else ("running" if ordered else "unknown")
    return {
        "session_id": str(session_id),
        "run_id": str(run_id),
        "stream_id": str(run_id),
        "event_count": len(ordered),
        "last_seq": int((last or {}).get("seq") or 0),
        "last_event_id": (last or {}).get("event_id"),
        "terminal": bool(terminal),
        "terminal_state": status,
        "last_event": (last or {}).get("event"),
    }


def latest_run_summary(session_id: str, run_id: str, *, session_dir: Path | None = None) -> dict:
    journal = read_run_events(session_id, run_id, session_dir=session_dir)
    return _summary_from_events(session_id, run_id, journal.get("events") or [])


def find_run_summary(run_id: str, *, session_dir: Path | None = None) -> dict | None:
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        session_id = path.parent.name
        events, _malformed = _read_jsonl(path)
        summary = _summary_from_events(session_id, rid, events)
        summary["path"] = str(path)
        return summary
    return None


def delete_run_journal(session_id: str, *, session_dir: Path | None = None) -> bool:
    """Remove the entire per-session run-journal directory (``_run_journal/{sid}/``).

    The run journal stores one directory per session containing a ``{rid}.jsonl``
    file per run, so removing the session's directory clears every run's full
    request/response payloads. Invalid/empty ids and a missing directory are a
    no-op so callers can invoke this unconditionally on delete. Returns ``True``
    if a directory was removed, ``False`` otherwise.
    """
    import shutil

    sid = str(session_id or "").strip()
    # Reject path-traversal ids: the regex below permits dots, so a bare "." or
    # ".." would resolve `root / RUN_JOURNAL_DIR_NAME / sid` to the journal ROOT
    # (or its parent) and rmtree the wrong directory. The route call site only
    # passes real sids, but this is a public helper — guard it directly.
    if sid in (".", "..") or not sid or "/" in sid or "\\" in sid or not _SAFE_ID_RE.fullmatch(sid):
        return False
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_journal_dir = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_journal_dir.exists():
        return False
    shutil.rmtree(session_journal_dir, ignore_errors=True)
    removed = not session_journal_dir.exists()
    # Evict any writer locks the removed runs left behind. `_lock_for` keys are
    # ``(str(path.parent), path.name, pid)`` and every run file for this session
    # lives directly under ``session_journal_dir``, so drop all keys whose parent
    # dir matches — pid-independent — to keep `_WRITER_LOCKS` from growing forever.
    # Guard on confirmed removal: `rmtree(ignore_errors=True)` can silently leave
    # the directory (locked files on Windows, permission transients). If the files
    # still exist their locks are still live — evicting them would hand a later
    # `_lock_for` caller a brand-new Lock, breaking mutual exclusion with a writer
    # still holding the old one.
    if removed:
        dir_key = str(session_journal_dir)
        with _WRITER_LOCKS_GUARD:
            for key in [k for k in _WRITER_LOCKS if k[0] == dir_key]:
                del _WRITER_LOCKS[key]
        # Drop cached next-seq entries for the removed runs too. Every run file
        # for this session lives directly under ``session_journal_dir``, so its
        # cache key's parent dir matches. Without this, a run re-created at the
        # same path would resume the stale cached seq instead of restarting at 1.
        # Hold ``_SEQ_CACHE_LOCK`` — the SAME mutex ``_reserve_next_seq``/
        # ``_note_assigned_seq`` take — so a concurrent append on another path
        # cannot mutate the dict mid-iteration (``dictionary changed size``).
        with _SEQ_CACHE_LOCK:
            for key in [k for k in _SEQ_CACHE if str(Path(k).parent) == dir_key]:
                del _SEQ_CACHE[key]
    return removed


def stale_interrupted_event(session_id: str, run_id: str, *, after_seq: int | None = None) -> dict | None:
    summary = latest_run_summary(session_id, run_id)
    if summary.get("terminal") or not summary.get("event_count"):
        return None
    seq = int(summary.get("last_seq") or 0) + 1
    if after_seq is not None and seq <= int(after_seq):
        return None
    payload = {
        "type": "interrupted",
        "recovery_control": True,
        "message": "The live worker stopped before this run finished.",
        "hint": "The transcript was restored to the last journaled event. Start a new turn if you still need the task to continue.",
        "session_id": session_id,
        "stream_id": run_id,
        "journal_last_seq": summary.get("last_seq"),
    }
    return {
        "version": 1,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": run_id,
        "session_id": session_id,
        "event": "apperror",
        "type": "apperror",
        "created_at": time.time(),
        "terminal": True,
        "terminal_state": "lost-worker-bookkeeping",
        "payload": payload,
        "synthetic": True,
    }
