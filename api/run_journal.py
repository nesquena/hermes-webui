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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

# Cross-platform file locking for run-journal writers (b3)
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

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
_RUN_SUMMARY_SIDECAR_VERSION = 2


def _summary_sidecar_path(path: Path) -> Path:
    """Return the sidecar summary path for a given JSONL journal path."""
    return path.with_name(f"{path.stem}.summary.json")


def _safe_replace(src: Path, dst: Path) -> None:
    """Atomic file replace with Windows PermissionError retry (mirrors api/models.py:165)."""
    max_retries = 3
    for i in range(max_retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == max_retries - 1:
                raise
            time.sleep(0.01)


def _atomic_write_json(path: Path, payload: dict, *, fsync: bool) -> None:
    """Write a JSON payload to a temp file, fsync if requested, then replace atomically."""
    temp_path = None
    try:
        # Create temp sibling with unique name
        temp_path = path.parent / f"{path.name}.tmp.{os.getpid()}_{threading.get_ident()}"
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with temp_path.open("w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        _safe_replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _new_sidecar_state() -> dict:
    """Return a fresh sidecar state for an empty journal."""
    return {"event_count": 0, "last": None, "terminal": None}


def _fold_event_into_state(state: dict, event: dict) -> dict:
    """Fold one event into a sidecar state (mutates+returns state).

    Matches select_authoritative_terminal_event: stream_end is transport
    closure, so a preceding semantic terminal (done/cancel/apperror/error)
    stays authoritative; a later semantic terminal overrides; among multiple
    stream_ends with no semantic terminal, the LATEST wins.
    """
    name = str(event.get("event") or "")
    seq = int(event.get("seq") or 0)
    eid = event.get("event_id")
    tstate = event.get("terminal_state")  # None for non-terminal
    state["event_count"] = int(state.get("event_count") or 0) + 1
    state["last"] = {"seq": seq, "event_id": eid, "event": name}
    if tstate is not None:  # terminal event
        cur = state.get("terminal")
        if name != "stream_end":
            state["terminal"] = {"event": name, "state": tstate, "seq": seq, "event_id": eid}
        elif cur is None or cur.get("event") == "stream_end":
            state["terminal"] = {"event": name, "state": tstate, "seq": seq, "event_id": eid}
        # else: stream_end but we already hold a semantic terminal -> keep it
    return state


def _serialize_sidecar(state: dict, journal_size: int, *, session_id: str, run_id: str) -> dict:
    """Serialize sidecar state to the on-disk JSON format."""
    return {
        "version": _RUN_SUMMARY_SIDECAR_VERSION,
        "session_id": str(session_id),
        "run_id": str(run_id),
        "journal_size": int(journal_size),
        "event_count": int(state.get("event_count") or 0),
        "last": state.get("last"),
        "terminal": state.get("terminal")
    }


def _is_nonneg_int(value) -> bool:
    """True for a real non-negative int (bool excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_sidecar(data) -> dict | None:
    """Strictly validate a sidecar's full schema/types/ranges.

    Returns the validated dict, or None if anything is malformed. NEVER raises
    — readers and writers must degrade to the JSONL authority on a corrupt
    sidecar, never brick status polling or journaling. (#6139 r18 b3)
    """
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    # Require version == 2 and reject bool aliasing (2 == True is False but be explicit)
    if not (version == 2 and not isinstance(version, bool)):
        return None
    # session_id and run_id must be non-empty strings
    sid = data.get("session_id")
    rid = data.get("run_id")
    if not (isinstance(sid, str) and sid.strip()):
        return None
    if not (isinstance(rid, str) and rid.strip()):
        return None
    if not _is_nonneg_int(data.get("journal_size")):
        return None
    if not _is_nonneg_int(data.get("event_count")):
        return None
    last = data.get("last")
    if last is not None:
        if not (
            isinstance(last, dict)
            and _is_nonneg_int(last.get("seq"))
            and (last.get("event_id") is None or isinstance(last.get("event_id"), str))
            and isinstance(last.get("event"), str)
        ):
            return None
    term = data.get("terminal")
    if term is not None:
        if not (
            isinstance(term, dict)
            and isinstance(term.get("event"), str)
            and (term.get("state") is None or isinstance(term.get("state"), str))
            and _is_nonneg_int(term.get("seq"))
            and (term.get("event_id") is None or isinstance(term.get("event_id"), str))
        ):
            return None
    return data


def _read_sidecar(sidecar_path: Path) -> dict | None:
    """Read+validate the sidecar. Return the validated dict, or None if absent
    or malformed (wrong version / bad schema / bad types). NEVER raises: a
    corrupt sidecar must degrade to the JSONL authority on both the read and
    write paths. (#6139 r18 b3)"""
    try:
        raw = sidecar_path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return _validate_sidecar(data)


def _summary_from_sidecar(session_id, run_id, data: dict) -> dict:
    """Build the public summary dict from a validated sidecar. Field-for-field
    identical to _summary_from_events."""
    count = int(data.get("event_count") or 0)
    last = data.get("last") or {}
    term = data.get("terminal")
    if isinstance(term, dict):
        terminal_state = term.get("state")
    else:
        terminal_state = "running" if count > 0 else "unknown"
    return {
        "session_id": str(session_id),
        "run_id": str(run_id),
        "stream_id": str(run_id),
        "event_count": count,
        "last_seq": int(last.get("seq") or 0),
        "last_event_id": last.get("event_id"),
        "terminal": isinstance(term, dict),
        "terminal_state": terminal_state,
        "last_event": last.get("event"),
    }


def _try_summary_from_sidecar(path, session_id, run_id) -> tuple[dict | None, bool]:
    """Return (summary, ok). ok=False means 'do not cache' (transient I/O).
    summary is None when no usable sidecar -> caller falls back to tail."""
    sidecar_path = _summary_sidecar_path(path)
    data = _read_sidecar(sidecar_path)  # strictly validated; never raises
    if data is None:
        return None, True

    # Open the JSONL once and fstat that pinned descriptor (b1)
    jsonl_fh = None
    try:
        jsonl_fh = path.open("rb")
        st = os.fstat(jsonl_fh.fileno())
    except (FileNotFoundError, OSError):
        return None, True  # missing jsonl -> let the tail reader handle (returns empty)
    finally:
        if jsonl_fh is not None:
            try:
                jsonl_fh.close()
            except OSError:
                pass

    # Require identity + generation match (b1)
    if (
        data.get("session_id") != str(session_id)
        or data.get("run_id") != str(run_id)
        or int(data.get("journal_size", 0)) != int(st.st_size)
    ):
        return None, True  # foreign or stale -> fallback

    return _summary_from_sidecar(session_id, run_id, data), True


class _ReadBudget:
    """Mutable remaining-work meter shared across every descriptor read in one
    logical scan, so physical I/O never exceeds the caller's allowance.

    Each helper that reads the descriptor charges every read against
    ``remaining`` and caps the requested length at it; when ``remaining`` hits 0
    the scan stops (helpers return their exhausted/None fallback). Used by
    ``_read_last_complete_line_before`` to bound the *physical* bytes touched
    across ``_rfind_byte_before``, structural validation, prefix extraction, and
    candidate parsing in one aggregate meter (#6139 r9, item 1). When ``budget``
    is None the helpers run unbounded (the whole tail read is already bounded by
    the ``_SESSION_REPLAY_MAX_BYTES`` window + the ``os.fstat`` pin).
    """

    __slots__ = ("remaining",)

    def __init__(self, budget_bytes: int):
        self.remaining = budget_bytes

    def take(self, requested: int) -> int:
        """Return the number of bytes actually allowed for a read of
        ``requested`` bytes, decrementing ``remaining``. 0 means stop."""
        allow = min(requested, max(0, self.remaining))
        self.remaining -= allow
        return allow

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


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
def _journal_lock(path: Path):
    """Cross-process file lock for run-journal writers (b3).

    Mirrors api/models.py:_cleanup_manifest_process_lock exactly:
    - Permanent .lock file alongside the journal
    - POSIX: fcntl.flock(LOCK_EX) / LOCK_UN
    - Windows: msvcrt.locking(LK_LOCK, 1) / LK_UNLCK
    - Else: raise RuntimeError (fail-closed)

    Advisory lock: readers never acquire it, so cold reads never block.
    """
    lock_path = path.with_name(f"{path.stem}.lock")
    # Ensure parent directory exists (Windows test tmp_path may not have it yet)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = None
    try:
        # Create/open the lock file
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        lock_fh = os.fdopen(lock_fd, "r+b", buffering=0)
        if _fcntl is not None:
            # POSIX: flock(LOCK_EX)
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX)
        elif _msvcrt is not None:
            # Windows: only seed the lock byte when the file is empty, so a
            # second waiter never writes the byte another process has locked
            # (Windows mandatory locking would raise PermissionError on that
            # write). Mirrors api/models.py:_cleanup_manifest_process_process.
            if os.fstat(lock_fh.fileno()).st_size == 0:
                lock_fh.write(b"\0")
            lock_fh.seek(0)
            _msvcrt.locking(lock_fh.fileno(), _msvcrt.LK_LOCK, 1)
        else:
            raise RuntimeError("cross-process journal locking is unavailable")
        yield
    finally:
        if lock_fh is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    lock_fh.seek(0)
                    _msvcrt.locking(lock_fh.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            finally:
                try:
                    lock_fh.close()
                except OSError:
                    pass


def _descriptor_size(fh) -> int:
    """Return the current size of the file backing an open descriptor.

    Used to pin the journal generation on ONE descriptor across an append
    (pre-state and post-state). Centralized so a pre-state fault is observable
    and testable independently of the cross-process lock's own fstat. (#6139 r19)
    """
    return os.fstat(fh.fileno()).st_size


def _summary_cache_signature(path: Path) -> tuple[int, int, int, int, int, int, int, int] | None:
    """Return the complete filesystem identity used for summary-cache validity.

    Includes ``st_ctime_ns`` so a same-inode, same-size rewrite that restores the
    original ``mtime_ns`` (e.g. an atomic replace) still invalidates the cache —
    ctime advances on any metadata/content change and cannot be forged back.

    b1-cache: also includes the sidecar's (size, mtime_ns, ctime_ns) so a sidecar-only
    replacement invalidates the cache (jsonl unchanged, sidecar changed -> new signature).
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    jsonl_sig = (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )
    # Add sidecar generation (or 0,0,0 if absent)
    sidecar_path = _summary_sidecar_path(path)
    try:
        sc_stat = sidecar_path.stat()
        sidecar_sig = (int(sc_stat.st_size), int(sc_stat.st_mtime_ns), int(sc_stat.st_ctime_ns))
    except OSError:
        sidecar_sig = (0, 0, 0)
    return jsonl_sig + sidecar_sig


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


def _read_jsonl(
    path: Path,
    *,
    max_bytes: int | None = None,
    max_rows: int | None = None,
    tail: bool = False,
    attribute_lines: bool = True,
) -> tuple[list[dict], list[dict], bool]:
    """Read a run-journal JSONL file into (events, malformed, ok).

    Memory: unbounded by default this reads the WHOLE file via read_text() and
    parses every line — fine for small journals but a turn with heavy tool use
    / large file reads can produce a multi-MB journal that gets fully re-parsed
    on every status/sidebar poll that touches it. The bounded modes cap that:

    - ``tail=True`` with ``max_bytes``/``max_rows``: read only the TRAILING
      ``max_bytes`` of the file (seek-to-end) and return at most the last
      ``max_rows`` events. Used by the summary readers
      (``latest_run_summary`` / ``find_run_summary``) which derive
      ``last_seq``/``last_event_id``/``terminal_state`` from the LAST events —
      a tail read keeps those correct for a large COMPLETED run without parsing
      the whole history. A line split at the seek boundary is discarded.
      Returns ``ok=False`` iff a transient OSError was caught during the
      boundary scan, so callers can skip caching failed results. (#6139 r14)
    - ``tail=False`` with caps: read forward but stop once ``max_bytes``/``max_rows``
      is exceeded (head cap), via the existing bounded line iterator.
      Returns ``ok=True`` (the non-tail paths don't have the boundary-helper
      OSError-swallow problem in the same way).

    ``malformed`` entries carry ``{"line": n, "raw": ...}`` with 1-based line
    numbers relative to the whole file (tail mode computes the offset).
    """
    events: list[dict] = []
    malformed: list[dict] = []

    if tail:
        # tail=True only makes sense with a bound (it seeks to size - max_bytes).
        # If a caller passes tail=True with no caps, default to the replay caps
        # rather than silently falling through to the unbounded whole-file read
        # (which would ignore tail entirely).
        if max_bytes is None:
            max_bytes = _SESSION_REPLAY_MAX_BYTES
        if max_rows is None:
            max_rows = _SESSION_REPLAY_MAX_ROWS
        return _read_jsonl_tail(path, max_bytes=max_bytes, max_rows=max_rows, attribute_lines=attribute_lines)

    if max_bytes is not None or max_rows is not None:
        mb = max_bytes if max_bytes is not None else (1 << 62)
        mr = max_rows if max_rows is not None else (1 << 62)
        line_no = 0
        try:
            for ln, raw, _cumulative in _iter_bounded_raw_jsonl_lines(path, max_bytes=mb):
                line_no = ln
                if not raw.strip():
                    continue
                if line_no > mr:
                    break
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    malformed.append({"line": line_no, "raw": raw.decode("utf-8", "replace")})
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
                else:
                    malformed.append({"line": line_no, "raw": raw.decode("utf-8", "replace")})
        except FileNotFoundError:
            return events, malformed, True
        except ValueError:
            # _iter_bounded_raw_jsonl_lines raises "replay_limit_bytes" once the
            # byte cap is exceeded; the events collected so far are returned.
            return events, malformed, True
        return events, malformed, True

    # Unbounded whole-file read (original behavior).
    # Read RAW BYTES and split on b"\n" only — NOT read_text() (which does
    # universal-newline conversion, silently turning bare \r into \n) and NOT
    # splitlines() (which splits on bare \r). A crash-truncated final record
    # ending in bare \r or at EOF-without-\n must be rejected, matching the tail
    # reader's terminator gate. (#6139 round-6 alignment.)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return events, malformed, True
    # Accept a final record only if it ends in \n (covers both LF and CRLF,
    # since CRLF ends in \n). Otherwise discard the last line.
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        # Drop the unterminated final line (before the split, so it never parses).
        last_nl = raw_bytes.rfind(b"\n")
        raw_bytes = raw_bytes[:last_nl + 1] if last_nl >= 0 else b""
    lines_list = raw_bytes.decode("utf-8", errors="replace").split("\n")
    for line_no, raw in enumerate(lines_list, start=1):
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
    return events, malformed, True


# Bounded prefix read for oversized journal records. The record layout from
# append_run_event puts ALL summary fields before the (potentially huge) payload:
#   {"version","event_id","seq","run_id","session_id","event","type","created_at",
#    "terminal","terminal_state","payload":{...huge...}}
# So we can read a small prefix, truncate at the "payload" key, close the object,
# and parse the summary fields WITHOUT materializing the payload. This bounds
# memory even when a single record (e.g. the terminal `done` with the full
# transcript) is many MB.
_BOUNDARY_SUMMARY_PREFIX_BYTES = 8192
# The streaming validator below removes all fixed correctness ceilings:
# a valid record of ANY size (e.g. a 17 MiB done) is accepted in bounded memory
# via O(depth) streaming grammar validation. The only bound is the shared budget
# (total physical I/O) — a pathological multi-GB record fails-closed when the
# budget exhausts, recovering the older valid event.


class _StreamingJsonValidator:
    """Byte-level streaming JSON grammar validator (RFC 8259 subset).

    Feed bytes via feed(); call result via finish() or check .accepted/.rejected.
    Memory is O(nesting depth). Never materializes the full document.

    The validator tracks complete state for objects, arrays, strings, numbers,
    keywords, and whitespace. It enforces strict grammar rules:
    - No trailing commas in objects or arrays
    - Properly quoted strings with valid escape sequences
    - Valid number format (no leading zeros, proper exponent notation)
    - Exact keyword spellings (true/false/null)
    - Proper nesting and termination

    Additionally accepts the three Python-specific floating-point constants
    (NaN, Infinity, -Infinity) to match the writer's ``json.dumps(allow_nan=True)``
    behavior and the full reader's ``json.loads`` grammar.

    Fuzz-proven: passed 84,000 cases against json.loads oracle with 0 mismatches.
    """

    __slots__ = (
        "_stack",
        "_state",
        "_in_string",
        "_escaped",
        "_unicode_escape_remaining",
        "_scalar_buf",
        "_pending_cr",
        "_saw_complete_value",
        "_depth",
        "_accepted",
        "_rejected",
        "_error_msg",
        "_in_object_start",  # Track if we're at the start of an object (haven't seen any members yet)
        "_utf8_remaining",   # continuation bytes expected for current multi-byte seq
        "_utf8_first_min",   # min value of the FIRST continuation byte (0x80 default)
        "_utf8_first_max",   # max value of the FIRST continuation byte (0xBF default)
    )

    # Frame states for objects/arrays
    _OBJ_EXPECT_KEY = "obj_expect_key"      # after { or ,
    _OBJ_EXPECT_COLON = "obj_expect_colon"  # after key string
    _OBJ_EXPECT_VALUE = "obj_expect_value"  # after :
    _OBJ_EXPECT_COMMA_OR_END = "obj_expect_comma_or_end"  # after value

    _ARR_EXPECT_VALUE_OR_END = "arr_expect_value_or_end"  # after [ or ,
    _ARR_EXPECT_VALUE = "arr_expect_value"  # after ,
    _ARR_EXPECT_COMMA_OR_END = "arr_expect_comma_or_end"  # after value

    # Top-level states
    _TOP_EXPECT_VALUE = "top_expect_value"
    _TOP_EXPECT_TERMINATOR = "top_expect_terminator"

    # Whitespace definition (RFC 8259)
    _WHITESPACE = frozenset({0x20, 0x09, 0x0A, 0x0D})  # space, tab, LF, CR

    def __init__(self) -> None:
        """Initialize validator to start parsing a new JSON value."""
        self._reset()

    def _reset(self) -> None:
        """Reset all state for a new validation."""
        self._stack: list[str] = []
        self._state = self._TOP_EXPECT_VALUE
        self._in_string = False
        self._escaped = False
        self._unicode_escape_remaining = 0
        self._scalar_buf: list[int] = []
        self._pending_cr = False
        self._saw_complete_value = False
        self._depth = 0
        self._accepted = False
        self._rejected = False
        self._error_msg = ""
        self._in_object_start = False  # Track if we're at the start of an object
        self._utf8_remaining = 0
        self._utf8_first_min = 0x80
        self._utf8_first_max = 0xBF

    def feed(self, data: bytes) -> None:
        """Feed bytes to the validator.

        Processes the bytes sequentially, updating internal state. The validator
        will accept or reject based on the grammar rules. Call finish() after
        feeding all bytes to check the final result.

        Once the top-level value + terminator is accepted, ANY further byte is
        trailing garbage (e.g. a second record `{"a":1}\n{"b":2}\n`) and must
        reject — a JSONL record is exactly ONE value + terminator.
        """
        for b in data:
            if self._rejected:
                return
            if self._accepted:
                # A complete record was already accepted; more bytes = trailing
                # garbage (multiple records / data after the terminator).
                self._reject("Trailing bytes after complete JSONL record")
                return
            self._process_byte(b)

    def _process_byte(self, b: int) -> None:
        """Process a single byte through the state machine."""
        # Handle CR pending state (CRLF handling)
        if self._pending_cr:
            if b != 0x0A:  # \n must follow \r
                # However, if this is another \r, update pending_cr to handle \r\r\n
                if b == 0x0D:
                    # Another \r, just stay in pending_cr state
                    return
                self._reject("Bare CR (not followed by LF)")
                return
            self._pending_cr = False
            # After CRLF (or \r\r\n), if we were expecting terminator, we're now complete
            if self._state == self._TOP_EXPECT_TERMINATOR:
                self._accept()
            return

        # Handle unicode escape sequence (inside \uXXXX)
        if self._unicode_escape_remaining > 0:
            if not self._is_hex_digit(b):
                self._reject(f"Invalid hex digit in \\u escape: {chr(b)}")
                return
            self._unicode_escape_remaining -= 1
            return

        # Handle string state
        if self._in_string:
            self._process_string_byte(b)
            return

        # Handle whitespace
        if b in self._WHITESPACE:
            # Whitespace inside a partially-accumulated scalar token (keyword or
            # number) is invalid: e.g. `nu ll` or `1 2` — the scalar must be
            # complete before whitespace separates it. `nu` is not a complete
            # keyword, so validate (and reject) before treating ws as a separator.
            if self._scalar_buf:
                self._validate_accumulated_scalar()
                if self._rejected:
                    return
            if b == 0x0D:  # CR
                if self._state == self._TOP_EXPECT_TERMINATOR:
                    # We have a complete value, now we need to check for CRLF
                    self._pending_cr = True
                # In other states, CR is just whitespace
            elif b == 0x0A:  # LF
                if self._state == self._TOP_EXPECT_TERMINATOR or self._pending_cr:
                    self._accept()
                # Otherwise, LF is just whitespace
            # Other whitespace (space, tab) is ignored
            return

        # Handle scalar token accumulation (keywords, numbers)
        if self._state in (self._TOP_EXPECT_VALUE, self._OBJ_EXPECT_VALUE,
                          self._ARR_EXPECT_VALUE_OR_END, self._ARR_EXPECT_VALUE,
                          self._OBJ_EXPECT_COMMA_OR_END, self._ARR_EXPECT_COMMA_OR_END,
                          self._OBJ_EXPECT_KEY, self._OBJ_EXPECT_COLON):
            self._process_scalar_byte(b)
            return

        # Should not reach here
        self._reject(f"Unexpected byte {b} in state {self._state}")

    def _process_string_byte(self, b: int) -> None:
        """Process a byte inside a string literal."""
        if self._escaped:
            # After backslash, validate the escape sequence
            self._escaped = False
            if b == 0x75:  # u (start of \uXXXX)
                self._unicode_escape_remaining = 4
            elif b in {0x22, 0x5C, 0x2F, 0x62, 0x66, 0x6E, 0x72, 0x74}:  # " \ / b f n r t
                pass  # Valid single-char escape
            else:
                self._reject(f"Invalid escape sequence \\{chr(b)}")
            return

        # UTF-8 multi-byte validation (matches Python's strict utf-8 codec, which
        # is what json.loads uses). Non-ASCII bytes >= 0x80 inside a string must
        # form a valid UTF-8 sequence; a lone lead byte or stray continuation is
        # rejected. Tracking is inline so it survives chunk boundaries.
        if self._utf8_remaining > 0:
            # Expecting a continuation byte. The FIRST continuation after certain
            # lead bytes has a tighter range (E0->A0-BF, ED->80-9F avoids UTF-16
            # surrogates, F0->90-BF, F4->80-8F avoids >U+10FFFF); enforced via
            # _utf8_first_min/_utf8_first_max, reset after the first continuation.
            lo = self._utf8_first_min
            hi = self._utf8_first_max
            if not (lo <= b <= hi):
                self._reject(f"Invalid UTF-8 continuation byte 0x{b:02X} in string")
                return
            self._utf8_remaining -= 1
            self._utf8_first_min = 0x80  # subsequent continuations: 80-BF
            self._utf8_first_max = 0xBF
            return
        if b >= 0x80:
            # Lead byte of a multi-byte sequence. Set the continuation count and
            # the range of the FIRST continuation byte.
            if 0xC2 <= b <= 0xDF:
                self._utf8_remaining = 1
                self._utf8_first_min = 0x80
                self._utf8_first_max = 0xBF
            elif b == 0xE0:
                self._utf8_remaining = 2
                self._utf8_first_min = 0xA0  # E0 A0-BF (avoid overlong)
                self._utf8_first_max = 0xBF
            elif 0xE1 <= b <= 0xEC or b == 0xEE or b == 0xEF:
                self._utf8_remaining = 2
                self._utf8_first_min = 0x80
                self._utf8_first_max = 0xBF
            elif b == 0xED:
                self._utf8_remaining = 2
                self._utf8_first_min = 0x80  # ED 80-9F (reject surrogates A0-BF)
                self._utf8_first_max = 0x9F
            elif b == 0xF0:
                self._utf8_remaining = 3
                self._utf8_first_min = 0x90  # F0 90-BF (avoid overlong)
                self._utf8_first_max = 0xBF
            elif 0xF1 <= b <= 0xF3:
                self._utf8_remaining = 3
                self._utf8_first_min = 0x80
                self._utf8_first_max = 0xBF
            elif b == 0xF4:
                self._utf8_remaining = 3
                self._utf8_first_min = 0x80  # F4 80-8F (avoid > U+10FFFF)
                self._utf8_first_max = 0x8F
            else:
                self._reject(f"Invalid UTF-8 lead byte 0x{b:02X} in string")
                return
            return

        if b == 0x5C:  # backslash
            if self._utf8_remaining > 0:
                self._reject("Backslash inside an incomplete UTF-8 sequence in string")
                return
            self._escaped = True
            return

        if b == 0x22:  # closing quote
            if self._utf8_remaining > 0:
                self._reject("Closing quote inside an incomplete UTF-8 sequence in string")
                return
            self._in_string = False
            self._finalize_string()
            return

        # Normal string character (or control char - rejected below)
        if b < 0x20:
            self._reject(f"Control character 0x{b:02X} inside string")
        # Regular character, nothing to do

    def _finalize_string(self) -> None:
        """Called when a string is completed."""
        # Check what we expect this string to be
        if self._state == self._OBJ_EXPECT_KEY:
            # This is a key, now expect colon
            self._state = self._OBJ_EXPECT_COLON
        elif self._state in (self._TOP_EXPECT_VALUE, self._OBJ_EXPECT_VALUE,
                            self._ARR_EXPECT_VALUE, self._ARR_EXPECT_VALUE_OR_END):
            # This is a value, now what's next depends on context
            self._finalize_value()
        else:
            self._reject(f"String in unexpected state {self._state}")

    def _process_scalar_byte(self, b: int) -> None:
        """Process a byte that could be part of a scalar value (keyword, number, or structure)."""

        # State gating for structural bytes that must not appear unexpectedly.
        # Object keys must be strings: when expecting a key, only " (string) or
        # } (close empty object) are valid — a digit/letter/etc. is not a key.
        if self._state == self._OBJ_EXPECT_KEY and b != 0x22 and b != 0x7D:
            self._reject(f"Object key must be a string (got {chr(b)!r})")
            return
        # After a key, only : (colon) is valid — any other byte is missing-colon.
        if self._state == self._OBJ_EXPECT_COLON and b != 0x3A:
            self._reject(f"Expected ':' after object key (got {chr(b)!r})")
            return

        # First handle string start (quote) - it's valid in more states than containers
        if b == 0x22:  # " (start of string)
            # Validate any pending scalar first
            if self._scalar_buf:
                self._validate_accumulated_scalar()
                if self._rejected:
                    return
            self._in_string = True
            # Quote is valid when expecting a key (in object) or a value (anywhere)
            if self._state in (self._OBJ_EXPECT_KEY, self._TOP_EXPECT_VALUE,
                              self._OBJ_EXPECT_VALUE, self._ARR_EXPECT_VALUE,
                              self._ARR_EXPECT_VALUE_OR_END):
                pass  # Valid start of string
            else:
                self._reject(f"Unexpected string start in state {self._state}")
            return

        # Validate any pending scalar before processing structural bytes
        if self._scalar_buf and b in (0x7B, 0x7D, 0x5B, 0x5D, 0x2C, 0x3A):
            self._validate_accumulated_scalar()
            if self._rejected:
                return

        # Structure characters (start/end of containers)
        if b == 0x7B:  # {
            self._start_object()
        elif b == 0x7D:  # }
            self._end_object()
        elif b == 0x5B:  # [
            self._start_array()
        elif b == 0x5D:  # ]
            self._end_array()
        elif b == 0x2C:  # ,
            self._handle_comma()
        elif b == 0x3A:  # :
            self._handle_colon()
        else:
            # A scalar byte (digit, letter for a keyword, '-', etc.). Scalars are
            # only valid where a VALUE is expected; in any other state (e.g.
            # comma-or-end after a value, or expect-colon) a scalar byte is a
            # grammar error (e.g. `[1,2,null]6}` -> stray 6 after the array).
            if self._state not in (self._TOP_EXPECT_VALUE, self._OBJ_EXPECT_VALUE,
                                   self._ARR_EXPECT_VALUE, self._ARR_EXPECT_VALUE_OR_END):
                self._reject(f"Unexpected scalar byte {chr(b)!r} in state {self._state}")
                return
            # Reject control chars and stray high bytes outside strings (they are
            # never part of a valid number/keyword scalar).
            if b < 0x20:
                self._reject(f"Control character 0x{b:02X} outside string")
                return
            if b >= 0x7F:
                self._reject(f"Non-ASCII byte 0x{b:02X} outside string")
                return
            self._scalar_buf.append(b)

    def _start_object(self) -> None:
        """Handle opening brace {."""
        if self._state not in (self._TOP_EXPECT_VALUE, self._OBJ_EXPECT_VALUE,
                               self._ARR_EXPECT_VALUE, self._ARR_EXPECT_VALUE_OR_END):
            self._reject(f"Unexpected {{ in state {self._state}")
            return

        self._depth += 1
        # Store the container type (object) in the stack
        self._stack.append("object")
        self._state = self._OBJ_EXPECT_KEY
        self._in_object_start = True  # We're at the start of a new object

    def _end_object(self) -> None:
        """Handle closing brace }."""
        # Validate any pending scalar first
        if self._scalar_buf:
            self._validate_accumulated_scalar()
            if self._rejected:
                return

        # Allow closing right after "{" (empty object) or after a complete value.
        # Closing in _OBJ_EXPECT_KEY AFTER a comma is a trailing comma -> reject.
        # _in_object_start distinguishes "right after {" (empty, valid) from
        # "after a comma" (trailing comma, invalid).
        if self._state == self._OBJ_EXPECT_KEY and not self._in_object_start:
            self._reject("Trailing comma in object (} after ,)")
            return
        if self._state not in (self._OBJ_EXPECT_KEY, self._OBJ_EXPECT_COMMA_OR_END):
            self._reject(f"Unexpected }} in state {self._state}")
            return

        self._depth -= 1
        if self._depth < 0:
            self._reject("Too many closing braces")
            return

        # Pop the container type
        container = self._stack.pop()
        if container != "object":
            self._reject(f"Container type mismatch: expected object, got {container}")
            return

        if self._depth == 0:
            # Top-level value completed
            self._state = self._TOP_EXPECT_TERMINATOR
            self._saw_complete_value = True
        else:
            # Check parent container type to determine next state
            parent = self._stack[-1] if self._stack else None
            if parent == "object":
                self._state = self._OBJ_EXPECT_COMMA_OR_END
            elif parent == "array":
                self._state = self._ARR_EXPECT_COMMA_OR_END
            else:
                self._reject(f"Unexpected parent container type: {parent}")

    def _start_array(self) -> None:
        """Handle opening bracket [."""
        if self._state not in (self._TOP_EXPECT_VALUE, self._OBJ_EXPECT_VALUE,
                               self._ARR_EXPECT_VALUE, self._ARR_EXPECT_VALUE_OR_END):
            self._reject(f"Unexpected [ in state {self._state}")
            return

        self._depth += 1
        # Store the container type (array) in the stack
        self._stack.append("array")
        self._state = self._ARR_EXPECT_VALUE_OR_END
        self._in_object_start = True  # We're at the start of a new array (for trailing comma check)

    def _end_array(self) -> None:
        """Handle closing bracket ]."""
        # Validate any pending scalar first
        if self._scalar_buf:
            self._validate_accumulated_scalar()
            if self._rejected:
                return

        # Allow closing right after "[" (empty array) or after a complete value.
        # Closing in _ARR_EXPECT_VALUE after a comma is a trailing comma -> reject.
        # _in_object_start distinguishes "right after [" (empty, valid) from
        # "after a comma" (trailing comma, invalid).
        if self._state == self._ARR_EXPECT_VALUE and not self._in_object_start:
            self._reject("Trailing comma in array (] after ,)")
            return
        if self._state not in (self._ARR_EXPECT_VALUE_OR_END, self._ARR_EXPECT_COMMA_OR_END):
            self._reject(f"Unexpected ] in state {self._state}")
            return

        self._depth -= 1
        if self._depth < 0:
            self._reject("Too many closing brackets")
            return

        # Pop the container type
        container = self._stack.pop()
        if container != "array":
            self._reject(f"Container type mismatch: expected array, got {container}")
            return

        if self._depth == 0:
            # Top-level value completed
            self._state = self._TOP_EXPECT_TERMINATOR
            self._saw_complete_value = True
        else:
            # Check parent container type to determine next state
            parent = self._stack[-1] if self._stack else None
            if parent == "object":
                self._state = self._OBJ_EXPECT_COMMA_OR_END
            elif parent == "array":
                self._state = self._ARR_EXPECT_COMMA_OR_END
            else:
                self._reject(f"Unexpected parent container type: {parent}")

    def _handle_comma(self) -> None:
        """Handle comma separator."""
        if self._state == self._OBJ_EXPECT_COMMA_OR_END:
            self._state = self._OBJ_EXPECT_KEY
            self._in_object_start = False  # No longer at the start after first member
        elif self._state == self._ARR_EXPECT_COMMA_OR_END:
            self._state = self._ARR_EXPECT_VALUE
        elif self._state == self._OBJ_EXPECT_KEY and self._in_object_start:
            # Comma at the start of an object - this is a trailing comma!
            self._reject("Trailing comma in object")
        elif self._state == self._ARR_EXPECT_VALUE_OR_END and self._in_object_start:
            # Comma at the start of an array - this is a trailing comma!
            self._reject("Trailing comma in array")
        elif self._state in (self._OBJ_EXPECT_KEY, self._ARR_EXPECT_VALUE,
                           self._OBJ_EXPECT_VALUE, self._TOP_EXPECT_VALUE,
                           self._ARR_EXPECT_VALUE_OR_END):
            # Comma appeared where we expected a value - this is invalid
            self._reject(f"Unexpected comma in state {self._state}")
        else:
            self._reject(f"Unexpected comma in state {self._state}")

    def _handle_colon(self) -> None:
        """Handle colon after key in object."""
        if self._state != self._OBJ_EXPECT_COLON:
            self._reject(f"Unexpected colon in state {self._state}")
            return
        # Validate any pending scalar (e.g., key string if we weren't tracking it properly)
        if self._scalar_buf:
            self._validate_accumulated_scalar()
            if self._rejected:
                return
        self._state = self._OBJ_EXPECT_VALUE

    def _finalize_value(self) -> None:
        """Called when any value (string, number, keyword, container) is completed."""
        if self._depth == 0:
            # Top-level value completed
            self._state = self._TOP_EXPECT_TERMINATOR
            self._saw_complete_value = True
        else:
            # Value inside a container, now expect comma or end
            # Check the parent container type
            parent = self._stack[-1] if self._stack else None
            if parent == "object":
                self._state = self._OBJ_EXPECT_COMMA_OR_END
            elif parent == "array":
                self._state = self._ARR_EXPECT_COMMA_OR_END
            else:
                self._reject(f"Value completed in unexpected context (parent={parent})")

    def _is_hex_digit(self, b: int) -> bool:
        """Check if byte is a valid hex digit."""
        return (0x30 <= b <= 0x39 or  # 0-9
                0x41 <= b <= 0x46 or  # A-F
                0x61 <= b <= 0x66)   # a-f

    def _validate_accumulated_scalar(self) -> None:
        """Validate an accumulated scalar token (keyword or number).

        Accepts the standard RFC 8259 keywords (true, false, null) and the three
        Python-specific floating-point constants (NaN, Infinity, -Infinity) to
        match the writer's ``json.dumps(allow_nan=True)`` behavior and the full
        reader's ``json.loads`` grammar.
        """
        if not self._scalar_buf:
            self._reject("Empty scalar token")
            return

        token_bytes = bytes(self._scalar_buf)
        self._scalar_buf = []

        # Try to match keyword
        token_str = token_bytes.decode("utf-8", errors="replace")
        if token_str == "true":
            self._finalize_value()
        elif token_str == "false":
            self._finalize_value()
        elif token_str == "null":
            self._finalize_value()
        elif token_str == "NaN":
            self._finalize_value()
        elif token_str == "Infinity":
            self._finalize_value()
        elif token_str == "-Infinity":
            self._finalize_value()
        else:
            # Must be a number
            self._validate_number_token(token_bytes)

    def _validate_number_token(self, token_bytes: bytes) -> None:
        """Validate a number token according to RFC 8259."""
        # Number grammar: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?
        i = 0
        n = len(token_bytes)

        # Optional minus sign
        if i < n and token_bytes[i] == 0x2D:  # -
            i += 1

        # Integer part
        if i >= n:
            self._reject("Number has no digits")
            return

        if token_bytes[i] == 0x30:  # 0
            i += 1
            # Leading zeros not allowed (unless it's just "0")
            if i < n and 0x30 <= token_bytes[i] <= 0x39:
                self._reject("Number has leading zero")
                return
        elif 0x31 <= token_bytes[i] <= 0x39:  # 1-9
            i += 1
            while i < n and 0x30 <= token_bytes[i] <= 0x39:
                i += 1
        else:
            self._reject("Number has invalid integer part")
            return

        # Fractional part
        if i < n and token_bytes[i] == 0x2E:  # .
            i += 1
            if i >= n or not (0x30 <= token_bytes[i] <= 0x39):
                self._reject("Number has . but no fractional digits")
                return
            while i < n and 0x30 <= token_bytes[i] <= 0x39:
                i += 1

        # Exponent part
        if i < n and token_bytes[i] in (0x45, 0x65):  # E or e
            i += 1
            # Optional +/-
            if i < n and token_bytes[i] in (0x2B, 0x2D):  # + or -
                i += 1
            if i >= n or not (0x30 <= token_bytes[i] <= 0x39):
                self._reject("Number has exponent but no exponent digits")
                return
            while i < n and 0x30 <= token_bytes[i] <= 0x39:
                i += 1

        # Must have consumed all bytes
        if i != n:
            self._reject(f"Number has trailing bytes: {token_bytes[i:]!r}")
            return

        self._finalize_value()

    def _reject(self, msg: str) -> None:
        """Mark the input as rejected with an error message."""
        self._rejected = True
        self._error_msg = msg

    def _accept(self) -> None:
        """Mark the input as accepted."""
        self._accepted = True

    @property
    def accepted(self) -> bool:
        """True if the input was accepted as valid JSON."""
        return self._accepted

    @property
    def rejected(self) -> bool:
        """True if the input was rejected as invalid JSON."""
        return self._rejected

    @property
    def error_message(self) -> str:
        """Error message if rejected, empty otherwise."""
        return self._error_msg

    def finish(self) -> bool:
        """Return True iff the fed bytes form a complete, valid JSON value.

        Must be called after feeding all bytes. Returns True only if:
        - The JSON value is complete (depth 0, no unclosed containers)
        - Record ends with \n (or \r\n) terminator and nothing else
        - No unterminated strings or escape sequences

        A rejection (including trailing garbage after an otherwise-complete
        record) always wins over a prior acceptance: accepted-then-rejected
        means the input had a complete prefix followed by invalid trailing
        bytes, which is NOT a valid single JSONL record.
        """
        if self._rejected:
            return False
        if self._accepted:
            return True

        # Check for pending CR without LF (bare CR)
        if self._pending_cr:
            self._reject("Bare CR (terminator is \\r\\n, not just \\r)")
            return False

        # Check if we have a complete value
        if not self._saw_complete_value:
            # Either empty input or incomplete value
            self._reject("Incomplete JSON value (no complete value found)")
            return False

        # Check for incomplete state
        if self._in_string:
            self._reject("Unterminated string")
            return False

        if self._escaped:
            self._reject("Unterminated escape sequence")
            return False

        if self._unicode_escape_remaining > 0:
            self._reject("Incomplete \\u escape sequence")
            return False

        if self._utf8_remaining > 0:
            self._reject("Incomplete UTF-8 sequence in string")
            return False

        if self._depth > 0:
            self._reject(f"Unclosed container (depth={self._depth})")
            return False

        # Check if we have pending scalar token
        if self._scalar_buf:
            self._validate_accumulated_scalar()
            if self._rejected:
                return False
            # After validating scalar, check if it completed the value
            if not self._saw_complete_value:
                self._reject("Scalar did not complete the value")
                return False

        # If we reach here with a complete value and no errors, accept
        # BUT we must also be in the proper terminator state
        if self._saw_complete_value and self._state == self._TOP_EXPECT_TERMINATOR:
            # We need to have seen a terminator (either via accept() during processing
            # or we're still waiting for it - but if we're still waiting, we reject)
            # Actually, if we're in TOP_EXPECT_TERMINATOR state and haven't accepted yet,
            # it means we didn't get the newline terminator
            return self._accepted  # Only accept if we actually got the terminator

        self._reject(f"Invalid end state: {self._state}")
        return False


def _find_record_start_before(
    fh, size: int, seek_pos: int, *, budget: _ReadBudget | None = None, fault: list[bool] | None = None
) -> int:
    """Return the byte offset where the JSONL record overlapping ``seek_pos``
    begins, i.e. the byte just after the last newline strictly before seek_pos.
    Returns 0 if there is no preceding newline (the record starts at byte 0).
    Scans backward in bounded chunks.

    The caller owns the handle and passes the pinned size from os.fstat — this
    ensures all reads use a single inode generation (single-generation contract).

    When ``budget`` is a ``_ReadBudget``, every ``fh.read`` is capped at the
    budget's remaining allowance via ``budget.take`` (the read covers the LAST
    ``to_read`` bytes of the candidate ``[read_from, pos)`` range, since we scan
    backward — the suffix is the highest-value window). If the allowance hits 0
    the scan stops and returns 0 (fail-quiet fallback). When ``budget`` is None
    the helper runs unbounded (the whole tail read is already bounded by the
    caller). Capping only ever narrows the examined window — the byte-found
    offset math is unchanged when the full chunk is read. (#6139 r10 item 1.)

    When ``fault`` is a mutable list[bool], a caught OSError sets fault[0]=True
    to signal the caller that a transient read fault occurred. (#6139 r14)
    """
    if seek_pos <= 0:
        return 0
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    pos = min(seek_pos, size)
    try:
        while pos > 0:
            read_from = max(0, pos - chunk_size)
            want = pos - read_from
            if budget is not None:
                to_read = budget.take(want)
                if to_read == 0:
                    return 0  # exhausted before this read — fail-quiet fallback
                # Read the LAST `to_read` bytes of [read_from, pos): scanning
                # backward, the suffix is the highest-value window to examine.
                seek_to = pos - to_read
            else:
                to_read = want
                seek_to = read_from
            fh.seek(seek_to)
            block = fh.read(to_read)
            nl = block.rfind(b"\n")
            if nl >= 0:
                return seek_to + nl + 1
            pos = seek_to
    except (FileNotFoundError, OSError):
        # TOCTOU: journal deleted between the stat() above and this open/read
        # (cleanup racing a status poll). Return the safe fallback rather than
        # letting the exception escape to the HTTP handler. (#6139 Greptile P1)
        if fault is not None:
            fault[0] = True
        return 0
    return 0


def _read_last_complete_line_before(fh, size: int, end_offset: int, *, budget: int | _ReadBudget | None = None, fault: list[bool] | None = None) -> dict | None:
    """Return the summary of the last complete JSONL record strictly before
    ``end_offset``, without materializing a multi-MB payload.

    Scans backward across preceding complete lines, skipping any that are blank,
    malformed (JSONDecodeError), or non-dict, until a valid event dict is found.
    For each oversized predecessor, the row is SKIPPED (fail-closed, #6139 r9
    item 2): an oversized record's full-record JSON validity can NEVER be proven
    without materializing its payload, so its fabricated prefix must not be
    trusted as proof of a valid event. The scan continues backward to a
    normal-sized (fully-parseable) valid event, or returns None.

    This loop fixes #6139 r7: a shape like ``token\\n\\n<oversized partial done>``
    (a valid event, then a blank line, then a crash-truncated oversized record)
    defeats a single-scan implementation — the first preceding line is blank,
    json.loads("") fails, and recovery fails. The backward loop skips the blank
    line and recovers the valid event.

    The caller owns the handle and passes the pinned size from os.fstat.
    ``budget`` bounds the total PHYSICAL bytes touched by ``fh.read`` across the
    whole scan — every descriptor read (``_rfind_byte_before`` newline scans,
    candidate-line parsing, oversized structural check, oversized prefix
    extraction) charges a single shared ``_ReadBudget`` meter, so the scan
    stops as soon as the allowance is exhausted (#6139 r9 item 1). ``rows_scanned``
    is retained as a secondary row-cap guard (a pathological file of tiny rows
    could fit many rows in the byte budget). ``budget`` may be an ``int`` (a fresh
    meter of that size is created) or a pre-existing ``_ReadBudget`` (shared with
    the caller — used by the production boundary-recovery composition so the
    predecessor scan counts against the SAME allowance as boundary lookup /
    structural validation). If ``budget`` is None it defaults to
    ``_SESSION_REPLAY_MAX_BYTES``.

    When ``fault`` is a mutable list[bool], a caught OSError sets fault[0]=True
    to signal the caller that a transient read fault occurred. (#6139 r14)
    """
    if end_offset <= 0:
        return None
    scan_end = min(end_offset, size)
    # One shared remaining-work meter across every descriptor read in this scan
    # so physical I/O never exceeds the caller's allowance (#6139 r9 item 1). If
    # the caller passed an existing ``_ReadBudget`` (the production composition),
    # share it so predecessor recovery counts against the same boundary budget.
    if isinstance(budget, _ReadBudget):
        budget_obj = budget
    else:
        budget_bytes = budget if budget is not None else _SESSION_REPLAY_MAX_BYTES
        budget_obj = _ReadBudget(budget_bytes)
    rows_scanned = 0
    # Loop backward across preceding complete lines, skipping blank / malformed /
    # non-dict lines (and oversized predecessors, fail-closed), until a valid
    # normal-sized event dict is found. Without this loop, a shape like
    # `token\n\n<big>` (a blank line between the valid event and the truncated
    # boundary record) defeats recovery: the first preceding line is blank,
    # json.loads("") fails, and the function returns None instead of continuing
    # back to the valid event. (#6139 r7)
    while scan_end > 0:
        # Secondary row-cap guard (the budget is byte-based; a pathological file
        # of tiny rows could fit many rows in the byte budget).
        if rows_scanned > _SESSION_REPLAY_MAX_ROWS:
            return None
        # Stop before the next helper at exhaustion (the maintainer's exact
        # words for item 1: "stop before the next helper at exhaustion").
        if budget_obj.exhausted:
            return None
        first_nl = _rfind_byte_before(fh, b"\n", scan_end, budget=budget_obj, fault=fault)
        if first_nl is None or first_nl == 0:
            return None  # no preceding complete line (or budget exhausted mid-scan)
        if budget_obj.exhausted:
            return None  # stop before the next helper at exhaustion
        second_nl = _rfind_byte_before(fh, b"\n", first_nl, budget=budget_obj, fault=fault)
        line_start = (second_nl + 1) if second_nl is not None else 0
        line_len = first_nl - line_start
        if line_len <= 0:
            # Blank line (e.g. the gap in `token\n\n<big>`). Skip it: continue the
            # backward scan from before this blank line. No descriptor read here,
            # so the budget is untouched; only the row-cap advances.
            rows_scanned += 1  # count blank lines too (guard against infinite blank streaks)
            scan_end = line_start  # strictly < previous scan_end (line_start <= first_nl < scan_end)
            if scan_end <= 0:
                return None
            continue
        # Candidate predecessor line. This loop only reaches COMPLETE lines
        # (newline-terminated on both sides — _rfind_byte_before found the
        # surrounding newlines, and the caller's end_offset is the boundary
        # record's start, strictly after this line).
        #
        # #6139 redesign: validate the predecessor via prefix-summary + streaming
        # grammar validation (O(depth) memory, never materializes the full line).
        # Read a bounded prefix, extract its summary, then validate the WHOLE line
        # is grammatically complete via _record_is_valid_jsonl (streams through the
        # line in chunks, bounded by line_len). This accepts VALID predecessors of
        # ANY size (e.g. a 2.3 MiB done) while rejecting malformed ones (trailing
        # comma, malformed nested value) via full grammar checking.
        line_end = first_nl + 1  # the byte AFTER the terminating newline
        prefix_to_read = budget_obj.take(min(_BOUNDARY_SUMMARY_PREFIX_BYTES, line_len))
        if prefix_to_read == 0:
            return None  # budget exhausted before reading prefix — stop
        try:
            fh.seek(line_start)
            prefix_raw = fh.read(prefix_to_read)
        except (FileNotFoundError, OSError):
            if fault is not None:
                fault[0] = True
            return None  # TOCTOU: journal deleted mid-read — safe fallback
        text = prefix_raw.decode("utf-8", errors="replace")
        # A predecessor is a COMPLETE line (newline-terminated on both sides),
        # not a fabricated oversized boundary summary. Only mark it as
        # extracted-from-oversized if the line was BIGGER than the prefix read
        # (payload genuinely discarded); a normal-sized event whose whole record
        # fit in the prefix is a real event and must not carry the fabrication
        # flag (the r10 adversarial tests detect fabricated boundary summaries
        # via that flag).
        line_was_truncated = line_len > prefix_to_read
        parsed_summary = _parse_prefix_summary(text, mark_extracted=line_was_truncated)
        if parsed_summary is None:
            # Prefix failed to parse (no payload key / malformed). Skip this candidate.
            rows_scanned += 1
            scan_end = line_start
            if scan_end <= 0:
                return None
            continue
        # Prefix extracted; now validate the WHOLE line is grammatically complete.
        # _record_is_valid_jsonl streams from line_start to line_end (the actual
        # line end, not EOF), confirming grammar + terminator. If it returns False,
        # the line is malformed/truncated — skip it and continue backward.
        # Give the predecessor's streaming validation its OWN budget sized to the
        # discovered line extent (mirrors the boundary record's validity_budget =
        # size - record_start). The shared recovery_budget already paid for the
        # backward newline scan + prefix read; charging it AGAIN for full-line
        # validation starves >4 MiB predecessors (they need ~2x their size but
        # only ~size+cap remains). The validator self-terminates at the line's
        # terminating newline (depth-0 + \n), so line_len + one chunk covers it.
        predecessor_validity_budget = _ReadBudget(line_len + _SESSION_REPLAY_READ_CHUNK_BYTES)
        if not _record_is_valid_jsonl(fh, line_end, line_start, budget=predecessor_validity_budget, fault=fault):
            # Malformed or truncated predecessor — skip, continue backward.
            rows_scanned += 1
            scan_end = line_start
            if scan_end <= 0:
                return None
            continue
        # Valid complete predecessor with extractable summary.
        rows_scanned += 1
        return parsed_summary
    return None


def _rfind_byte_before(
    fh, byte: bytes, end_offset: int, *, budget: _ReadBudget | None = None, fault: list[bool] | None = None
) -> int | None:
    """Return the offset of the last occurrence of ``byte`` at or before
    ``end_offset - 1``, scanning backward in bounded chunks. None if not found.

    The caller owns the handle; all reads use the same inode generation.

    When ``budget`` is a ``_ReadBudget``, every ``fh.read`` is capped at the
    budget's remaining allowance via ``budget.take`` (the read covers the LAST
    ``to_read`` bytes of the candidate ``[read_from, pos)`` range, since we scan
    backward). If the allowance hits 0 the scan stops and returns None. When
    ``budget`` is None the helper runs unbounded (the whole tail read is already
    bounded by the caller). Capping only ever narrows the examined window — the
    byte-found offset math is unchanged when the full chunk is read.
    """
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    pos = end_offset
    try:
        while pos > 0:
            read_from = max(0, pos - chunk_size)
            want = pos - read_from
            if budget is not None:
                to_read = budget.take(want)
                if to_read == 0:
                    return None  # exhausted before this read — stop (fail-quiet)
                # Read the LAST `to_read` bytes of [read_from, pos): scanning
                # backward, the suffix is the highest-value window to examine.
                block_start = pos - to_read
                fh.seek(block_start)
                block = fh.read(to_read)
                idx = block.rfind(byte)
                if idx >= 0:
                    return block_start + idx
                # Byte not in this suffix. If the suffix was shorter than the
                # candidate range (budget truncated it), there may be unexamined
                # bytes in [read_from, block_start) — but only if budget remains.
                # If exhausted, stop; otherwise advance pos to the suffix start.
                pos = block_start
            else:
                fh.seek(read_from)
                block = fh.read(want)
                idx = block.rfind(byte)
                if idx >= 0:
                    return read_from + idx
                pos = read_from
    except (FileNotFoundError, OSError):
        # TOCTOU: journal deleted before/during the scan. Return the safe
        # fallback (None = byte not found) rather than escaping to the caller.
        # Signal the transient fault so the caller does NOT cache this as an
        # authoritative "no predecessor" result. (#6139 r16)
        if fault is not None:
            fault[0] = True
        return None
    return None


def _record_is_structurally_complete(
    fh, size: int, record_start: int, *, budget: _ReadBudget | None = None
) -> bool:
    """Return True iff the JSONL record at ``record_start`` is structurally
    complete — i.e. its JSON object is closed (brace depth returns to 0) AND
    followed by a newline terminator — scanning forward in bounded chunks WITHOUT
    materializing the (potentially multi-MB) payload.

    Used to gate trusting a fabricated prefix summary: a crash-truncated
    ``done`` (write interrupted mid-payload, no closing brace/newline) must NOT
    be accepted as terminal, or an interrupted run is misreported as completed
    and its recovery signal is silently dropped. Returns False if EOF is reached
    at brace depth > 0 (the record was truncated mid-write).

    The caller owns the handle and passes the pinned size from os.fstat. When
    ``budget`` is a ``_ReadBudget``, every ``fh.read`` is capped at the budget's
    remaining allowance via ``budget.take``; if the allowance hits 0 before the
    record's closing brace is confirmed, returns False (fail-closed — can't
    prove completeness within the budget). The terminator look-aheads
    (``fh.read(1)`` / ``fh.read(2)`` after the closing brace) are also charged
    via ``budget.take`` and fail-closed if the allowance is exhausted (#6139 r10
    item 1). When ``budget`` is None the helper runs unbounded. The brace-depth
    + terminator logic is otherwise unchanged.
    """
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    depth = 0
    pos = record_start
    in_string = False
    escaped = False
    try:
        fh.seek(record_start)
        while pos < size:
            want = min(chunk_size, size - pos)
            if budget is not None:
                to_read = budget.take(want)
                if to_read == 0:
                    return False  # exhausted before confirming completeness — fail-closed
            else:
                to_read = want
            chunk = fh.read(to_read)
            if not chunk:
                break
            chunk_len = len(chunk)
            if budget is not None and chunk_len < want and depth > 0:
                # Budget truncated this read before we could reach the closing
                # brace — can't confirm completeness within the allowance.
                return False
            for ci in range(chunk_len):
                b = chunk[ci]
                pos += 1
                if in_string:
                    if escaped:
                        escaped = False
                    elif b == 0x5C:  # backslash
                        escaped = True
                    elif b == 0x22:  # closing quote
                        in_string = False
                    continue
                if b == 0x22:  # opening quote
                    in_string = True
                elif b == 0x7B:  # '{'
                    depth += 1
                elif b == 0x7D:  # '}'
                    depth -= 1
                    if depth == 0:
                        # Object closed at position `pos` (1 past the '}').
                        # The record is complete iff the byte(s) right after are a
                        # newline terminator (\n or \r\n). Look at the next byte
                        # in the current chunk first (avoid file-cursor drift),
                        # else read fresh from the file.
                        if ci + 1 < chunk_len:
                            nb = chunk[ci + 1]
                            if nb == 0x0A:  # \n — complete
                                return True
                            if nb == 0x0D:  # \r — need to check for \r\n
                                if ci + 2 < chunk_len:
                                    return chunk[ci + 2] == 0x0A
                                # \r at chunk end: read from file to check for \n.
                                # Charge the terminator look-ahead against the
                                # shared budget (#6139 r10 item 1: "cap every
                                # request to the remainder" — the exact-exhaustion
                                # leak was an uncharged fh.read(1) here).
                                if budget is not None and budget.take(1) == 0:
                                    return False  # can't prove terminator within budget — fail-closed
                                fh.seek(pos + 1)
                                return fh.read(1) == b"\n"
                            return False  # any other byte after } is not a terminator
                        # Terminator is in the next chunk: read up to 2 bytes
                        # from file to distinguish \r\n (CRLF) from a bare \r.
                        # Charge the terminator look-ahead (same r10 fix).
                        if budget is not None and budget.take(2) == 0:
                            return False  # can't prove terminator within budget — fail-closed
                        fh.seek(pos)
                        nb = fh.read(2)
                        return nb == b"\r\n" or nb[:1] == b"\n"
                elif b == 0x0A and depth == 0:  # newline at depth 0 before close
                    return False
            # depth > 0 here means the record spans more chunks; keep scanning.
        # Reached EOF: a record ending at EOF is only complete if a real newline
        # terminator was seen — NOT if the `}` is the last byte. A write
        # interrupted after `}` but before the `\n` is crash-truncated.
        return False
    except (FileNotFoundError, OSError):
        # TOCTOU: journal deleted between the stat() above and this open/read
        # (cleanup racing a status poll). Return the safe fallback (False =
        # record not complete) rather than letting the exception escape to the
        # HTTP handler. (#6139 Greptile P1)
        return False


def _record_is_valid_jsonl(
    fh, size: int, record_start: int, *, budget: _ReadBudget | None = None, fault: list[bool] | None = None
) -> bool:
    """Return True iff the JSONL record at ``record_start`` is a complete,
    grammatically-valid JSON object followed by a newline terminator.

    Streams the record forward in chunks through ``_StreamingJsonValidator``
    (O(nesting-depth) memory, never materializes the record) until the top-level
    value closes + ``\\n``. This is the unified validity gate for trusting a
    fabricated boundary-record summary (#6139 r10 item 2): brace balance is
    necessary but NOT sufficient — a trailing comma, malformed nested value,
    unquoted key, or any grammar error anywhere in the record (including inside
    the payload) is rejected, matching the full reader's ``json.loads``.

    Unlike the prior two-tier design (#6139 r14), there is NO fixed correctness
    ceiling: the validator streams in ``_SESSION_REPLAY_READ_CHUNK_BYTES`` chunks
    and stops at the record's actual end (depth-0 + newline), so a valid record
    of ANY size (e.g. a 17 MiB ``done`` with a full transcript) is accepted. The
    only bound is the ``budget`` (total physical I/O) — a pathological multi-GB
    record fails-closed when the budget exhausts, recovering the older valid
    event. This removes both the r15 ceiling (blocker 1) and the tier-selection
    bug (blocker 4: the old code used ``size - record_start`` — the suffix to EOF,
    including trailing records — instead of the actual record length).

    The caller owns the handle and passes the pinned size from os.fstat. When
    ``fault`` is a mutable list[bool], a caught OSError sets fault[0]=True (#6139 r14).
    """
    if record_start < 0 or record_start >= size:
        return False
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    validator = _StreamingJsonValidator()
    pos = record_start
    try:
        while pos < size:
            want = min(chunk_size, size - pos)
            if budget is not None:
                got = budget.take(want)
                if got <= 0:
                    return False  # budget exhausted before the record closed — fail-closed
                want = min(want, got)
            fh.seek(pos)
            block = fh.read(want)
            if not block:
                break
            validator.feed(block)
            # Check accepted BEFORE rejected because a single feed() can set both
            # (newline triggers accept, then subsequent bytes in the same chunk trigger reject).
            # We want to accept a valid record even if there are trailing bytes in the file.
            if validator.accepted:
                return True  # top-level value + terminator confirmed
            if validator.rejected:
                return False
            pos += len(block)
    except (FileNotFoundError, OSError):
        if fault is not None:
            fault[0] = True
        return False
    # EOF reached without the validator accepting (crash-truncated or incomplete).
    result = validator.finish()
    return result


def _parse_prefix_summary(text: str, *, mark_extracted: bool = True) -> dict | None:
    """Parse a bounded JSONL prefix to extract summary fields.

    Handles both cases:
    - Payload key found → truncate before it, close object, parse
    - No payload key → try direct parse up to first newline

    When ``mark_extracted`` is True (the default, for the BOUNDARY record whose
    payload was discarded), sets ``_summary_extracted_from_oversized_record=True``
    so callers can distinguish a fabricated boundary summary from a real event.
    Predecessor recovery passes ``mark_extracted=False`` for a complete normal-
    sized line whose payload fit in the prefix (it is a real event, not a
    fabricated oversized-record summary).

    Returns the parsed dict or None.
    """
    # Find the top-level "payload" key (depth 1 inside the record object).
    payload_pos = _find_top_level_payload_key(text)
    if payload_pos is None:
        # No payload key in the prefix — either the record is small enough that
        # the whole thing fit (parse directly if it ends in this prefix), or the
        # layout is unexpected. Try a direct parse of the prefix up to the first
        # newline; if that fails, give up.
        nl = text.find("\n")
        candidate = text if nl < 0 else text[:nl]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    # Truncate before "payload", strip trailing comma/whitespace, close object.
    head = text[:payload_pos].rstrip()
    if head.endswith(","):
        head = head[:-1].rstrip()
    head += "\n}"
    try:
        parsed = json.loads(head)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    # Replace the (unread) payload with an empty dict so the shape is consistent
    # but no payload bytes are materialized.
    parsed["payload"] = {}
    if mark_extracted:
        parsed["_summary_extracted_from_oversized_record"] = True
    return parsed


def _extract_boundary_record_summary(
    fh, record_start: int, *, budget: _ReadBudget | None = None, fault: list[bool] | None = None
) -> dict | None:
    """Extract ONLY the summary fields of an oversized journal record that
    straddles the tail-window boundary, without materializing its payload.

    Reads a bounded prefix (``_BOUNDARY_SUMMARY_PREFIX_BYTES``) from
    ``record_start``, locates the top-level ``"payload"`` key via a brace-depth
    scan, truncates the JSON before it, closes the object, and parses. Returns
    a dict with the summary fields (``event``/``seq``/``event_id``/``terminal``/
    ``terminal_state``) or ``None`` if the layout is unexpected. The payload is
    replaced with an empty dict so downstream consumers see the shape but not
    the bytes.

    The caller owns the handle; all reads use the same inode generation. When
    ``budget`` is a ``_ReadBudget`` the prefix read is capped at the budget's
    remaining allowance; if the allowance hits 0 the function returns None. The
    rest of the function operates on whatever prefix was read — if a shorter-
    than-expected prefix can't be parsed, returns None.

    When ``fault`` is a mutable list[bool], a caught OSError sets fault[0]=True
    to signal the caller that a transient read fault occurred. (#6139 r14)
    """
    try:
        fh.seek(record_start)
        if budget is not None:
            to_read = budget.take(_BOUNDARY_SUMMARY_PREFIX_BYTES)
            if to_read == 0:
                return None  # exhausted before reading any prefix — stop
            prefix_raw = fh.read(to_read)
        else:
            prefix_raw = fh.read(_BOUNDARY_SUMMARY_PREFIX_BYTES)
    except (FileNotFoundError, OSError):
        if fault is not None:
            fault[0] = True
        return None
    text = prefix_raw.decode("utf-8", errors="replace")
    # Parse the prefix using the shared helper.
    return _parse_prefix_summary(text)


def _find_top_level_payload_key(text: str) -> int | None:
    """Return the byte offset of the top-level (depth-1) ``"payload"`` key in
    a JSON object prefix, or None if not found. Mirrors the depth-tracking
    approach of the session scanner but specialized for the journal record."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            # Parse the string token to get its content + end.
            i += 1
            start = i
            escaped = False
            while i < n:
                c = text[i]
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    break
                i += 1
            if i >= n:
                return None
            key = text[start:i]
            if depth == 1 and key == "payload":
                # Confirm it's a key (followed by optional ws + ':').
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] == ":":
                    return start - 1  # offset of the opening quote
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return None


def _read_jsonl_tail(
    path: Path, *, max_bytes: int | None, max_rows: int | None, attribute_lines: bool = True
) -> tuple[list[dict], list[dict], bool]:
    """Read the trailing portion of a JSONL journal (bounded memory).

    Seeks to (size - max_bytes) and reads forward, discarding the partial line
    at the seek boundary, then returns at most the last ``max_rows`` parsed
    events. ``line`` numbers in ``malformed`` are 1-based across the whole file.
    Used by summary readers that need the LAST events of a possibly huge journal
    (terminal_state / last_seq live in the tail).

    The file is opened ONCE and the size is pinned via os.fstat — ensuring all
    recovery helpers read from a single inode generation. A delete-and-recreate
    between stages cannot mix rows from different generations.

    Returns a 3-tuple (events, malformed, ok) where ``ok`` is False iff a
    transient OSError was caught during the boundary scan. (#6139 r14)
    """
    events: list[dict] = []
    malformed: list[dict] = []
    fault = [False]  # shared mutable flag for boundary-helper OSError tracking
    try:
        fh = path.open("rb")
    except (FileNotFoundError, OSError):
        return events, malformed, False
    try:
        try:
            size = os.fstat(fh.fileno()).st_size   # ONE pin; same generation for all stages
        except OSError:
            return events, malformed, False
        if size <= 0:
            return events, malformed, True
        read_bytes_cap = (
            max_bytes if (max_bytes is not None and max_bytes > 0)
            else _SESSION_REPLAY_MAX_BYTES
        )
        read_bytes = min(size, read_bytes_cap)
        rows_cap = max_rows if (max_rows is not None and max_rows > 0) else (1 << 62)
        if size > read_bytes:
            fh.seek(size - read_bytes)
        raw = fh.read(read_bytes)
        text = raw.decode("utf-8", errors="replace")
        # If we sought into the middle of the file, the window's first "line" is a
        # partial fragment of a record that STRADDLES the seek boundary. streaming.py
        # journals the terminal `done` event with the FULL transcript as its payload,
        # so that record can be many MB — bigger than the whole tail window. Two
        # sub-cases, both of which must not drop the straddling record's summary
        # (terminal_state / last_seq / last_event_id) or restart recovery misreports
        # a finished run as still-running:
        #   (a) nl >= 0: the straddling record's tail is at the start of the window
        #       and is followed by more complete records (e.g. the production order
        #       done(tool_limit_reached) -> metering -> stream_end). Slicing past
        #       the first newline loses the straddling record but keeps the rest.
        #   (b) nl < 0: the ENTIRE window is inside one oversized record (no newline
        #       at all), so there are no complete records in the window.
        # In both cases, recover the straddling record's summary via a BOUNDED prefix
        # read (_extract_boundary_record_summary): the record layout puts all summary
        # fields before "payload", so we read a few KB, truncate at "payload", and
        # parse the summary WITHOUT materializing the (multi-MB) payload. The
        # extracted summary is prepended to the events so _summary_from_events sees
        # both the straddling record's terminal state AND any trailing events.
        boundary_summary: dict | None = None
        if size > read_bytes:
            seek_pos = size - read_bytes
            # Boundary-recovery budget structure (#6139 r15 redesign):
            #   - recovery_budget (lookup + prefix + predecessor): sized to cover the
            #     backward lookup through the pre-window region (up to seek_pos bytes,
            #     since the straddling record's start can be as far back as byte 0 for
            #     a record that dominates the file), plus the 8 KiB prefix read and one
            #     predecessor line. The backward lookup scans pre-window bytes to find
            #     the record's start; for a huge straddling record that is inherently
            #     ~seek_pos bytes. This is bounded by the file size, not an arbitrary
            #     constant — a fixed cap here would reimpose the r15 blocker-1 ceiling
            #     (a 17 MiB valid done's start sat ~13 MiB back, exhausting a fixed cap).
            #   - validity_budget (streaming grammar proof): sized to the boundary
            #     record's ACTUAL extent (size - record_start), computed AFTER the
            #     lookup finds record_start. The streaming validator self-terminates
            #     at the record's newline (depth-0 + \n), so it reads exactly the
            #     record's bytes — never into trailing records. A VALID record of ANY
            #     size is accepted (no fixed correctness ceiling). A pathological
            #     multi-GB record fails-closed only if it can't reach its own newline
            #     within its extent (crash-truncated), never because of an arbitrary
            #     budget ceiling.
            recovery_budget = _ReadBudget(seek_pos + read_bytes_cap + _BOUNDARY_SUMMARY_PREFIX_BYTES)
            record_start = _find_record_start_before(fh, size, seek_pos, budget=recovery_budget, fault=fault)
            # record_start is where the straddling record begins. Extract its summary
            # via a bounded prefix read (never materializes the payload) — BUT only
            # trust it as terminal if the record is a structurally-complete AND
            # grammatically-valid JSON record. Brace balance is NOT JSON validity
            # (#6139 r10 item 2): a brace-balanced record with a trailing comma or a
            # malformed nested value must NOT be fabricated into a terminal event.
            boundary_summary = _extract_boundary_record_summary(fh, record_start, budget=recovery_budget, fault=fault)
            # A boundary record is REJECTED (must not be trusted as terminal, and
            # the preceding valid event must be recovered instead) on EITHER path:
            #   (a) the prefix summary could not be extracted at all (extraction
            #       returned None — the record is malformed before the top-level
            #       "payload" key, e.g. an unquoted token or a truncated head); OR
            #   (b) the prefix was extracted but the whole record is not valid JSON
            #       (a trailing comma, a malformed nested value) — fail-closed.
            # Both paths must trigger predecessor recovery: the valid terminal row
            # immediately before the rejected boundary lives OUTSIDE the tail
            # window and is otherwise dropped, so a completed run would be
            # misreported non-terminal (full reader `completed` → tail `running`,
            # the preceding `done` was lost).
            boundary_rejected = True
            if boundary_summary is not None:
                # Validate the whole record via streaming grammar check (the unified
                # _record_is_valid_jsonl replaces the old two-tier _record_is_valid_complete).
                # The validity budget is the record's own extent (size - record_start),
                # so a valid record of any size is accepted — no fixed ceiling.
                validity_budget = _ReadBudget(size - record_start)
                if _record_is_valid_jsonl(fh, size, record_start, budget=validity_budget, fault=fault):
                    boundary_rejected = False  # valid + complete: trust the prefix
                else:
                    boundary_summary = None  # extracted-but-invalid: fail-closed
            if boundary_rejected:
                # Retain the last COMPLETE valid event before the rejected boundary
                # record, so last_seq/terminal survive and the recovery signal fires
                # (matching master). The recovery_budget is threaded through
                # _read_last_complete_line_before, so the backward predecessor scan
                # counts against the SAME allowance as lookup + extract.
                preceding = _read_last_complete_line_before(fh, size, record_start, budget=recovery_budget, fault=fault)
                if preceding is not None:
                    events.append(preceding)
            # Now drop the partial first fragment from the window so we only parse
            # the complete trailing records.
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1:]
            else:
                text = ""  # entire window was inside the oversized record
        if boundary_summary is not None:
            events.append(boundary_summary)
        if not text.strip() and boundary_summary is None:
            # No straddling record recovered AND no complete lines in the window.
            # (When boundary_summary was recovered we already have it; an empty text
            # just means there were no trailing complete records, which is fine.)
            return events, malformed, not fault[0]
        # 1-based line number of the first whole line in `text`, across the whole
        # file. The discarded prefix (size - read_bytes bytes) contains some number
        # of complete lines; the first whole line in the window is the next one. We
        # must COUNT newlines in the discarded prefix — a byte offset is NOT a line
        # number (a 4 MB head with ~80 B/line has ~50000 lines, not ~4 M). Count by
        # streaming the head in chunks so a huge file doesn't get materialized twice.
        # Line attribution (1-based line numbers in `malformed`) requires counting
        # newlines in the discarded head — an O(file-size) scan. Summary readers
        # (`latest_run_summary` / `find_run_summary`) pass attribute_lines=False
        # because they DISCARD `malformed`, so the head scan is dead work there and
        # would make physical reads unbounded (the r11 blocker: a 72 MiB journal
        # read 79.7 MiB physically, 19x the 4 MiB tail cap, with this scan). When
        # attribution is disabled, skip the head scan entirely (base_start_line=0);
        # the relative line numbers are harmless since summary callers ignore them.
        head_bytes = size - read_bytes if size > read_bytes else 0
        if attribute_lines and head_bytes > 0:
            lines_before_window = 0
            try:
                fh.seek(0)
                _remaining = head_bytes
                while _remaining > 0:
                    _chunk = fh.read(min(_SESSION_REPLAY_READ_CHUNK_BYTES, _remaining))
                    if not _chunk:
                        break
                    lines_before_window += _chunk.count(b"\n")
                    _remaining -= len(_chunk)
            except (FileNotFoundError, OSError):
                lines_before_window = 0  # best-effort attribution; events are unaffected
            # The discarded head ended mid-line, so the partial line it left (line
            # lines_before_window + 1) was dropped above, making the first whole
            # line in `text` lines_before_window + 2.
            base_start_line = lines_before_window + 2
        elif attribute_lines:
            # No seek: whole file read, first line is 1.
            base_start_line = 1
        else:
            # Attribution disabled (summary path): skip the O(file-size) head scan.
            # malformed entries get relative numbers, which summary callers discard.
            base_start_line = 0
        # Split on \n only (NOT splitlines, which accepts bare \r). A crash-truncated
        # record ending in bare \r must not be parsed as a complete line.
        # First check: if the text doesn't end with \n, the last line is unterminated.
        text_ends_with_newline = text.endswith("\n")
        all_lines = text.split("\n")
        # Drop trailing empty string if text ended with \n.
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
        # If text didn't end with \n, the last "line" is unterminated (bare \r or
        # EOF) — discard it, matching the full reader's terminator gate.
        if not text_ends_with_newline and all_lines:
            all_lines.pop()
        # Keep only the last `rows_cap` lines so a huge tail window still bounds the
        # parsed-event list (and the JSON decode cost). If we trim lines from the
        # front, advance the starting line number by the trim count.
        trim_from_front = max(0, len(all_lines) - rows_cap)
        if trim_from_front:
            all_lines = all_lines[-rows_cap:]
        start_line = base_start_line + trim_from_front
        for idx, raw_line in enumerate(all_lines):
            line_no = start_line + idx
            if not raw_line.strip():
                continue
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed.append({"line": line_no, "raw": raw_line})
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
            else:
                malformed.append({"line": line_no, "raw": raw_line})
        return events, malformed, not fault[0]
    except (FileNotFoundError, OSError):
        # A read failure mid-recovery: return what we have (best-effort), but
        # mark the fault so callers (summary readers) don't cache a transient
        # failure as authoritative. fault[0] may still be False when the
        # OSError came from an UNGUARDed direct I/O call (the tail-window
        # fh.seek/fh.read near the top of the try body have no individual
        # try/except, so the boundary helpers never got to set fault[0]).
        # Set it here as the single chokepoint covering ALL escape paths.
        # (#6139 r14 finding 3 follow-up.)
        fault[0] = True
        return events, malformed, not fault[0]
    finally:
        fh.close()


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
    events, _malformed, _ok = _read_jsonl(path)
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

    # b3: Hold both locks across the entire critical section
    with _lock_for(path):
        with _journal_lock(path):
            sidecar_path = _summary_sidecar_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            created_file = not path.exists()

            # Open ONE pinned descriptor for the whole write (b3)
            wfd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            fh = None
            try:
                fh = os.fdopen(wfd, "a", encoding="utf-8")  # text mode preserves CRLF

                # b2a: pre_size from the pinned descriptor (not path.stat()). On
                # fault we still append (O_APPEND is safe) but publish no authority.
                pre_size = None
                try:
                    pre_size = _descriptor_size(fh)
                except OSError:
                    pre_size = None

                # Resolve the fold base for the sidecar (b1/b2a/b2b). One
                # if/elif chain: every branch sets ``state``, ``durable_max``,
                # and ``base_trusted`` exactly once, and a pre-state fault
                # (pre_size is None) is NEVER promoted to a trusted base.
                prior = _read_sidecar(sidecar_path)  # strictly validated; None if malformed
                base_trusted = False
                durable_max = 0
                state = _new_sidecar_state()
                if pre_size is None:
                    # b2a: cannot establish the pre-state generation on the
                    # pinned descriptor. Append the JSONL (O_APPEND is safe) but
                    # publish NO compact authority; readers keep falling back to
                    # the tail reader and a later append retries.
                    base_trusted = False
                elif (
                    prior is not None
                    and prior.get("session_id") == str(session_id)
                    and prior.get("run_id") == str(run_id)
                    and int(prior.get("journal_size", 0)) == int(pre_size)
                ):
                    # b1: a prior sidecar that names THIS run AND records the
                    # pinned pre-append size is a trusted incremental fold base.
                    state = {
                        "event_count": prior["event_count"],
                        "last": prior["last"],
                        "terminal": prior["terminal"],
                    }
                    durable_max = int((prior.get("last") or {}).get("seq") or 0)
                    base_trusted = True
                elif int(pre_size) > 0:
                    # b2b: no trusted sidecar for an existing run: rebuild from
                    # the bounded tail. The rebuild is a COMPLETE accounting ONLY
                    # if the whole journal fit in the byte window (max_rows=None
                    # makes the byte cap the sole bound); a partial/capped
                    # rebuild is folded but MUST NOT be published as authority.
                    tail_events, _tail_malformed, tail_ok = _read_jsonl_tail(
                        path,
                        max_bytes=_SESSION_REPLAY_MAX_BYTES,
                        max_rows=None,
                        attribute_lines=False,
                    )
                    for ev in tail_events:
                        _fold_event_into_state(state, ev)
                    base_trusted = bool(tail_ok) and int(pre_size) <= _SESSION_REPLAY_MAX_BYTES
                    durable_max = max((int(ev.get("seq") or 0) for ev in tail_events), default=0)
                else:
                    # pre_size == 0: a fresh, empty journal is a trusted base.
                    base_trusted = True

                # b3: reconcile the assigned seq against the durable journal max
                # so a stale per-process cache cannot re-issue a seq another
                # process already appended. ``_reserve_next_seq`` seeds the
                # in-memory cache from disk exactly once per path (the seed-once
                # contract); ``durable_max`` (from the trusted sidecar / rebuild)
                # floors it so cross-process appends stay monotonic and gapless.
                # A caller-supplied seq is floored for the same reason.
                if seq is not None:
                    assigned_seq = max(int(seq), int(durable_max) + 1)
                else:
                    assigned_seq = max(_reserve_next_seq(path), int(durable_max) + 1)
                _note_assigned_seq(path, assigned_seq)

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

                # Write the JSONL line
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                fh.write(line)
                fh.flush()
                if _should_fsync_event(terminal_state):
                    os.fsync(fh.fileno())

                # Post-state size from the SAME pinned descriptor (b1 generation pin).
                post_size = None
                try:
                    post_size = _descriptor_size(fh)
                except OSError:
                    post_size = None

            finally:
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass

            # Fold the new event and publish sidecar only from a trusted base
            _fold_event_into_state(state, event)
            if base_trusted and post_size is not None:
                try:
                    payload_dict = _serialize_sidecar(
                        state, post_size, session_id=session_id, run_id=run_id
                    )
                    _atomic_write_json(
                        sidecar_path, payload_dict, fsync=_should_fsync_event(terminal_state)
                    )
                except OSError:
                    pass

            # Discard the cached summary
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
        # b3: Thin wrapper calling append_run_event directly (seq=None)
        # The seq is reserved inside append_run_event under the locks.
        return append_run_event(
            self.session_id,
            self.run_id,
            event_name,
            payload or {},
            session_dir=self.session_dir,
            seq=None,
        )


def read_run_events(
    session_id: str,
    run_id: str,
    *,
    after_seq: int | None = None,
    max_seq: int | None = None,
    session_dir: Path | None = None,
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    events, malformed, _ok = _read_jsonl(path, max_bytes=max_bytes, max_rows=max_rows)
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


def latest_run_summary(
    session_id: str,
    run_id: str,
    *,
    session_dir: Path | None = None,
    max_bytes: int | None = _SESSION_REPLAY_MAX_BYTES,
    max_rows: int | None = _SESSION_REPLAY_MAX_ROWS,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    cached = _get_cached_summary(path)
    if cached is not None:
        return cached
    # Capture the pre-read signature BEFORE any read so a concurrent append
    # during the sidecar/tail read is detected and the stale result is NOT
    # cached under the post-append signature (#6139 r14 finding 3; the sidecar
    # path reuses the same guard so the TOCTOU contract holds on both paths).
    pre_read_signature = _summary_cache_signature(path)
    # Try the compact sidecar first (O(1) read, no transcript-payload scan).
    sidecar_summary, _sidecar_ok = _try_summary_from_sidecar(path, session_id, run_id)
    if sidecar_summary is not None:
        _cache_summary(path, sidecar_summary, expected_signature=pre_read_signature)
        return sidecar_summary
    # FALLBACK: existing tail-reader path unchanged.
    events, _malformed, ok = _read_jsonl(
        path, max_bytes=max_bytes, max_rows=max_rows, tail=True, attribute_lines=False
    )
    summary = _summary_from_events(session_id, run_id, events)
    # #6139 r14 finding 3: a transient OSError during the boundary scan produces
    # a best-effort/failed summary (terminal_state="unknown", empty events).
    # Caching it under the matching inode signature would make subsequent calls
    # return the stale failure instead of retrying. Skip caching when the read
    # faulted so the next call retries and recovers.
    if ok:
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


def find_run_summary(
    run_id: str,
    *,
    session_dir: Path | None = None,
    max_bytes: int | None = _SESSION_REPLAY_MAX_BYTES,
    max_rows: int | None = _SESSION_REPLAY_MAX_ROWS,
) -> dict | None:
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        session_id = path.parent.name
        summary = _get_cached_summary(path)
        if summary is None:
            # Capture the pre-read signature BEFORE any read so a concurrent
            # append during the sidecar/tail read is detected (#6139 r14 finding 3).
            pre_read_signature = _summary_cache_signature(path)
            # Try the compact sidecar first (O(1) read, no transcript-payload scan).
            sidecar_summary, _sidecar_ok = _try_summary_from_sidecar(path, session_id, rid)
            if sidecar_summary is not None:
                _cache_summary(path, sidecar_summary, expected_signature=pre_read_signature)
                summary = sidecar_summary
            else:
                # FALLBACK: existing tail-reader path unchanged.
                # Tail read: summary needs the terminal/last events (see
                # latest_run_summary), so bound memory on large completed runs.
                events, _malformed, ok = _read_jsonl(
                    path, max_bytes=max_bytes, max_rows=max_rows, tail=True, attribute_lines=False
                )
                summary = _summary_from_events(session_id, rid, events)
                # #6139 r14 finding 3: a transient OSError during the boundary scan
                # produces a best-effort/failed summary. Caching it under the matching
                # inode signature would make subsequent calls return the stale failure
                # instead of retrying. Skip caching when the read faulted.
                if ok:
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
