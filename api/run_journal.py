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
from copy import deepcopy
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

try:  # Native Windows uses its byte-range lock instead.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None


RUN_JOURNAL_DIR_NAME = "_run_journal"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WRITER_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()
# Next sequence per path; a complete append publishes it under the per-path
# lock. Cold writers validate and repair an uncommitted EOF tail before reuse;
# hot writers avoid rereading the growing file. The shared dict mutex also
# protects structural access against cross-path deletion/eviction.
_SEQ_CACHE: dict[str, int] = {}
_SEQ_CACHE_SIGNATURES: dict[str, tuple[int, int, int, int, int]] = {}
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
# Events that are live-UI-only telemetry with no recovery value in the run
# journal. They are skipped at WRITE time (never durably journaled, so they
# cannot bloat the journal on marathon runs) and filtered at REPLAY time (so
# legacy journals that already contain a backlog never stream it to a
# reconnecting browser tab). Readers deliberately do NOT filter: cursor math
# (``cursor_event_missing`` bound) and the offline-gap coverage check count
# journal seqs and must keep seeing every row.
REPLAY_SKIPPED_SSE_EVENTS = frozenset({"metering"})
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


@contextmanager
def _journal_process_lock(fd: int, path: Path, *, shared: bool = False):
    """Settle cooperating processes before seed/read/write/rollback.

    POSIX locks the held journal inode. Native Windows uses a companion byte
    lock; keep that file in place while this journal exists so waiters share it.
    Unsupported lock backends fail closed before journal mutation.
    """
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_SH if shared else _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        lock_fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'r+b', buffering=0) as lock_file:
            if os.fstat(lock_fd).st_size == 0:
                lock_file.write(b'\0')
            lock_file.seek(0)
            _msvcrt.locking(lock_fd, _msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(lock_fd, _msvcrt.LK_UNLCK, 1)
        return
    raise OSError('cross-process run journal locking is unavailable')


def _held_journal_signature(fd: int) -> tuple[int, int, int, int, int]:
    stat = os.fstat(fd)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


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
        return deepcopy(summary)


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
        _SUMMARY_CACHE[key] = (signature, deepcopy(summary))
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
        key = str(path)
        with _SEQ_CACHE_LOCK:
            cached_next = _SEQ_CACHE.get(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        created_file = not path.exists()
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600)
        try:
            with _journal_process_lock(fd, path):
                # Inspect the same inode that will receive the write. Only a cold
                # cache seeds/repairs from disk; ordinary appends stay O(1).
                signature = _held_journal_signature(fd)
                size = signature[2]
                with _SEQ_CACHE_LOCK:
                    if _SEQ_CACHE_SIGNATURES.get(key) != signature:
                        cached_next = None
                if cached_next is None:
                    with os.fdopen(os.dup(fd), "rb") as fh:
                        fh.seek(0)
                        next_seq, repair_size, add_newline = _prepare_journal_append(
                            fh, str(session_id), str(run_id), size,
                        )
                else:
                    next_seq, repair_size, add_newline = cached_next, size, False
                assigned_seq = int(seq) if seq is not None else next_seq
                terminal_state = _terminal_state_for_event(event_name, payload)
                event = {
                    "version": 2,
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
                # Encode before mutating the file or publishing a sequence. JSON's
                # ASCII escapes preserve lone provider surrogates without losing a
                # row or replacing content; normal Unicode stays readable on disk.
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                try:
                    encoded = line.encode("utf-8")
                except UnicodeEncodeError:
                    encoded = (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
                if add_newline:
                    encoded = b"\n" + encoded
                try:
                    if repair_size != size:
                        os.ftruncate(fd, repair_size)
                    remaining = memoryview(encoded)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("run journal write made no progress")
                        remaining = remaining[written:]
                    if _should_fsync_event(terminal_state):
                        os.fsync(fd)
                except BaseException:
                    # No buffered close can flush a partial row after this rollback.
                    # Even if rollback itself fails, evict the cache so a later
                    # append must inspect/repair the actual file before proceeding.
                    with _SEQ_CACHE_LOCK:
                        _SEQ_CACHE.pop(key, None)
                        _SEQ_CACHE_SIGNATURES.pop(key, None)
                    _discard_cached_summary(path)
                    os.ftruncate(fd, repair_size)
                    raise
                with _SEQ_CACHE_LOCK:
                    _SEQ_CACHE[key] = max(next_seq, assigned_seq + 1)
                    _SEQ_CACHE_SIGNATURES[key] = _held_journal_signature(fd)
                _discard_cached_summary(path)
        finally:
            os.close(fd)
        if created_file:
            _fsync_parent_dir(path)
        return event


class RunJournalWriter:
    """Stateful writer for one WebUI stream/run."""

    def __init__(self, session_id: str, run_id: str, *, session_dir: Path | None = None):
        self.session_id = _validate_id(session_id, "session_id")
        self.run_id = _validate_id(run_id, "run_id")
        self.session_dir = Path(session_dir) if session_dir is not None else None

    def append_sse_event(self, event_name: str, payload=None) -> dict | None:
        # Live-UI-only telemetry (metering) has no recovery value in the journal:
        # nothing reads those rows back for recovery, and journaling them at ~10 Hz
        # on marathon runs balloons the durable file (12+ MB of a single 18 MB run
        # was metering). Skip the write entirely and return None so callers'
        # journal-id plumbing (``(journaled or {}).get("event_id")``) is untouched.
        # Not reserving a seq keeps the remaining journaled seqs contiguous, which
        # the offline-gap coverage and replay-cursor contiguity checks rely on.
        if str(event_name or "").strip() in REPLAY_SKIPPED_SSE_EVENTS:
            return None
        # Allocate the sequence inside the same per-path transaction that writes
        # the row. Reserving here, then releasing the lock before append, lets a
        # concurrent writer put a higher sequence on disk first.
        return append_run_event(
            self.session_id,
            self.run_id,
            event_name,
            payload or {},
            session_dir=self.session_dir,
        )


def journal_replay_visible(event) -> bool:
    """Return True when a journal row should be streamed to a reconnecting tab.

    Live-UI-only telemetry rows (see ``REPLAY_SKIPPED_SSE_EVENTS``) carry no
    recovery value — replaying a metering backlog only re-paints a stale TPS
    number while multiplying the reconnect burst size. Writers no longer journal
    them, but legacy journals may already contain them, so the replay emit sites
    filter through this predicate. Non-dict rows are passed through (visible) so
    an unexpected shape can never silently swallow user-visible output.
    """
    if not isinstance(event, dict):
        return True
    name = str(event.get("event") or event.get("type") or "")
    return name not in REPLAY_SKIPPED_SSE_EVENTS


def _scan_validated_journal(
    lines, session_id: str, run_id: str, *, writer_seed: bool = False,
) -> tuple[list[dict], list[dict], int]:
    """Validate rows; return the last complete prefix byte offset for tail repair."""
    events: list[dict] = []
    line_no = 0
    prefix_bytes = 0
    last_seq = 0
    gapless_started = False
    try:
        for current_line, raw in enumerate(lines, start=1):
            line_no = current_line
            if not raw.strip():
                prefix_bytes += len(raw)
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                torn_utf8 = (
                    isinstance(exc, UnicodeDecodeError)
                    and exc.reason == "unexpected end of data"
                    and exc.end == len(raw)
                )
                if not raw.endswith(b"\n") and (isinstance(exc, json.JSONDecodeError) or torn_utf8):
                    return events, [{"line": line_no, "reason": "recovery_torn_tail"}], prefix_bytes
                raise ValueError("recovery_malformed_row") from exc
            seq = event.get("seq") if isinstance(event, dict) else None
            # Version1 (or absent version) was written by the legacy producer,
            # which consumed sequences on failed writes. Preserve increasing
            # legacy cursor IDs; version2 declares gapless publication. Once
            # upgraded, a run cannot downgrade its validation contract.
            version = event.get('version', 1) if isinstance(event, dict) else None
            if type(version) is not int or version not in (1, 2) or (gapless_started and version != 2):
                raise ValueError('recovery_protocol_version')
            gapless_started = gapless_started or version == 2
            sequence_valid = (type(seq) is int and seq > last_seq and (
                writer_seed or version == 1 or seq == last_seq + 1
            ))
            if not isinstance(event, dict) or (
                not sequence_valid
                or event.get("event_id") != f"{run_id}:{seq}"
                or event.get("run_id") != run_id
                or event.get("session_id") != session_id
            ):
                raise ValueError("recovery_identity_or_sequence")
            name = event.get("event")
            if not isinstance(name, str) or not name or event.get("type", name) != name:
                raise ValueError("recovery_event_type")
            terminal_state = _terminal_state_for_event(name, event.get("payload"))
            if (event.get("terminal") is not bool(terminal_state)
                    or event.get("terminal_state") != terminal_state):
                raise ValueError("recovery_terminal_identity")
            events.append(event)
            last_seq = seq
            prefix_bytes += len(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        return [], [{"line": line_no, "reason": str(exc)}], 0
    return events, [], prefix_bytes


def _prepare_journal_append(lines, session_id: str, run_id: str, size: int) -> tuple[int, int, bool]:
    """Plan first-process tail repair and reseeding before any bytes are changed."""
    events, malformed, prefix_bytes = _scan_validated_journal(
        lines, session_id, run_id, writer_seed=True,
    )
    if malformed and malformed[0]["reason"] != "recovery_torn_tail":
        raise ValueError(malformed[0]["reason"])
    next_seq = events[-1]["seq"] + 1 if events else 1
    if malformed:
        return next_seq, prefix_bytes, False
    if size:
        lines.seek(-1, os.SEEK_END)
        return next_seq, size, lines.read(1) != b"\n"
    return next_seq, size, False


def _read_validated_recovery_events(
    path: Path, session_id: str, run_id: str,
) -> tuple[list[dict], list[dict]]:
    """Validate a complete durable run, tolerating an uncommitted torn EOF tail."""
    try:
        # Client replay caps are not durable-recovery limits. Read incrementally
        # without splitting lines or dropping a valid run's terminal snapshot.
        # Recovery must not observe a complete terminal row between write and
        # a failed fsync/rollback in this process. Writer seeding calls the
        # scanner directly while holding this same lock, avoiding reentrancy.
        # Open first so an absent journal cannot retain a reader-only registry
        # lock that deletion (no directory to remove) cannot evict. Do not read
        # any bytes until the existing-file append transaction has settled.
        with path.open("rb") as lines, _lock_for(path):
            with _journal_process_lock(lines.fileno(), path, shared=True):
                events, malformed, _prefix_bytes = _scan_validated_journal(lines, session_id, run_id)
                return events, malformed
    except FileNotFoundError:
        return [], []


def read_run_events(
    session_id: str,
    run_id: str,
    *,
    after_seq: int | None = None,
    max_seq: int | None = None,
    session_dir: Path | None = None,
    validated_recovery: bool = False,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    if validated_recovery:
        events, malformed = _read_validated_recovery_events(path, str(session_id), str(run_id))
    else:
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


def runtime_model_from_events(session_id: str, stream_id: str, events: Iterable[dict]) -> dict | None:
    """Project only observed serving identity for this journal owner.

    A fallback warning or malformed later observation invalidates old evidence;
    the configured selection and free-text status never supply serving identity.
    """
    observed = None
    for event in events:
        if not isinstance(event, dict) or event.get("session_id") != session_id or event.get("run_id") != stream_id:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            if event.get("event") == "runtime_model":
                observed = None
            continue
        if event.get("event") == "warning" and payload.get("type") == "fallback":
            observed = None
        elif event.get("event") == "runtime_model":
            observed = None
            if (payload.get("session_id") != session_id or payload.get("stream_id") != stream_id
                or not isinstance(payload.get("model"), str) or not payload["model"].strip()
                or not isinstance(payload.get("fallback_active"), bool)
                or payload.get("phase") not in ("observed_output", "route_observed")):
                continue
            observed = {key: payload[key] for key in (
                "session_id", "stream_id", "model", "fallback_active", "phase")}
            if isinstance(payload.get("provider"), str) and payload["provider"].strip():
                observed["provider"] = payload["provider"]
    return observed


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
        "runtime_model": runtime_model_from_events(session_id, run_id, ordered),
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


def find_run_file(run_id: str, *, session_dir: Path | None = None) -> tuple[str, Path] | None:
    """Locate a run journal file by run id WITHOUT parsing its body.

    Hot callers that immediately read the full journal (the live-snapshot
    rebuild) must not pay :func:`find_run_summary`'s full-file parse first:
    on a long live run the file holds tens of thousands of rows and parsing
    it twice per rebuild dominated the snapshot cost. Returns
    ``(session_id, path)`` for the first match, or ``None`` when the run id
    is invalid or no journal exists.
    """
    try:
        rid = _validate_id(run_id, "run_id")
    except ValueError:
        return None
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        return path.parent.name, path
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
        # Hold ``_SEQ_CACHE_LOCK`` — the SAME mutex append publication takes —
        # so a concurrent append on another path
        # cannot mutate the dict mid-iteration (``dictionary changed size``).
        with _SEQ_CACHE_LOCK:
            for cache in (_SEQ_CACHE, _SEQ_CACHE_SIGNATURES):
                for cache_key in [entry for entry in cache if str(Path(entry).parent) == dir_key]:
                    del cache[cache_key]
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
