"""Append-only WebUI run event journal helpers.

This is the first #1925 journal/replay slice.  It mirrors SSE events emitted by
the existing in-process streaming path without changing execution ownership.
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
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

_REAL_FSYNC = os.fsync

RUN_JOURNAL_DIR_NAME = "_run_journal"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WRITER_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()
# Next-seq to assign per run-journal file path, kept in memory so repeat appends
# to the same run do not re-parse the whole file on every call. Every entry is
# ``(generation, file_size_before_append, next_seq)`` so a cache from another
# process's append or a delete/recreate cannot be reused against a different
# durable file state. The per-path ``_lock_for(path)`` serializes same-path
# reserve→append so seqs stay monotonic and file order matches; ``_SEQ_CACHE_LOCK``
# (below) additionally guards every *structural* access to the dict (peek/publish/
# evict) so ``delete_run_journal`` can iterate + drop keys while a concurrent
# append on ANOTHER path inserts one, without a ``dictionary changed size during
# iteration`` crash. See ``_peek_next_seq`` and ``delete_run_journal``.
_SEQ_CACHE: dict[str, tuple[str, int, int]] = {}
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
_LEGACY_TERMINAL_RECOVERY_MAX_BYTES = 16 * 1024 * 1024
_BOUNDED_REPLAY_MAX_SCAN_BYTES = 32 * 1024 * 1024
_BOUNDED_REPLAY_MAX_SCAN_ROWS = 4096
_BOUNDED_REPLAY_MAX_MALFORMED = 64
_REPLAY_RESUME_TOKEN_MAX_CHARS = 16 * 1024
_REPLAY_RESUME_TOKEN_VERSION = 3
_REPLAY_RESUME_TOKEN_HMAC_DOMAIN = b"hermes-webui:run-journal-replay:v3\0"
_REPLAY_RESUME_STATE_MAX_BYTES = 8 * 1024
_RUN_JOURNAL_GENERATION_VERSION = 1
_RUN_JOURNAL_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_RUN_JOURNAL_GENERATION_LOCK_STRIPES = 64
_RUN_JOURNAL_LIFECYCLE_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_RUN_JOURNAL_LIFECYCLE_LOCKS_GUARD = threading.Lock()
# The per-session journal directory is deliberately disposable.  Admission
# state must therefore live beside it, not below it, so a writer that was
# constructed before a delete cannot recreate the deleted plaintext journal.
# The record is atomically replaced under the same lifecycle authority used by
# append/delete, making the incarnation durable and visible across processes.
_RUN_JOURNAL_INCARNATION_DIR_NAME = ".incarnations"
_RUN_JOURNAL_INCARNATION_VERSION = 2
_RUN_JOURNAL_INCARNATION_RE = re.compile(r"^[0-9a-f]{32}$")
_RUN_JOURNAL_WRITER_RETIRED_ERROR = "run journal writer incarnation retired"
_RUN_JOURNAL_WRITER_REQUIRED_ERROR = "run journal writer incarnation required"
_SNAPSHOT_ARGS_MAX_ITEMS = 64
_SNAPSHOT_ARGS_MAX_DEPTH = 8
_SNAPSHOT_ARGS_MAX_STRING_CHARS = 8192
_SNAPSHOT_ARGS_MAX_TOTAL_CHARS = 64 * 1024
_SNAPSHOT_ARGS_TRUNCATED_SUFFIX = "...[truncated]"


class RunJournalRetiredAuthorityError(RuntimeError):
    """Raised when a valid retired authority blocks a new activation."""


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


def _run_journal_incarnation_path(path: Path) -> Path:
    """Return the durable admission record for ``path``'s session.

    ``path.parent`` is the disposable ``_run_journal/{session_id}`` subtree,
    so the record is kept in a sibling directory under ``_run_journal``.  The
    path itself is stable across delete/recreate cycles and across processes.
    """
    journal_root = path.parent.parent
    return journal_root / _RUN_JOURNAL_INCARNATION_DIR_NAME / f"{path.parent.name}.json"


def _read_run_journal_incarnation(path: Path) -> dict | None:
    """Read one durable authority record, distinguishing absent from invalid."""
    incarnation_path = _run_journal_incarnation_path(path)
    try:
        raw = incarnation_path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid run journal authority") from exc
    except OSError as exc:
        raise RuntimeError("unreadable run journal authority") from exc
    def _strict_object(pairs):
        record = {}
        for key, value in pairs:
            if key in record:
                raise ValueError("duplicate authority field")
            record[key] = value
        return record

    try:
        record = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("invalid run journal authority") from exc
    if not isinstance(record, dict):
        raise RuntimeError("invalid run journal authority")
    incarnation = record.get("incarnation")
    version = record.get("version")
    state = record.get("state")
    if version == 1 and state is None and set(record) == {
        "version",
        "session_id",
        "incarnation",
    }:
        # Version 1 records predate explicit lifecycle state. They can only be
        # migrated by the canonical activation entrypoint; reads never replace
        # malformed or unreadable authority.
        state = "active"
    elif version != _RUN_JOURNAL_INCARNATION_VERSION or set(record) != {
        "version",
        "session_id",
        "state",
        "incarnation",
    }:
        raise RuntimeError("invalid run journal authority")
    if (
        version not in (1, _RUN_JOURNAL_INCARNATION_VERSION)
        or record.get("session_id") != path.parent.name
        or state not in ("active", "retired")
        or not isinstance(incarnation, str)
        or _RUN_JOURNAL_INCARNATION_RE.fullmatch(incarnation) is None
    ):
        raise RuntimeError("invalid run journal authority")
    return {
        "version": int(version),
        "session_id": path.parent.name,
        "state": state,
        "incarnation": incarnation,
    }


def _write_run_journal_incarnation(path: Path, incarnation: str, *, state: str) -> None:
    """Atomically persist one versioned authority record outside the journal."""
    if _RUN_JOURNAL_INCARNATION_RE.fullmatch(str(incarnation)) is None:
        raise ValueError("invalid run-journal incarnation")
    if state not in ("active", "retired"):
        raise ValueError("invalid run-journal authority state")
    incarnation_path = _run_journal_incarnation_path(path)
    incarnation_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = incarnation_path.with_name(
        f".{incarnation_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    raw = json.dumps(
        {
            "version": _RUN_JOURNAL_INCARNATION_VERSION,
            "session_id": path.parent.name,
            "state": state,
            "incarnation": str(incarnation),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            _REAL_FSYNC(fh.fileno())
        os.replace(temporary, incarnation_path)
        _fsync_parent_dir(incarnation_path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _activate_run_journal_incarnation_locked(
    path: Path,
    *,
    reactivate_retired: bool,
) -> str:
    """Activate authority only for a caller at a canonical session boundary."""
    current = _read_run_journal_incarnation(path)
    if current is not None and current["state"] == "active":
        incarnation = str(current["incarnation"])
        if current["version"] != _RUN_JOURNAL_INCARNATION_VERSION:
            _write_run_journal_incarnation(path, incarnation, state="active")
        return incarnation
    if current is not None and not reactivate_retired:
        if current["state"] == "retired":
            raise RunJournalRetiredAuthorityError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)
        raise RuntimeError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)
    incarnation = secrets.token_hex(16)
    _write_run_journal_incarnation(path, incarnation, state="active")
    return incarnation


def activate_run_journal_session(
    session_id: str,
    *,
    session_dir: Path | None = None,
    reactivate_retired: bool = False,
) -> str:
    """Return a capability for a canonical, live session lifecycle boundary.

    Normal restore/run-start callers must leave ``reactivate_retired`` false so
    a session whose deletion is awaiting retry cannot silently become writable.
    Only a canonical create/import that intentionally reuses a retired id may
    request a new incarnation.
    """
    path = _run_path(session_id, ".authority", session_dir=session_dir)
    with _run_journal_lifecycle_authority(path):
        return _activate_run_journal_incarnation_locked(
            path,
            reactivate_retired=bool(reactivate_retired),
        )


def validate_run_journal_session_activation(
    session_id: str,
    *,
    session_dir: Path | None = None,
) -> None:
    """Fail closed on retired/invalid authority without creating new state."""
    path = _run_path(session_id, ".authority", session_dir=session_dir)
    with _run_journal_lifecycle_authority(path):
        current = _read_run_journal_incarnation(path)
        if current is not None and current["state"] == "retired":
            raise RunJournalRetiredAuthorityError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)
        if current is not None and current["state"] != "active":
            raise RuntimeError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)


def _retire_run_journal_incarnation_locked(
    path: Path,
    *,
    session_exists: bool,
) -> bool:
    """Durably rotate a session admission record before journal deletion.

    Return whether a record existed (or a legacy session subtree required one)
    so a missing directory with no admitted writer remains a true no-op.
    """
    current = _read_run_journal_incarnation(path)
    if current is None and not session_exists:
        return False
    if current is None:
        incarnation = secrets.token_hex(16)
    else:
        incarnation = str(current["incarnation"])
    if (
        current is None
        or current["state"] != "retired"
        or current["version"] != _RUN_JOURNAL_INCARNATION_VERSION
    ):
        _write_run_journal_incarnation(path, incarnation, state="retired")
    return True


def _run_generation_path(path: Path) -> Path:
    """Return the durable generation sidecar for one JSONL journal."""
    return path.with_name(f"{path.name}.generation")


def _run_generation_lock_path(path: Path) -> Path:
    session_id = path.parent.name
    stripe = _run_journal_lifecycle_stripe(session_id)
    return path.parent.parent / ".generation-locks" / f"{stripe:02d}.lock"


def _run_journal_lifecycle_stripe(session_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(str(session_id).encode("utf-8")).digest()[:2],
        "big",
    ) % _RUN_JOURNAL_GENERATION_LOCK_STRIPES


def _run_journal_lifecycle_lock_for(path: Path) -> threading.Lock:
    key = (
        os.path.abspath(str(path.parent.parent)),
        _run_journal_lifecycle_stripe(path.parent.name),
    )
    with _RUN_JOURNAL_LIFECYCLE_LOCKS_GUARD:
        lock = _RUN_JOURNAL_LIFECYCLE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _RUN_JOURNAL_LIFECYCLE_LOCKS[key] = lock
        return lock


@contextmanager
def _run_journal_lifecycle_authority(path: Path):
    """Hold the per-session lifecycle authority in the fixed lock order.

    The thread stripe is deliberately keyed by the same session hash as the
    stable cross-process generation stripe.  Every path operation that can
    create, replace, or remove a session journal must enter this context before
    opening a journal descriptor; the per-run lock is acquired by the caller
    only after this context is held.
    """
    with _run_journal_lifecycle_lock_for(path):
        with _run_generation_process_lock(path):
            yield


@contextmanager
def _run_generation_process_lock(path: Path):
    """Serialize generation initialization across WebUI worker processes."""
    lock_path = _run_generation_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
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
        raise RuntimeError("cross-process run-journal generation locking is unavailable")


def _run_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_dev), int(stat.st_ino)


def _read_run_generation_record(path: Path) -> tuple[str, tuple[int, int] | None] | None:
    generation_path = _run_generation_path(path)
    try:
        raw = generation_path.read_text(encoding="ascii")
        record = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    generation = record.get("generation")
    if (
        record.get("version") != _RUN_JOURNAL_GENERATION_VERSION
        or not isinstance(generation, str)
        or _RUN_JOURNAL_GENERATION_RE.fullmatch(generation) is None
    ):
        return None
    device = record.get("device")
    inode = record.get("inode")
    if device is None and inode is None:
        identity = None
    elif (
        isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
    ):
        return None
    else:
        identity = (device, inode)
    return generation, identity


def _write_run_generation_record(
    path: Path,
    generation: str,
    *,
    identity: tuple[int, int] | None = None,
) -> None:
    """Persist one generation nonce and the current file identity atomically."""
    if _RUN_JOURNAL_GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid run-journal generation")
    if identity is None:
        identity = _run_file_identity(path)
    record = {
        "version": _RUN_JOURNAL_GENERATION_VERSION,
        "generation": generation,
        "device": identity[0] if identity is not None else None,
        "inode": identity[1] if identity is not None else None,
    }
    generation_path = _run_generation_path(path)
    generation_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = generation_path.with_name(
        f".{generation_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            _REAL_FSYNC(fh.fileno())
        os.replace(temporary, generation_path)
        _fsync_parent_dir(generation_path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _prepare_run_generation_for_append(
    path: Path,
    *,
    identity: tuple[int, int] | None = None,
    force_rotate: bool = False,
) -> tuple[str, bool]:
    """Return the nonce to use for an append and whether its sidecar needs writing."""
    if identity is None:
        identity = _run_file_identity(path)
    record = _read_run_generation_record(path)
    if force_rotate or identity is None or record is None or record[1] != identity:
        return secrets.token_hex(16), True
    return record[0], False


def _ensure_run_generation_for_read_locked(
    path: Path,
    *,
    identity: tuple[int, int] | None = None,
) -> str | None:
    """Load/create the nonce used by replay tokens for the currently-open JSONL."""
    if identity is None:
        identity = _run_file_identity(path)
    if identity is None:
        return None
    if _run_file_identity(path) != identity:
        return None
    record = _read_run_generation_record(path)
    if record is not None and record[1] == identity:
        return record[0]
    generation = secrets.token_hex(16)
    _write_run_generation_record(path, generation, identity=identity)
    return generation


def _ensure_run_generation_for_read(
    path: Path,
    *,
    identity: tuple[int, int] | None = None,
) -> str | None:
    """Load/create the replay nonce while holding the lifecycle authority."""
    with _run_journal_lifecycle_authority(path):
        return _ensure_run_generation_for_read_locked(path, identity=identity)


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


def _peek_next_seq(
    path: Path,
    *,
    generation: str,
    pre_write_size: int,
) -> int:
    """Return the next seq candidate without publishing it to the cache.

    Callers MUST hold the lifecycle authority and ``_lock_for(path)``. A cache
    entry is reusable only when both its durable generation and its file size
    match the descriptor's pre-write state. Otherwise the candidate is rebuilt
    from the physical journal, which catches appends made by another process
    and delete/recreate cycles that this process did not observe.
    """
    key = str(path)
    with _SEQ_CACHE_LOCK:
        cached = _SEQ_CACHE.get(key)
        if (
            cached is not None
            and cached[0] == generation
            and cached[1] == int(pre_write_size)
        ):
            return cached[2]
    # Cache miss: seed from disk WITHOUT holding the module-global lock, so a
    # slow first-access file read for one path can't block every other path's
    # cache ops. The caller holds the per-path lock, so only one thread per path
    # can reach this branch — no double-seed, and no same-path writer can race
    # the value in between.
    seeded = _next_seq(path)
    return seeded


def _reserve_next_seq(path: Path) -> int:
    """Back-compat physical-seed helper for callers outside append transactions."""
    return _next_seq(path)


def _discard_seq_cache(path: Path) -> None:
    with _SEQ_CACHE_LOCK:
        _SEQ_CACHE.pop(str(path), None)


def _publish_seq_cache(
    path: Path,
    *,
    generation: str,
    file_size: int,
    seq: int,
) -> None:
    """Publish an automatic candidate after its physical append is validated."""
    key = str(path)
    entry = (str(generation), int(file_size), int(seq) + 1)
    with _SEQ_CACHE_LOCK:
        _SEQ_CACHE[key] = entry


def _evict_run_journal_session_state(session_journal_dir: Path) -> None:
    """Drop process-local writer/sequence/summary state for one session."""
    dir_key = str(session_journal_dir)
    with _WRITER_LOCKS_GUARD:
        for key in [key for key in _WRITER_LOCKS if key[0] == dir_key]:
            del _WRITER_LOCKS[key]
    with _SEQ_CACHE_LOCK:
        for cache_key in [
            entry for entry in _SEQ_CACHE if str(Path(entry).parent) == dir_key
        ]:
            del _SEQ_CACHE[cache_key]
    with _SUMMARY_CACHE_LOCK:
        for cache_key in [
            entry for entry in _SUMMARY_CACHE if str(Path(entry).parent) == dir_key
        ]:
            del _SUMMARY_CACHE[cache_key]


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
            _REAL_FSYNC(dir_fd)
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
    _incarnation: str | None = None,
) -> dict:
    """Append one durable run event and fsync it according to the journal policy."""
    path = _run_path(session_id, run_id, session_dir=session_dir)
    if _incarnation is None:
        raise RuntimeError(_RUN_JOURNAL_WRITER_REQUIRED_ERROR)
    incarnation = str(_incarnation)
    if _RUN_JOURNAL_INCARNATION_RE.fullmatch(incarnation) is None:
        raise RuntimeError(_RUN_JOURNAL_WRITER_REQUIRED_ERROR)
    payload = payload if payload is not None else {}
    event_name = str(event_name or "").strip()
    if not event_name:
        raise ValueError("event_name is required")
    with _run_journal_lifecycle_authority(path):
        authority = _read_run_journal_incarnation(path)
        if (
            authority is None
            or authority["state"] != "active"
            or incarnation != authority["incarnation"]
        ):
            # Check before taking the per-run lock or touching the disposable
            # session subtree.  A writer captured before delete must be unable
            # to recreate any journal, generation sidecar, or process cache.
            raise RuntimeError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)
        with _lock_for(path):
            fd = None
            created_file = False
            physical_write_attempted = False
            try:
                # Directory creation and descriptor opening stay inside the
                # lifecycle authority. A concurrent delete therefore cannot
                # unlink the directory between setup and the physical append.
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(
                        path,
                        os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_WRONLY,
                        0o600,
                    )
                    created_file = True
                except FileExistsError:
                    fd = os.open(path, os.O_APPEND | os.O_WRONLY)

                stat = os.fstat(fd)
                identity = int(stat.st_dev), int(stat.st_ino)
                pre_write_size = int(stat.st_size)
                if _run_file_identity(path) != identity:
                    raise OSError("run journal replaced during append")
                generation, generation_needs_write = _prepare_run_generation_for_append(
                    path,
                    identity=identity,
                    force_rotate=created_file,
                )
                if generation_needs_write:
                    _write_run_generation_record(path, generation, identity=identity)

                if created_file:
                    # A deleted/recreated path must never inherit this process's
                    # old next-seq candidate.
                    _discard_seq_cache(path)
                assigned_seq = (
                    int(seq)
                    if seq is not None
                    else _peek_next_seq(
                        path,
                        generation=generation,
                        pre_write_size=pre_write_size,
                    )
                )
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
                line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                fh = os.fdopen(fd, "a", encoding="utf-8")
                fd = None
                with fh:
                    physical_write_attempted = True
                    fh.write(line)
                    fh.flush()
                    if _should_fsync_event(terminal_state):
                        os.fsync(fh.fileno())
                    final_stat = os.fstat(fh.fileno())
                    final_identity = int(final_stat.st_dev), int(final_stat.st_ino)
                    final_size = int(final_stat.st_size)
                    if final_identity != _run_file_identity(path):
                        raise OSError("run journal replaced during append")
                    if _read_run_generation_record(path) != (generation, identity):
                        raise OSError("run journal generation changed during append")
                if created_file:
                    # Parent durability is part of the same lifecycle
                    # transaction, but only runs after descriptor/path and
                    # generation validation has succeeded.
                    _fsync_parent_dir(path)
                # Publish process caches only after the descriptor/path and
                # generation identities have been validated successfully.
                if seq is None:
                    _publish_seq_cache(
                        path,
                        generation=generation,
                        file_size=final_size,
                        seq=assigned_seq,
                    )
                else:
                    # An explicit sequence may not reflect the highest value
                    # another writer placed on disk. Force the next automatic
                    # append to rebuild from the physical journal.
                    _discard_seq_cache(path)
                _discard_cached_summary(path)
                return event
            except BaseException:
                # A write may have reached the journal before a final
                # identity/generation or parent-durability check failed. Do
                # not let a pre-write seq/summary cache describe that stale
                # state on the next append or recovery lookup; reseed from
                # the physical file instead.
                if physical_write_attempted:
                    _discard_seq_cache(path)
                    _discard_cached_summary(path)
                raise
            finally:
                if fd is not None:
                    os.close(fd)


class RunJournalWriter:
    """Stateful writer for one WebUI stream/run."""

    def __init__(
        self,
        session_id: str,
        run_id: str,
        *,
        session_dir: Path | None = None,
        incarnation: str | None = None,
    ):
        self.session_id = _validate_id(session_id, "session_id")
        self.run_id = _validate_id(run_id, "run_id")
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self._path = _run_path(self.session_id, self.run_id, session_dir=self.session_dir)
        if incarnation is None or _RUN_JOURNAL_INCARNATION_RE.fullmatch(str(incarnation)) is None:
            raise RuntimeError(_RUN_JOURNAL_WRITER_REQUIRED_ERROR)
        self._incarnation = str(incarnation)
        # Validate the capability before allocating any per-run process state.
        with _run_journal_lifecycle_authority(self._path):
            authority = _read_run_journal_incarnation(self._path)
            if (
                authority is None
                or authority["state"] != "active"
                or authority["incarnation"] != self._incarnation
            ):
                raise RuntimeError(_RUN_JOURNAL_WRITER_RETIRED_ERROR)
        self._lock = _lock_for(self._path)

    def append_sse_event(self, event_name: str, payload=None) -> dict:
        # Sequence allocation belongs to append_run_event's lifecycle
        # transaction. Reserving here would publish a candidate before the
        # physical write could prove its descriptor/path identity.
        return append_run_event(
            self.session_id,
            self.run_id,
            event_name,
            payload or {},
            session_dir=self.session_dir,
            _incarnation=self._incarnation,
        )


def _recover_legacy_overcap_terminal_event(
    event: dict,
    *,
    session_id: str,
    run_id: str,
    max_seq: int | None,
) -> dict | None:
    """Convert one bounded-size legacy terminal row into a fixed recovery marker."""
    if not isinstance(event, dict) or event.get("terminal") is not True:
        return None
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    if (
        seq <= 0
        or (max_seq is not None and seq > int(max_seq))
        or str(event.get("event_id") or "") != f"{run_id}:{seq}"
        or str(event.get("run_id") or "") != str(run_id)
        or str(event.get("session_id") or "") != str(session_id)
        or str(event.get("event") or "") not in TERMINAL_SSE_EVENTS
    ):
        return None
    event_name = str(event.get("event") or "")
    terminal_state = str(event.get("terminal_state") or "").strip().lower()
    if terminal_state not in {
        "completed", "interrupted-by-user", "interrupted-by-crash", "errored",
        "tool_limit_reached",
    }:
        terminal_state = _terminal_state_for_event(event_name, {}) or "errored"
    return {
        "version": 1,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": str(run_id),
        "session_id": str(session_id),
        "event": event_name,
        "type": event_name,
        "terminal": True,
        "terminal_state": terminal_state,
        "payload": {
            "terminal_session_persisted": False,
            "terminal_disposition": {
                "version": "terminal_disposition_v1",
                "kind": "consumed_non_materializable",
                "reason": "legacy_terminal_payload_too_large",
                "session_id": str(session_id),
                "run_id": str(run_id),
                "stream_id": str(run_id),
            },
        },
    }


def _serialized_event_size(event: dict) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


class _ReplayInvalidJsonEnvelope(ValueError):
    """Raised when a retained JSON row uses a non-standard numeric constant."""


def _reject_nonfinite_json_value(value: str):
    raise _ReplayInvalidJsonEnvelope(value)


def _replay_resume_signing_key() -> bytes:
    # Reuse the installation-persistent key so valid cursors survive restarts
    # without introducing a second secret lifecycle.
    from api.auth import _signing_key

    return _signing_key()


def _replay_resume_payload_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")


def _replay_resume_signature(payload_bytes: bytes) -> bytes:
    return hmac.new(
        _replay_resume_signing_key(),
        _REPLAY_RESUME_TOKEN_HMAC_DOMAIN + payload_bytes,
        hashlib.sha256,
    ).digest()


def _encode_urlsafe_unpadded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_urlsafe_unpadded(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _encode_replay_resume_state(scanner: "_TopLevelEnvelopeScanner") -> str:
    raw = json.dumps(
        scanner.snapshot(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(raw) > _REPLAY_RESUME_STATE_MAX_BYTES:
        raise ValueError("replay_resume_state_too_large")
    return _encode_urlsafe_unpadded(raw)


def _decode_replay_resume_state(encoded: Any) -> "_TopLevelEnvelopeScanner | None":
    if encoded is None:
        return None
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("invalid replay resume state")
    raw = _decode_urlsafe_unpadded(encoded)
    if len(raw) > _REPLAY_RESUME_STATE_MAX_BYTES:
        raise ValueError("invalid replay resume state")
    try:
        state = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid replay resume state") from None
    return _TopLevelEnvelopeScanner.from_snapshot(state)


def _encode_replay_resume_token(
    fh,
    *,
    session_id: str,
    run_id: str,
    max_seq: int | None,
    offset: int,
    next_after_seq: int,
    last_physical_seq: int,
    line_no: int,
    generation: str,
    scanner: "_TopLevelEnvelopeScanner | None" = None,
) -> str:
    stat = os.fstat(fh.fileno())
    if _RUN_JOURNAL_GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid run-journal generation")
    payload = {
        "v": _REPLAY_RESUME_TOKEN_VERSION,
        "d": int(stat.st_dev),
        "i": int(stat.st_ino),
        "o": int(offset),
        "s": int(next_after_seq),
        "p": int(last_physical_seq),
        "l": int(line_no),
        "c": max_seq,
        "x": str(session_id),
        "r": str(run_id),
        "g": generation,
        "q": None if scanner is None else _encode_replay_resume_state(scanner),
    }
    payload_bytes = _replay_resume_payload_bytes(payload)
    token = ".".join((
        _encode_urlsafe_unpadded(payload_bytes),
        _encode_urlsafe_unpadded(_replay_resume_signature(payload_bytes)),
    ))
    if len(token) > _REPLAY_RESUME_TOKEN_MAX_CHARS:
        raise ValueError("replay_resume_token_too_large")
    return token


def _decode_replay_resume_token(
    fh,
    token: str,
    *,
    session_id: str,
    run_id: str,
    expected_after_seq: int | None,
    expected_max_seq: int | None,
    expected_generation: str | None,
) -> tuple[int, int, int, int, int, "_TopLevelEnvelopeScanner | None"] | None:
    raw_token = str(token or "").strip()
    if (
        not raw_token
        or len(raw_token) > _REPLAY_RESUME_TOKEN_MAX_CHARS
        or raw_token.count(".") != 1
    ):
        return None
    try:
        payload_token, signature_token = raw_token.split(".")
        payload_bytes = _decode_urlsafe_unpadded(payload_token)
        signature = _decode_urlsafe_unpadded(signature_token)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(signature, _replay_resume_signature(payload_bytes)):
        return None
    try:
        payload = json.loads(payload_bytes.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "v", "d", "i", "o", "s", "p", "l", "c", "x", "r", "g", "q",
    }:
        return None
    canonical_payload = _replay_resume_payload_bytes(payload)
    if not hmac.compare_digest(payload_bytes, canonical_payload):
        return None
    values = [payload[key] for key in ("v", "d", "i", "o", "s", "p", "l")]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    version, device, inode, offset, token_seq, physical_seq, line_no = values
    token_max_seq = payload["c"]
    if (
        version != _REPLAY_RESUME_TOKEN_VERSION
        or offset < 0
        or token_seq < 0
        or (expected_after_seq is not None and token_seq != expected_after_seq)
        or physical_seq < 0
        or line_no < 0
        or isinstance(token_max_seq, bool)
        or (token_max_seq is not None and not isinstance(token_max_seq, int))
        or token_max_seq != expected_max_seq
        or payload["x"] != str(session_id)
        or payload["r"] != str(run_id)
        or not isinstance(payload["g"], str)
        or _RUN_JOURNAL_GENERATION_RE.fullmatch(payload["g"]) is None
        or expected_generation is None
        or payload["g"] != expected_generation
    ):
        return None
    try:
        scanner = _decode_replay_resume_state(payload["q"])
    except (TypeError, ValueError):
        return None
    partial = scanner is not None
    if partial and offset <= 0:
        return None
    boundary_scan_bytes = 0
    try:
        stat = os.fstat(fh.fileno())
        if device != int(stat.st_dev) or inode != int(stat.st_ino) or offset > int(stat.st_size):
            return None
        if not partial and offset > 0 and offset != int(stat.st_size):
            fh.seek(offset - 1)
            boundary = fh.read(1)
            boundary_scan_bytes = len(boundary)
            if boundary != b"\n":
                return None
        fh.seek(offset)
    except (OSError, ValueError):
        return None
    return offset, token_seq, physical_seq, line_no, boundary_scan_bytes, scanner


class _StreamingJsonValidator:
    """Validate one UTF-8 JSON value without retaining its payload."""

    _MAX_NESTING = 128
    _NUMBER_FINAL_STATES = frozenset({"zero", "integer", "fraction", "exponent"})
    _HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._frames: list[dict[str, str]] = []
        self._started = False
        self._done = False
        self._invalid = False
        self._finalized = False
        self._mode: str | None = None
        self._string_role = ""
        self._escape_state = ""
        self._unicode_digits = 0
        self._literal = ""
        self._literal_index = 0
        self._number_state = ""

    @staticmethod
    def _is_value_delimiter(char: str) -> bool:
        return char in " \t\r\n,}]"

    def _push(self, kind: str) -> None:
        if len(self._frames) >= self._MAX_NESTING:
            self._invalid = True
            return
        state = "key_or_end" if kind == "object" else "value_or_end"
        self._frames.append({"kind": kind, "state": state})

    def _complete_value(self) -> None:
        if not self._frames:
            if not self._started or self._done:
                self._invalid = True
                return
            self._done = True
            return
        frame = self._frames[-1]
        if frame["kind"] == "object" and frame["state"] == "value":
            frame["state"] = "comma_or_end"
            return
        if frame["kind"] == "array" and frame["state"] in {"value_or_end", "value"}:
            frame["state"] = "comma_or_end"
            return
        self._invalid = True

    def _close_container(self, kind: str) -> None:
        if not self._frames or self._frames[-1]["kind"] != kind:
            self._invalid = True
            return
        frame = self._frames[-1]
        allowed = (
            {"key_or_end", "comma_or_end"}
            if kind == "object"
            else {"value_or_end", "comma_or_end"}
        )
        if frame["state"] not in allowed:
            self._invalid = True
            return
        self._frames.pop()
        self._complete_value()

    def _start_string(self, role: str) -> None:
        self._mode = "string"
        self._string_role = role
        self._escape_state = ""
        self._unicode_digits = 0

    def _feed_string(self, char: str) -> None:
        if self._escape_state == "unicode":
            if char not in self._HEX_DIGITS:
                self._invalid = True
                return
            self._unicode_digits += 1
            if self._unicode_digits == 4:
                self._escape_state = ""
            return
        if self._escape_state == "escape":
            if char == "u":
                self._escape_state = "unicode"
                self._unicode_digits = 0
            elif char in '\"\\/bfnrt':
                self._escape_state = ""
            else:
                self._invalid = True
            return
        if char == "\\":
            self._escape_state = "escape"
            return
        if char == '"':
            role = self._string_role
            self._mode = None
            self._string_role = ""
            if role == "key":
                if not self._frames or self._frames[-1]["kind"] != "object":
                    self._invalid = True
                    return
                self._frames[-1]["state"] = "colon"
            else:
                self._complete_value()
            return
        if ord(char) < 0x20:
            self._invalid = True

    def _start_literal(self, literal: str) -> None:
        self._mode = "literal"
        self._literal = literal
        self._literal_index = 1

    def _finish_scalar(self) -> None:
        self._mode = None
        self._literal = ""
        self._literal_index = 0
        self._number_state = ""
        self._complete_value()

    def _feed_literal(self, char: str) -> bool:
        if self._literal_index < len(self._literal):
            if char != self._literal[self._literal_index]:
                self._invalid = True
                return False
            self._literal_index += 1
            return False
        if not self._is_value_delimiter(char):
            self._invalid = True
            return False
        self._finish_scalar()
        return True

    def _start_number(self, char: str) -> None:
        self._mode = "number"
        if char == "-":
            self._number_state = "sign"
        elif char == "0":
            self._number_state = "zero"
        else:
            self._number_state = "integer"

    def _feed_number(self, char: str) -> bool:
        state = self._number_state
        if state == "sign":
            if char == "0":
                self._number_state = "zero"
            elif char in "123456789":
                self._number_state = "integer"
            else:
                self._invalid = True
            return False
        if state in {"zero", "integer"}:
            if char.isascii() and char.isdigit() and state == "integer":
                return False
            if char == ".":
                self._number_state = "decimal"
                return False
            if char in "eE":
                self._number_state = "exponent_marker"
                return False
        elif state == "decimal":
            if char.isascii() and char.isdigit():
                self._number_state = "fraction"
                return False
            self._invalid = True
            return False
        elif state == "fraction":
            if char.isascii() and char.isdigit():
                return False
            if char in "eE":
                self._number_state = "exponent_marker"
                return False
        elif state == "exponent_marker":
            if char in "+-":
                self._number_state = "exponent_sign"
                return False
            if char.isascii() and char.isdigit():
                self._number_state = "exponent"
                return False
            self._invalid = True
            return False
        elif state == "exponent_sign":
            if char.isascii() and char.isdigit():
                self._number_state = "exponent"
            else:
                self._invalid = True
            return False
        elif state == "exponent":
            if char.isascii() and char.isdigit():
                return False
        if state not in self._NUMBER_FINAL_STATES or not self._is_value_delimiter(char):
            self._invalid = True
            return False
        self._finish_scalar()
        return True

    def _start_value(self, char: str) -> None:
        if char == '"':
            self._start_string("value")
        elif char == "{":
            self._push("object")
        elif char == "[":
            self._push("array")
        elif char == "t":
            self._start_literal("true")
        elif char == "f":
            self._start_literal("false")
        elif char == "n":
            self._start_literal("null")
        elif char == "-" or (char.isascii() and char.isdigit()):
            self._start_number(char)
        else:
            self._invalid = True

    def _feed_char(self, char: str) -> None:
        reprocess = True
        while reprocess and not self._invalid:
            reprocess = False
            if self._mode == "string":
                self._feed_string(char)
                return
            if self._mode == "literal":
                reprocess = self._feed_literal(char)
                continue
            if self._mode == "number":
                reprocess = self._feed_number(char)
                continue
            if char in " \t\r\n":
                return
            if self._done:
                self._invalid = True
                return
            if not self._started:
                self._started = True
                if char != "{":
                    self._invalid = True
                    return
                self._push("object")
                return
            if not self._frames:
                self._invalid = True
                return
            frame = self._frames[-1]
            state = frame["state"]
            if frame["kind"] == "object":
                if state in {"key_or_end", "key"}:
                    if char == '"':
                        self._start_string("key")
                    elif char == "}" and state == "key_or_end":
                        self._close_container("object")
                    else:
                        self._invalid = True
                    return
                if state == "colon":
                    if char == ":":
                        frame["state"] = "value"
                    else:
                        self._invalid = True
                    return
                if state == "value":
                    self._start_value(char)
                    return
                if state == "comma_or_end":
                    if char == ",":
                        frame["state"] = "key"
                    elif char == "}":
                        self._close_container("object")
                    else:
                        self._invalid = True
                    return
            else:
                if state in {"value_or_end", "value"}:
                    if char == "]" and state == "value_or_end":
                        self._close_container("array")
                    else:
                        self._start_value(char)
                    return
                if state == "comma_or_end":
                    if char == ",":
                        frame["state"] = "value"
                    elif char == "]":
                        self._close_container("array")
                    else:
                        self._invalid = True
                    return
            self._invalid = True

    def feed(self, chunk: bytes) -> None:
        if self._invalid or self._finalized:
            return
        try:
            decoded = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._invalid = True
            return
        for char in decoded:
            self._feed_char(char)
            if self._invalid:
                return

    def snapshot(self) -> dict[str, Any]:
        """Return only bounded parser state needed to continue one partial row."""
        pending, decoder_flag = self._decoder.getstate()
        return {
            "d": pending.hex(),
            "df": int(decoder_flag),
            "f": [[frame["kind"], frame["state"]] for frame in self._frames],
            "a": self._started,
            "n": self._done,
            "i": self._invalid,
            "z": self._finalized,
            "m": self._mode,
            "r": self._string_role,
            "e": self._escape_state,
            "u": self._unicode_digits,
            "l": self._literal,
            "li": self._literal_index,
            "q": self._number_state,
        }

    @classmethod
    def from_snapshot(cls, state: Any) -> "_StreamingJsonValidator":
        if not isinstance(state, dict):
            raise ValueError("invalid replay validator state")
        validator = cls()
        try:
            pending_hex = state["d"]
            decoder_flag = state["df"]
            frames = state["f"]
            if (
                not isinstance(pending_hex, str)
                or len(pending_hex) > 8
                or not isinstance(decoder_flag, int)
                or isinstance(decoder_flag, bool)
                or not isinstance(frames, list)
                or len(frames) > cls._MAX_NESTING
            ):
                raise ValueError("invalid replay validator state")
            pending = bytes.fromhex(pending_hex)
            restored_frames: list[dict[str, str]] = []
            allowed_states = {
                "key_or_end", "key", "colon", "value", "comma_or_end",
                "value_or_end",
            }
            for frame in frames:
                if (
                    not isinstance(frame, list)
                    or len(frame) != 2
                    or frame[0] not in {"object", "array"}
                    or frame[1] not in allowed_states
                ):
                    raise ValueError("invalid replay validator state")
                restored_frames.append({"kind": frame[0], "state": frame[1]})
            bool_fields = ("a", "n", "i", "z")
            if any(not isinstance(state[name], bool) for name in bool_fields):
                raise ValueError("invalid replay validator state")
            mode = state["m"]
            if mode not in {None, "string", "literal", "number"}:
                raise ValueError("invalid replay validator state")
            role = state["r"]
            if role not in {"", "key", "value"}:
                raise ValueError("invalid replay validator state")
            escape_state = state["e"]
            if escape_state not in {"", "escape", "unicode"}:
                raise ValueError("invalid replay validator state")
            unicode_digits = state["u"]
            if (
                isinstance(unicode_digits, bool)
                or not isinstance(unicode_digits, int)
                or unicode_digits < 0
                or unicode_digits > 4
            ):
                raise ValueError("invalid replay validator state")
            literal = state["l"]
            literal_index = state["li"]
            number_state = state["q"]
            if (
                not isinstance(literal, str)
                or len(literal) > 5
                or literal not in {"", "true", "false", "null"}
                or isinstance(literal_index, bool)
                or not isinstance(literal_index, int)
                or literal_index < 0
                or literal_index > len(literal)
                or not isinstance(number_state, str)
                or number_state not in {
                    "", "sign", "zero", "integer", "decimal", "fraction",
                    "exponent_marker", "exponent_sign", "exponent",
                }
            ):
                raise ValueError("invalid replay validator state")
            validator._decoder.setstate((pending, decoder_flag))
            validator._frames = restored_frames
            validator._started = state["a"]
            validator._done = state["n"]
            validator._invalid = state["i"]
            validator._finalized = state["z"]
            validator._mode = mode
            validator._string_role = role
            validator._escape_state = escape_state
            validator._unicode_digits = unicode_digits
            validator._literal = literal
            validator._literal_index = literal_index
            validator._number_state = number_state
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise ValueError("invalid replay validator state") from None
        if validator._finalized:
            raise ValueError("invalid replay validator state")
        return validator

    def valid(self) -> bool:
        if not self._finalized:
            self._finalized = True
            try:
                decoded = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._invalid = True
                decoded = ""
            for char in decoded:
                self._feed_char(char)
            if self._mode == "literal":
                if self._literal_index == len(self._literal):
                    self._finish_scalar()
                else:
                    self._invalid = True
            elif self._mode == "number":
                if self._number_state in self._NUMBER_FINAL_STATES:
                    self._finish_scalar()
                else:
                    self._invalid = True
            elif self._mode is not None:
                self._invalid = True
        return (
            not self._invalid
            and self._started
            and self._done
            and not self._frames
            and self._mode is None
        )


class _TopLevelEnvelopeScanner:
    """Extract cursor identity from one JSON object without retaining its payload.

    Oversized legacy rows cannot be decoded as one allocation. This scanner only
    accepts unique top-level ``seq``/owner fields, so a nested or truncated
    ``"seq"`` can never become replay cursor authority.
    """

    _AUTHORITY_STRING_FIELDS = frozenset({"event_id", "run_id", "session_id"})
    _TERMINAL_STRING_FIELDS = frozenset({"event", "terminal_state"})
    _STRING_FIELDS = _AUTHORITY_STRING_FIELDS | _TERMINAL_STRING_FIELDS
    _CAPTURE_LIMIT = 1024
    _MAX_NESTING = 128

    def __init__(self):
        self._validator = _StreamingJsonValidator()
        self._stack: list[int] = []
        self._state = "start"
        self._invalid = False
        self._in_string = False
        self._escape = False
        self._string_role = ""
        self._string_buf = bytearray()
        self._string_overflow = False
        self._active_key: str | None = None
        self._primitive_buf: bytearray | None = None
        self._primitive_overflow = False
        self._fields: dict[str, object] = {}
        self._seq_seen = 0

    @staticmethod
    def _decode_string(raw: bytearray, overflow: bool) -> str | None:
        if overflow:
            return None
        try:
            value = json.loads((b'"' + bytes(raw) + b'"').decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) else None

    def _append_string_byte(self, value: int) -> None:
        if self._string_role not in {"key", "value"}:
            return
        if len(self._string_buf) >= self._CAPTURE_LIMIT:
            self._string_overflow = True
            return
        self._string_buf.append(value)

    def _record_field(self, key: str, value) -> None:
        if key == "seq":
            self._seq_seen += 1
        if key in self._fields:
            self._invalid = True
            return
        self._fields[key] = value

    def _finish_string(self) -> None:
        role = self._string_role
        decoded = self._decode_string(self._string_buf, self._string_overflow)
        self._in_string = False
        self._escape = False
        self._string_role = ""
        self._string_buf.clear()
        self._string_overflow = False
        if role == "key":
            self._active_key = decoded
            self._state = "colon"
            return
        if role == "value":
            if self._active_key == "seq":
                self._record_field("seq", None)
            elif self._active_key in self._STRING_FIELDS:
                self._record_field(self._active_key, decoded)
                if decoded is None and self._active_key in self._AUTHORITY_STRING_FIELDS:
                    self._invalid = True
            elif self._active_key == "terminal":
                self._record_field("terminal", None)
            self._active_key = None
            self._state = "comma_or_end"

    def _finish_primitive(self) -> None:
        key = self._active_key
        token = bytes(self._primitive_buf or b"").strip()
        if key == "seq":
            if (
                not self._primitive_overflow
                and re.fullmatch(rb"-?(?:0|[1-9]\d*)", token)
            ):
                try:
                    value = int(token)
                except ValueError:
                    value = None
            else:
                value = None
            self._record_field("seq", value)
        elif key in self._STRING_FIELDS:
            self._record_field(key, None)
            if key in self._AUTHORITY_STRING_FIELDS:
                self._invalid = True
        elif key == "terminal":
            self._record_field(
                "terminal",
                True if token == b"true" else False if token == b"false" else None,
            )
        self._primitive_buf = None
        self._primitive_overflow = False
        self._active_key = None
        self._state = "comma_or_end"

    def _push(self, value: int) -> None:
        if len(self._stack) >= self._MAX_NESTING:
            self._invalid = True
            return
        self._stack.append(value)

    def _pop(self, value: int) -> None:
        expected = ord("{") if value == ord("}") else ord("[")
        if not self._stack or self._stack[-1] != expected:
            self._invalid = True
            return
        self._stack.pop()

    def feed(self, chunk: bytes) -> None:
        self._validator.feed(chunk)
        if self._invalid:
            return
        for value in chunk:
            if self._in_string:
                if self._escape:
                    self._append_string_byte(value)
                    self._escape = False
                elif value == ord("\\"):
                    self._append_string_byte(value)
                    self._escape = True
                elif value == ord('"'):
                    self._finish_string()
                else:
                    self._append_string_byte(value)
                continue

            if self._primitive_buf is not None:
                if len(self._stack) == 1 and value in {ord(","), ord("}")}:
                    self._finish_primitive()
                else:
                    if len(self._primitive_buf) >= self._CAPTURE_LIMIT:
                        self._primitive_overflow = True
                    else:
                        self._primitive_buf.append(value)
                    continue

            if value in b" \t\r\n":
                continue
            if self._state == "closed":
                self._invalid = True
                return
            if self._state == "start":
                if value != ord("{"):
                    self._invalid = True
                    return
                self._push(value)
                self._state = "key_or_end"
                continue

            depth = len(self._stack)
            if value == ord('"'):
                self._in_string = True
                self._escape = False
                self._string_buf.clear()
                self._string_overflow = False
                if depth == 1 and self._state == "key_or_end":
                    self._string_role = "key"
                elif depth == 1 and self._state == "value":
                    self._string_role = "value"
                else:
                    self._string_role = "nested"
                continue

            if depth > 1:
                if value in {ord("{"), ord("[")}:
                    self._push(value)
                elif value in {ord("}"), ord("]")}:
                    self._pop(value)
                    if len(self._stack) == 1 and self._state == "value_nested":
                        self._active_key = None
                        self._state = "comma_or_end"
                continue

            if self._state == "colon":
                if value != ord(":"):
                    self._invalid = True
                    return
                self._state = "value"
                continue
            if self._state == "value":
                if value in {ord("{"), ord("[")}:
                    if self._active_key in self._STRING_FIELDS or self._active_key in {
                        "seq", "terminal",
                    }:
                        if self._active_key == "seq":
                            self._record_field("seq", None)
                        else:
                            self._record_field(self._active_key, None)
                        if self._active_key in self._AUTHORITY_STRING_FIELDS:
                            self._invalid = True
                    self._push(value)
                    self._state = "value_nested"
                    continue
                self._primitive_buf = bytearray([value])
                self._primitive_overflow = False
                continue
            if self._state == "key_or_end":
                if value == ord("}"):
                    self._pop(value)
                    self._state = "closed"
                    continue
                self._invalid = True
                return
            if self._state == "comma_or_end":
                if value == ord(","):
                    self._state = "key_or_end"
                    continue
                if value == ord("}"):
                    self._pop(value)
                    self._state = "closed"
                    continue
                self._invalid = True
                return

    def snapshot(self) -> dict[str, Any]:
        """Serialize bounded envelope/parser state, never row payload bytes."""
        stack = bytes(self._stack)
        string_buf = bytes(self._string_buf)
        primitive_buf = (
            None if self._primitive_buf is None else bytes(self._primitive_buf)
        )
        return {
            "v": 1,
            "s": self._state,
            "k": _encode_urlsafe_unpadded(stack),
            "i": self._invalid,
            "w": self._in_string,
            "e": self._escape,
            "r": self._string_role,
            "b": _encode_urlsafe_unpadded(string_buf),
            "bo": self._string_overflow,
            "a": self._active_key,
            "p": (
                None
                if primitive_buf is None
                else _encode_urlsafe_unpadded(primitive_buf)
            ),
            "po": self._primitive_overflow,
            "f": dict(self._fields),
            "q": self._seq_seen,
            "j": self._validator.snapshot(),
        }

    @classmethod
    def from_snapshot(cls, state: Any) -> "_TopLevelEnvelopeScanner":
        if not isinstance(state, dict):
            raise ValueError("invalid replay envelope state")
        scanner = cls()
        try:
            if state.get("v") != 1:
                raise ValueError("invalid replay envelope state")
            parser_state = state["s"]
            if parser_state not in {
                "start", "key_or_end", "colon", "value", "value_nested",
                "comma_or_end", "closed",
            }:
                raise ValueError("invalid replay envelope state")
            stack_token = state["k"]
            if not isinstance(stack_token, str) or len(stack_token) > 256:
                raise ValueError("invalid replay envelope state")
            stack = _decode_urlsafe_unpadded(stack_token)
            if len(stack) > cls._MAX_NESTING or any(value not in {91, 123} for value in stack):
                raise ValueError("invalid replay envelope state")
            bool_fields = ("i", "w", "e", "bo", "po")
            if any(not isinstance(state[name], bool) for name in bool_fields):
                raise ValueError("invalid replay envelope state")
            role = state["r"]
            if role not in {"", "key", "value", "nested"}:
                raise ValueError("invalid replay envelope state")
            string_token = state["b"]
            if not isinstance(string_token, str) or len(string_token) > 2048:
                raise ValueError("invalid replay envelope state")
            string_buf = _decode_urlsafe_unpadded(string_token)
            if len(string_buf) > cls._CAPTURE_LIMIT:
                raise ValueError("invalid replay envelope state")
            active_key = state["a"]
            if active_key is not None and (
                not isinstance(active_key, str) or len(active_key) > cls._CAPTURE_LIMIT
            ):
                raise ValueError("invalid replay envelope state")
            primitive_token = state["p"]
            if primitive_token is None:
                primitive_buf = None
            else:
                if not isinstance(primitive_token, str) or len(primitive_token) > 2048:
                    raise ValueError("invalid replay envelope state")
                primitive_buf = _decode_urlsafe_unpadded(primitive_token)
                if len(primitive_buf) > cls._CAPTURE_LIMIT:
                    raise ValueError("invalid replay envelope state")
            fields = state["f"]
            if not isinstance(fields, dict) or set(fields) - {
                "seq", "event_id", "run_id", "session_id", "event", "terminal_state",
                "terminal",
            }:
                raise ValueError("invalid replay envelope state")
            for key, value in fields.items():
                if key == "seq":
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                    ):
                        raise ValueError("invalid replay envelope state")
                elif key == "terminal":
                    if value is not None and not isinstance(value, bool):
                        raise ValueError("invalid replay envelope state")
                elif value is not None and (
                    not isinstance(value, str) or len(value) > cls._CAPTURE_LIMIT
                ):
                    raise ValueError("invalid replay envelope state")
            seq_seen = state["q"]
            if isinstance(seq_seen, bool) or not isinstance(seq_seen, int) or not 0 <= seq_seen <= 2:
                raise ValueError("invalid replay envelope state")
            validator = _StreamingJsonValidator.from_snapshot(state["j"])
            scanner._validator = validator
            scanner._stack = list(stack)
            scanner._state = parser_state
            scanner._invalid = state["i"]
            scanner._in_string = state["w"]
            scanner._escape = state["e"]
            scanner._string_role = role
            scanner._string_buf = bytearray(string_buf)
            scanner._string_overflow = state["bo"]
            scanner._active_key = active_key
            scanner._primitive_buf = (
                None if primitive_buf is None else bytearray(primitive_buf)
            )
            scanner._primitive_overflow = state["po"]
            scanner._fields = dict(fields)
            scanner._seq_seen = seq_seen
        except (KeyError, TypeError, ValueError):
            raise ValueError("invalid replay envelope state") from None
        return scanner

    def authoritative_seq(
        self,
        session_id: str,
        run_id: str,
        *,
        require_complete_json: bool,
    ) -> tuple[int | None, str]:
        seq = self._fields.get("seq")
        if self._seq_seen != 1 or isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            return None, "replay_invalid_seq"
        if (
            (require_complete_json and not self._validator.valid())
            or self._invalid
            or self._state != "closed"
            or self._stack
            or self._in_string
        ):
            return None, "replay_invalid_envelope"
        if (
            self._fields.get("session_id") != str(session_id)
            or self._fields.get("run_id") != str(run_id)
            or self._fields.get("event_id") != f"{run_id}:{seq}"
        ):
            return None, "replay_invalid_identity"
        return seq, ""

    def recovered_terminal_event(
        self,
        *,
        session_id: str,
        run_id: str,
        max_seq: int | None,
    ) -> dict | None:
        return _recover_legacy_overcap_terminal_event(
            self._fields,
            session_id=session_id,
            run_id=run_id,
            max_seq=max_seq,
        )


def _read_bounded_physical_row(
    fh,
    *,
    max_scan_bytes: int,
    scanner: _TopLevelEnvelopeScanner | None = None,
    max_retained_bytes: int | None = None,
) -> tuple[bytes | None, _TopLevelEnvelopeScanner, int, bool] | None:
    """Read one JSONL row within the remaining aggregate physical-read budget."""
    resumed = scanner is not None
    scanner = scanner or _TopLevelEnvelopeScanner()
    retention_limit = (
        _LEGACY_TERMINAL_RECOVERY_MAX_BYTES
        if max_retained_bytes is None
        else max(0, int(max_retained_bytes))
    )
    retained = bytearray()
    total_bytes = 0
    while total_bytes < max_scan_bytes:
        remaining_scan = max_scan_bytes - total_bytes
        chunk = fh.readline(min(_SESSION_REPLAY_READ_CHUNK_BYTES, remaining_scan))
        if not chunk:
            break
        total_bytes += len(chunk)
        scanner.feed(chunk)
        if not resumed:
            remaining = retention_limit + 1 - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
        if chunk.endswith(b"\n"):
            return (
                (
                    None
                    if resumed or total_bytes > retention_limit
                    else bytes(retained)
                ),
                scanner,
                total_bytes,
                True,
            )
    if total_bytes == 0:
        return None
    try:
        complete = fh.tell() >= int(os.fstat(fh.fileno()).st_size)
    except (OSError, ValueError):
        complete = False
    return (
        (
            None
            if resumed or total_bytes > retention_limit
            else bytes(retained)
        ),
        scanner,
        total_bytes,
        complete,
    )


def _replay_limit_result(
    session_id: str,
    run_id: str,
    events: list[dict],
    malformed: list[dict],
    *,
    line_no: int,
    reason: str,
    next_after_seq: int,
    resume_token: str | None,
    scanned_bytes: int,
    scanned_rows: int,
    malformed_count: int,
    record_limit_diagnostic: bool = True,
) -> dict:
    diagnostics = list(malformed)
    if record_limit_diagnostic and len(diagnostics) < _BOUNDED_REPLAY_MAX_MALFORMED:
        diagnostics.append({"line": line_no, "reason": reason})
    return {
        "session_id": str(session_id), "run_id": str(run_id), "events": events,
        "malformed": diagnostics,
        "complete": False, "limit_reason": reason, "next_after_seq": next_after_seq,
        "resume_token": resume_token,
        "scanned_bytes": scanned_bytes,
        "scanned_rows": scanned_rows,
        "malformed_count": malformed_count,
    }


def read_run_events(
    session_id: str,
    run_id: str,
    *,
    after_seq: int | None = None,
    max_seq: int | None = None,
    session_dir: Path | None = None,
    max_bytes: int | None = None,
    max_rows: int | None = None,
    resume_token: str | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    if max_bytes is None and max_rows is None:
        if resume_token is not None:
            raise ValueError("resume_token requires bounded replay")
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

    row_cap = None if max_rows is None else int(max_rows)
    if row_cap is not None and row_cap < 1:
        raise ValueError("max_rows must be at least 1")
    byte_cap = None if max_bytes is None else max(0, int(max_bytes))
    floor = int(after_seq) if after_seq is not None else None
    ceiling = int(max_seq) if max_seq is not None else None
    events: list[dict] = []
    malformed: list[dict] = []
    malformed_count = 0
    emitted_bytes = 0
    scanned_bytes = 0
    scanned_rows = 0
    next_after_seq = floor or 0
    last_physical_seq = 0
    fh = None
    generation = None
    missing = False
    with _run_journal_lifecycle_authority(path):
        try:
            fh = path.open("rb")
        except FileNotFoundError:
            missing = True
        else:
            try:
                stat = os.fstat(fh.fileno())
                generation = _ensure_run_generation_for_read_locked(
                    path,
                    identity=(int(stat.st_dev), int(stat.st_ino)),
                )
            except BaseException:
                fh.close()
                raise
    if missing:
        if resume_token is not None:
            return _replay_limit_result(
                str(session_id), str(run_id), events, malformed,
                line_no=0, reason="replay_cursor_invalid",
                next_after_seq=next_after_seq, resume_token=None,
                scanned_bytes=0, scanned_rows=0, malformed_count=0,
                record_limit_diagnostic=False,
            )
        return {
            "session_id": str(session_id), "run_id": str(run_id), "events": events,
            "malformed": malformed, "complete": True, "limit_reason": None,
            "next_after_seq": next_after_seq,
            "resume_token": None, "scanned_bytes": 0, "scanned_rows": 0,
            "malformed_count": 0,
        }
    assert fh is not None
    with fh:
        if generation is None:
            return _replay_limit_result(
                str(session_id), str(run_id), events, malformed,
                line_no=0, reason="replay_cursor_invalid",
                next_after_seq=next_after_seq, resume_token=None,
                scanned_bytes=0, scanned_rows=0, malformed_count=0,
                record_limit_diagnostic=False,
            )
        line_no = 0
        resumed_scanner: _TopLevelEnvelopeScanner | None = None

        def continuation_token(
            *,
            offset: int,
            logical_seq: int,
            physical_seq: int,
            completed_lines: int,
            scanner: _TopLevelEnvelopeScanner | None = None,
        ) -> str | None:
            try:
                return _encode_replay_resume_token(
                    fh,
                    session_id=str(session_id),
                    run_id=str(run_id),
                    max_seq=ceiling,
                    offset=offset,
                    next_after_seq=logical_seq,
                    last_physical_seq=physical_seq,
                    line_no=completed_lines,
                    generation=generation,
                    scanner=scanner,
                )
            except (OSError, TypeError, ValueError):
                return None

        if resume_token is not None:
            resumed = _decode_replay_resume_token(
                fh,
                resume_token,
                session_id=str(session_id),
                run_id=str(run_id),
                expected_after_seq=floor,
                expected_max_seq=ceiling,
                expected_generation=generation,
            )
            if resumed is None:
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=0, reason="replay_cursor_invalid",
                    next_after_seq=next_after_seq, resume_token=None,
                    scanned_bytes=0, scanned_rows=0, malformed_count=0,
                    record_limit_diagnostic=False,
                )
            (
                _offset,
                next_after_seq,
                last_physical_seq,
                line_no,
                boundary_scan_bytes,
                resumed_scanner,
            ) = resumed
            scanned_bytes += boundary_scan_bytes
            floor = next_after_seq
        page_start_offset = fh.tell()
        while True:
            try:
                at_eof = fh.tell() >= int(os.fstat(fh.fileno()).st_size)
            except (OSError, ValueError):
                at_eof = False
            if at_eof:
                break
            if scanned_rows >= _BOUNDED_REPLAY_MAX_SCAN_ROWS:
                continuation = continuation_token(
                    offset=fh.tell(),
                    logical_seq=next_after_seq,
                    physical_seq=last_physical_seq,
                    completed_lines=line_no,
                    scanner=resumed_scanner,
                )
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=line_no + 1, reason="replay_scan_limit_rows",
                    next_after_seq=next_after_seq, resume_token=continuation,
                    scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                    malformed_count=malformed_count, record_limit_diagnostic=False,
                )
            remaining_scan_bytes = _BOUNDED_REPLAY_MAX_SCAN_BYTES - scanned_bytes
            if remaining_scan_bytes <= 0:
                continuation = continuation_token(
                    offset=fh.tell(),
                    logical_seq=next_after_seq,
                    physical_seq=last_physical_seq,
                    completed_lines=line_no,
                    scanner=resumed_scanner,
                )
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=line_no + 1, reason="replay_scan_limit_bytes",
                    next_after_seq=next_after_seq, resume_token=continuation,
                    scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                    malformed_count=malformed_count, record_limit_diagnostic=False,
                )
            row_start_offset = fh.tell()
            row_start_seq = next_after_seq
            row_start_physical_seq = last_physical_seq
            row_start_line_no = line_no
            row_scanner = resumed_scanner
            resumed_scanner = None
            row = _read_bounded_physical_row(
                fh,
                max_scan_bytes=remaining_scan_bytes,
                scanner=row_scanner,
                max_retained_bytes=(
                    max(
                        _LEGACY_TERMINAL_RECOVERY_MAX_BYTES,
                        byte_cap or 0,
                    )
                    if floor is not None
                    else _LEGACY_TERMINAL_RECOVERY_MAX_BYTES
                ),
            )
            if row is None:
                break
            scanned_rows += 1
            raw_bytes, envelope, row_bytes, row_complete = row
            scanned_bytes += row_bytes
            if not row_complete:
                if row_start_offset > page_start_offset:
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                else:
                    continuation = continuation_token(
                        offset=fh.tell(),
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                        scanner=envelope,
                    )
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=row_start_line_no + 1, reason="replay_scan_limit_bytes",
                    next_after_seq=row_start_seq, resume_token=continuation,
                    scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                    malformed_count=malformed_count, record_limit_diagnostic=False,
                )
            line_no += 1
            if raw_bytes is not None and not raw_bytes.strip():
                continue
            event = None
            if raw_bytes is not None:
                try:
                    event = json.loads(
                        raw_bytes.decode("utf-8"),
                        parse_constant=_reject_nonfinite_json_value,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed_count += 1
                    if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                        malformed.append({"line": line_no, "raw": ""})
                    continue
                except _ReplayInvalidJsonEnvelope:
                    malformed_count += 1
                    if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                        malformed.append({"line": line_no, "reason": "replay_invalid_envelope"})
                    continue
                except ValueError:
                    malformed_count += 1
                    if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                        malformed.append({"line": line_no, "reason": "replay_invalid_json_value"})
                    continue
                if not isinstance(event, dict):
                    malformed_count += 1
                    if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                        malformed.append({"line": line_no, "raw": ""})
                    continue
                seq, identity_error = envelope.authoritative_seq(
                    str(session_id), str(run_id), require_complete_json=True,
                )
            else:
                seq, identity_error = envelope.authoritative_seq(
                    str(session_id), str(run_id), require_complete_json=True,
                )
            if seq is None:
                malformed_count += 1
                if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                    malformed.append({"line": line_no, "reason": identity_error})
                continue
            if seq <= last_physical_seq:
                malformed_count += 1
                if len(malformed) < _BOUNDED_REPLAY_MAX_MALFORMED:
                    malformed.append({"line": line_no, "reason": "replay_invalid_seq_order"})
                continue
            last_physical_seq = seq
            if floor is not None and seq <= floor:
                continue
            if ceiling is not None and seq > ceiling:
                continue
            if row_cap is not None and len(events) >= row_cap:
                continuation = continuation_token(
                    offset=row_start_offset,
                    logical_seq=row_start_seq,
                    physical_seq=row_start_physical_seq,
                    completed_lines=row_start_line_no,
                )
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=line_no, reason="replay_limit_rows",
                    next_after_seq=row_start_seq, resume_token=continuation,
                    scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                    malformed_count=malformed_count,
                )
            if raw_bytes is None:
                recovered_event = envelope.recovered_terminal_event(
                    session_id=str(session_id),
                    run_id=str(run_id),
                    max_seq=ceiling,
                )
                recovered_size = (
                    _serialized_event_size(recovered_event)
                    if recovered_event is not None
                    else None
                )
                if recovered_event is None:
                    # This row cannot be materialized within the hard-line ceiling.
                    # Its exact top-level identity has been consumed and the complete
                    # physical line drained, so the next page can safely advance.
                    continuation = continuation_token(
                        offset=fh.tell(),
                        logical_seq=seq,
                        physical_seq=last_physical_seq,
                        completed_lines=line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                assert recovered_size is not None
                if byte_cap is not None and recovered_size > byte_cap:
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=row_start_seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                if byte_cap is not None and emitted_bytes + recovered_size > byte_cap:
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=row_start_seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                events.append(recovered_event)
                emitted_bytes += recovered_size
                next_after_seq = seq
                continue
            assert event is not None
            event_size = _serialized_event_size(event)
            if byte_cap is not None and emitted_bytes + event_size > byte_cap:
                if event_size <= byte_cap:
                    # The candidate fits a fresh page. It has not been emitted or
                    # dispositioned, so leave the cursor on the last delivered row.
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=row_start_seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                recovered_event = (
                    _recover_legacy_overcap_terminal_event(
                        event,
                        session_id=str(session_id),
                        run_id=str(run_id),
                        max_seq=ceiling,
                    )
                    if event.get("terminal") is True
                    else None
                )
                recovered_size = (
                    _serialized_event_size(recovered_event)
                    if recovered_event is not None
                    else None
                )
                if recovered_event is None:
                    # This row cannot fit even on an empty page. Consume its exact
                    # sequence as a non-materializable disposition to ensure progress.
                    continuation = continuation_token(
                        offset=fh.tell(),
                        logical_seq=seq,
                        physical_seq=last_physical_seq,
                        completed_lines=line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                assert recovered_size is not None
                if recovered_size > byte_cap:
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=row_start_seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                if emitted_bytes + recovered_size > byte_cap:
                    continuation = continuation_token(
                        offset=row_start_offset,
                        logical_seq=row_start_seq,
                        physical_seq=row_start_physical_seq,
                        completed_lines=row_start_line_no,
                    )
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=row_start_seq, resume_token=continuation,
                        scanned_bytes=scanned_bytes, scanned_rows=scanned_rows,
                        malformed_count=malformed_count,
                    )
                event = recovered_event
                event_size = recovered_size
            events.append(event)
            emitted_bytes += event_size
            next_after_seq = seq
    return {
        "session_id": str(session_id), "run_id": str(run_id), "events": events,
        "malformed": malformed, "complete": True, "limit_reason": None,
        "next_after_seq": next_after_seq,
        "resume_token": None, "scanned_bytes": scanned_bytes,
        "scanned_rows": scanned_rows, "malformed_count": malformed_count,
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
    deletion_lock_path = session_journal_dir / ".delete.jsonl"
    # The lifecycle authority covers the existence check, rmtree, residual
    # validation, and cache eviction. Writers cannot publish a late append or
    # recreate the per-run lock/cache while this block owns the session stripe.
    with _run_journal_lifecycle_authority(deletion_lock_path):
        session_exists = session_journal_dir.exists()
        # Rotate the durable admission before removing bytes.  This retires
        # every RunJournalWriter constructed before this delete, including one
        # that has not appended yet, while leaving the authority record outside
        # the subtree that rmtree removes.  A missing session with no admitted
        # writer remains a no-op and does not create any state.
        retired = _retire_run_journal_incarnation_locked(
            deletion_lock_path,
            session_exists=session_exists,
        )
        if not session_exists or not retired:
            if retired:
                _evict_run_journal_session_state(session_journal_dir)
            return False
        shutil.rmtree(session_journal_dir)
        if session_journal_dir.exists():
            raise OSError("run journal cleanup left residual files")
        # Cache eviction stays inside the lifecycle authority so a new writer
        # cannot split from an old lock during deletion.  It also resets the
        # next-seq/summary caches, ensuring a recreated path starts at seq 1.
        _evict_run_journal_session_state(session_journal_dir)
        return True


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
