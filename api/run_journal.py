"""Append-only WebUI run event journal helpers.

This is the first #1925 journal/replay slice.  It mirrors SSE events emitted by
the existing in-process streaming path without changing execution ownership.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - platform-specific imports.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

try:  # pragma: no cover - platform-specific imports.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None

RUN_JOURNAL_DIR_NAME = "_run_journal"
TERMINAL_RUN_INDEX_NAME = "_terminal_runs.jsonl"
TERMINAL_RUN_INDEX_AUTHORITY_NAME = "_terminal_runs.authority.json"
TERMINAL_RUN_INDEX_AUTHORITY_VERSION = "terminal_index_authority_v1"
TERMINAL_RUN_INDEX_COMPACTED_MARKER_VERSION = "terminal_index_compacted_v1"
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
_RUN_EVENTS_MAX_BYTES = 2 * 1024 * 1024
_RUN_EVENTS_MAX_ROWS = 2048
_TERMINAL_INDEX_MAX_BYTES = 512 * 1024
_TERMINAL_INDEX_MAX_ROWS = 1024
_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES = _TERMINAL_INDEX_MAX_BYTES * 2
_TERMINAL_INDEX_DIGEST_SAMPLE_BYTES = 16 * 1024
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


def _terminal_index_path(session_id: str, session_dir: Path | None = None) -> Path:
    sid = _validate_id(session_id, "session_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    return root / RUN_JOURNAL_DIR_NAME / sid / TERMINAL_RUN_INDEX_NAME


def _terminal_index_authority_path(index_path: Path) -> Path:
    return index_path.with_name(TERMINAL_RUN_INDEX_AUTHORITY_NAME)


def _lock_for(path: Path) -> threading.Lock:
    key = (str(path.parent), path.name, str(os.getpid()))
    with _WRITER_LOCKS_GUARD:
        lock = _WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITER_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _terminal_index_process_lock(path: Path):
    """Serialize terminal-index append/replace across WebUI processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return
        if _msvcrt is not None:
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            _msvcrt.locking(  # type: ignore[attr-defined]
                lock_file.fileno(), _msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
            )
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), _msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            return
        raise RuntimeError("cross-process terminal index locking is unavailable")


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
) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    malformed: list[dict] = []
    row_count = 0

    def parse_row(line_no: int, raw_bytes: bytes) -> bool:
        nonlocal row_count
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            malformed.append({"line": line_no, "raw": ""})
            return True
        if not raw.strip():
            return True
        row_count += 1
        if max_rows is not None and row_count > max(0, int(max_rows)):
            malformed.append({"line": line_no, "reason": "replay_limit_rows"})
            return False
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append({"line": line_no, "raw": raw.rstrip("\n")})
            return True
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed.append({"line": line_no, "raw": raw.rstrip("\n")})
        return True

    if max_bytes is not None:
        try:
            for line_no, raw_bytes, _total_bytes in _iter_bounded_raw_jsonl_lines(
                path,
                max_bytes=max(0, int(max_bytes)),
            ):
                if not parse_row(line_no, raw_bytes):
                    break
        except ValueError as exc:
            malformed.append({"line": None, "reason": str(exc) or "replay_limit_bytes"})
        return events, malformed

    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return events, malformed
    with fh:
        for line_no, raw_bytes in enumerate(fh, start=1):
            if not parse_row(line_no, raw_bytes):
                break
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


def _terminal_index_authority_from_raw(raw: object) -> dict | None:
    if not isinstance(raw, dict) or raw.get("version") != TERMINAL_RUN_INDEX_AUTHORITY_VERSION:
        return None
    epoch = str(raw.get("epoch") or "").strip()
    if not epoch:
        return None
    try:
        mutation_seq = int(raw.get("mutation_seq"))
    except (TypeError, ValueError):
        return None
    if mutation_seq < 0:
        return None
    return {
        "version": TERMINAL_RUN_INDEX_AUTHORITY_VERSION,
        "epoch": epoch,
        "mutation_seq": mutation_seq,
    }


def _terminal_index_new_authority() -> dict:
    return {
        "version": TERMINAL_RUN_INDEX_AUTHORITY_VERSION,
        "epoch": uuid.uuid4().hex,
        "mutation_seq": 0,
    }


def _terminal_index_read_authority_locked(path: Path) -> dict | None:
    try:
        raw = json.loads(_terminal_index_authority_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _terminal_index_authority_from_raw(raw)


def _terminal_index_write_authority_locked(path: Path, authority: dict) -> None:
    tmp_path = path.with_name(f".{TERMINAL_RUN_INDEX_AUTHORITY_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(authority, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, _terminal_index_authority_path(path))
    _fsync_parent_dir(path)


def _terminal_index_current_authority_locked(path: Path, *, create_missing: bool = True) -> dict:
    authority = _terminal_index_read_authority_locked(path)
    if authority is not None:
        return authority
    authority = _terminal_index_new_authority()
    if create_missing:
        _terminal_index_write_authority_locked(path, authority)
    return authority


def _terminal_index_next_authority(authority: dict | None) -> dict:
    current = _terminal_index_authority_from_raw(authority) or _terminal_index_new_authority()
    return {
        "version": TERMINAL_RUN_INDEX_AUTHORITY_VERSION,
        "epoch": current["epoch"],
        "mutation_seq": int(current["mutation_seq"]) + 1,
    }


def _terminal_index_digest_for_open_file(fh, size: int) -> str:
    hasher = hashlib.blake2b(digest_size=16)
    bounded_size = max(0, int(size))
    hasher.update(str(bounded_size).encode("ascii"))
    if bounded_size <= 0:
        return hasher.hexdigest()
    sample = max(1, int(_TERMINAL_INDEX_DIGEST_SAMPLE_BYTES))
    ranges = [(0, min(sample, bounded_size))]
    tail_start = max(0, bounded_size - sample)
    if tail_start > ranges[0][1]:
        ranges.append((tail_start, bounded_size))
    original_pos = fh.tell()
    try:
        for start, end in ranges:
            fh.seek(start)
            hasher.update(fh.read(end - start))
    finally:
        fh.seek(original_pos)
    return hasher.hexdigest()


def _terminal_index_generation_from_open_file(fh, *, authority: dict | None = None) -> dict[str, int | str | dict]:
    stat_result = os.fstat(fh.fileno())
    size = max(0, int(stat_result.st_size))
    return _terminal_index_generation_from_stat(
        stat_result,
        authority=authority,
        digest=_terminal_index_digest_for_open_file(fh, size),
    )


def _terminal_index_offset_is_boundary(src, end_offset: int) -> bool:
    if end_offset <= 0:
        return True
    try:
        src.seek(end_offset - 1)
        return src.read(1) == b"\n"
    except OSError:
        return False


def _append_terminal_run_index(
    session_id: str,
    run_id: str,
    event: dict,
    *,
    session_dir: Path | None = None,
) -> None:
    path = _terminal_index_path(session_id, session_dir=session_dir)
    entry = {
        "version": 1,
        "session_id": str(session_id),
        "run_id": str(run_id),
        "stream_id": str(run_id),
        "last_seq": event.get("seq"),
        "last_event_id": event.get("event_id"),
        "terminal": True,
        "terminal_state": event.get("terminal_state"),
        "last_event": event.get("event"),
        "created_at": event.get("created_at"),
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    created_file = False
    with _lock_for(path):
        with _terminal_index_process_lock(path):
            authority = _terminal_index_current_authority_locked(path, create_missing=False)
            created_file = not path.exists()
            needs_separator = False
            try:
                if path.stat().st_size > 0:
                    read_fd = os.open(path, os.O_RDONLY)
                    try:
                        os.lseek(read_fd, -1, os.SEEK_END)
                        needs_separator = os.read(read_fd, 1) != b"\n"
                    finally:
                        os.close(read_fd)
            except OSError:
                needs_separator = False
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                if needs_separator:
                    fh.write("\n")
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            _terminal_index_write_authority_locked(path, _terminal_index_next_authority(authority))
    if created_file:
        _fsync_parent_dir(path)


def terminal_run_index_size_for_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
) -> int:
    try:
        path = _terminal_index_path(session_id, session_dir=session_dir)
    except ValueError:
        return 0
    try:
        return max(0, int(path.stat().st_size))
    except OSError:
        return 0


def _terminal_index_generation_from_stat(
    stat_result,
    *,
    authority: dict | None = None,
    digest: str | None = None,
) -> dict[str, int | str | dict]:
    generation: dict[str, int | str | dict] = {
        "dev": int(stat_result.st_dev),
        "ino": int(stat_result.st_ino),
        "size": max(0, int(stat_result.st_size)),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "ctime_ns": int(stat_result.st_ctime_ns),
    }
    clean_digest = str(digest or "").strip().lower()
    if clean_digest:
        generation["digest"] = clean_digest
    clean_authority = _terminal_index_authority_from_raw(authority)
    if clean_authority is not None:
        generation["authority"] = clean_authority
    return generation


def _terminal_index_generation_from_cursor(cursor: dict | None) -> dict[str, int | str | dict] | None:
    if not isinstance(cursor, dict):
        return None
    raw = cursor.get("generation")
    if not isinstance(raw, dict):
        return None
    generation: dict[str, int | str | dict] = {}
    for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns"):
        try:
            generation[key] = int(raw.get(key))
        except (TypeError, ValueError):
            return None
    generation["size"] = max(0, int(generation["size"]))
    raw_digest = raw.get("digest")
    if raw_digest is not None:
        digest = str(raw_digest or "").strip().lower()
        if len(digest) != 32 or any(char not in "0123456789abcdef" for char in digest):
            return None
        generation["digest"] = digest
    raw_authority = raw.get("authority")
    if raw_authority is not None:
        authority = _terminal_index_authority_from_raw(raw_authority)
        if authority is None:
            return None
        generation["authority"] = authority
    return generation


def _terminal_index_cursor_end_offset(cursor: dict | None, index_size: int) -> int | None:
    if not isinstance(cursor, dict):
        return None
    try:
        end_offset = int(cursor.get("end_offset"))
    except (TypeError, ValueError):
        return None
    return max(0, min(max(0, int(index_size)), end_offset))


def _terminal_index_window(
    path: Path,
    *,
    max_bytes: int,
    end_offset: int | None = None,
    index_cursor: dict | None = None,
) -> dict | None:
    with _lock_for(path):
        with _terminal_index_process_lock(path):
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                return None
            with os.fdopen(fd, "rb", closefd=True) as fh:
                try:
                    authority = _terminal_index_current_authority_locked(path)
                    generation = _terminal_index_generation_from_open_file(fh, authority=authority)
                except OSError:
                    return None
                size = int(generation["size"])
                cursor_generation = _terminal_index_generation_from_cursor(index_cursor)
                if cursor_generation is not None and cursor_generation == generation:
                    cursor_end = _terminal_index_cursor_end_offset(index_cursor, size)
                    end = size if cursor_end is None else cursor_end
                else:
                    try:
                        end = size if end_offset is None else max(0, min(int(end_offset), int(size)))
                    except (TypeError, ValueError):
                        end = size
                if end <= 0:
                    return {
                        "data_start": 0,
                        "read_end": 0,
                        "data": b"",
                        "index_generation": generation,
                        "index_size": size,
                        "next_end_offset": None,
                        "exhausted": True,
                    }
                byte_limit = max(0, int(max_bytes))
                start = max(0, end - byte_limit)
                try:
                    fh.seek(start)
                    data = fh.read(end - start)
                except OSError:
                    return None
    if start and data:
        newline = data.find(b"\n")
        if newline < 0:
            # The tail begins inside a row larger than the allowed window. Skip
            # this bounded extent instead of buffering forward to find its end;
            # the next page can continue before ``start``.
            return {
                "data_start": end,
                "read_end": end,
                "data": b"",
                "index_generation": generation,
                "index_size": size,
                "next_end_offset": start,
                "exhausted": start <= 0,
            }
        start += newline + 1
        data = data[newline + 1 :]
    return {
        "data_start": start,
        "read_end": end,
        "data": data,
        "index_generation": generation,
        "index_size": size,
        "next_end_offset": None,
        "exhausted": start <= 0,
    }


def _terminal_index_entry_page(
    path: Path,
    *,
    max_bytes: int,
    max_rows: int,
    end_offset: int | None = None,
    index_cursor: dict | None = None,
) -> dict | None:
    window = _terminal_index_window(
        path,
        max_bytes=max_bytes,
        end_offset=end_offset,
        index_cursor=index_cursor,
    )
    if window is None:
        return None
    start = int(window.get("data_start") or 0)
    data = window.get("data") if isinstance(window.get("data"), bytes) else b""
    rows: list[tuple[int, int, bytes]] = []
    pos = 0
    while pos < len(data):
        newline = data.find(b"\n", pos)
        if newline < 0:
            raw = data[pos:]
            row_start = start + pos
            row_end = start + len(data)
            pos = len(data)
        else:
            raw = data[pos:newline]
            row_start = start + pos
            row_end = start + newline + 1
            pos = newline + 1
        rows.append((row_start, row_end, raw))
    parsed_rows: list[tuple[int, int, dict | None]] = []
    row_limit = max(0, int(max_rows))
    if row_limit > 0:
        for row_start, row_end, raw in reversed(rows[-row_limit:]):
            if not raw.strip():
                parsed_rows.append((row_start, row_end, None))
                continue
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed_rows.append((row_start, row_end, None))
                continue
            parsed_rows.append((row_start, row_end, parsed if isinstance(parsed, dict) else None))
    return {
        "rows": parsed_rows,
        "index_generation": window.get("index_generation"),
        "index_size": int(window.get("index_size") or 0),
        "next_index_end_offset": window.get("next_end_offset"),
        "exhausted": bool(window.get("exhausted")),
    }


def _terminal_index_entry_rows(
    path: Path,
    *,
    max_bytes: int,
    max_rows: int,
    end_offset: int | None = None,
    index_cursor: dict | None = None,
):
    page = _terminal_index_entry_page(
        path,
        max_bytes=max_bytes,
        max_rows=max_rows,
        end_offset=end_offset,
        index_cursor=index_cursor,
    )
    if page is None:
        return
    for row_start, row_end, parsed in page.get("rows") or []:
        yield row_start, row_end, parsed


def _iter_terminal_index_entries(
    path: Path,
    *,
    max_bytes: int,
    max_rows: int,
    end_offset: int | None = None,
):
    for _row_start, _row_end, parsed in _terminal_index_entry_rows(
        path,
        max_bytes=max_bytes,
        max_rows=max_rows,
        end_offset=end_offset,
    ) or []:
        if isinstance(parsed, dict):
            yield parsed


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
        if terminal_state:
            _append_terminal_run_index(
                session_id,
                run_id,
                event,
                session_dir=session_dir,
            )
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
    max_bytes: int | None = _RUN_EVENTS_MAX_BYTES,
    max_rows: int | None = _RUN_EVENTS_MAX_ROWS,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    events: list[dict] = []
    malformed: list[dict] = []
    emitted_rows = 0
    emitted_bytes = 0
    complete = True
    limit_reason: str | None = None
    next_after_seq = int(after_seq) if after_seq is not None else None
    floor = int(after_seq) if after_seq is not None else None
    ceiling = int(max_seq) if max_seq is not None else None
    row_cap = None if max_rows is None else max(0, int(max_rows))
    byte_cap = None if max_bytes is None else max(0, int(max_bytes))
    if ceiling is not None and ceiling <= (floor or 0):
        return {
            "session_id": str(session_id),
            "run_id": str(run_id),
            "events": events,
            "malformed": malformed,
            "complete": True,
            "limit_reason": None,
            "next_after_seq": next_after_seq,
        }
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return {
            "session_id": str(session_id),
            "run_id": str(run_id),
            "events": events,
            "malformed": malformed,
            "complete": True,
            "limit_reason": None,
            "next_after_seq": next_after_seq,
        }
    with fh:
        for line_no, raw_bytes in enumerate(fh, 1):
            if not raw_bytes.strip():
                continue
            if (
                (floor is None or floor <= 0)
                and byte_cap is not None
                and emitted_bytes + len(raw_bytes) > byte_cap
            ):
                limit_reason = "replay_limit_bytes"
                malformed.append({"line": None, "reason": limit_reason})
                complete = False
                break
            try:
                raw = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                malformed.append({"line": line_no, "raw": ""})
                complete = False
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                malformed.append({"line": line_no, "raw": raw.rstrip("\n")})
                complete = False
                continue
            if not isinstance(parsed, dict):
                malformed.append({"line": line_no, "raw": raw.rstrip("\n")})
                complete = False
                continue
            try:
                seq = int(parsed.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if floor is not None and seq <= floor:
                continue
            if ceiling is not None and seq > ceiling:
                break
            if row_cap is not None and emitted_rows >= row_cap:
                limit_reason = "replay_limit_rows"
                malformed.append({"line": line_no, "reason": limit_reason})
                complete = False
                break
            if byte_cap is not None and emitted_bytes + len(raw_bytes) > byte_cap:
                limit_reason = "replay_limit_bytes"
                malformed.append({"line": None, "reason": limit_reason})
                complete = False
                break
            events.append(parsed)
            emitted_rows += 1
            emitted_bytes += len(raw_bytes)
            next_after_seq = seq
    return {
        "session_id": str(session_id),
        "run_id": str(run_id),
        "events": events,
        "malformed": malformed,
        "complete": complete and not malformed,
        "limit_reason": limit_reason,
        "next_after_seq": next_after_seq,
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
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    cache_allowed = max_bytes is None and max_rows is None
    cached = _get_cached_summary(path) if cache_allowed else None
    if cached is not None:
        return cached
    pre_read_signature = _summary_cache_signature(path)
    if max_bytes is None and max_rows is None:
        events, _malformed = _read_jsonl(path)
    else:
        events, _malformed = _read_jsonl(path, max_bytes=max_bytes, max_rows=max_rows)
    summary = _summary_from_events(session_id, run_id, events)
    if cache_allowed:
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
        if path.name == TERMINAL_RUN_INDEX_NAME:
            continue
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
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> dict | None:
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        session_id = path.parent.name
        cache_allowed = max_bytes is None and max_rows is None
        summary = _get_cached_summary(path) if cache_allowed else None
        if summary is None:
            pre_read_signature = _summary_cache_signature(path)
            if max_bytes is None and max_rows is None:
                events, _malformed = _read_jsonl(path)
            else:
                events, _malformed = _read_jsonl(path, max_bytes=max_bytes, max_rows=max_rows)
            summary = _summary_from_events(session_id, rid, events)
            if cache_allowed:
                _cache_summary(path, summary, expected_signature=pre_read_signature)
        summary["path"] = str(path)
        return summary
    return None


def latest_terminal_run_summary_for_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
) -> dict | None:
    """Return the newest terminal run journal summary for one session."""
    summaries = terminal_run_summaries_for_session(
        session_id,
        session_dir=session_dir,
        limit=1,
    )
    return summaries[0] if summaries else None


def _terminal_index_summary_from_entry(
    entry: dict,
    *,
    session_id: str,
    session_root: Path,
) -> dict | None:
    if entry.get("version") != 1:
        return None
    raw_session_id = entry.get("session_id")
    if not isinstance(raw_session_id, str) or raw_session_id.strip() != session_id:
        return None
    try:
        run_id = _validate_id(str(entry.get("run_id") or ""), "run_id")
    except ValueError:
        return None
    try:
        stream_id = _validate_id(str(entry.get("stream_id") or run_id), "stream_id")
    except ValueError:
        return None
    last_seq = entry.get("last_seq")
    if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq <= 0:
        return None
    last_event_id = str(entry.get("last_event_id") or "").strip()
    if last_event_id != f"{run_id}:{last_seq}":
        return None
    if entry.get("terminal") is not True:
        return None
    return {
        "session_id": session_id,
        "run_id": run_id,
        "stream_id": stream_id,
        "event_count": int(entry.get("event_count") or 0),
        "last_seq": last_seq,
        "last_event_id": last_event_id,
        "terminal": True,
        "terminal_state": entry.get("terminal_state"),
        "last_event": entry.get("last_event"),
        "path": str(session_root / f"{run_id}.jsonl"),
    }


def _terminal_index_compacted_marker() -> bytes:
    row = {
        "version": TERMINAL_RUN_INDEX_COMPACTED_MARKER_VERSION,
        "compacted": True,
    }
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def compact_terminal_run_index_for_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    index_cursor: dict | None = None,
) -> bool:
    """Drop only terminal-index rows durably acknowledged by reconciliation."""
    try:
        path = _terminal_index_path(session_id, session_dir=session_dir)
    except ValueError:
        return False
    cursor_generation = _terminal_index_generation_from_cursor(index_cursor)
    if cursor_generation is None or not isinstance(index_cursor, dict):
        return False
    try:
        cursor_index_size = max(0, int(index_cursor.get("index_size") or 0))
        cursor_end_offset = int(index_cursor.get("end_offset"))
    except (TypeError, ValueError):
        return False
    if cursor_index_size <= 0 or int(cursor_generation.get("size") or 0) != cursor_index_size:
        return False

    with _lock_for(path):
        with _terminal_index_process_lock(path):
            try:
                read_fd = os.open(path, os.O_RDONLY)
            except OSError:
                return False
            with os.fdopen(read_fd, "rb", closefd=True) as src:
                try:
                    authority = _terminal_index_current_authority_locked(path)
                    current_generation = _terminal_index_generation_from_open_file(src, authority=authority)
                except OSError:
                    return False
                current_size = int(current_generation.get("size") or 0)
                if current_generation != cursor_generation:
                    return False
                if current_size <= _TERMINAL_INDEX_COMPACT_TRIGGER_BYTES:
                    return False
                end_offset = max(0, min(current_size, cursor_end_offset))
                if end_offset >= current_size:
                    return False
                if not _terminal_index_offset_is_boundary(src, end_offset):
                    return False
                tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                try:
                    with tmp_path.open("wb") as dst:
                        if end_offset <= 0:
                            dst.write(_terminal_index_compacted_marker())
                        else:
                            src.seek(0)
                            remaining = end_offset
                            while remaining > 0:
                                chunk = src.read(min(_SESSION_REPLAY_READ_CHUNK_BYTES, remaining))
                                if not chunk:
                                    raise OSError("short terminal-index prefix read")
                                dst.write(chunk)
                                remaining -= len(chunk)
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.replace(tmp_path, path)
                    _terminal_index_write_authority_locked(path, _terminal_index_next_authority(authority))
                    _fsync_parent_dir(path)
                except OSError:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                    return False
    return True


def terminal_run_summary_page_for_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    limit: int = 16,
    max_candidates: int = 64,
    skip_run_ids: Iterable[str] | None = None,
    barrier_run_ids: Iterable[str] | None = None,
    index_end_offset: int | None = None,
    index_cursor: dict | None = None,
) -> dict:
    """Return one bounded page of terminal summaries plus compact index cursor metadata."""
    try:
        sid = _validate_id(session_id, "session_id")
    except ValueError:
        return {
            "summaries": [],
            "index_size": 0,
            "index_generation": None,
            "next_index_end_offset": None,
            "exhausted": True,
        }
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_root.exists():
        return {
            "summaries": [],
            "index_size": 0,
            "index_generation": None,
            "next_index_end_offset": None,
            "exhausted": True,
        }
    try:
        limit = max(1, min(int(limit), 64))
    except (TypeError, ValueError):
        limit = 16
    try:
        max_candidates = max(limit, min(int(max_candidates), 256))
    except (TypeError, ValueError):
        max_candidates = 64
    skip_run_ids = {str(run_id or "").strip() for run_id in (skip_run_ids or [])}
    barrier_run_ids = {str(run_id or "").strip() for run_id in (barrier_run_ids or [])}
    summaries: list[dict] = []
    seen_run_ids: set[str] = set()
    inspected = 0
    next_index_end_offset: int | None = None

    index_path = _terminal_index_path(sid, session_dir=root)
    index_page = _terminal_index_entry_page(
        index_path,
        max_bytes=_TERMINAL_INDEX_MAX_BYTES,
        max_rows=max(max_candidates * 4, _TERMINAL_INDEX_MAX_ROWS),
        end_offset=index_end_offset,
        index_cursor=index_cursor,
    )
    index_size = int((index_page or {}).get("index_size") or 0)
    index_generation = (index_page or {}).get("index_generation")
    next_index_end_offset = (index_page or {}).get("next_index_end_offset")
    for row_start, row_end, entry in (index_page or {}).get("rows") or []:
        next_index_end_offset = row_start
        if not isinstance(entry, dict):
            inspected += 1
            if inspected >= max_candidates:
                break
            continue
        summary = _terminal_index_summary_from_entry(entry, session_id=sid, session_root=session_root)
        if summary is None:
            inspected += 1
            if inspected >= max_candidates:
                break
            continue
        run_id = str(summary.get("run_id") or "")
        if run_id in barrier_run_ids:
            next_index_end_offset = row_end
            inspected += 1
            break
        if run_id in skip_run_ids or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        inspected += 1
        if inspected > max_candidates:
            break
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    if summaries or inspected >= max_candidates or next_index_end_offset is not None:
        return {
            "summaries": summaries,
            "index_size": index_size,
            "index_generation": index_generation,
            "next_index_end_offset": next_index_end_offset,
            "exhausted": next_index_end_offset in {None, 0},
        }
    if index_size > 0:
        return {
            "summaries": [],
            "index_size": index_size,
            "index_generation": index_generation,
            "next_index_end_offset": 0,
            "exhausted": True,
        }

    candidates: list[tuple[float, str, Path]] = []
    try:
        scandir_iter = os.scandir(session_root)
    except OSError:
        return {
            "summaries": summaries,
            "index_size": index_size,
            "index_generation": index_generation,
            "next_index_end_offset": None,
            "exhausted": True,
        }
    with scandir_iter as entries:
        for entry in entries:
            name = entry.name
            if not name.endswith(".jsonl") or name.startswith("_"):
                continue
            try:
                run_id = _validate_id(name[:-6], "run_id")
            except ValueError:
                continue
            if run_id in skip_run_ids or run_id in seen_run_ids:
                continue
            inspected += 1
            if inspected > max_candidates:
                break
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, run_id, Path(entry.path)))
    for _mtime, run_id, path in sorted(candidates, reverse=True):
        summary = latest_run_summary(
            sid,
            run_id,
            session_dir=root,
        )
        if not summary.get("terminal"):
            continue
        summary = dict(summary)
        summary["path"] = str(path)
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    return {
        "summaries": summaries,
        "index_size": index_size,
        "index_generation": index_generation,
        "next_index_end_offset": None,
        "exhausted": True,
    }


def terminal_run_summaries_for_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    limit: int = 16,
    max_candidates: int = 64,
    skip_run_ids: Iterable[str] | None = None,
) -> list[dict]:
    """Return newest terminal run summaries for one session.

    ``skip_run_ids`` lets callers exclude already-resolved or active streams
    before the bounded summary parse, so durable reconciliation records can
    advance through invalid settled terminals without an unbounded directory
    scan.
    """
    try:
        wanted = max(1, min(int(limit), 64))
    except (TypeError, ValueError):
        wanted = 16
    summaries: list[dict] = []
    seen: set[str] = set()
    cursor: dict | None = None
    base_skip_run_ids = {str(run_id or "").strip() for run_id in (skip_run_ids or [])}
    for _page_idx in range(8):
        page = terminal_run_summary_page_for_session(
            session_id,
            session_dir=session_dir,
            limit=wanted,
            max_candidates=max_candidates,
            skip_run_ids=base_skip_run_ids.union(seen),
            index_cursor=cursor,
        )
        page_summaries = page.get("summaries") if isinstance(page, dict) else []
        if isinstance(page_summaries, list):
            for summary in page_summaries:
                run_id = str((summary or {}).get("run_id") or "").strip()
                if not run_id or run_id in seen:
                    continue
                seen.add(run_id)
                summaries.append(summary)
                if len(summaries) >= wanted:
                    return summaries
        if not isinstance(page, dict) or page.get("exhausted"):
            break
        generation = page.get("index_generation")
        next_end_offset = page.get("next_index_end_offset")
        try:
            index_size = int(page.get("index_size") or 0)
            end_offset = int(next_end_offset)
        except (TypeError, ValueError):
            break
        if not isinstance(generation, dict) or index_size <= 0 or end_offset < 0:
            break
        cursor = {
            "index_size": index_size,
            "generation": generation,
            "end_offset": min(index_size, end_offset),
        }
    return summaries


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
    byte_cap = None if max_bytes is None else max(0, int(max_bytes))
    row_cap = None if max_rows is None else max(0, int(max_rows))

    def error(status: str) -> dict:
        return {
            "session_id": sid,
            "cursor_run_id": cursor_run_id,
            "cursor_seq": cursor_seq,
            "status": status,
            "events": [],
        }

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
        return error("cursor_invalid")
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
    runs: list[tuple[float, str, Path]] = []
    retained_rows = 0
    retained_bytes = 0

    def first_event_created_at(path: Path) -> float:
        fallback = path.stat().st_mtime
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    # Ordering is advisory only here. The replay pass below
                    # validates every row it will trust before emitting it.
                    if byte_cap is not None and len(raw) > byte_cap:
                        return fallback
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return fallback
                    if isinstance(event, dict):
                        return _event_created_at(event, fallback=fallback)
                    return fallback
        except FileNotFoundError:
            return fallback
        return fallback

    def parse_replay_run(path: Path, run_id: str, *, after_seq: int | None = None) -> tuple[str, list[dict]]:
        nonlocal retained_rows, retained_bytes
        events: list[dict] = []
        expected_seq = 1
        cursor_seen = after_seq is None
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    if byte_cap is not None and len(raw) > byte_cap:
                        return "replay_limit_bytes", []
                    try:
                        event = json.loads(raw.decode("utf-8"))
                        seq = int(event.get("seq")) if isinstance(event, dict) else 0
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                        return "replay_malformed", []
                    if (
                        seq != expected_seq
                        or event.get("event_id") != f"{run_id}:{seq}"
                        or event.get("run_id") != run_id
                        or event.get("session_id") != sid
                    ):
                        return "replay_noncontiguous", []
                    expected_seq += 1
                    if after_seq is not None and seq == after_seq:
                        cursor_seen = True
                    if after_seq is not None and seq <= after_seq:
                        continue
                    if row_cap is not None and retained_rows >= row_cap:
                        return "replay_limit_rows", []
                    if byte_cap is not None and retained_bytes + len(raw) > byte_cap:
                        return "replay_limit_bytes", []
                    retained_rows += 1
                    retained_bytes += len(raw)
                    events.append(event)
        except FileNotFoundError:
            return "missing", []
        if not cursor_seen:
            return "cursor_event_missing", []
        return "ok", events

    for path in sorted(session_root.glob("*.jsonl")) if session_root.exists() else []:
        if path.name == TERMINAL_RUN_INDEX_NAME:
            continue
        run_id = path.stem
        try:
            run_id = _validate_id(run_id, "run_id")
        except ValueError:
            continue
        runs.append((first_event_created_at(path), run_id, path))
    runs.sort(key=lambda run: (run[0], run[1]))
    cursor_index = next((index for index, (_created_at, run_id, _path) in enumerate(runs) if run_id == cursor_run_id), None)
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
    if cursor_seq is None:
        return error("cursor_event_missing")
    cursor_status, replay_events = parse_replay_run(
        runs[cursor_index][2],
        str(cursor_run_id),
        after_seq=cursor_seq,
    )
    if cursor_status == "missing":
        return error("cursor_run_missing")
    if cursor_status != "ok":
        return error(cursor_status)
    for _created_at, run_id, path in runs[cursor_index + 1:]:
        status, events = parse_replay_run(path, run_id)
        if status == "missing":
            continue
        if status != "ok":
            return error(status)
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
