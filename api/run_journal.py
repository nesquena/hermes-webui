"""Append-only WebUI run event journal helpers.

This is the first #1925 journal/replay slice.  It mirrors SSE events emitted by
the existing in-process streaming path without changing execution ownership.

Terminal runs are subject to a retention sweep (``sweep_run_journal``, #7613):
`delete_run_journal` only runs on session deletion, so a long-lived or pinned
session would otherwise accumulate one ``{run_id}.jsonl`` per run forever. The
sweep retires ``terminal: true`` runs past age / count / size caps and never
touches non-terminal runs — those are the crashed-run recovery payloads this
journal exists to serve.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

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

# ── Retention (#7613) ───────────────────────────────────────────────────────
# `delete_run_journal` has exactly one call site — session deletion — so without
# a sweep a long-lived/pinned session accumulates one `{run_id}.jsonl` per run
# forever (measured at 916 MB of completed-run logs on one real install; 98.6%
# of that footprint is `terminal: true` runs). The sweep retires TERMINAL runs
# only, bounded by three independent caps (whichever is strictest wins):
#
#   * `ttl_days`              — run file's own mtime older than the TTL
#   * `max_runs_per_session`  — beyond the newest N terminal runs in the session
#   * `max_bytes_per_session` — retained terminal bytes beyond the session budget
#
# Non-terminal runs are NEVER reclaimed: they are the crashed-run payloads the
# journal exists to recover. The age signal is the FILE's mtime — never the
# directory's (a directory mtime updates whenever any file inside changes, so
# an actively-written session always looks fresh while its dir can still be
# reaped out from under a live writer). A value of 0 disables that one cap.
#
# Caps resolve env var > settings.json > module default (mirrors
# `_resolve_session_ttl`), so operators can tune them without a code change.
RUN_JOURNAL_SWEEP_ENV = "HERMES_WEBUI_RUN_JOURNAL_SWEEP"
_RETENTION_TTL_ENV = "HERMES_WEBUI_RUN_JOURNAL_RETENTION_TTL_DAYS"
_RETENTION_MAX_RUNS_ENV = "HERMES_WEBUI_RUN_JOURNAL_RETENTION_MAX_RUNS_PER_SESSION"
_RETENTION_MAX_BYTES_ENV = "HERMES_WEBUI_RUN_JOURNAL_RETENTION_MAX_BYTES_PER_SESSION"
_RETENTION_TTL_SETTING = "run_journal_retention_ttl_days"
_RETENTION_MAX_RUNS_SETTING = "run_journal_retention_max_runs_per_session"
_RETENTION_MAX_BYTES_SETTING = "run_journal_retention_max_bytes_per_session"
DEFAULT_RUN_JOURNAL_RETENTION_TTL_DAYS = 14.0
DEFAULT_RUN_JOURNAL_RETENTION_MAX_RUNS_PER_SESSION = 40
DEFAULT_RUN_JOURNAL_RETENTION_MAX_BYTES_PER_SESSION = 256 * 1024 * 1024
_RETENTION_TTL_MAX_DAYS = 3650.0
_RETENTION_MAX_RUNS_LIMIT = 100_000
_RETENTION_MAX_BYTES_LIMIT = 100 * 1024 * 1024 * 1024  # 100 GiB
_SWEEP_DISABLED_VALUES = frozenset({"0", "false", "no", "off", "disabled", "none"})
# Minimum spacing between retention sweeps when driven by the shared
# maintenance tick (``api.background_process._reaper_loop`` via
# ``maybe_sweep_run_journal``). The tick itself fires far more often; this gate
# keeps the sweep hourly and, on a fresh process, delays the first pass so boot
# never competes with a large scan. State lives in the tick owner, not here.
RETENTION_SWEEP_INTERVAL_SECS = 3600.0
RETENTION_FIRST_SWEEP_DELAY_SECS = 60.0
# A run file must be untouched for this long before ANY cap can reclaim it.
# The run's writer can append post-terminal rows (metering / stream_end /
# title generation arrive around the terminal row), and a just-settled run may
# still be replayed to a reconnecting client; both want a settlement window.
_RETENTION_MIN_QUIESCENT_SECONDS = 3600.0
# Backward scan budget for terminal-row detection. The scan walks the file
# backwards and stops at the first VERIFIED terminal row: the common case
# (terminal row near EOF) reads one chunk; a multi-MB single row (giant
# `apperror` payload) needs the walk to cross its payload to reach its own
# prefix. Files whose terminal row sits further back than this budget are left
# untouched (fail closed — a missed reclaim is safe, a wrong one is not).
_RETENTION_VERIFY_MAX_BYTES = 64 * 1024 * 1024
_RETENTION_VERIFY_CHUNK_BYTES = 256 * 1024
_RETENTION_MARKER_BYTES = b'"terminal":true'
# A serialized run row always starts with this exact byte prefix (compact
# separators; key order fixed by `append_run_event`; `json.dumps` escapes
# quotes inside string values, and ids are restricted to [A-Za-z0-9_.-], so
# these bytes can only occur at a row start).
_RETENTION_ROW_START_BYTES = b'{"version":1,"event_id":"'
# Serialized run-row header, anchored at the row start (``re.match``).
# Groups: 1 = event_id, 2 = seq, 3 = run_id, 4 = session_id, 5 = terminal flag.
# Mirrors `append_run_event`'s compact serialization exactly; `created_at`
# permits the full float character set `json.dumps` can emit.
_RETENTION_HEADER_RE = re.compile(
    rb'\{"version":1,'
    rb'"event_id":"([^"\\]{1,300})",'
    rb'"seq":(\d{1,20}),'
    rb'"run_id":"([^"\\]{1,300})",'
    rb'"session_id":"([^"\\]{1,300})",'
    rb'"event":"[^"\\]{0,300}",'
    rb'"type":"[^"\\]{0,300}",'
    rb'"created_at":[-0-9.eE+]{1,64},'
    rb'"terminal":(true|false),'
)
# How far back a candidate marker may hunt for its row start. A genuine
# terminal row carries the marker in its first ~500 bytes; a nested
# `"terminal":true` inside a payload is rejected by the ownership-checked
# header parse (see `_verify_terminal_header_at`).
_RETENTION_ROW_START_HUNT_BYTES = 64 * 1024
_RETENTION_HEADER_MAX_BYTES = 8 * 1024
# Sweep scheduling state, shared by the maintenance tick and manual callers.
# ``None`` = this process has not yet armed its first-pass delay;
# otherwise it is the wall-clock time the last sweep was started.
_SWEEP_LAST_STARTED: float | None = None
_SWEEP_THREAD_LOCK = threading.Lock()
# Serializes sweep bodies: the maintenance tick and any explicit caller never
# scan (and unlink) concurrently.
_SWEEP_RUN_LOCK = threading.Lock()


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


# ── Retention sweep (#7613) ─────────────────────────────────────────────────


def _resolve_within(root_real: str, path: Path) -> bool:
    """True when ``path``'s fully-resolved location stays inside ``root_real``.

    Containment is checked on the RESOLVED path so a symlink (or a symlinked
    parent) cannot redirect the sweep outside the journal root. ``root_real`` is
    expected to be an ``os.path.realpath`` string. Comparison is boundary-aware:
    a sibling directory whose name merely starts with the root's prefix is not
    "inside" it.
    """
    try:
        candidate = os.path.realpath(path)
    except OSError:
        return False
    if candidate == root_real:
        return True
    return candidate.startswith(root_real + os.sep)


def _stat_signature(st: os.stat_result) -> tuple[int, int, int, int, int]:
    """Complete filesystem identity used to prove a run file is unchanged.

    Any append, rewrite, or same-path recreation moves at least one component
    (``ctime`` advances on every metadata/content change and cannot be forged
    back), so a mismatch between classification and reclaim means the file was
    not quiescent and must not be unlinked.
    """
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(st.st_ctime_ns),
    )


def _verify_terminal_header_at(path: Path, marker_pos: int, size: int) -> bool:
    """Prove a top-level terminal journal row owns the marker at ``marker_pos``.

    Hunts backwards for the row start, requires it to begin at a line boundary
    (journal rows are newline-delimited and compact JSON escapes control
    characters, so a genuine row start is always preceded by ``\\n`` or is the
    file start — a nested ``"terminal":true`` inside a payload never is), then
    schema-matches the row header and cross-checks its ids against the path.
    Returns False when it cannot *prove* terminality (fail closed: a missed
    reclaim is safe, a wrong one is not).
    """
    hunt_start = max(0, marker_pos - _RETENTION_ROW_START_HUNT_BYTES - 1)
    # Read past the marker so the anchored header pattern can consume the
    # separator/comma that follows it within `_RETENTION_HEADER_MAX_BYTES`.
    read_end = min(size, marker_pos + len(_RETENTION_MARKER_BYTES) + 8)
    try:
        with path.open("rb") as fh:
            fh.seek(hunt_start)
            buf = fh.read(read_end - hunt_start)
    except OSError:
        return False
    search_end = marker_pos - hunt_start
    for _attempt in range(8):  # bounded: real headers sit immediately before the marker
        start_idx = buf.rfind(_RETENTION_ROW_START_BYTES, 0, search_end)
        if start_idx == -1:
            return False
        at_line_start = (
            buf[start_idx - 1 : start_idx] == b"\n" if start_idx > 0 else hunt_start == 0
        )
        if at_line_start:
            header = buf[start_idx : start_idx + _RETENTION_HEADER_MAX_BYTES]
            match = _RETENTION_HEADER_RE.match(header)
            if match:
                event_id, seq_text, row_run_id, row_session_id, terminal_flag = match.group(
                    1, 2, 3, 4, 5
                )
                if terminal_flag == b"true":
                    stem = path.stem.encode()
                    parent = path.parent.name.encode()
                    if row_run_id == stem and row_session_id == parent:
                        try:
                            seq = int(seq_text)
                        except ValueError:
                            return False
                        if event_id == f"{path.stem}:{seq}".encode():
                            return True
                # A verified non-terminal / foreign header is not this file's
                # terminal row; keep walking earlier candidates.
        search_end = start_idx
    return False


def _journal_file_is_terminal(path: Path, size: int) -> bool:
    """Return True when the run file provably contains a top-level terminal row.

    Walks the file backwards in bounded chunks, stopping at the first VERIFIED
    terminal row — the common case (marker near EOF) reads one chunk. The
    backward walk matters: a multi-megabyte single row (giant ``apperror``
    payload) carries its marker inside its header, so the scan must cross the
    row to reach it. Files whose terminal row sits deeper than the scan budget
    return False and are left untouched.
    """
    if size <= 0:
        return False
    remaining = min(size, _RETENTION_VERIFY_MAX_BYTES)
    overlap = len(_RETENTION_MARKER_BYTES) - 1
    pos = size
    carry = b""
    carry_abs_start: int | None = None
    while remaining > 0 and pos > 0:
        span = min(_RETENTION_VERIFY_CHUNK_BYTES, remaining)
        span_start = max(0, pos - span)
        try:
            with path.open("rb") as fh:
                fh.seek(span_start)
                data = fh.read(pos - span_start)
        except OSError:
            return False
        buf = data + carry
        data_len = len(data)
        search_end = len(buf)
        while True:
            idx = buf.rfind(_RETENTION_MARKER_BYTES, 0, search_end)
            if idx == -1:
                break
            if idx < data_len:
                abs_pos = span_start + idx
            elif carry_abs_start is not None:
                abs_pos = carry_abs_start + (idx - data_len)
            else:
                abs_pos = -1
            if abs_pos >= 0 and _verify_terminal_header_at(path, abs_pos, size):
                return True
            search_end = idx
        remaining -= pos - span_start
        carry = data[:overlap]
        carry_abs_start = span_start
        pos = span_start
    return False


def _resolve_retention_cap(
    env_name: str,
    setting_name: str,
    settings: dict,
    *,
    kind: type,
    default,
    minimum,
    maximum,
):
    """Resolve one cap: env var > settings.json > default (mirrors ``_resolve_session_ttl``).

    Invalid or out-of-range values fall through to the next source; a value of
    0 is valid and disables that one cap.
    """
    candidates: list[Any] = [os.getenv(env_name), settings.get(setting_name)]
    for raw in candidates:
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw) if kind is float else int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if minimum <= value <= maximum:
            return value
    return default


def resolve_run_journal_retention_caps() -> dict:
    """Current retention caps as ``{"ttl_days", "max_runs_per_session", "max_bytes_per_session"}``."""
    settings: dict = {}
    try:
        from api.config import load_settings

        loaded = load_settings()
        if isinstance(loaded, dict):
            settings = loaded
    except Exception:
        # Retention must never depend on config import health; defaults stand.
        settings = {}
    return {
        "ttl_days": _resolve_retention_cap(
            _RETENTION_TTL_ENV,
            _RETENTION_TTL_SETTING,
            settings,
            kind=float,
            default=DEFAULT_RUN_JOURNAL_RETENTION_TTL_DAYS,
            minimum=0.0,
            maximum=_RETENTION_TTL_MAX_DAYS,
        ),
        "max_runs_per_session": _resolve_retention_cap(
            _RETENTION_MAX_RUNS_ENV,
            _RETENTION_MAX_RUNS_SETTING,
            settings,
            kind=int,
            default=DEFAULT_RUN_JOURNAL_RETENTION_MAX_RUNS_PER_SESSION,
            minimum=0,
            maximum=_RETENTION_MAX_RUNS_LIMIT,
        ),
        "max_bytes_per_session": _resolve_retention_cap(
            _RETENTION_MAX_BYTES_ENV,
            _RETENTION_MAX_BYTES_SETTING,
            settings,
            kind=int,
            default=DEFAULT_RUN_JOURNAL_RETENTION_MAX_BYTES_PER_SESSION,
            minimum=0,
            maximum=_RETENTION_MAX_BYTES_LIMIT,
        ),
    }


def _reclaim_run_file(
    path: Path,
    expected_signature: tuple[int, int, int, int, int],
    *,
    journal_root_real: str | None = None,
) -> int:
    """Unlink ``path`` if its stat identity still matches; return bytes freed (0 = skipped).

    The stat-identity re-check runs twice — immediately before the per-path
    writer lock, and again under it — so a trailing append (or same-path
    recreation) between classification and reclaim aborts the unlink instead of
    destroying rows written after the file was judged quiescent.

    ``journal_root_real`` (when given) is the resolved journal root and acts as
    a final containment gate: the unlink is refused unless the path still
    resolves inside it. This is deliberately redundant with the directory-walk
    filter — the unlink is the irreversible step, so it re-validates at the
    point of use rather than trusting an upstream check.
    """
    if journal_root_real is not None and not _resolve_within(journal_root_real, path):
        return 0
    try:
        st = path.stat()
    except OSError:
        return 0
    if _stat_signature(st) != expected_signature:
        return 0
    freed = 0
    with _lock_for(path):
        try:
            st = path.stat()
        except OSError:
            return 0
        if _stat_signature(st) != expected_signature:
            return 0
        try:
            path.unlink()
            freed = int(st.st_size)
        except FileNotFoundError:
            return 0
        except OSError:
            logger.debug("Run-journal retention could not unlink %s", path, exc_info=True)
            return 0
    # Confirmed removal: evict this run's cached state, mirroring
    # `delete_run_journal`. A later run re-created at the same path must restart
    # at seq 1 (not resume a stale cached seq) and must not reuse a lock the
    # removed file left behind.
    path_key = str(path)
    with _SEQ_CACHE_LOCK:
        _SEQ_CACHE.pop(path_key, None)
    _discard_cached_summary(path)
    if not path.exists():
        dir_key = str(path.parent)
        with _WRITER_LOCKS_GUARD:
            for key in [
                k for k in _WRITER_LOCKS if k[0] == dir_key and k[1] == path.name
            ]:
                del _WRITER_LOCKS[key]
    return freed


def _sweep_run_journal_session(
    session_journal_dir: Path,
    caps: dict,
    now: float,
    counters: dict,
    journal_root_real: str,
) -> None:
    """Classify and (if eligible) reclaim terminal runs inside one session's journal dir.

    ``journal_root_real`` is the resolved journal root; every candidate file must
    still resolve inside it, so a symlinked entry cannot redirect the sweep.
    """
    try:
        paths = sorted(session_journal_dir.glob("*.jsonl"))
    except OSError:
        counters["errors"] += 1
        return
    entries: list[tuple[Path, os.stat_result, bool]] = []
    for path in paths:
        try:
            if path.is_symlink():
                # A run file that is itself a link could point anywhere; the
                # journal only ever creates plain files.
                continue
            if not path.is_file():
                continue
            if not _resolve_within(journal_root_real, path):
                continue
            st = path.stat()
        except OSError:
            continue
        counters["files_scanned"] += 1
        terminal = _journal_file_is_terminal(path, int(st.st_size))
        if terminal:
            counters["terminal_files"] += 1
        entries.append((path, st, terminal))
    terminal_entries = [entry for entry in entries if entry[2]]
    # Newest first: the caps keep the most recent settled runs.
    terminal_entries.sort(
        key=lambda entry: (entry[1].st_mtime_ns, entry[0].name), reverse=True
    )
    retained_bytes = 0
    size_cap_exceeded = False
    for rank, (path, st, _terminal) in enumerate(terminal_entries):
        size = int(st.st_size)
        age_seconds = now - float(st.st_mtime)
        reclaimed = False
        # Settlement window: never reclaim a file a writer may still be
        # appending to (post-terminal `metering` / `stream_end` rows arrive
        # around the terminal row) or a client may still be reconnecting to.
        if age_seconds >= _RETENTION_MIN_QUIESCENT_SECONDS:
            ttl_days = float(caps["ttl_days"])
            max_runs = int(caps["max_runs_per_session"])
            max_bytes = int(caps["max_bytes_per_session"])
            over_ttl = ttl_days > 0 and age_seconds > ttl_days * 86400.0
            over_count = max_runs > 0 and rank >= max_runs
            # Size cap: retire runs from the newest-first prefix once the
            # retained budget would be exceeded, and keep retiring everything
            # older than the first overflow (sticky) so the retained set is a
            # contiguous newest-first prefix. The newest terminal run (rank 0)
            # is exempt so a session always keeps its most recent settled
            # anchor; the TTL still reaps it once it is old enough.
            over_size = False
            if max_bytes > 0 and rank > 0:
                if size_cap_exceeded or (retained_bytes + size) > max_bytes:
                    size_cap_exceeded = True
                    over_size = True
            if over_ttl or over_count or over_size:
                freed = _reclaim_run_file(
                    path,
                    _stat_signature(st),
                    journal_root_real=journal_root_real,
                )
                if freed > 0:
                    reclaimed = True
                    counters["removed_files"] += 1
                    counters["removed_bytes"] += freed
                    logger.debug(
                        "Run-journal retention reclaimed %s (%s bytes, age %.1fd)",
                        path,
                        freed,
                        age_seconds / 86400.0,
                    )
                else:
                    counters["skipped_files"] += 1
        if not reclaimed:
            # Only bytes that survive count against the per-session budget.
            retained_bytes += size


def sweep_run_journal(
    *,
    session_dir: Path | None = None,
    ttl_days: float | None = None,
    max_runs_per_session: int | None = None,
    max_bytes_per_session: int | None = None,
    now: float | None = None,
) -> dict:
    """Retire TERMINAL run journals past the age / count / size caps (#7613).

    A sweep, not a delete: non-terminal runs are the crashed-run payloads the
    journal exists to recover and are never touched; terminal runs are
    reclaimed only after a settlement window. Caps may be passed explicitly
    (0 disables one cap; tests rely on this) or left ``None`` to resolve
    env var > settings.json > default via ``resolve_run_journal_retention_caps``.

    Cheap by construction: per-file ``stat`` first, classification reads only
    the region needed to prove a terminal row, and reclaim re-checks the
    file's complete stat identity before unlinking. Returns a counters dict
    (``removed_files``, ``removed_bytes``, ``files_scanned``,
    ``terminal_files``, ``sessions_scanned``, ``skipped_files``, ``errors``,
    ``caps``).
    """
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    caps = resolve_run_journal_retention_caps()
    if ttl_days is not None:
        caps["ttl_days"] = max(0.0, float(ttl_days))
    if max_runs_per_session is not None:
        caps["max_runs_per_session"] = max(0, int(max_runs_per_session))
    if max_bytes_per_session is not None:
        caps["max_bytes_per_session"] = max(0, int(max_bytes_per_session))
    counters = {
        "removed_files": 0,
        "removed_bytes": 0,
        "files_scanned": 0,
        "terminal_files": 0,
        "sessions_scanned": 0,
        "skipped_files": 0,
        "errors": 0,
        "caps": dict(caps),
    }
    journal_root = root / RUN_JOURNAL_DIR_NAME
    try:
        if not journal_root.exists():
            return counters
        journal_root_real = os.path.realpath(journal_root)
        session_dirs = [
            entry
            for entry in sorted(journal_root.iterdir())
            if not entry.is_symlink()
            and entry.is_dir()
            and _resolve_within(journal_root_real, entry)
        ]
    except OSError:
        counters["errors"] += 1
        return counters
    sweep_now = time.time() if now is None else float(now)
    with _SWEEP_RUN_LOCK:
        for session_journal_dir in session_dirs:
            if not _SAFE_ID_RE.fullmatch(session_journal_dir.name):
                continue
            counters["sessions_scanned"] += 1
            try:
                _sweep_run_journal_session(
                    session_journal_dir, caps, sweep_now, counters, journal_root_real
                )
            except Exception:
                counters["errors"] += 1
                logger.warning(
                    "Run-journal retention sweep failed for %s",
                    session_journal_dir,
                    exc_info=True,
                )
    if counters["removed_files"]:
        logger.info(
            "Run-journal retention reclaimed %d file(s) / %d bytes across %d session(s)",
            counters["removed_files"],
            counters["removed_bytes"],
            counters["sessions_scanned"],
        )
    return counters


def run_journal_sweep_enabled() -> bool:
    """False when ``HERMES_WEBUI_RUN_JOURNAL_SWEEP`` is set to a disabled value."""
    raw = os.getenv(RUN_JOURNAL_SWEEP_ENV, "").strip().lower()
    return raw not in _SWEEP_DISABLED_VALUES


def maybe_sweep_run_journal(now: float | None = None) -> dict | None:
    """Run one retention sweep if due; return its counters, or None when not due.

    Called from the shared maintenance tick in ``api.background_process``
    alongside the SessionChannel reaper — the sweep is hourly and never runs on
    `server.py`'s boot path (first pass is delayed) nor on any request path
    (`GET /api/session` stays side-effect-free). Gating state lives here so any
    caller — the tick, a test, a manual invocation — shares it. Returns None
    when disabled, not yet due, or already being swept.
    """
    global _SWEEP_LAST_STARTED
    if not run_journal_sweep_enabled():
        return None
    tick_now = time.time() if now is None else float(now)
    with _SWEEP_THREAD_LOCK:
        if _SWEEP_LAST_STARTED is None:
            # Fresh process: arm so the first pass is due after
            # RETENTION_FIRST_SWEEP_DELAY_SECS (never during boot itself),
            # then one sweep per RETENTION_SWEEP_INTERVAL_SECS after that.
            _SWEEP_LAST_STARTED = tick_now - (
                RETENTION_SWEEP_INTERVAL_SECS - RETENTION_FIRST_SWEEP_DELAY_SECS
            )
            return None
        if tick_now - _SWEEP_LAST_STARTED < RETENTION_SWEEP_INTERVAL_SECS:
            return None
        _SWEEP_LAST_STARTED = tick_now
    try:
        return sweep_run_journal()
    except Exception:
        logger.warning("Run-journal retention sweep failed", exc_info=True)
        return None


def _reset_run_journal_sweep_schedule() -> None:
    """Forget the sweep schedule so the next due-check re-arms the boot delay.

    Used by tests to prove the first-pass delay independently of process age.
    """
    global _SWEEP_LAST_STARTED
    with _SWEEP_THREAD_LOCK:
        _SWEEP_LAST_STARTED = None


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
