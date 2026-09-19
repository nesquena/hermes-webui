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
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

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
# Summary callers only need terminal state and the latest cursor. Re-parsing a
# completed journal's full payload (which can include multi-megabyte tool or
# session results) on every status/reconnect probe is needless. This process
# cache is keyed by a complete stat identity, so it is never used after an
# atomic replacement, append, truncate, or same-path file recreation.
_SUMMARY_CACHE_MAX_ENTRIES = 128
_SUMMARY_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int, int], dict]] = OrderedDict()
_SUMMARY_CACHE_LOCK = threading.Lock()
# Events that mark a run terminal in the journal / summary sense.
TERMINAL_SSE_EVENTS = frozenset({"done", "cancel", "apperror", "error", "stream_end"})
# Events that should close an SSE relay drain loop. `done` is intentionally
# excluded: background title generation and `stream_end` are emitted after
# `done`, and breaking early would drop them. `apperror` is included because
# it terminates with no trailing `stream_end`.
SSE_RELAY_CLOSE_EVENTS = frozenset({"stream_end", "cancel", "apperror", "error"})
# Back-compat alias used by older call sites / tests.
_TERMINAL_SSE_EVENTS = TERMINAL_SSE_EVENTS
_FSYNC_MODE_ENV = "HERMES_WEBUI_RUN_JOURNAL_FSYNC"
_FSYNC_MODE_EAGER = "eager"
_FSYNC_MODE_TERMINAL_ONLY = "terminal-only"
_SESSION_REPLAY_MAX_BYTES = 4 * 1024 * 1024
_SESSION_REPLAY_MAX_ROWS = 4096
_SESSION_REPLAY_READ_CHUNK_BYTES = 64 * 1024
_SNAPSHOT_ARGS_MAX_ITEMS = 64
_SNAPSHOT_ARGS_MAX_DEPTH = 8
_SNAPSHOT_ARGS_MAX_STRING_CHARS = 8192
_SNAPSHOT_ARGS_MAX_TOTAL_CHARS = 64 * 1024
_SNAPSHOT_ARGS_TRUNCATED_SUFFIX = "...[truncated]"


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


def _summary_cache_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return the complete filesystem identity used for summary-cache validity.

    Includes ``st_ctime_ns`` so a same-inode, same-size rewrite that restores the
    original ``mtime_ns`` (e.g. an atomic replace) still invalidates the cache —
    ctime advances on any metadata/content change and cannot be forged back.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _get_cached_summary(path: Path) -> dict | None:
    signature = _summary_cache_signature(path)
    if signature is None:
        return None
    key = str(path)
    with _SUMMARY_CACHE_LOCK:
        cached = _SUMMARY_CACHE.get(key)
        if cached is None:
            return None
        cached_signature, summary = cached
        if cached_signature != signature:
            _SUMMARY_CACHE.pop(key, None)
            return None
        _SUMMARY_CACHE.move_to_end(key)
        return dict(summary)


def _cache_summary(
    path: Path,
    summary: dict,
    *,
    expected_signature: tuple[int, int, int, int, int] | None = None,
) -> None:
    signature = _summary_cache_signature(path)
    # The pre-read signature is an enforced TOCTOU precondition. In particular,
    # a journal created after a missing-file read has ``None -> signature`` and
    # must not cache the empty/unknown result under the new file's identity.
    if signature is None or signature != expected_signature:
        return
    key = str(path)
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[key] = (signature, dict(summary))
        _SUMMARY_CACHE.move_to_end(key)
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_MAX_ENTRIES:
            _SUMMARY_CACHE.popitem(last=False)


def _discard_cached_summary(path: Path) -> None:
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.pop(str(path), None)


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


def _parse_run_journal_event_id(raw: str | None) -> tuple[str | None, int | None]:
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    if ":" in raw:
        run_id, tail = raw.rsplit(":", 1)
    else:
        run_id, tail = None, raw
    try:
        seq = max(0, int(tail))
    except (TypeError, ValueError):
        return run_id or None, None
    return run_id or None, seq


def _snapshot_args_take_budget(budget: dict[str, int], amount: int) -> int:
    remaining = max(0, int(budget.get("remaining") or 0))
    take = min(remaining, max(0, amount))
    budget["remaining"] = remaining - take
    return take


def _bound_snapshot_args_string(value: str, budget: dict[str, int]) -> str:
    max_chars = min(len(value), _SNAPSHOT_ARGS_MAX_STRING_CHARS)
    take = _snapshot_args_take_budget(budget, max_chars)
    out = value[:take]
    if take < len(value):
        suffix_take = _snapshot_args_take_budget(budget, len(_SNAPSHOT_ARGS_TRUNCATED_SUFFIX))
        out += _SNAPSHOT_ARGS_TRUNCATED_SUFFIX[:suffix_take]
    return out


def _bound_run_journal_snapshot_value(value: Any, budget: dict[str, int], depth: int) -> Any:
    if budget.get("remaining", 0) <= 0:
        return None
    if isinstance(value, str):
        return _bound_snapshot_args_string(value, budget)
    if isinstance(value, dict):
        if depth >= _SNAPSHOT_ARGS_MAX_DEPTH:
            return {}
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _SNAPSHOT_ARGS_MAX_ITEMS or budget.get("remaining", 0) <= 0:
                break
            bounded_key = _bound_snapshot_args_string(str(key), budget)
            if not bounded_key:
                continue
            out[bounded_key] = _bound_run_journal_snapshot_value(item, budget, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if depth >= _SNAPSHOT_ARGS_MAX_DEPTH:
            return []
        return [
            _bound_run_journal_snapshot_value(item, budget, depth + 1)
            for item in value[:_SNAPSHOT_ARGS_MAX_ITEMS]
            if budget.get("remaining", 0) > 0
        ]
    if isinstance(value, (bool, int, float)) or value is None:
        try:
            _snapshot_args_take_budget(budget, len(json.dumps(value)))
        except (TypeError, ValueError):
            return None
        return value
    return _bound_snapshot_args_string(str(value), budget)


def bound_run_journal_snapshot_args(args: Any) -> Any:
    """Return recovery tool args with realistic values intact and pathological payloads bounded."""
    if args is None:
        return {}
    budget = {"remaining": _SNAPSHOT_ARGS_MAX_TOTAL_CHARS}
    return _bound_run_journal_snapshot_value(args, budget, 0)


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


def _event_created_at(event: dict, *, fallback: float = 0.0) -> float:
    try:
        return float(event.get("created_at") or fallback)
    except (TypeError, ValueError):
        return fallback


def _iter_bounded_raw_jsonl_lines(path: Path, *, max_bytes: int, retained_bytes: int = 0):
    line_no = 0
    buffered = bytearray()
    total_bytes = int(retained_bytes)
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_SESSION_REPLAY_READ_CHUNK_BYTES)
                if not chunk:
                    if buffered:
                        if total_bytes + len(buffered) > max_bytes:
                            raise ValueError("replay_limit_bytes")
                        line_no += 1
                        total_bytes += len(buffered)
                        yield line_no, bytes(buffered), total_bytes
                    return
                start = 0
                while start < len(chunk):
                    newline = chunk.find(b"\n", start)
                    if newline == -1:
                        buffered.extend(chunk[start:])
                        if total_bytes + len(buffered) > max_bytes:
                            raise ValueError("replay_limit_bytes")
                        break
                    buffered.extend(chunk[start : newline + 1])
                    if total_bytes + len(buffered) > max_bytes:
                        raise ValueError("replay_limit_bytes")
                    line_no += 1
                    total_bytes += len(buffered)
                    yield line_no, bytes(buffered), total_bytes
                    buffered.clear()
                    start = newline + 1
    except FileNotFoundError:
        return


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
        _discard_cached_summary(path)
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


def select_authoritative_terminal_event(events: Iterable[dict]) -> dict | None:
    """Return the terminal event that owns the run's settled outcome.

    ``stream_end`` is transport closure, so a preceding semantic terminal event
    (done, cancel, or error) remains authoritative. Among semantic terminal
    events, the latest journal row wins.
    """
    terminal_events = [
        event
        for event in events
        if isinstance(event, dict) and event.get("terminal")
    ]
    return next(
        (
            event
            for event in reversed(terminal_events)
            if event.get("event") != "stream_end"
        ),
        terminal_events[-1] if terminal_events else None,
    )


def _summary_from_events(session_id: str, run_id: str, events: Iterable[dict]) -> dict:
    ordered = [event for event in events if isinstance(event, dict)]
    last = ordered[-1] if ordered else None
    terminal = select_authoritative_terminal_event(ordered)
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
    path = _run_path(session_id, run_id, session_dir=session_dir)
    cached = _get_cached_summary(path)
    if cached is not None:
        return cached
    pre_read_signature = _summary_cache_signature(path)
    events, _malformed = _read_jsonl(path)
    summary = _summary_from_events(session_id, run_id, events)
    _cache_summary(path, summary, expected_signature=pre_read_signature)
    return summary


def session_journal_fingerprint(session_id: str, *, session_dir: Path | None = None) -> tuple[int, float, int]:
    """Cheap, bounded fingerprint of a session's run journal: (file_count, max_mtime, total_size).

    Reads only directory + per-file stat metadata (never parses journal bodies), so it stays
    O(runs) and cannot be tipped over by a large ``done`` row. Used to detect that the journal
    advanced during an idle live-subscribe wait — a run that starts AND finishes inside a single
    keepalive tick leaves the journal changed but never materializes a live in-memory stream, so a
    no-cursor idle subscriber would otherwise miss it until a manual refresh. Returns (0, 0.0, 0)
    when the session has no journal yet. Invalid ids resolve to the empty fingerprint rather than
    raising so callers can probe unconditionally.
    """
    try:
        sid = _validate_id(session_id, "session_id")
    except ValueError:
        return (0, 0.0, 0)
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_root.exists():
        return (0, 0.0, 0)
    count = 0
    max_mtime = 0.0
    total_size = 0
    for path in session_root.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        count += 1
        total_size += st.st_size
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
    return (count, max_mtime, total_size)


def find_run_summary(run_id: str, *, session_dir: Path | None = None) -> dict | None:
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        session_id = path.parent.name
        summary = _get_cached_summary(path)
        if summary is None:
            pre_read_signature = _summary_cache_signature(path)
            events, _malformed = _read_jsonl(path)
            summary = _summary_from_events(session_id, rid, events)
            _cache_summary(path, summary, expected_signature=pre_read_signature)
        summary["path"] = str(path)
        return summary
    return None


def read_session_run_events(
    session_id: str,
    *,
    after_event_id: str | None = None,
    session_dir: Path | None = None,
    max_bytes: int = _SESSION_REPLAY_MAX_BYTES,
    max_rows: int = _SESSION_REPLAY_MAX_ROWS,
) -> dict:
    """Replay durable run-journal rows for one session after an opaque cursor."""
    sid = _validate_id(session_id, "session_id")
    cursor_run_id, cursor_seq = _parse_run_journal_event_id(after_event_id)
    raw_cursor = str(after_event_id or "").strip()
    if raw_cursor and cursor_run_id is not None:
        try:
            cursor_run_id = _validate_id(cursor_run_id, "run_id")
        except ValueError:
            cursor_seq = None
    if raw_cursor:
        try:
            if int(raw_cursor.rsplit(":", 1)[-1]) < 0:
                cursor_seq = None
        except (TypeError, ValueError):
            pass
    if raw_cursor and (cursor_run_id is None or cursor_seq is None or cursor_seq <= 0):
        return {
            "session_id": sid,
            "cursor_run_id": cursor_run_id,
            "cursor_seq": cursor_seq,
            "status": "cursor_invalid",
            "events": [],
        }
    if not raw_cursor:
        return {
            "session_id": sid,
            "cursor_run_id": None,
            "cursor_seq": None,
            "status": "ok",
            "events": [],
        }
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    runs: list[tuple[float, str, list[dict]]] = []
    retained_rows = 0
    retained_bytes = 0
    for path in sorted(session_root.glob("*.jsonl")) if session_root.exists() else []:
        run_id = path.stem
        try:
            run_id = _validate_id(run_id, "run_id")
        except ValueError:
            continue
        events: list[dict] = []
        expected_seq = 1
        try:
            for _line_no, raw, total_bytes in _iter_bounded_raw_jsonl_lines(
                path,
                max_bytes=max_bytes,
                retained_bytes=retained_bytes,
            ):
                retained_bytes = total_bytes
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"))
                    seq = int(event.get("seq")) if isinstance(event, dict) else 0
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_malformed", "events": []}
                if (
                    seq != expected_seq
                    or event.get("event_id") != f"{run_id}:{seq}"
                    or event.get("run_id") != run_id
                    or event.get("session_id") != sid
                ):
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_noncontiguous", "events": []}
                expected_seq += 1
                retained_rows += 1
                if retained_rows > max_rows:
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_limit_rows", "events": []}
                events.append(event)
        except FileNotFoundError:
            continue
        except ValueError as exc:
            if str(exc) == "replay_limit_bytes":
                return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_limit_bytes", "events": []}
            raise
        created_at = min((_event_created_at(event) for event in events), default=path.stat().st_mtime)
        runs.append((created_at, run_id, events))
    runs.sort(key=lambda run: (run[0], run[1]))
    cursor_index = next((index for index, (_created_at, run_id, _events) in enumerate(runs) if run_id == cursor_run_id), None)
    if cursor_index is None:
        foreign_paths = root.joinpath(RUN_JOURNAL_DIR_NAME).glob(f"*/{cursor_run_id}.jsonl") if cursor_run_id else []
        foreign_session_id = next((path.parent.name for path in foreign_paths if path.parent.name != sid), "")
        status = "cursor_run_missing"
        if foreign_session_id:
            status = "cursor_session_mismatch"
        return {
            "session_id": sid,
            "cursor_run_id": cursor_run_id,
            "cursor_seq": cursor_seq,
            "status": status,
            "events": [],
        }
    cursor_events = runs[cursor_index][2]
    if cursor_seq is None or cursor_seq > len(cursor_events):
        return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "cursor_event_missing", "events": []}
    replay_events = [event for event in cursor_events if event["seq"] > cursor_seq]
    for _created_at, _run_id, events in runs[cursor_index + 1:]:
        replay_events.extend(events)
    return {
        "session_id": sid,
        "cursor_run_id": cursor_run_id,
        "cursor_seq": cursor_seq,
        "status": "ok",
        "events": replay_events,
    }


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
            for cache_key in [entry for entry in _SEQ_CACHE if str(Path(entry).parent) == dir_key]:
                del _SEQ_CACHE[cache_key]
        with _SUMMARY_CACHE_LOCK:
            for cache_key in [entry for entry in _SUMMARY_CACHE if str(Path(entry).parent) == dir_key]:
                del _SUMMARY_CACHE[cache_key]
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


# ── Retention sweep (issue #7613) ────────────────────────────────────────
# ``delete_run_journal`` only fires on full session delete (#3802/#3811), so a
# long-lived / pinned session accumulates one ``{run_id}.jsonl`` per agent
# turn forever. #7613 reported 916 MB across 104 sessions on one install,
# 98.6% of it from terminal runs (the rest is the load-bearing non-terminal
# recovery payload and MUST be left alone). Nothing ever reads a terminal
# run for a stream no client is still attached to, so a bounded sweep
# reclaiming only ``terminal: true`` files is safe.
#
# Throttle state for ``maybe_sweep_run_journals``. The lock is held only
# across the read/update of the timestamp; the sweep itself is unlocked
# because all in-memory cache access inside it already takes the per-cache
# mutexes (``_WRITER_LOCKS_GUARD``, ``_SEQ_CACHE_LOCK``, ``_SUMMARY_CACHE_LOCK``).
_LAST_SWEEP_AT: float = 0.0
_LAST_SWEEP_LOCK = threading.Lock()


def _resolve_retention(retention: dict | None) -> dict:
    """Return the retention spec from a caller-supplied dict or from ``api.config``.

    The dict shape (keys: ``enabled``, ``max_age_secs``, ``max_runs_per_session``,
    ``aggressive_total_bytes``, ``min_interval_secs``) matches the constants in
    ``api.config``. Returning a plain dict lets test code build literal specs
    without importing ``api.config`` (which would trigger yaml reads).
    """
    if retention is not None:
        return dict(retention)
    from api import config as _cfg

    return {
        "enabled": bool(getattr(_cfg, "RUN_JOURNAL_RETENTION_ENABLED", True)),
        "max_age_secs": int(getattr(_cfg, "RUN_JOURNAL_RETENTION_MAX_AGE_SECS", 14 * 86400)),
        "max_runs_per_session": int(
            getattr(_cfg, "RUN_JOURNAL_RETENTION_MAX_RUNS_PER_SESSION", 100)
        ),
        "aggressive_total_bytes": int(
            getattr(_cfg, "RUN_JOURNAL_RETENTION_AGGRESSIVE_TOTAL_BYTES", 500 * 1024 * 1024)
        ),
        "min_interval_secs": int(getattr(_cfg, "RUN_JOURNAL_RETENTION_MIN_INTERVAL_SECS", 300)),
    }


def should_purge_run(
    summary: dict,
    file_mtime: float,
    *,
    retention: dict,
    now: float,
    is_over_count_cap: bool = False,
) -> bool:
    """Return whether a single run-journal file should be reclaimed.

    The predicate is **terminal-only**: a run whose summary reports a
    settled outcome (``terminal`` True) is safe to retire when no client
    can still be attached to that stream. Non-terminal files (the
    live-recovery payload) are NEVER returned, regardless of age, count,
    or size — the journal exists to recover crashed runs and silently
    deleting it would destroy the user's only recoverable output.

    Within the terminal surface, three orthogonal triggers compose (OR):

    1. **Age cap** — terminal file older than ``max_age_secs`` (halved
       when the caller's spec has ``aggressive=True``).
    2. **Count cap** — set by the caller's per-session sort: when the
       session's terminal file count exceeds ``max_runs_per_session``
       (0 disables), the caller marks the oldest ``count - cap`` files
       as ``is_over_count_cap=True`` and the rest as False. Only those
       marked files purge via the count trigger; the unmarked ones
       survive even if the session is over-cap. This pushes the sort
       work to one place (the session sweep) instead of every call.
    3. **Aggressive mode** — set by ``purge_all_terminal_journals`` when
       the total on-disk bytes exceed ``aggressive_total_bytes``; halves
       the age cap so an out-of-control install self-heals on the next
       sweep. The terminal-only predicate is preserved at every size.

    File mtime is intentionally NOT the primary signal. Directory mtime
    is unusable (#7613 warns: it updates on any write inside, so a live
    session looks fresh while a long-idle one can be reaped). The file's
    own mtime is the cheap, accurate proxy for "when did this run
    actually finish writing" and is correct even for an actively-written
    session (its files' mtime advances on each fsync).
    """
    if not summary or not summary.get("terminal"):
        return False
    max_age = float(retention["max_age_secs"])
    if retention.get("aggressive"):
        max_age = max(1.0, max_age / 2.0)
    if (now - float(file_mtime)) >= max_age:
        return True
    cap = int(retention.get("max_runs_per_session", 0) or 0)
    if cap > 0 and is_over_count_cap:
        return True
    return False


def _purge_run_file(path: Path) -> bool:
    """Delete a single ``{run_id}.jsonl`` and evict its three in-memory caches.

    Mirrors the per-directory eviction in ``delete_run_journal`` (#5784, #5799)
    but at the single-file granularity required by retention. Every cache key
    that mentions ``path`` is dropped under its own mutex, the same pattern
    the directory-level sweep uses. A failed unlink is treated as "already
    gone" and still evicts the caches — a stale cache entry would hand a
    fresh writer a stale lock or seq and is strictly worse than the file
    being present.
    """
    # Path safety: stem and parent.name must both be safe ids so we can't
    # unlink something outside the journal dir even if a caller hands us
    # a hostile path. _SAFE_ID_RE rejects path separators but allows
    # '.', '..', and '..foo' — reject '.' and '..' explicitly, mirroring
    # ``delete_run_journal`` (#5784).
    if path.suffix != ".jsonl" or path.stem in (".", ".."):
        return False
    if path.parent.name in (".", "..") or not _SAFE_ID_RE.fullmatch(path.parent.name):
        return False
    dir_key = str(path.parent)
    name = path.name
    with _WRITER_LOCKS_GUARD:
        for key in [k for k in _WRITER_LOCKS if k[0] == dir_key and k[1] == name]:
            del _WRITER_LOCKS[key]
    with _SEQ_CACHE_LOCK:
        for cache_key in [
            k
            for k in _SEQ_CACHE
            if str(Path(k).parent) == dir_key and Path(k).name == name
        ]:
            del _SEQ_CACHE[cache_key]
    with _SUMMARY_CACHE_LOCK:
        for cache_key in [
            k
            for k in _SUMMARY_CACHE
            if str(Path(k).parent) == dir_key and Path(k).name == name
        ]:
            del _SUMMARY_CACHE[cache_key]
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not path.exists()


def purge_session_terminal_journals(
    session_id: str,
    *,
    session_dir: Path | None = None,
    retention: dict | None = None,
    now: float | None = None,
) -> int:
    """Reclaim terminal run-journal files under ``_run_journal/<session_id>/``.

    Iterates each ``*.jsonl`` in the session's run-journal dir, asks
    ``should_purge_run`` whether the run qualifies (terminal-only predicate),
    and deletes the matching files. The age cap and aggressive-mode decision
    are derived from the resolved retention spec; the total-bytes threshold
    that flips aggressive mode is checked at the all-session level by
    ``purge_all_terminal_journals`` so this function stays simple and
    testable.

    Returns the number of files deleted. Non-terminal files are NEVER
    touched regardless of age, count, or size.
    """
    spec = _resolve_retention(retention)
    if not spec.get("enabled"):
        return 0
    if now is None:
        now = time.time()
    try:
        sid = _validate_id(session_id, "session_id")
    except ValueError:
        return 0
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_root.exists() or not session_root.is_dir():
        return 0
    rows: list[tuple[Path, float, bool]] = []  # (path, mtime, terminal)
    for path in session_root.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        try:
            _validate_id(path.stem, "run_id")
        except ValueError:
            continue
        summary = latest_run_summary(sid, path.stem, session_dir=root)
        rows.append((path, st.st_mtime, bool(summary and summary.get("terminal"))))
    if not rows:
        return 0
    # Sort terminal files by mtime asc; the oldest (count - cap) are the
    # count-cap targets. The cap check is intentionally per-session so a
    # session with 5 terminal files and a cap of 2 reclaims 3, leaving 2.
    terminal_rows = sorted(
        (r for r in rows if r[2]), key=lambda r: r[1]
    )
    terminal_count = len(terminal_rows)
    cap = int(spec.get("max_runs_per_session", 0) or 0)
    over_cap_paths: set[Path] = set()
    if cap > 0 and terminal_count > cap:
        over_cap_paths = {r[0] for r in terminal_rows[: terminal_count - cap]}
    deleted = 0
    for path, mtime, terminal in rows:
        if not terminal:
            continue
        if not should_purge_run(
            {"terminal": True},
            mtime,
            retention=spec,
            now=now,
            is_over_count_cap=path in over_cap_paths,
        ):
            continue
        if _purge_run_file(path):
            deleted += 1
    return deleted


def purge_all_terminal_journals(
    *,
    session_dir: Path | None = None,
    retention: dict | None = None,
    now: float | None = None,
) -> dict:
    """Sweep every session's terminal run journals.

    Computes the total on-disk size across all sessions; if it exceeds
    ``aggressive_total_bytes`` the spec passed to per-session sweeps has
    ``aggressive=True`` set so the age cap is halved and the install
    self-heals. Returns ``{"scanned": N, "deleted": N, "aggressive": bool,
    "total_bytes": N}`` for the caller's log line / health check.
    """
    spec = _resolve_retention(retention)
    if not spec.get("enabled"):
        return {"scanned": 0, "deleted": 0, "aggressive": False, "total_bytes": 0}
    if now is None:
        now = time.time()
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    if not journal_root.exists() or not journal_root.is_dir():
        return {"scanned": 0, "deleted": 0, "aggressive": False, "total_bytes": 0}
    total_bytes = 0
    session_dirs: list[Path] = []
    for session_path in journal_root.iterdir():
        try:
            if not session_path.is_dir():
                continue
        except OSError:
            continue
        if not _SAFE_ID_RE.fullmatch(session_path.name):
            continue
        session_dirs.append(session_path)
        # Sum sizes in the same pass to avoid a second iterdir on huge trees.
        for journal_file in session_path.glob("*.jsonl"):
            try:
                total_bytes += journal_file.stat().st_size
            except OSError:
                continue
    aggressive_threshold = int(spec.get("aggressive_total_bytes", 0) or 0)
    aggressive = bool(aggressive_threshold) and total_bytes >= aggressive_threshold
    per_session_spec = dict(spec)
    if aggressive:
        per_session_spec["aggressive"] = True
    deleted_total = 0
    for session_path in session_dirs:
        try:
            deleted_total += purge_session_terminal_journals(
                session_path.name,
                session_dir=root,
                retention=per_session_spec,
                now=now,
            )
        except Exception:
            # One bad session must not abort the whole sweep.
            continue
    return {
        "scanned": len(session_dirs),
        "deleted": deleted_total,
        "aggressive": aggressive,
        "total_bytes": total_bytes,
    }


def maybe_sweep_run_journals(
    *,
    session_dir: Path | None = None,
    force: bool = False,
) -> dict | None:
    """Throttled entry point for the retention sweep.

    Skips the sweep when ``min_interval_secs`` has not elapsed since the
    last successful call. ``force=True`` bypasses the throttle (used by
    the startup hook so a freshly-restarted server catches up on a
    long-lived install's accumulated terminal runs).

    Returns the sweep stats dict on a real run, or ``None`` when the
    throttle skipped. The throttle lock is held only across the
    read/update; the sweep itself is unlocked because all in-memory cache
    access inside the sweep takes its own per-cache mutex.
    """
    spec = _resolve_retention(None)
    if not spec.get("enabled"):
        return None
    global _LAST_SWEEP_AT
    now = time.time()
    with _LAST_SWEEP_LOCK:
        if not force and (now - _LAST_SWEEP_AT) < float(spec["min_interval_secs"]):
            return None
        _LAST_SWEEP_AT = now
    return purge_all_terminal_journals(session_dir=session_dir, retention=spec, now=now)
