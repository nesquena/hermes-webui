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
_TERMINAL_SSE_EVENTS = {"done", "cancel", "apperror", "error", "stream_end"}
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


def _read_jsonl(
    path: Path,
    *,
    max_bytes: int | None = None,
    max_rows: int | None = None,
    tail: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Read a run-journal JSONL file into (events, malformed).

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
    - ``tail=False`` with caps: read forward but stop once ``max_bytes``/``max_rows``
      is exceeded (head cap), via the existing bounded line iterator.

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
        return _read_jsonl_tail(path, max_bytes=max_bytes, max_rows=max_rows)

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
            return events, malformed
        except ValueError:
            # _iter_bounded_raw_jsonl_lines raises "replay_limit_bytes" once the
            # byte cap is exceeded; the events collected so far are returned.
            return events, malformed
        return events, malformed

    # Unbounded whole-file read (original behavior).
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


# Bounded prefix read for oversized journal records. The record layout from
# append_run_event puts ALL summary fields before the (potentially huge) payload:
#   {"version","event_id","seq","run_id","session_id","event","type","created_at",
#    "terminal","terminal_state","payload":{...huge...}}
# So we can read a small prefix, truncate at the "payload" key, close the object,
# and parse the summary fields WITHOUT materializing the payload. This bounds
# memory even when a single record (e.g. the terminal `done` with the full
# transcript) is many MB.
_BOUNDARY_SUMMARY_PREFIX_BYTES = 8192


def _find_record_start_before(path: Path, seek_pos: int) -> int:
    """Return the byte offset where the JSONL record overlapping ``seek_pos``
    begins, i.e. the byte just after the last newline strictly before seek_pos.
    Returns 0 if there is no preceding newline (the record starts at byte 0).
    Scans backward in bounded chunks."""
    if seek_pos <= 0:
        return 0
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return 0
    pos = min(seek_pos, size)
    with path.open("rb") as fh:
        while pos > 0:
            read_from = max(0, pos - chunk_size)
            fh.seek(read_from)
            block = fh.read(pos - read_from)
            nl = block.rfind(b"\n")
            if nl >= 0:
                return read_from + nl + 1
            pos = read_from
    return 0


def _record_is_structurally_complete(path: Path, record_start: int) -> bool:
    """Return True iff the JSONL record at ``record_start`` is structurally
    complete — i.e. its JSON object is closed (brace depth returns to 0) AND
    followed by a newline terminator — scanning forward in bounded chunks WITHOUT
    materializing the (potentially multi-MB) payload.

    Used to gate trusting a fabricated prefix summary: a crash-truncated
    ``done`` (write interrupted mid-payload, no closing brace/newline) must NOT
    be accepted as terminal, or an interrupted run is misreported as completed
    and its recovery signal is silently dropped. Returns False if EOF is reached
    at brace depth > 0 (the record was truncated mid-write).
    """
    chunk_size = _SESSION_REPLAY_READ_CHUNK_BYTES
    depth = 0
    pos = record_start
    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return False
    in_string = False
    escaped = False
    with path.open("rb") as fh:
        fh.seek(record_start)
        while pos < size:
            chunk = fh.read(min(chunk_size, size - pos))
            if not chunk:
                break
            chunk_len = len(chunk)
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
                            return nb in (0x0A, 0x0D)  # \n or \r
                        # Terminator is in the next chunk: read 1 byte from file.
                        # (fh cursor is at pos + (chunk_len - ci - 1); seek to pos.)
                        fh.seek(pos)
                        nb = fh.read(1)
                        return nb in (b"\n", b"\r") or (nb == b"" and pos >= size)
                elif b == 0x0A and depth == 0:  # newline at depth 0 before close
                    return False
            # depth > 0 here means the record spans more chunks; keep scanning.
        # Reached EOF: complete only if the object closed exactly at EOF (depth 0).
        return depth == 0


def _extract_boundary_record_summary(path: Path, record_start: int) -> dict | None:
    """Extract ONLY the summary fields of an oversized journal record that
    straddles the tail-window boundary, without materializing its payload.

    Reads a bounded prefix (``_BOUNDARY_SUMMARY_PREFIX_BYTES``) from
    ``record_start``, locates the top-level ``"payload"`` key via a brace-depth
    scan, truncates the JSON before it, closes the object, and parses. Returns
    a dict with the summary fields (``event``/``seq``/``event_id``/``terminal``/
    ``terminal_state``) or ``None`` if the layout is unexpected. The payload is
    replaced with an empty dict so downstream consumers see the shape but not
    the bytes.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(record_start)
            prefix_raw = fh.read(_BOUNDARY_SUMMARY_PREFIX_BYTES)
    except (FileNotFoundError, OSError):
        return None
    text = prefix_raw.decode("utf-8", errors="replace")
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
    parsed["_summary_extracted_from_oversized_record"] = True
    return parsed


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
    path: Path, *, max_bytes: int | None, max_rows: int | None
) -> tuple[list[dict], list[dict]]:
    """Read the trailing portion of a JSONL journal (bounded memory).

    Seeks to (size - max_bytes) and reads forward, discarding the partial line
    at the seek boundary, then returns at most the last ``max_rows`` parsed
    events. ``line`` numbers in ``malformed`` are 1-based across the whole file.
    Used by summary readers that need the LAST events of a possibly huge journal
    (terminal_state / last_seq live in the tail).
    """
    events: list[dict] = []
    malformed: list[dict] = []
    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return events, malformed
    if size <= 0:
        return events, malformed
    read_bytes_cap = (
        max_bytes if (max_bytes is not None and max_bytes > 0)
        else _SESSION_REPLAY_MAX_BYTES
    )
    read_bytes = min(size, read_bytes_cap)
    rows_cap = max_rows if (max_rows is not None and max_rows > 0) else (1 << 62)
    try:
        with path.open("rb") as fh:
            if size > read_bytes:
                fh.seek(size - read_bytes)
            raw = fh.read(read_bytes)
    except (FileNotFoundError, OSError):
        return events, malformed
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
        record_start = _find_record_start_before(path, seek_pos)
        # record_start is where the straddling record begins. Extract its summary
        # via a bounded prefix read (never materializes the payload) — BUT only
        # trust it as terminal if the record is structurally complete. A crash-
        # truncated `done` (write interrupted mid-payload: no closing brace, no
        # newline) must NOT be fabricated into a terminal event, or an interrupted
        # run is misreported as completed and its apperror recovery signal is
        # silently dropped. Stale-but-nonterminal is recoverable; falsely-terminal
        # is not. If incomplete, discard the summary and fall through to the
        # preceding complete records (the run stays nonterminal/`running`).
        boundary_summary = _extract_boundary_record_summary(path, record_start)
        if boundary_summary is not None and not _record_is_structurally_complete(path, record_start):
            boundary_summary = None  # crash-truncated record: don't trust its prefix
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
        return events, malformed
    # 1-based line number of the first whole line in `text`, across the whole
    # file. The discarded prefix (size - read_bytes bytes) contains some number
    # of complete lines; the first whole line in the window is the next one. We
    # must COUNT newlines in the discarded prefix — a byte offset is NOT a line
    # number (a 4 MB head with ~80 B/line has ~50000 lines, not ~4 M). Count by
    # streaming the head in chunks so a huge file doesn't get materialized twice.
    head_bytes = size - read_bytes if size > read_bytes else 0
    lines_before_window = 0
    if head_bytes > 0:
        try:
            with path.open("rb") as _hf:
                _remaining = head_bytes
                while _remaining > 0:
                    _chunk = _hf.read(min(_SESSION_REPLAY_READ_CHUNK_BYTES, _remaining))
                    if not _chunk:
                        break
                    lines_before_window += _chunk.count(b"\n")
                    _remaining -= len(_chunk)
        except (FileNotFoundError, OSError):
            lines_before_window = 0  # best-effort attribution; events are unaffected
    # `text`'s first whole line in the file: the discarded head ended mid-line,
    # so the partial line it left (line `lines_before_window + 1`) was dropped
    # above, making the first whole line `lines_before_window + 2`. When there
    # was no seek (whole file read), the first line is 1.
    base_start_line = lines_before_window + 2 if head_bytes > 0 else 1
    all_lines = text.splitlines()
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
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    events, malformed = _read_jsonl(path, max_bytes=max_bytes, max_rows=max_rows)
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
    # Summary derives last_seq / last_event_id / terminal_state from the LAST
    # events, so read the bounded TAIL (not the whole file) — a large completed
    # run's terminal marker lives at the end and must not require parsing the
    # full history. Callers needing head/all events use read_run_events().
    pre_read_signature = _summary_cache_signature(path)
    events, _malformed = _read_jsonl(
        path, max_bytes=max_bytes, max_rows=max_rows, tail=True
    )
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
            pre_read_signature = _summary_cache_signature(path)
            # Tail read: summary needs the terminal/last events (see
            # latest_run_summary), so bound memory on large completed runs.
            events, _malformed = _read_jsonl(
                path, max_bytes=max_bytes, max_rows=max_rows, tail=True
            )
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
