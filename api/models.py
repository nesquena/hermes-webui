"""Hermes Web UI -- Session model and in-memory session store."""
import collections
import copy
import datetime
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

try:  # pragma: no cover - platform-specific imports.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

try:  # pragma: no cover - platform-specific imports.
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None

import api.config as _cfg
from api.compression_anchor import is_context_compression_marker
from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, STREAMS, STREAMS_LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL, PROJECTS_FILE, HOME,
    get_effective_default_model, _get_session_agent_lock,
)
from api.workspace import get_last_workspace
from api.usage import prompt_cache_hit_percent
from api.agent_sessions import (
    _is_continuation_session,
    is_cli_session_row,
    normalize_agent_session_source,
    open_state_db_readonly,
    read_importable_agent_session_rows,
    read_session_lineage_metadata,
)
from api.process_event_utils import stamp_message_source

logger = logging.getLogger(__name__)
CLI_VISIBLE_SESSION_LIMIT = 20
# How many messageful cron sessions to surface in the project-chip layer.
# Needs to exceed CLI_VISIBLE_SESSION_LIMIT so older cron runs stay
# addressable even when many newer non-cron sessions dominate the default
# sidebar window (#3172).
CRON_PROJECT_CHIP_LIMIT = 200
WEBHOOK_PROJECT_CHIP_LIMIT = 200
_CLI_SESSIONS_CACHE_TTL_SECONDS = 5.0
# While a turn is actively streaming, hold the CLI/cron projection longer than
# one poll interval (mirrors the route-level #4808 hold-down). The frontend
# polls /api/sessions on the static/sessions.js `_streamingPollMs` cadence.
# Pair this wider window with the stable streaming cache key below so repeated
# polls reuse the projection instead of re-running the expensive state.db
# CLI/cron projection. (#4842) Keep this strictly greater than
# `_streamingPollMs`/1000 (see tests/test_streaming_cache_ttl_vs_poll.py).
_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 45.0
_CLI_SESSIONS_CACHE_LOCK = threading.Lock()
_CLI_SESSIONS_CACHE_INFLIGHT: "dict[tuple, threading.Event]" = {}
_CLI_SESSIONS_CACHE_INVALIDATION_VERSION = 0
# LRU-bounded (drop-oldest) so a long-lived process under churn — where the
# state.db fingerprint advances on every streamed message and the structural
# clear-on-mutation listener doesn't fire for every fingerprint advance — can't
# accumulate orphaned heavy deepcopies. Each value is a copy.deepcopy() of the
# full CLI/cron session list (the expensive projection behind #4842/#4672), so
# the cap is deliberately small. TTL is still the primary freshness control;
# the cap is the backstop that the plain dict previously lacked. Mirrors the
# _CLAUDE_CODE_PARSE_CACHE / _SIDECAR_METADATA_CACHE LRU pattern.
_CLI_SESSIONS_CACHE: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
_CLI_SESSIONS_CACHE_MAX_ENTRIES = 8
_CLI_SESSIONS_CACHE_WAIT_SECONDS = 0.25
# Event waits that keep stale rows visible while a rebuild is in flight.
_CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10

# Per-file parse cache for Claude Code JSONL transcripts (#4718/#4662 phase 4).
# ``~/.claude/projects`` is a GLOBAL, profile-independent directory, but the
# sidebar re-derives every Claude Code row from scratch on each /api/sessions
# build — fully re-reading and JSON-parsing up to CLAUDE_CODE_MAX_FILES
# transcripts line-by-line (hundreds of MB) just to recover a title + message
# count. That parse dominates the cold sidebar build (~650-1000ms measured on a
# 200-file / ~130MB tree) and it repeats on every profile switch, on the 5s
# CLI-cache expiry, and on every sidebar poll, because the higher CLI cache is
# keyed per active profile while the underlying transcripts never change between
# switches. This cache memoizes the EXPENSIVE per-file parse result keyed by the
# file's (path, mtime_ns, size, ctime_ns); a warm sidebar build then re-stats the
# files (~4ms for 200) instead of re-parsing them. Any external edit/append to a
# transcript changes mtime_ns/size/ctime_ns and transparently invalidates just
# that one file's entry. Bounded so a pathological projects tree can't grow it unbounded.
_CLAUDE_CODE_PARSE_CACHE_LOCK = threading.Lock()
_CLAUDE_CODE_PARSE_CACHE: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()
_CLAUDE_CODE_PARSE_CACHE_MAX = 1000

# Per-file cache for the UI-owned sidecar metadata (title + archived) that the
# state.db sidebar projection overlays onto each CLI/cron row (#4842). The
# projection calls _state_projection_sidecar_metadata() once per row in BOTH
# the main visible pass AND the higher-capped (CRON_PROJECT_CHIP_LIMIT=200)
# cron-only second pass, and each call was an uncached open() + 64KB prefix
# read + a pure-Python JSON-key scan. On a cron-heavy profile that is up to
# ~200 sidecar file reads per /api/sessions build — and because the enclosing
# _CLI_SESSIONS_CACHE is keyed on a state.db content fingerprint that advances
# on every streamed message row, that whole scan was re-paid on essentially
# every streaming poll during a live turn (the "100% CPU / multi-second get_cli_sessions"
# in #4842/#4808/#4672). This memoizes the parse result keyed by the sidecar's
# (path, mtime_ns, size, ctime_ns) stat signature: a warm projection re-stats
# each file (~1 stat) instead of re-reading+parsing it, while any genuine
# rename/archive/edit bumps the signature and transparently invalidates just
# that one entry. Bounded so a pathological session store can't grow it without
# limit. Mirrors the Claude Code parse cache (#4718).
_SIDECAR_METADATA_CACHE_LOCK = threading.Lock()
_SIDECAR_METADATA_CACHE: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
_SIDECAR_METADATA_CACHE_MAX = 2000

# #5854: authoritative facts for a LEGACY (pre-#5854) sidecar whose scenes
# serialize before `messages`, so the cheap metadata-prefix read can't recover
# its message_count or scene fingerprint. Without this, an unchanged legacy
# large-scene session would full-parse on every poll (recreating the #4633
# churn for legacy files) and could not be LRU-evicted. Populated once per file
# from a full Session.load(); keyed by the sidecar's stat signature so any edit
# invalidates it. Bounded. Value: {"message_count": int, "scene_index": dict}.
_LEGACY_SIDECAR_FACTS_LOCK = threading.Lock()
_LEGACY_SIDECAR_FACTS: "collections.OrderedDict[tuple, dict]" = collections.OrderedDict()
_LEGACY_SIDECAR_FACTS_MAX = 2000

# ---------------------------------------------------------------------------
# Stale temp-file cleanup
# ---------------------------------------------------------------------------
# Both Session.save() and _write_session_index() use the atomic-write pattern:
#   write to  <path>.tmp.<pid>.<tid>  →  os.replace() to final path
# If the process crashes between write and replace the .tmp file is left
# behind.  Because the name embeds pid + tid, leftover files can never be
# reused by a different process/thread, so they are safe to remove on the
# next startup.  _cleanup_stale_tmp_files() is called from the full-rebuild
# path of _write_session_index (i.e. at first index access / startup) and
# removes any *.tmp.* file whose mtime is older than one hour.
# ---------------------------------------------------------------------------

_STALE_TMP_AGE_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Windows-safe os.replace() with retry
# ---------------------------------------------------------------------------
# On Windows, os.replace() raises WinError 5 (ERROR_ACCESS_DENIED) when the
# target file is momentarily locked by another process (antivirus scanner,
# browser polling the session JSON, etc.).  This helper retries with
# exponential backoff on PermissionError, which is the Python exception
# mapped from WinError 5.  On non-Windows platforms it is a thin wrapper
# (one attempt, no delay).
# ---------------------------------------------------------------------------

_WINDOWS_REPLACE_MAX_RETRIES = 5
_WINDOWS_REPLACE_INITIAL_DELAY = 0.05  # 50 ms


def _safe_replace(src: Path, dst: Path) -> None:
    """Atomic replace with retries on Windows file-locking errors."""
    if os.name != 'nt':
        os.replace(src, dst)
        return

    delay = _WINDOWS_REPLACE_INITIAL_DELAY
    for attempt in range(_WINDOWS_REPLACE_MAX_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _WINDOWS_REPLACE_MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2  # 50 -> 100 -> 200 -> 400 -> 800 ms


# Serializes index writers so concurrent Session.save() calls cannot race on
# stale baselines while still allowing LOCK to be released before disk I/O.
_INDEX_WRITE_LOCK = threading.RLock()
_SESSION_INDEX_REBUILD_LOCK = threading.Lock()
_SESSION_INDEX_REBUILD_THREAD = None
_SESSION_INDEX_REBUILD_THREAD_TARGET: tuple[Path, Path] | None = None

# Serializes ``_record_webui_zero_message_orphan_tombstone`` /
# ``_clear_webui_zero_message_orphan_tombstone`` so two concurrent sidebar
# polls (or a poll racing ``Session.save`` / ``new_session`` /
# ``import_cli_session``) cannot lose each other's load-modify-write/unlink.
# Without this lock each operation rewrites the entire tombstone file from
# scratch, so a concurrent recorder and clearer can land last-writer-wins and
# silently drop each other's update — defeating the self-healing invariant
# that ``Session.save`` clears the tombstone the same poll that re-prunes
# would otherwise re-add the row for. ``threading.Lock`` is sufficient (the
# WebUI sidebar polling path is single-process) but must wrap the WHOLE
# load-modify-write/unlink sequence in both helpers.
_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK = threading.Lock()
_WEBUI_DELETED_SESSION_TOMBSTONE_LOCK = threading.Lock()

# Path-safety contract for session IDs.  Accept alphanumerics, underscore, and
# hyphen so API/gateway-issued ids (``api-*``, ``reachy-voice-*``) round-trip
# through filesystem load/save/delete/worktree paths without traversal risk.
# Dots and slashes are rejected so the id can never name a parent directory
# or hide an unexpected extension.
_SAFE_SID_CHARS = frozenset(
    '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-'
)


def is_safe_session_id(sid) -> bool:
    """Return True iff ``sid`` is a non-empty path-safe session id.

    Centralizes the validation previously duplicated across
    ``Session.load``, ``Session.load_metadata_only``,
    ``_repair_stale_pending``, ``/api/session/worktree/remove``, and
    ``/api/session/delete`` so every call site agrees on what characters
    are allowed.  See #3023.
    """
    if not sid or not isinstance(sid, str):
        return False
    return all(c in _SAFE_SID_CHARS for c in sid)


def _cleanup_stale_tmp_files() -> None:
    """Best-effort removal of stale ``*.tmp.*`` files from SESSION_DIR.

    Only files whose mtime is older than ``_STALE_TMP_AGE_SECONDS`` are
    removed so that in-flight writes from a long-running sibling process
    are not disturbed.  Errors are logged and swallowed — this must never
    prevent startup.
    """
    cutoff = time.time() - _STALE_TMP_AGE_SECONDS
    try:
        for p in SESSION_DIR.glob('*.tmp.*'):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logger.debug("Cleaned up stale tmp file: %s", p.name)
            except OSError:
                pass  # best-effort
    except Exception:
        pass  # SESSION_DIR may not exist yet; that's fine


_PERSISTED_SESSION_IDS_CACHE: tuple[Path | None, int | None, frozenset[str]] = (None, None, frozenset())


def _persisted_session_ids_snapshot() -> frozenset[str]:
    """Return persisted session ids, caching the directory snapshot by mtime.

    `/api/sessions` and incremental index writes may run every few seconds. A
    full `SESSION_DIR.glob('*.json')` on a large session directory is expensive,
    and doing that scan while request threads contend on LOCK makes the sidebar
    look like it was designed by a committee of glaciers. Cache the listing until
    the directory mtime changes, and let callers take the snapshot before
    entering critical sections.
    """
    global _PERSISTED_SESSION_IDS_CACHE
    try:
        dir_mtime_ns = SESSION_DIR.stat().st_mtime_ns
    except Exception:
        dir_mtime_ns = None
    cached_dir, cached_mtime_ns, cached_ids = _PERSISTED_SESSION_IDS_CACHE
    if cached_dir == SESSION_DIR and cached_mtime_ns == dir_mtime_ns:
        return cached_ids
    try:
        ids = frozenset(
            p.stem
            for p in SESSION_DIR.glob('*.json')
            if not p.name.startswith('_')
        )
    except Exception:
        ids = frozenset()
    _PERSISTED_SESSION_IDS_CACHE = (SESSION_DIR, dir_mtime_ns, ids)
    return ids


def _session_dir_has_persisted_session_files() -> bool:
    """Return True when the current session dir has at least one session JSON file."""
    try:
        return any(not p.name.startswith('_') for p in SESSION_DIR.glob('*.json'))
    except Exception:
        return False


def _rebuild_session_index_background(expected_session_dir: Path, expected_index_file: Path) -> None:
    global _SESSION_INDEX_REBUILD_THREAD, _SESSION_INDEX_REBUILD_THREAD_TARGET
    current_thread = threading.current_thread()
    try:
        with _SESSION_INDEX_REBUILD_LOCK:
            if SESSION_DIR != expected_session_dir or SESSION_INDEX_FILE != expected_index_file:
                return
        _write_session_index(
            updates=None,
            session_dir=expected_session_dir,
            session_index_file=expected_index_file,
        )
    except Exception:
        logger.debug("Background session-index rebuild failed", exc_info=True)
    finally:
        with _SESSION_INDEX_REBUILD_LOCK:
            if _SESSION_INDEX_REBUILD_THREAD is current_thread and _SESSION_INDEX_REBUILD_THREAD_TARGET == (
                expected_session_dir,
                expected_index_file,
            ):
                _SESSION_INDEX_REBUILD_THREAD = None
                _SESSION_INDEX_REBUILD_THREAD_TARGET = None


def _start_session_index_rebuild_thread() -> None:
    """Start one background full-index rebuild if the index is missing."""
    global _SESSION_INDEX_REBUILD_THREAD, _SESSION_INDEX_REBUILD_THREAD_TARGET
    target = (SESSION_DIR, SESSION_INDEX_FILE)
    with _SESSION_INDEX_REBUILD_LOCK:
        if SESSION_INDEX_FILE.exists():
            return
        if (
            _SESSION_INDEX_REBUILD_THREAD is not None
            and _SESSION_INDEX_REBUILD_THREAD.is_alive()
            and _SESSION_INDEX_REBUILD_THREAD_TARGET == target
        ):
            return
        _SESSION_INDEX_REBUILD_THREAD_TARGET = target
        _SESSION_INDEX_REBUILD_THREAD = threading.Thread(
            target=_rebuild_session_index_background,
            args=target,
            name="session-index-rebuild",
            daemon=True,
        )
        _SESSION_INDEX_REBUILD_THREAD.start()


def _index_entry_exists(session_id: str, in_memory_ids=None) -> bool:
    """Return True if an index entry still has backing state.

    A session can legitimately exist either as a persisted JSON file or as an
    in-memory Session object that has not been flushed yet.  This helper is used
    to prune stale `_index.json` rows left behind after session-id rotation or
    file removal.
    """
    if not session_id:
        return False
    if in_memory_ids is None:
        with LOCK:
            in_memory_ids = set(SESSIONS.keys())
    if session_id in in_memory_ids:
        return True
    p = SESSION_DIR / f'{session_id}.json'
    return p.exists()


def _write_session_index(updates=None, *, session_dir: Path | None = None, session_index_file: Path | None = None):
    """Update the session index file.

    When *updates* is provided (a list of Session objects whose compact
    entries should be refreshed), this does a targeted in-place update of
    the existing index — O(1) for single-session changes.  When *updates*
    is None, a full rebuild is performed (used on startup / first call).

    LOCK protects only in-memory session snapshots.  JSON parsing, payload
    construction, and disk I/O run outside LOCK so active-stream saves do not
    block ordinary session reads longer than necessary.  The on-disk index
    read-modify-write is NOT unsynchronized: it stays fully serialized by
    ``_INDEX_WRITE_LOCK`` (held across this whole function), so narrowing LOCK
    cannot introduce a lost-update or index-corruption race between writers.
    """
    session_dir = session_dir or SESSION_DIR
    session_index_file = session_index_file or SESSION_INDEX_FILE
    _tmp = session_index_file.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')

    with _INDEX_WRITE_LOCK:
        # Lazy full-rebuild path — used when index doesn't exist yet.
        if updates is None or not session_index_file.exists():
            _cleanup_stale_tmp_files()  # best-effort sweep on startup / first call
            entry_map: dict[str, dict] = {}
            for p in session_dir.glob('*.json'):
                if p.name.startswith('_'):
                    continue
                try:
                    s = _load_session_from_path(p)
                    if s:
                        c = s.compact()
                        sid = c.get('session_id')
                        if sid:
                            # Dedup by session_id: prefer entry with more messages
                            # (handles old-format session_xxx.json files alongside
                            #  WebUI-format xxx.json with the same session_id)
                            existing = entry_map.get(sid)
                            if existing is None or (
                                c.get('message_count', 0) > existing.get('message_count', 0)
                            ):
                                entry_map[sid] = c
                except Exception:
                    logger.debug("Failed to load session from %s", p)
            entries = list(entry_map.values())

            existing_ids = set(entry_map.keys())
            with LOCK:
                in_memory_entries = [
                    s.compact()
                    for s in SESSIONS.values()
                    if s.session_id not in existing_ids
                ]
            entries.extend(in_memory_entries)
            entries.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
            _payload = json.dumps(entries, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, session_index_file)
            except Exception:
                # Best-effort cleanup of stale tmp on failure
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            return

        # Fast path: patch existing index with updated sessions.
        # This avoids loading every session file on every single save().
        _fallback = False
        try:
            # Avoid N filesystem exists() checks under LOCK by collecting
            # on-disk IDs once before entering the critical section.
            on_disk_ids = _persisted_session_ids_snapshot()
            existing = json.loads(session_index_file.read_bytes())
            if not isinstance(existing, list):
                raise ValueError("session index must be a list")
            with LOCK:
                in_memory_ids = set(SESSIONS.keys())
                updated_map = {s.session_id: s.compact() for s in updates}

            existing = [
                e for e in existing
                if (e.get('session_id') in in_memory_ids or e.get('session_id') in on_disk_ids)
            ]

            existing_ids = {e.get('session_id') for e in existing}
            # Add any updated entries not yet in the index.
            for sid, entry in updated_map.items():
                if sid not in existing_ids:
                    existing.append(entry)
            # Replace matching entries in-place.
            for i, e in enumerate(existing):
                sid = e.get('session_id')
                if sid in updated_map:
                    existing[i] = updated_map[sid]
            existing.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
            _payload = json.dumps(existing, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, session_index_file)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception:
            _fallback = True

    if _fallback:
        # Corrupt or missing index — fall back to full rebuild (called outside LOCK to avoid deadlock).
        # Propagate the resolved target so a rebuild scoped to a specific session dir
        # (the background rebuild thread) falls back to rebuilding THAT dir's index,
        # not the global SESSION_DIR (Opus advisor, stage-344 — defensive; today the
        # only kwargs-caller passes updates=None and never reaches the fast path).
        _write_session_index(
            updates=None,
            session_dir=session_dir,
            session_index_file=session_index_file,
        )


def prune_session_from_index(session_id: str) -> None:
    """Remove one session row from the persisted sidebar index if present."""
    sid = str(session_id or "")
    if not sid or not SESSION_INDEX_FILE.exists():
        return
    _tmp = SESSION_INDEX_FILE.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')

    _fallback = False
    with _INDEX_WRITE_LOCK:
        try:
            with LOCK:
                existing = json.loads(SESSION_INDEX_FILE.read_bytes())
                if not isinstance(existing, list):
                    raise ValueError("session index must be a list")
                pruned = [e for e in existing if e.get('session_id') != sid]
                if len(pruned) == len(existing):
                    return
                _payload = json.dumps(pruned, ensure_ascii=False, indent=2)

            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                _safe_replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception:
            _fallback = True

    if _fallback:
        _write_session_index(updates=None)


# ---------------------------------------------------------------------------
# #4985 webui zero-message orphan tombstone
# ---------------------------------------------------------------------------
# ``prune_session_from_index()`` only removes a row from SESSION_INDEX_FILE —
# the on-disk sidecar at ``SESSION_DIR / f"{sid}.json"`` is intentionally
# kept (it may hold legitimate WebUI-owned metadata a future code path wants
# to recover). On the next ``/api/sessions`` poll, ``all_sessions()``'s
# ``recover_missing_index_sidecars`` pass (``missing_persisted_ids``) sees
# the orphaned sidecar, re-loads it via ``Session.load_metadata_only()``,
# and writes it back to SESSION_INDEX_FILE — undoing the prune.
#
# For #4985 zero-message webui orphans, that round-trip would also re-add
# the row to the sidebar (it survives #1171 because it is titled or has a
# stale positive message_count), so the next prune fires again. N orphans
# therefore cost 2N fsync'd index writes + N state.db probes per poll,
# forever. The fix is a small, dedicated tombstone set written alongside
# the prune: any sid in the tombstone is skipped by
# ``recover_missing_index_sidecars`` (no re-add to index) and is therefore
# never re-presented to the prune batch.
#
# The file lives in SESSION_DIR (sibling of _index.json) so it is
# profile-local and survives across processes, and is intentionally NOT
# itself listed as a session sidecar — it is excluded from
# ``_persisted_session_ids_snapshot()`` via the same ``name.startswith('_')``
# convention (its name starts with ``.``, a dot — but we add a dedicated
# check below for paranoia). Bounded size keeps the file from growing
# without limit on long-running installs.
# ---------------------------------------------------------------------------

WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP = 500
WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION = 1
WEBUI_DELETED_SESSION_TOMBSTONE_CAP = 1000
WEBUI_DELETED_SESSION_TOMBSTONE_VERSION = 1


def _webui_zero_message_orphan_tombstone_file() -> "Path":
    """Return the current tombstone file path.

    Resolved at call time (not module load) so tests that monkeypatch
    ``SESSION_DIR`` (e.g. ``_real_pipeline``) get a per-test path without
    having to also rewrite the module-level constant. Mirrors how
    ``SESSION_INDEX_FILE`` is computed but resolved at call time so the
    real path tracks the live ``SESSION_DIR``.
    """
    return SESSION_DIR / "_pruned_webui_orphans.json"


def _load_webui_zero_message_orphan_tombstone() -> frozenset[str]:
    """Return sids we've explicitly pruned as webui zero-message orphans.

    Degrades to ``frozenset()`` on any read error, missing file, version
    mismatch, or schema mismatch so the recovery path never accidentally
    admits a row that should stay tombstoned.
    """
    p = _webui_zero_message_orphan_tombstone_file()
    if not p.exists():
        return frozenset()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        logger.debug(
            "Failed to load webui zero-message orphan tombstone",
            exc_info=True,
        )
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    try:
        if int(raw.get("version", 0)) != WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION:
            return frozenset()
    except (TypeError, ValueError):
        return frozenset()
    ids = raw.get("ids", [])
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(
        str(sid).strip() for sid in ids if str(sid or "").strip()
    )


def _save_webui_zero_message_orphan_tombstone(ids) -> None:
    """Persist the tombstone set with a bounded size cap (lexicographically-first N entries).

    Sorts + dedupes so the on-disk file is deterministic and diff-friendly.
    Atomic write via ``.tmp.<pid>.<tid>`` + ``os.replace`` mirrors
    ``_write_session_index`` and ``Session.save`` so a crash mid-write does
    not leave a half-written tombstone file.

    Note on eviction order: ``sorted_ids[:WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP]``
    keeps the lexicographically-FIRST ``N`` sids (sorted ascending), not the
    last-pruned ``N``. Session ids are random UUIDs (``uuid.uuid4().hex[:12]``),
    so the eviction is effectively random across installs; the cap exists to
    keep the file bounded on long-running installs, not to implement FIFO
    pruning. If true FIFO is ever needed, switch the slice to ``[-N:]`` and
    keep an insertion-ordered data structure.
    """
    try:
        sorted_ids = sorted(set(
            str(sid).strip() for sid in (ids or []) if str(sid or "").strip()
        ))
    except TypeError:
        return
    if len(sorted_ids) > WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP:
        sorted_ids = sorted_ids[-WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_CAP:]
    payload = {
        "version": WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_VERSION,
        "ids": sorted_ids,
    }
    p = _webui_zero_message_orphan_tombstone_file()
    _tmp = None
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, p)
    except Exception:
        logger.debug(
            "Failed to save webui zero-message orphan tombstone",
            exc_info=True,
        )
        if _tmp is not None:
            try:
                _tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _record_webui_zero_message_orphan_tombstone(sid: str) -> None:
    """Add ``sid`` to the tombstone.

    No-op if already present (avoids re-sorting and re-fsync'ing on every
    redundant prune). Called from the ``#4985`` prune helper in
    ``api.routes`` immediately after ``prune_session_from_index``.

    Wraps the entire load-modify-write in ``_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK``
    so two concurrent sidebar polls (or a poll racing ``Session.save`` /
    ``new_session`` / ``import_cli_session``) cannot lose each other's
    writes. Without the lock each operation rewrites the entire tombstone
    file from scratch, so a concurrent recorder and clearer can land
    last-writer-wins and silently drop each other's update — defeating the
    self-healing invariant that ``Session.save`` clears the tombstone the
    same poll that the prune helper re-prunes would otherwise re-add the
    row for.
    """
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK:
        current = set(_load_webui_zero_message_orphan_tombstone())
        if sid in current:
            return
        current.add(sid)
        _save_webui_zero_message_orphan_tombstone(current)


def _clear_webui_zero_message_orphan_tombstone(sid: str) -> None:
    """Remove ``sid`` from the tombstone.

    Called when a new Session is created with an explicit sid (e.g.
    ``new_session()`` / ``import_cli_session()``) and belt-and-suspenders
    whenever ``Session.save`` writes a real conversation (a save with
    ``len(messages) > 0`` proves the row is alive, so the tombstone entry
    must drop). Safe to call with an unknown sid (no-op). If the tombstone
    becomes empty as a result, the file is removed entirely so an empty
    poll-time load stays free.

    Wraps the entire load-modify-write/unlink in
    ``_WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK`` so concurrent
    recorders/clearers cannot lose each other's writes — see the docstring
    on ``_record_webui_zero_message_orphan_tombstone``.
    """
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_ZERO_MESSAGE_ORPHAN_TOMBSTONE_LOCK:
        current = set(_load_webui_zero_message_orphan_tombstone())
        if sid not in current:
            return
        current.discard(sid)
        if current:
            _save_webui_zero_message_orphan_tombstone(current)
            return
        try:
            _webui_zero_message_orphan_tombstone_file().unlink(missing_ok=True)
        except Exception:
            logger.debug(
                "Failed to remove empty webui zero-message orphan tombstone",
                exc_info=True,
            )


def _webui_deleted_session_tombstone_file() -> "Path":
    return SESSION_DIR / "_deleted_webui_sessions.json"


def _load_webui_deleted_session_tombstone() -> frozenset[str]:
    p = _webui_deleted_session_tombstone_file()
    if not p.exists():
        return frozenset()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        logger.debug("Failed to load webui deleted-session tombstone", exc_info=True)
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    try:
        if int(raw.get("version", 0)) != WEBUI_DELETED_SESSION_TOMBSTONE_VERSION:
            return frozenset()
    except (TypeError, ValueError):
        return frozenset()
    ids = raw.get("ids", [])
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(
        str(sid).strip() for sid in ids if str(sid or "").strip()
    )


def _save_webui_deleted_session_tombstone(ids) -> None:
    try:
        sorted_ids = sorted(set(
            str(sid).strip() for sid in (ids or []) if str(sid or "").strip()
        ))
    except TypeError:
        return
    if len(sorted_ids) > WEBUI_DELETED_SESSION_TOMBSTONE_CAP:
        sorted_ids = sorted_ids[-WEBUI_DELETED_SESSION_TOMBSTONE_CAP:]
    payload = {
        "version": WEBUI_DELETED_SESSION_TOMBSTONE_VERSION,
        "ids": sorted_ids,
    }
    p = _webui_deleted_session_tombstone_file()
    _tmp = None
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _tmp = p.with_suffix(
            f'.tmp.{os.getpid()}.{threading.current_thread().ident}'
        )
        with open(_tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, p)
    except Exception:
        logger.debug("Failed to save webui deleted-session tombstone", exc_info=True)
        if _tmp is not None:
            try:
                _tmp.unlink(missing_ok=True)
            except Exception:
                pass


def _record_webui_deleted_session_tombstone(sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(_load_webui_deleted_session_tombstone())
        if sid in current:
            return
        current.add(sid)
        _save_webui_deleted_session_tombstone(current)


def _clear_webui_deleted_session_tombstone(sid: str) -> None:
    sid = str(sid or "").strip()
    if not sid:
        return
    with _WEBUI_DELETED_SESSION_TOMBSTONE_LOCK:
        current = set(_load_webui_deleted_session_tombstone())
        if sid not in current:
            return
        current.discard(sid)
        if current:
            _save_webui_deleted_session_tombstone(current)
            return
        try:
            _webui_deleted_session_tombstone_file().unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to remove empty webui deleted-session tombstone", exc_info=True)


def _content_has_reasoning_only_parts(content) -> bool:
    if not isinstance(content, list) or not content:
        return False
    saw_reasoning = False
    for part in content:
        if not isinstance(part, dict):
            if str(part or '').strip():
                return False
            continue
        part_type = str(part.get('type') or '').lower()
        if part_type in {'thinking', 'reasoning'}:
            text = part.get('thinking') or part.get('reasoning') or part.get('text') or ''
            if str(text).strip():
                saw_reasoning = True
            continue
        if part_type == 'text' and str(part.get('text') or part.get('content') or '').strip():
            return False
        if part_type not in {'text', 'thinking', 'reasoning'}:
            return False
    return saw_reasoning


def _active_stream_ids():
    with STREAMS_LOCK:
        active_ids = set(STREAMS.keys())
    # STREAMS tracks the browser/SSE observation path. A worker can still be
    # running after the SSE stream entry disappears (for example while a request
    # is blocked in the provider, unwinding after cancel, or otherwise detached
    # from the client). Treat ACTIVE_RUNS as authoritative for worker liveness so
    # stale-pending repair does not append a misleading restart/interrupted
    # marker while the agent turn is still in flight.
    with _cfg.ACTIVE_RUNS_LOCK:
        active_ids.update(_cfg.ACTIVE_RUNS.keys())
    return active_ids


def _recovered_model_context_projection(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    projected = dict(message)
    projected.pop('reasoning', None)
    if projected.get('_error'):
        return None
    if _content_has_reasoning_only_parts(projected.get('content')):
        if projected.get('tool_calls'):
            projected['content'] = ''
        else:
            return None
    projected_text = _normalize_journal_recovery_text(projected.get('content'))
    if not projected_text and not projected.get('tool_call_id') and not projected.get('tool_calls'):
        return None
    return projected


def _append_recovered_context_projection(
    session,
    context_messages: list,
    recovered: dict,
) -> None:
    recovered_text = _normalize_journal_recovery_text(recovered.get('content'))
    if recovered_text:
        if recovered.get('role') == 'user':
            if _message_matches_pending_checkpoint(
                context_messages[-1] if context_messages else None,
                recovered.get('content'),
                recovered.get('timestamp'),
                recovered.get('_source'),
                recovered.get('attachments'),
            ):
                return
        else:
            for existing in reversed(context_messages[-8:]):
                if not isinstance(existing, dict) or existing.get('role') != recovered.get('role'):
                    continue
                if _normalize_journal_recovery_text(existing.get('content')) == recovered_text:
                    return
    context_messages.append(dict(recovered))


def _seed_recovered_context_from_messages(session, context_messages: list) -> None:
    for message in getattr(session, 'messages', None) or []:
        projected = _recovered_model_context_projection(message)
        if projected is None:
            continue
        context_messages.append(projected)


def _append_recovered_turn_to_context(session, recovered: dict) -> None:
    context_messages = getattr(session, 'context_messages', None)
    if not isinstance(context_messages, list):
        context_messages = []
        session.context_messages = context_messages
    if not context_messages:
        _seed_recovered_context_from_messages(session, context_messages)
    projected = _recovered_model_context_projection(recovered)
    if projected is None:
        return
    _append_recovered_context_projection(session, context_messages, projected)


def _append_recovered_pending_turn(session, *, timestamp: int | None = None) -> dict | None:
    pending_text = str(session.pending_user_message or '')
    if not pending_text:
        return None
    recovered_ts = int(time.time())
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        recovered_ts = int(timestamp)
    recovered: dict = {
        'role': 'user',
        'content': session.pending_user_message,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    pending_source = getattr(session, 'pending_user_source', None)
    stamp_message_source(recovered, pending_source)
    if session.pending_attachments:
        recovered['attachments'] = list(session.pending_attachments)
    session.messages.append(recovered)
    _append_recovered_turn_to_context(session, recovered)
    # The new user turn is now committed to messages (#3831): advance the
    # truncation watermark to the new message's timestamp so that
    # merge_session_messages_append_only() still filters out replaced
    # pre-edit rows from state.db whose timestamps fall below the boundary.
    # The merge's sidecar_advanced_past_watermark guard allows state.db rows
    # newer than the watermark, so post-edit turns are not dropped.
    # Never 0.0 (the truncate-to-empty sentinel, #2914).
    if getattr(session, 'truncation_watermark', None):
        session.truncation_watermark = recovered_ts
    return recovered


def _is_streaming_session(active_stream_id, active_stream_ids):
    return bool(active_stream_id and active_stream_id in active_stream_ids)

def _session_sort_timestamp(session):
    if isinstance(session, dict):
        return session.get('last_message_at') or session.get('updated_at') or 0
    return _last_message_timestamp(getattr(session, 'messages', None)) or getattr(session, 'updated_at', 0) or 0


def _message_timestamp(message):
    if not isinstance(message, dict):
        return None
    raw = message.get('_ts') or message.get('timestamp')
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_empty_partial_activity_message(message):
    """Return True for cancelled/recovered activity rows with no reply text."""
    if not isinstance(message, dict):
        return False
    if message.get('role') != 'assistant' or not message.get('_partial'):
        return False
    content = message.get('content', '')
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text' and str(part.get('text') or part.get('content') or '').strip():
                    return False
                continue
            if str(part or '').strip():
                return False
        return True
    return not str(content or '').strip()


def _last_message_timestamp(messages, *, tail_window: int = 8):
    """perf(session-load-latency) Priority 1: bounded tail-scan.

    Old behavior: reversed-iterate ALL messages until a non-tool, non-empty
    message's timestamp is found. For a 2,730-message session on eMMC, that's
    ~500ms of Python attribute lookups, repeated on every /api/session
    response.

    New behavior: the messages array is chronologically ordered, so the
    last non-tool message is at the very end. We scan only the last
    ``tail_window`` messages — covers the realistic case where 1-3 tool
    rows sit after the last assistant/user message. Falls back to a full
    scan only when no timestamp is found in the window, which preserves
    exact correctness for messages with very large trailing tool clusters
    (rare in practice; we'd need >8 consecutive tool rows to hit it).
    """
    if not isinstance(messages, list):
        return None
    n = len(messages)
    start = max(0, n - max(1, int(tail_window)))
    # Walk from the end backwards. reversed() over a slice still creates
    # a full reverse iterator, but only the slice's elements are touched.
    for message in reversed(messages[start:]):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    # Window miss — fall back to the original full-reversed scan. The
    # caller pays this cost only when the heuristic didn't find a hit,
    # which means the session is unusual (long tool tail or all-empty
    # messages).
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'tool':
            continue
        if _is_empty_partial_activity_message(message):
            continue
        ts = _message_timestamp(message)
        if ts:
            return ts
    return None


def _message_role(message):
    if not isinstance(message, dict):
        return ''
    return str(message.get('role', '')).strip().lower()


def _find_top_level_json_key(text, key):
    """Return the byte offset of a top-level JSON object key, if present."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            start = i
            i += 1
            escaped = False
            chars = []
            while i < n:
                c = text[i]
                if escaped:
                    chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '"':
                    break
                else:
                    chars.append(c)
                i += 1
            if i >= n:
                return None
            if depth == 1 and ''.join(chars) == key:
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                if j < n and text[j] == ':':
                    return start
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        i += 1
    return None


def _read_file_head(path: Path, max_prefix_bytes: int = 4096) -> str:
    """Read at most ``max_prefix_bytes`` bytes from ``path`` and decode UTF-8."""
    if not isinstance(path, Path):
        path = Path(path)
    if max_prefix_bytes <= 0:
        return ''
    with path.open('rb') as fp:
        return fp.read(max_prefix_bytes).decode('utf-8', errors='ignore')


def _read_metadata_json_prefix(path, max_prefix_bytes=65536):
    """Read only the metadata portion before the large arrays.

    #5854: stop at the top-level ``messages`` key OR the top-level
    ``anchor_activity_scenes`` key, whichever appears first. On the modern
    layout scenes serialize AFTER ``messages`` so this stops at ``messages`` as
    before (the prefix is now small — scene bodies are no longer in it). On the
    LEGACY layout scenes serialize BEFORE ``messages`` and can be 250-480KB, so
    stopping at ``anchor_activity_scenes`` keeps the read cheap and — critically
    — still captures ``message_count`` (which is written before both). Without
    the scenes-stop a legacy large-scene sidecar overflows ``max_prefix_bytes``
    and forces a full multi-MB parse on every poll (the #4633 churn).
    """
    buf = ''
    with open(path, 'r', encoding='utf-8') as f:
        while len(buf.encode('utf-8')) < max_prefix_bytes:
            chunk = f.read(4096)
            if not chunk:
                return None
            buf += chunk
            stop_pos = _find_top_level_json_key(buf, 'messages')
            scenes_pos = _find_top_level_json_key(buf, 'anchor_activity_scenes')
            if scenes_pos is not None and (stop_pos is None or scenes_pos < stop_pos):
                stop_pos = scenes_pos
            if stop_pos is None:
                continue
            prefix = buf[:stop_pos].rstrip()
            if prefix.endswith(','):
                prefix = prefix[:-1].rstrip()
            return f'{prefix}\n}}'
    return None


def _load_session_from_path(path: Path) -> "Session | None":
    """Load a session from an explicit JSON path without consulting SESSION_DIR."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
    return Session(**data)


def _lookup_index_message_count(session_id):
    """Return the indexed message count without loading the full session file."""
    return _index_message_count_map().get(str(session_id))


def _index_message_count_map(entries=None) -> dict[str, int]:
    """Return indexed message counts keyed by session id.

    ``load_metadata_only()`` is called in loops for stale lineage/sidebar rows.
    Reading and parsing ``_index.json`` once per row turns /api/sessions into an
    accidental O(n²) poll for old sidecars that predate persisted
    ``message_count``. Accepting already-loaded index rows lets callers reuse
    the index they just parsed.
    """
    if entries is None:
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
        except Exception:
            return {}
    if not isinstance(entries, list):
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get('session_id') or '')
        if not sid:
            continue
        count = entry.get('message_count')
        if not isinstance(count, int):
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
        if count >= 0:
            counts[sid] = count
    return counts


def _parse_nonnegative_int(value):
    if isinstance(value, int) and value >= 0:
        return value
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def model_explicit_pick_signature(model, model_provider) -> str:
    """Stable signature of a (model, provider) selection for #5979 explicit-pick
    provenance. The persisted ``Session.model_explicit_pick_signature`` is set to
    this when the user deliberately picks a model; the streaming resolver only
    treats a selection as deliberate when the CURRENT routing context produces
    the same signature. Any model/provider change (chat-start, session-update,
    normalization, provider repair) yields a different signature and thus
    invalidates the stale pick — so a #433 first-party leftover is never wrongly
    preserved. Uses \\x1f (unit separator) so it can't collide with model ids.
    """
    _m = str(model or "").strip()
    _p = str(model_provider or "").strip().lower()
    return f"{_m}\x1f{_p}"


class Session:
    def __init__(self, session_id: str=None, title: str='Untitled',
                 workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL,
                 model_provider=None,
                 messages=None, created_at=None, updated_at=None,
                 tool_calls=None, pinned: bool=False, archived: bool=False,
                 project_id: str=None, profile=None,
                 input_tokens: int=0, output_tokens: int=0, estimated_cost=None,
                 cache_read_tokens: int=0, cache_write_tokens: int=0,
                 personality=None,
                 active_stream_id: str=None,
                 pending_user_message: str=None,
                 pending_attachments=None,
                 pending_started_at=None,
                 pending_user_source: str=None,
                 context_messages=None,
                 compression_anchor_visible_idx=None,
                 compression_anchor_message_key=None,
                 compression_anchor_summary=None,
                 pre_compression_snapshot: bool=False,
                 context_engine=None,
                 compression_anchor_engine=None,
                 compression_anchor_mode=None,
                 compression_anchor_details=None,
                 context_engine_state=None,
                 context_length=None, threshold_tokens=None,
                 last_prompt_tokens=None,
                 post_compression_context_tokens_estimate=None,
                 compression_recovery=None,
                 recommended_recovery_action=None,
                 compression_recovery_source_session_id=None,
                 compression_recovery_action=None,
                 truncation_watermark=None,
                 truncation_boundary=None,
                 clear_generation=None,
                 gateway_routing=None, gateway_routing_history=None,
                 llm_title_generated: bool=False,
                 manual_title: bool=False,
                parent_session_id: str=None,
                worktree_path=None,
                worktree_branch=None,
                 worktree_repo_root=None,
                 worktree_created_at=None,
                 enabled_toolsets=None,
                 composer_draft=None,
                 anchor_activity_scenes=None,
                 process_wakeup_pause=None,
                 share_token=None,
                 share_created_at=None,
                 **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.model = model
        self.model_provider = str(model_provider).strip().lower() if model_provider else None
        # #5979: signature of the model the user DELIBERATELY picked this session
        # (``"<model>\x1f<provider>"``), or None. Used by the streaming resolver
        # to preserve a custom-proxy vendor namespace on a COLD catalog ONLY when
        # the current routing context still matches what was picked. Storing a
        # SIGNATURE (not a bare bool) means any later model/provider change — via
        # /api/chat/start, /api/session/update, normalization, or provider repair
        # — automatically invalidates the pick (the signatures no longer match),
        # so a stale first-party leftover (#433) is never wrongly preserved.
        # Restored from persisted metadata on load (arrives via **kwargs).
        self.model_explicit_pick_signature = kwargs.get('model_explicit_pick_signature') or None
        self.messages = messages or []
        self.tool_calls = tool_calls or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.pinned = bool(pinned)
        self.archived = bool(archived)
        self.project_id = project_id or None
        self.profile = profile
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.estimated_cost = estimated_cost
        self.cache_read_tokens = cache_read_tokens or 0
        self.cache_write_tokens = cache_write_tokens or 0
        self.personality = personality
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = pending_attachments or []
        self.pending_started_at = pending_started_at
        self.pending_user_source = pending_user_source
        self.context_messages = context_messages if isinstance(context_messages, list) else []
        self.compression_anchor_visible_idx = compression_anchor_visible_idx
        self.compression_anchor_message_key = compression_anchor_message_key
        self.compression_anchor_summary = compression_anchor_summary
        self.pre_compression_snapshot = bool(pre_compression_snapshot)
        self.context_engine = context_engine
        self.compression_anchor_engine = compression_anchor_engine
        self.compression_anchor_mode = compression_anchor_mode
        self.compression_anchor_details = compression_anchor_details if isinstance(compression_anchor_details, dict) else {}
        self.context_engine_state = context_engine_state if isinstance(context_engine_state, dict) else {}
        self.context_length = context_length
        self.threshold_tokens = threshold_tokens
        self.last_prompt_tokens = last_prompt_tokens
        _post_compression_tokens = _parse_nonnegative_int(post_compression_context_tokens_estimate)
        self.post_compression_context_tokens_estimate = (
            _post_compression_tokens if _post_compression_tokens and _post_compression_tokens > 0 else None
        )
        self.compression_recovery = compression_recovery if isinstance(compression_recovery, dict) else {}
        self.recommended_recovery_action = recommended_recovery_action
        self.compression_recovery_source_session_id = (
            str(compression_recovery_source_session_id).strip()
            if compression_recovery_source_session_id
            else None
        )
        self.compression_recovery_action = (
            str(compression_recovery_action).strip()
            if compression_recovery_action
            else None
        )
        self.truncation_watermark = truncation_watermark
        self.truncation_boundary = truncation_boundary
        self.clear_generation = clear_generation
        self.gateway_routing = gateway_routing if isinstance(gateway_routing, dict) else None
        self.gateway_routing_history = gateway_routing_history if isinstance(gateway_routing_history, list) else []
        self.llm_title_generated = bool(llm_title_generated)
        self.manual_title = bool(manual_title)
        self.parent_session_id = parent_session_id
        self.worktree_path = str(Path(worktree_path).expanduser().resolve()) if worktree_path else None
        self.worktree_branch = str(worktree_branch) if worktree_branch else None
        self.worktree_repo_root = str(Path(worktree_repo_root).expanduser().resolve()) if worktree_repo_root else None
        self.worktree_created_at = worktree_created_at
        self.is_cli_session = bool(kwargs.get('is_cli_session', False))
        self.source_tag = kwargs.get('source_tag')
        self.raw_source = kwargs.get('raw_source')
        self.session_source = kwargs.get('session_source')
        self.source_label = kwargs.get('source_label')
        self.read_only = bool(kwargs.get('read_only', False))
        self.enabled_toolsets = enabled_toolsets  # List[str] or None — per-session toolset override
        self.composer_draft = composer_draft if isinstance(composer_draft, dict) else {}
        self.anchor_activity_scenes = anchor_activity_scenes if isinstance(anchor_activity_scenes, dict) else {}
        self.process_wakeup_pause = process_wakeup_pause if isinstance(process_wakeup_pause, dict) else {}
        self.share_token = str(share_token).strip() if share_token else None
        self.share_created_at = share_created_at
        # #5854: a compact fingerprint of anchor_activity_scenes ({scene_key:
        # updated_at}) persisted BEFORE the messages array so the sidebar-poll
        # freshness check can compare scene freshness without parsing the full
        # (often 250-480KB) scene bodies, which serialize AFTER messages. None
        # on legacy sidecars (scenes-before-messages, no fingerprint) — callers
        # fall back to reading keys/updated_at off anchor_activity_scenes.
        _raw_scene_index = kwargs.get('anchor_scene_index')
        self._anchor_scene_index = _raw_scene_index if isinstance(_raw_scene_index, dict) else None
        raw_message_count = kwargs.get('message_count')
        parsed_message_count = None
        if raw_message_count is not None:
            try:
                parsed_message_count = int(raw_message_count)
            except (TypeError, ValueError):
                parsed_message_count = None
        self._metadata_message_count = parsed_message_count if parsed_message_count is not None and parsed_message_count >= 0 else None

    @property
    def path(self):
        return SESSION_DIR / f'{self.session_id}.json'

    def save(self, touch_updated_at: bool = True, skip_index: bool = False) -> None:
        if not is_safe_session_id(self.session_id):
            raise ValueError(f"Unsafe session_id {self.session_id!r}; refusing to write outside session store")
        # ── #1558 P0 guard ──────────────────────────────────────────────
        # Refuse to save a session that was loaded with metadata_only=True.
        # Such sessions have messages=[] (it's the whole point of the partial
        # load), and save() unconditionally writes self.messages to disk via
        # an atomic os.replace(). Saving a metadata-only stub thus wipes the
        # full conversation history — which is exactly the v0.50.279
        # _clear_stale_stream_state() regression that lost users 1000+
        # message conversations. Any caller that needs to mutate persisted
        # fields on a metadata-only session must reload with
        # metadata_only=False first.
        if getattr(self, '_loaded_metadata_only', False):
            raise RuntimeError(
                f"Refusing to save metadata-only session {self.session_id!r}: "
                f"would atomically overwrite on-disk messages with []. "
                f"Reload with metadata_only=False before mutating state. "
                f"See #1558."
            )
        if touch_updated_at:
            self.updated_at = time.time()
        # Write metadata fields first so load_metadata_only() can read them
        # without parsing the full messages array (which may be 400KB+).
        # Fields are listed in the order they should appear in the JSON file.
        METADATA_FIELDS = [
            'session_id', 'title', 'workspace', 'model', 'model_provider', 'model_explicit_pick_signature', 'created_at', 'updated_at',
            'pinned', 'archived', 'project_id', 'profile',
            'input_tokens', 'output_tokens', 'estimated_cost',
            'cache_read_tokens', 'cache_write_tokens',
            'personality', 'active_stream_id',
            'pending_user_message', 'pending_attachments', 'pending_started_at', 'pending_user_source',
            'compression_anchor_visible_idx', 'compression_anchor_message_key',
            'compression_anchor_summary', 'pre_compression_snapshot',
            'context_engine', 'compression_anchor_engine', 'compression_anchor_mode',
            'compression_anchor_details', 'context_engine_state',
            'context_length', 'threshold_tokens', 'last_prompt_tokens',
            'post_compression_context_tokens_estimate',
            'compression_recovery', 'recommended_recovery_action',
            'compression_recovery_source_session_id', 'compression_recovery_action',
            'truncation_watermark',
            'truncation_boundary',
            'clear_generation',
            'gateway_routing', 'gateway_routing_history', 'llm_title_generated', 'manual_title',
            'parent_session_id',
            'worktree_path', 'worktree_branch', 'worktree_repo_root', 'worktree_created_at',
            'is_cli_session', 'source_tag', 'raw_source', 'session_source', 'source_label', 'read_only',
            'enabled_toolsets', 'composer_draft',
            'process_wakeup_pause',
            'share_token', 'share_created_at',
        ]
        meta = {k: getattr(self, k, None) for k in METADATA_FIELDS}
        # #5854: message_count and a compact anchor-scene fingerprint go in the
        # metadata prefix (BEFORE messages) so load_metadata_only() and the
        # sidebar-poll freshness check never have to parse the full (250-480KB)
        # scene bodies. message_count is placed BEFORE anchor_scene_index so a
        # legacy-format reader that stops at a scene key still finds the count.
        # The full anchor_activity_scenes bodies serialize AFTER messages.
        meta['message_count'] = len(self.messages or [])
        meta['anchor_scene_index'] = _anchor_scene_index_from_records(self.anchor_activity_scenes)
        # Keep the in-memory fingerprint aligned with what we just persisted, so a
        # later metadata-only reload of THIS object (or any fingerprint reader)
        # sees the current value rather than a stale load-time snapshot (#5854
        # defense-in-depth; the cached-side freshness check reads real records,
        # not this, so this is belt-and-suspenders).
        self._anchor_scene_index = dict(meta['anchor_scene_index'])
        meta['messages'] = self.messages
        meta['tool_calls'] = self.tool_calls
        meta['anchor_activity_scenes'] = self.anchor_activity_scenes if isinstance(self.anchor_activity_scenes, dict) else {}
        # Fields not in METADATA_FIELDS (e.g. last_usage) go at the end. Exclude
        # the keys we placed explicitly above so they aren't emitted twice.
        _placed = {'message_count', 'anchor_scene_index', 'messages', 'tool_calls', 'anchor_activity_scenes'}
        extra = {k: v for k, v in self.__dict__.items()
                 if k not in METADATA_FIELDS and k not in _placed
                 and not k.startswith('_')}
        payload = json.dumps({**meta, **extra}, ensure_ascii=False, indent=2)

        # ── #1558 backup safeguard ──────────────────────────────────────
        # Before overwriting the session file, copy the previous version to
        # ``<sid>.json.bak`` IFF the previous file has more messages than the
        # incoming payload. The asymmetric guard means:
        #   * Normal grow-the-conversation saves never produce a backup
        #     (incoming messages >= existing) — keeps disk overhead near zero.
        #   * Any save that would shrink the messages array (the failure mode
        #     of #1558, plus anything similar in the future) leaves a recoverable
        #     snapshot of the pre-shrink state on disk.
        # The recovery path is api/session_recovery.py — at server startup and
        # via /api/session/recover, sessions whose JSON has fewer messages than
        # their .bak get restored automatically.
        try:
            if self.path.exists():
                existing_text = self.path.read_text(encoding='utf-8')
                try:
                    existing = json.loads(existing_text)
                    existing_msg_count = len(existing.get('messages') or [])
                except (json.JSONDecodeError, ValueError):
                    existing_msg_count = -1  # corrupt → always back up
                incoming_msg_count = len(self.messages or [])
                if (
                    existing_msg_count > 0
                    and incoming_msg_count == 0
                    and (self.active_stream_id or self.pending_user_message)
                ):
                    logger.warning(
                        "refusing to overwrite session %s messages with empty active/pending snapshot "
                        "(existing=%s, incoming=%s, stream=%s)",
                        self.session_id,
                        existing_msg_count,
                        incoming_msg_count,
                        self.active_stream_id,
                    )
                    return
                if existing_msg_count > incoming_msg_count:
                    bak_path = self.path.with_suffix('.json.bak')
                    # SHOULD-FIX #2 (Opus): atomic write via tmp+replace,
                    # mirroring the main save() pattern below. Prevents a
                    # torn .bak from a crash mid-write or a concurrent
                    # backup-producing save. Recovery defends against a
                    # torn .bak (JSONDecodeError → no_action), so the
                    # failure mode pre-fix was "backup is lost"; with
                    # this fix the backup either lands cleanly or doesn't
                    # land at all.
                    try:
                        bak_tmp = bak_path.with_suffix(
                            f'.bak.tmp.{os.getpid()}.{threading.current_thread().ident}'
                        )
                        with open(bak_tmp, 'w', encoding='utf-8') as bf:
                            bf.write(existing_text)
                            bf.flush()
                            os.fsync(bf.fileno())
                        _safe_replace(bak_tmp, bak_path)
                    except OSError:
                        # Backup is best-effort; main save proceeds regardless.
                        try:
                            bak_tmp.unlink(missing_ok=True)
                        except Exception:
                            pass
        except OSError:
            pass

        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            _safe_replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if not skip_index:
            _write_session_index(updates=[self])

        # #4985 belt-and-suspenders self-heal: a successful save with at
        # least one real message on the sidecar is unconditional proof the
        # row is alive (the #4985 "zero-message orphan" only ever exists
        # when ``len(self.messages) == 0``). Clear the tombstone so the
        # next ``/api/sessions`` poll does not need the prune helper to
        # run before the row re-appears — useful when the message-commit
        # happens on a poll that does not yet see state.db.messages rows
        # (e.g. the WebUI's own sidecar commit lands before the agent's
        # state.db append, or the helper is skipped via a different code
        # path). Wrapped because a tombstone failure must never block a
        # save. The helper's self-healing branch in
        # ``_prune_orphaned_webui_zero_message_sessions`` is the primary
        # fix; this is the belt.
        if self.messages:
            try:
                _clear_webui_zero_message_orphan_tombstone(self.session_id)
                _clear_webui_deleted_session_tombstone(self.session_id)
            except Exception:
                logger.debug(
                    "Failed to clear webui tombstone for %s",
                    self.session_id,
                    exc_info=True,
                )

    @classmethod
    def load(cls, sid):
        # Validate session ID format to prevent path traversal.  API/gateway
        # session ids may contain hyphens (for example ``api-*`` and
        # ``reachy-voice-*``); allow those but still reject dots/slashes.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        # #5854: snapshot the stat signature BEFORE reading so a legacy-facts
        # cache write is only committed if the file didn't change under us
        # during the parse (TOCTOU guard against an atomic replace mid-read).
        _pre_read_sig = _sidecar_stat_signature(p)
        data = json.loads(p.read_text(encoding='utf-8'))
        data['messages'], _collapsed_partials = _collapse_adjacent_duplicate_partials(data.get('messages'))
        session = cls(**data)
        if _collapsed_partials:
            try:
                # Self-heal bloated sessions on first full load without touching
                # recency/index ordering; save() creates a .bak because this
                # intentionally shrinks the transcript (#2592).
                session.save(touch_updated_at=False, skip_index=True)
            except Exception:
                logger.debug("Failed to persist collapsed duplicate partials for %s", sid, exc_info=True)
        else:
            # #5854: for a LEGACY sidecar (no modern anchor_scene_index key), the
            # cheap metadata-prefix read cannot recover message_count/scenes when
            # scenes serialize before them, so cache the authoritative facts we
            # just parsed. This keeps the metadata-only path and the eviction
            # check from full-parsing this unchanged file again on every poll.
            # Keyed by stat signature, so any edit invalidates it; the next
            # save() rewrites the modern layout and the fallback stops firing.
            # expected_sig guards against an atomic replace during the read.
            # (When _collapsed_partials fired, save() above already rewrote the
            # modern layout, so no legacy caching is needed.)
            if 'anchor_scene_index' not in data:
                try:
                    _legacy_sidecar_facts_put(
                        sid,
                        len(getattr(session, 'messages', None) or []),
                        _anchor_scene_index_from_records(getattr(session, 'anchor_activity_scenes', None)),
                        expected_sig=_pre_read_sig,
                    )
                except Exception:
                    logger.debug("legacy sidecar facts cache populate failed for %s", sid, exc_info=True)
        return session

    @classmethod
    def load_metadata_only(cls, sid, *, index_message_counts=None):
        """Load only the compact metadata fields, skipping the messages array.

        Session JSON files have metadata fields (session_id, title, model, etc.)
        at the top level, before the large messages array. Read only up to the
        top-level "messages" field and synthesize a small metadata-only object.
        Falls back to load() for legacy or unexpected file layouts.
        """
        # Same path-safety contract as load(): hyphens are valid session ids,
        # path separators and traversal dots are not.
        if not is_safe_session_id(sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        try:
            prefix = _read_metadata_json_prefix(p)
            if not prefix:
                return cls.load(sid)
            parsed = json.loads(prefix)
            needed = {'session_id', 'title', 'created_at', 'updated_at'}
            if not needed.issubset(parsed.keys()):
                return cls.load(sid)
            parsed['messages'] = []
            parsed['tool_calls'] = []
            session = cls(**parsed)
            sidecar_message_count = _parse_nonnegative_int(parsed.get('message_count'))
            index_message_count = None
            if sidecar_message_count is None:
                if index_message_counts is not None:
                    index_message_count = index_message_counts.get(str(sid))
                else:
                    index_message_count = _lookup_index_message_count(sid)
            # #5854 legacy-layout recovery: a pre-#5854 sidecar serialized
            # anchor_activity_scenes BEFORE message_count, so on a large-scene
            # legacy file the cheap prefix now stops at the scenes key and
            # captures NO message_count. The sidebar _index.json count can lag
            # behind external sidecar appends, so trusting it here would report a
            # stale/zero count and could drop an unsaved user tail on the next
            # get_session cache-replace. When the prefix carries NEITHER
            # message_count NOR the modern anchor_scene_index key (⇒ a legacy
            # file whose count fell after the scenes), recover the authoritative
            # facts. To avoid re-parsing an unchanged legacy file on every poll
            # (which would recreate the #4633 churn for legacy sidecars that are
            # never re-saved), consult a bounded stat-signature cache first and
            # only full-load on a miss, caching the result. The next save()
            # rewrites the modern layout so the fallback stops firing entirely.
            # A MODERN file always carries message_count in the prefix, so it
            # never reaches here — a genuine 0 stays 0.
            if (
                sidecar_message_count is None
                and 'anchor_scene_index' not in parsed
            ):
                _facts = _legacy_sidecar_facts_get(sid)
                if _facts is not None:
                    parsed['anchor_scene_index'] = _facts.get('scene_index') or {}
                    session = cls(**parsed)
                    session._metadata_message_count = _parse_nonnegative_int(_facts.get('message_count'))
                    session._loaded_metadata_only = True
                    return session
                # Cache miss → full-load. cls.load() itself populates the legacy
                # facts cache with a TOCTOU-guarded write (expected_sig), so we
                # do NOT re-cache here (an unguarded second write could stamp
                # stale facts under a replacement file's signature — Codex r5).
                return cls.load(sid)
            # Modern sidecars carry an accurate message_count, so it is the
            # source of truth and we skip the per-row _index.json read in the
            # common case. The sidebar index is only a cache (it can lag behind
            # external sidecar appends/backfills), so consult it solely as a
            # fallback when the sidecar has no count. When both are present we
            # still take the largest known count as a defensive measure.
            known_counts = [
                count for count in (index_message_count, sidecar_message_count)
                if count is not None
            ]
            session._metadata_message_count = max(known_counts) if known_counts else None
            # Mark this session as a metadata-only stub. save() refuses to write
            # such a session because doing so would atomically replace the
            # on-disk JSON with messages=[], wiping the conversation. Any
            # caller that needs to mutate persisted state on a metadata-only
            # session must reload it with metadata_only=False first.
            # See #1558 — v0.50.279 _clear_stale_stream_state() data-loss bug.
            session._loaded_metadata_only = True
            return session
        except Exception:
            # Corrupt prefix or decode error — fall back to full load
            return cls.load(sid)

    @staticmethod
    def _compute_user_message_count(messages) -> int:
        """perf(session-load-latency) Priority 1: bounded in-memory count.

        Returns the number of messages with role='user' in ``messages``.
        Pre-patch compact() did the same O(N) walk inline; the walk is
        extracted here so it can be measured and bounded independently.

        On the test corpus (a 2,400-message sidecar) this walk runs in
        tens of milliseconds on a Celeron N3350 with eMMC. Cost is
        proportional to the sidecar length the caller already loaded, not
        to anything new we read from disk.

        Critical: this walks ``messages`` (the sidecar) and NOT state.db.
        A previous version of this helper queried state.db for the same
        count, but the two sources can diverge by hundreds of messages
        during recovery / mid-flight writes / pending_user_message, and
        the sidebar's stale-row detection (see
        ``_looks_like_stale_zero_message_row`` and
        ``_row_may_need_sidecar_metadata_refresh``) consumes this field as
        if the sidecar were the source of truth. Mixing the two sources
        would silently flip the field's semantics.
        """
        if not isinstance(messages, list):
            return 0
        n = 0
        for m in messages:
            if isinstance(m, dict):
                # Inline role check to avoid the _message_role helper call
                # on every iteration. dict.get('role') with default '' is
                # materially faster than a function call for the hot loop.
                role = m.get('role')
                if isinstance(role, str) and role == 'user':
                    n += 1
        return n

    def compact(self, include_runtime=False, active_stream_ids=None) -> dict:
        active_stream_ids = active_stream_ids if active_stream_ids is not None else set()
        has_pending_user_message = bool(self.pending_user_message)
        message_count = (
            self._metadata_message_count
            if self._metadata_message_count is not None
            else len(self.messages)
        )
        if has_pending_user_message:
            message_count = max(message_count, 1)
        last_message_at = _last_message_timestamp(self.messages) or self.updated_at
        if has_pending_user_message and self.pending_started_at:
            last_message_at = self.pending_started_at
        return {
            'session_id': self.session_id,
            'title': self.title,
            'workspace': self.workspace,
            'model': self.model,
            'model_provider': self.model_provider,
            'message_count': message_count,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'last_message_at': last_message_at,
            'pinned': self.pinned,
            'archived': self.archived,
            'project_id': self.project_id,
            'profile': self.profile,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost': self.estimated_cost,
            'cache_read_tokens': self.cache_read_tokens,
            'cache_write_tokens': self.cache_write_tokens,
            'cache_hit_percent': prompt_cache_hit_percent(self.cache_read_tokens, self.input_tokens),
            'personality': self.personality,
            'compression_anchor_visible_idx': self.compression_anchor_visible_idx,
            'compression_anchor_message_key': self.compression_anchor_message_key,
            'compression_anchor_summary': self.compression_anchor_summary,
            'pre_compression_snapshot': self.pre_compression_snapshot,
            'context_engine': self.context_engine,
            'compression_anchor_engine': self.compression_anchor_engine,
            'compression_anchor_mode': self.compression_anchor_mode,
            'compression_anchor_details': self.compression_anchor_details,
            'context_engine_state': self.context_engine_state,
            'context_length': self.context_length,
            'threshold_tokens': self.threshold_tokens,
            'last_prompt_tokens': self.last_prompt_tokens,
            'post_compression_context_tokens_estimate': self.post_compression_context_tokens_estimate,
            'compression_recovery': self.compression_recovery,
            'recommended_recovery_action': self.recommended_recovery_action,
            'gateway_routing': self.gateway_routing,
            'gateway_routing_history': self.gateway_routing_history,
            'manual_title': self.manual_title,
            # Only emit 'parent_session_id' when set (the /branch fork link, #1342).
            # Sessions without a fork must not leak None — see test_session_lineage_metadata_api.
            **({'parent_session_id': self.parent_session_id} if self.parent_session_id else {}),
            **({
                'compression_recovery_source_session_id': self.compression_recovery_source_session_id,
                'compression_recovery_action': self.compression_recovery_action,
            } if (self.compression_recovery_source_session_id or self.compression_recovery_action) else {}),
            **({
                'worktree_path': self.worktree_path,
                'worktree_branch': self.worktree_branch,
                'worktree_repo_root': self.worktree_repo_root,
                'worktree_created_at': self.worktree_created_at,
            } if self.worktree_path else {}),
            'user_message_count': Session._compute_user_message_count(self.messages),
            'active_stream_id': self.active_stream_id,
            'pending_user_message': self.pending_user_message,
            'has_pending_user_message': has_pending_user_message,
            'is_cli_session': self.is_cli_session,
            'source_tag': self.source_tag,
            'raw_source': self.raw_source,
            'session_source': self.session_source,
            'source_label': self.source_label,
            'read_only': self.read_only,
            'enabled_toolsets': self.enabled_toolsets,
            'composer_draft': self.composer_draft if isinstance(self.composer_draft, dict) else {},
            'process_wakeup_pause': self.process_wakeup_pause if isinstance(self.process_wakeup_pause, dict) else {},
            'share_token': self.share_token,
            'share_created_at': self.share_created_at,
            'is_streaming': _is_streaming_session(
                self.active_stream_id, active_stream_ids
            ) if include_runtime else False,
        }


PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES = frozenset({
    'credential_pool_empty',
})
PROCESS_WAKEUP_PAUSE_ERROR = 'process_wakeup_paused'
_PROCESS_WAKEUP_PAUSE_VERSION = 1


def _process_wakeup_pause_part(value) -> str:
    return str(value or '').strip().lower()


def _process_wakeup_pause_provider_part(value) -> str:
    provider = _process_wakeup_pause_part(value)
    if not provider:
        return ''
    try:
        return _process_wakeup_pause_part(_cfg._resolve_provider_alias(provider))
    except Exception:
        return provider


def _process_wakeup_pause_lane(model=None, provider=None) -> tuple[str, str]:
    model_part = _process_wakeup_pause_part(model)
    provider_part = _process_wakeup_pause_provider_part(provider)
    try:
        resolved_model, resolved_provider = _cfg.canonical_model_provider_lane(model, provider)
    except Exception:
        logger.debug(
            "failed to canonicalize process_wakeup pause lane for model=%r provider=%r",
            model,
            provider,
            exc_info=True,
        )
        resolved_model, resolved_provider = None, None
    if resolved_model:
        model_part = _process_wakeup_pause_part(resolved_model)
    if resolved_provider:
        provider_part = _process_wakeup_pause_provider_part(resolved_provider)
    return model_part, provider_part


def _process_wakeup_pause_int(value, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _process_wakeup_pause_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _process_wakeup_pause_key(model=None, provider=None, classification=None) -> dict:
    model_part, provider_part = _process_wakeup_pause_lane(model, provider)
    return {
        'model': model_part,
        'provider': provider_part,
        'classification': _process_wakeup_pause_part(classification),
    }


def process_wakeup_pause_matches(session, *, model=None, provider=None, classification=None) -> bool:
    """Return True when the session has an active pause for this wakeup lane."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    expected = _process_wakeup_pause_key(model, provider, classification)
    for key, expected_value in expected.items():
        if key == 'classification' and not expected_value:
            continue
        if _process_wakeup_pause_part(pause.get(key)) != expected_value:
            return False
    return True


def clear_process_wakeup_pause(session, *, reason: str = '') -> bool:
    """Clear any persisted process-wakeup pause metadata.

    Returns True when a pause was present. Callers decide whether and how to
    persist, because some paths are already inside a larger session writeback.
    """
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause:
        return False
    session.process_wakeup_pause = {}
    if reason:
        session._last_process_wakeup_pause_clear_reason = str(reason)
    return True


def clear_process_wakeup_pause_if_model_changed(session, *, model=None, provider=None) -> bool:
    """Reset a wakeup pause when the resolved model/provider lane changed."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    current = _process_wakeup_pause_key(model, provider, pause.get('classification'))
    if (
        _process_wakeup_pause_part(pause.get('model')) == current['model']
        and _process_wakeup_pause_part(pause.get('provider')) == current['provider']
    ):
        return False
    return clear_process_wakeup_pause(session, reason='model_or_provider_changed')


def record_process_wakeup_provider_unavailable_pause(
    session,
    *,
    classification: str,
    model=None,
    provider=None,
) -> dict | None:
    """Record the first visible provider-unavailable wakeup failure.

    The persisted object is deliberately metadata-only: it records the lane and
    counters, not the wakeup prompt or provider response body, so it remains
    auditable without copying process output or credentials into diagnostics.
    """
    classification = _process_wakeup_pause_part(classification)
    if classification not in PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES:
        return None
    now = time.time()
    key = _process_wakeup_pause_key(model, provider, classification)
    credential_state_fingerprint = process_wakeup_credential_state_fingerprint(session)
    existing = getattr(session, 'process_wakeup_pause', None)
    same_window = (
        isinstance(existing, dict)
        and existing.get('paused')
        and _process_wakeup_pause_part(existing.get('model')) == key['model']
        and _process_wakeup_pause_part(existing.get('provider')) == key['provider']
        and _process_wakeup_pause_part(existing.get('classification')) == key['classification']
    )
    visible_error_count = 1
    suppressed_count = 0
    first_paused_at = now
    if same_window:
        first_paused_at = _process_wakeup_pause_float(existing.get('first_paused_at'), now)
        visible_error_count = _process_wakeup_pause_int(
            existing.get('visible_error_count'),
            1,
        ) + 1
        suppressed_count = _process_wakeup_pause_int(existing.get('suppressed_count'), 0)
    session.process_wakeup_pause = {
        'version': _PROCESS_WAKEUP_PAUSE_VERSION,
        'paused': True,
        'source': 'process_wakeup',
        'classification': key['classification'],
        'model': key['model'],
        'provider': key['provider'],
        'first_paused_at': first_paused_at,
        'last_error_at': now,
        'visible_error_count': visible_error_count,
        'suppressed_count': suppressed_count,
        'credential_state_fingerprint': credential_state_fingerprint,
    }
    return session.process_wakeup_pause


def suppress_process_wakeup_for_provider_pause(
    session,
    *,
    model=None,
    provider=None,
    classification: str = 'credential_pool_empty',
) -> dict | None:
    """Increment suppression metadata if this automatic wakeup is paused."""
    if not process_wakeup_pause_matches(
        session,
        model=model,
        provider=provider,
        classification=classification,
    ):
        return None
    pause = dict(getattr(session, 'process_wakeup_pause', {}) or {})
    pause['suppressed_count'] = _process_wakeup_pause_int(pause.get('suppressed_count'), 0) + 1
    pause['last_suppressed_at'] = time.time()
    pause['last_suppressed_reason'] = 'provider_unavailable_pause'
    session.process_wakeup_pause = pause
    return pause


_PROCESS_WAKEUP_AUTH_ROTATION_KEYS = frozenset({
    'expires_at',
    'expires_at_ms',
    'expires_in',
    'last_status',
    'last_status_at',
    'last_error_code',
    'last_error_reason',
    'last_error_message',
    'last_error_reset_at',
    'request_count',
    'updated_at',
})
_PROCESS_WAKEUP_AUTH_SECRET_PRESENCE_KEYS = frozenset({
    'access_token',
    'refresh_token',
    'id_token',
    'api_key',
    'secret',
    'client_secret',
    'runtime_api_key',
    'token',
})


def _process_wakeup_secret_presence(value):
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _process_wakeup_auth_fingerprint_payload(value):
    if isinstance(value, dict):
        payload = {}
        for key, child in value.items():
            key_text = str(key)
            key_norm = key_text.strip().lower()
            if key_norm in _PROCESS_WAKEUP_AUTH_ROTATION_KEYS:
                continue
            if key_norm in _PROCESS_WAKEUP_AUTH_SECRET_PRESENCE_KEYS:
                payload[key_text] = _process_wakeup_secret_presence(child)
            else:
                payload[key_text] = _process_wakeup_auth_fingerprint_payload(child)
        return payload
    if isinstance(value, list):
        return [_process_wakeup_auth_fingerprint_payload(item) for item in value]
    return value


def _process_wakeup_auth_store_fingerprint(path: Path) -> dict:
    p = Path(path).expanduser()
    payload: dict = {'path': str(p)}
    try:
        stat = p.stat()
    except FileNotFoundError:
        payload['missing'] = True
        return payload
    except OSError as exc:
        payload['error'] = exc.__class__.__name__
        return payload
    if not p.is_file():
        payload['kind'] = 'other'
        return payload
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        payload['kind'] = 'file'
        payload['semantic'] = 'unparsed-fallback'
        payload['mtime_ns'] = int(stat.st_mtime_ns)
        payload['size'] = int(stat.st_size)
        return payload
    sanitized = _process_wakeup_auth_fingerprint_payload(raw)
    try:
        encoded = json.dumps(
            sanitized,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
            default=str,
        ).encode('utf-8')
        payload['semantic_sha256'] = hashlib.sha256(encoded).hexdigest()
    except Exception:
        payload['kind'] = 'file'
        payload['semantic'] = 'encode-fallback'
        payload['mtime_ns'] = int(stat.st_mtime_ns)
        payload['size'] = int(stat.st_size)
    return payload


def process_wakeup_credential_state_fingerprint(session) -> str:
    """Return a metadata-only fingerprint for credential/config state.

    auth.json is rewritten by OAuth/token-refresh and request telemetry churn.
    Hash its semantic content instead of mtime/size so those rewrites do not
    clear a credential-exhausted process-wakeup pause. Secret fields are
    represented only by presence booleans: adding a credential changes the
    fingerprint, while rotating an existing token value does not persist or
    compare secret material.
    """
    try:
        hermes_home = _get_profile_home(getattr(session, 'profile', None))
    except Exception:
        hermes_home = Path(os.environ.get('HERMES_HOME') or HOME).expanduser()
    files = []
    for name in ('auth.json', 'config.yaml', 'config.yml', '.env'):
        path = hermes_home / name
        if name == 'auth.json':
            files.append((name, _process_wakeup_auth_store_fingerprint(path)))
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            files.append((name, 'missing'))
        except OSError as exc:
            files.append((name, 'error', exc.__class__.__name__))
        else:
            kind = 'file' if path.is_file() else 'other'
            files.append((name, kind, int(stat.st_mtime_ns), int(stat.st_size)))
    payload = {
        'version': 2,
        'profile': _process_wakeup_pause_part(getattr(session, 'profile', None)),
        'files': files,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def process_wakeup_pause_credential_state_changed(session) -> bool:
    """Return True when a stored credential pause should be revalidated."""
    pause = getattr(session, 'process_wakeup_pause', None)
    if not isinstance(pause, dict) or not pause.get('paused'):
        return False
    classification = _process_wakeup_pause_part(pause.get('classification'))
    if classification not in PROCESS_WAKEUP_PROVIDER_UNAVAILABLE_TYPES:
        return False
    previous = str(pause.get('credential_state_fingerprint') or '').strip()
    if not previous:
        return True
    return process_wakeup_credential_state_fingerprint(session) != previous


def _get_profile_home(profile) -> Path:
    """Resolve the hermes agent home directory for the given profile.

    Prefers the profile-specific helper from api.profiles; falls back to the
    HERMES_HOME environment variable or ~/.hermes, expanding ~ correctly.
    """
    try:
        from api.profiles import get_hermes_home_for_profile
        return Path(get_hermes_home_for_profile(profile))
    except ImportError:
        return Path(os.environ.get('HERMES_HOME') or '~/.hermes').expanduser()


_INTERRUPTED_RECOVERED_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The partial output above was recovered from the run journal, '
    'but the interrupted agent process could not continue.'
)
_INTERRUPTED_NO_OUTPUT_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'The user message above was preserved, but no agent output was recovered.'
)
_INTERRUPTED_PENDING_RETRY_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Recovering the partial output from the run journal — '
    'reload this session to retry.'
)
# Neutral wording used when the lazy retry path gives up (max attempts reached
# or the marker has been pending longer than _JOURNAL_RETRY_GIVEUP_SECONDS).
_INTERRUPTED_NEUTRAL_WORDING = (
    '**Response interrupted.**\n\n'
    'The live response stream stopped before this turn finished. '
    'Partial output may have been lost.'
)

_INTERRUPTION_CAUSE_DETAILS = {
    'process_restart': (
        'Evidence: the WebUI process started after this turn began, so this '
        'looks like a real process crash or restart.'
    ),
    'stream_run_split_brain': (
        'Evidence: the browser response stream was gone but the worker registry '
        'still listed the run. This is a stream/run bookkeeping split-brain.'
    ),
    'lost_worker_bookkeeping': (
        'Evidence: the stream was gone and worker bookkeeping no longer had an '
        'active run for it. This usually means the worker state was lost or '
        'cleaned up without a terminal event.'
    ),
    'unknown': (
        'Evidence: the stream stopped, but the WebUI could not classify the '
        'interruption more precisely.'
    ),
}


def _classify_interruption_cause(
    *, stream_id: str | None = None, pending_started_at=None,
) -> str:
    """Classify the stale live-response state without overstating certainty."""
    try:
        started = float(pending_started_at) if pending_started_at else None
    except (TypeError, ValueError):
        started = None

    if started is not None:
        try:
            if float(getattr(_cfg, 'SERVER_START_TIME', 0.0) or 0.0) > started:
                return 'process_restart'
        except (TypeError, ValueError):
            pass

    if stream_id:
        try:
            with _cfg.ACTIVE_RUNS_LOCK:
                if str(stream_id) in _cfg.ACTIVE_RUNS:
                    return 'stream_run_split_brain'
        except Exception:
            pass
        return 'lost_worker_bookkeeping'

    return 'unknown'


def _interrupted_content_for(
    *, recovered_output: bool, pending_retry: bool, interruption_cause: str,
) -> str:
    if recovered_output:
        outcome = (
            'The partial output above was recovered from the run journal, '
            'but the interrupted agent process could not continue.'
        )
    elif pending_retry:
        outcome = (
            'Recovering the partial output from the run journal — '
            'reload this session to retry.'
        )
    else:
        outcome = 'The user message above was preserved, but no agent output was recovered.'
    cause_detail = _INTERRUPTION_CAUSE_DETAILS.get(
        interruption_cause,
        _INTERRUPTION_CAUSE_DETAILS['unknown'],
    )
    return (
        '**Response interrupted.**\n\n'
        'The live response stream stopped before this turn finished. '
        f'{cause_detail} {outcome}'
    )


def _interrupted_recovery_marker(
    *,
    recovered_output: bool = False,
    pending_retry: bool = False,
    stream_id: str | None = None,
    pending_started_at=None,
) -> dict:
    """Build the standard interrupted-turn marker.

    ``recovered_output=True`` means the run journal already yielded visible
    text on this repair pass — the marker advertises that the partial output
    has been recovered.

    ``pending_retry=True`` is the lazy-retry hook: the journal was unreadable
    on this pass (page-cache loss, un-fsynced writes on slow FS, etc.). The
    marker carries a ``_pending_journal_recovery`` flag so a later
    ``get_session()`` can re-attempt recovery without baking a permanent
    "no output" claim into the transcript.

    The two are mutually exclusive; ``recovered_output`` wins if both are
    set so the caller cannot accidentally re-arm retry on a successful
    repair.
    """
    interruption_cause = _classify_interruption_cause(
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )
    content = _interrupted_content_for(
        recovered_output=recovered_output,
        pending_retry=pending_retry,
        interruption_cause=interruption_cause,
    )
    marker = {
        'role': 'assistant',
        'content': content,
        'timestamp': int(time.time()),
        '_error': True,
        'type': 'interrupted',
        'interruption_cause': interruption_cause,
    }
    if pending_retry and not recovered_output:
        marker['_pending_journal_recovery'] = True
    return marker


def _truncate_journal_tool_args(args, limit: int = 4) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in list(args.items())[:limit]:
        text = str(value)
        out[str(key)] = text[:120] + ('...' if len(text) > 120 else '')
    return out


def _normalize_journal_recovery_text(value) -> str:
    return " ".join(str(value or "").split())


def _message_matches_pending_checkpoint(message, pending_text, timestamp, source, attachments):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    try:
        message_timestamp = int(message.get('timestamp'))
        expected_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
        and message_timestamp == expected_timestamp
        and (message.get('_source') or 'webui') == (source or 'webui')
        and list(message.get('attachments') or []) == list(attachments or [])
    )


def _message_matches_pending_text(message, pending_text):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    return (
        _normalize_journal_recovery_text(message.get('content'))
        == _normalize_journal_recovery_text(pending_text)
    )


def _latest_user_matches_pending_text(messages, pending_text):
    if not isinstance(messages, list) or not pending_text:
        return False
    for message in reversed(messages):
        if isinstance(message, dict) and message.get('role') == 'user':
            return _message_matches_pending_text(message, pending_text)
    return False


def _partial_message_signature(message: dict) -> tuple:
    """Return a stable identity for partial assistant markers recovered on load."""
    if not isinstance(message, dict):
        return ('', '', ())
    tool_sig = []
    for tool_call in message.get('_partial_tool_calls') or []:
        if not isinstance(tool_call, dict):
            continue
        try:
            args_sig = json.dumps(
                tool_call.get('args') or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            args_sig = str(tool_call.get('args') or '')
        tool_sig.append((
            str(tool_call.get('name') or ''),
            args_sig,
            bool(tool_call.get('done', False)),
            bool(tool_call.get('is_error', False)),
            str(tool_call.get('preview') or tool_call.get('snippet') or ''),
        ))
    return (
        str(message.get('content') or '').strip(),
        str(message.get('reasoning') or '').strip(),
        tuple(tool_sig),
    )


def _collapse_adjacent_duplicate_partials(messages) -> tuple[list, bool]:
    """Collapse repeated identical partial markers from the same failed turn."""
    if not isinstance(messages, list):
        return messages, False
    collapsed = []
    changed = False
    previous_partial_sig = None
    for message in messages:
        if isinstance(message, dict) and message.get('_partial'):
            sig = _partial_message_signature(message)
            if previous_partial_sig == sig:
                changed = True
                continue
            previous_partial_sig = sig
        else:
            previous_partial_sig = None
        collapsed.append(message)
    return collapsed, changed


def _find_existing_assistant_for_journal_content(
    session,
    content: str,
    *,
    max_index: int | None = None,
    excluded_indexes: set[int] | None = None,
) -> int | None:
    candidate = _normalize_journal_recovery_text(content)
    if not candidate:
        return None
    messages = session.messages or []
    stop = len(messages) if max_index is None else min(len(messages), max_index)
    substring_match = None
    for idx in range(stop):
        if excluded_indexes and idx in excluded_indexes:
            continue
        message = messages[idx]
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        if message.get('_error'):
            continue
        existing = _normalize_journal_recovery_text(message.get('content'))
        if not existing:
            continue
        if existing == candidate:
            return idx
        if substring_match is None and len(candidate) >= 24 and candidate in existing:
            substring_match = idx
    return substring_match


def _journal_tool_already_present(
    session,
    name: str,
    preview: str,
    *,
    stream_id: str | None = None,
) -> bool:
    """Return True when an equivalent tool card already exists.

    Matching rule:

    * If the existing tool card carries ``_recovered_stream_id``, that means a
      previous journal-recovery run materialized it.  The retry can safely
      collapse against it only when both stream ids match — otherwise a
      legitimately-repeated tool (e.g. a second ``terminal: ls`` in a
      different turn) would be dropped.
    * If the existing tool card has no ``_recovered_stream_id`` (a live tool
      card, or a tool card carried over from a core transcript that pre-dates
      stream-id tagging), the legacy name+preview match still wins.  This
      preserves the "core transcript already has this tool, don't duplicate
      it" invariant the original repair path established.
    * When ``stream_id`` is omitted, the helper degrades cleanly to its
      pre-fix session-wide behaviour.
    """
    candidate_name = str(name or '')
    candidate_preview = _normalize_journal_recovery_text(preview)
    candidate_stream = str(stream_id) if stream_id else None
    for tool_call in session.tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        if str(tool_call.get('name') or '') != candidate_name:
            continue
        existing_preview = _normalize_journal_recovery_text(
            tool_call.get('preview') or tool_call.get('snippet') or ''
        )
        if existing_preview != candidate_preview:
            continue
        if candidate_stream is not None:
            existing_stream = tool_call.get('_recovered_stream_id')
            # A tool card explicitly tagged with a recovered_stream_id that
            # differs from ours belongs to another retry's turn — don't let
            # it pre-empt this retry.  Untagged tool cards (live or carried
            # over from the core transcript) still match.
            if existing_stream and str(existing_stream) != candidate_stream:
                continue
        return True
    return False


def _run_journal_has_visible_output(session, stream_id: str | None) -> bool:
    if not stream_id:
        return False
    try:
        from api.run_journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        return False
    for event in journal.get('events') or []:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        if event_name == 'token' and str(payload.get('text') or ''):
            return True
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                continue
            if str(payload.get('text') or '').strip():
                return True
        if event_name == 'reasoning':
            reasoning_text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if reasoning_text.strip():
                return True
        if event_name == 'tool':
            return True
    return False


def _run_journal_terminal_state(session, stream_id: str | None) -> str | None:
    if not stream_id:
        return None
    try:
        from api.run_journal import latest_run_summary
        summary = latest_run_summary(session.session_id, stream_id)
    except Exception:
        return None
    if not summary.get('terminal'):
        return None
    return str(summary.get('terminal_state') or '') or None


def _journal_is_still_arriving(session, stream_id: str | None) -> bool:
    """Return True for journals that may become visible on a later read.

    `read_run_events()` deliberately collapses missing files and empty files
    into an empty event list, so the lazy retry path needs a small filesystem
    visibility check to avoid burning all retry attempts while WSL2 / network
    filesystems are still surfacing the journal.  Non-empty journals are treated
    as sealed enough for retry-budget accounting; if they contain no visible
    output, the normal capped give-up path handles them.
    """
    if not stream_id:
        return False
    try:
        from api.run_journal import _run_path, latest_run_summary

        path = _run_path(session.session_id, stream_id)
        summary = latest_run_summary(session.session_id, stream_id)
        if summary.get('terminal'):
            return False
        try:
            return (not path.exists()) or path.stat().st_size == 0
        except OSError:
            return True
    except Exception:
        logger.debug(
            "Session %s: failed to classify journal visibility for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False


def _append_journaled_partial_output(
    session,
    stream_id: str | None,
    *,
    dedupe_existing: bool = False,
) -> bool:
    """Recover already-emitted visible output from a dead stream journal.

    This repair path is intentionally conservative: it restores user-visible
    assistant text, display-only reasoning, and tool-card metadata that had
    already been emitted over SSE before the WebUI process died. Restored
    reasoning stays out of ``context_messages`` so it cannot become provider-
    facing history. The repair does not try to continue execution.
    """
    if not stream_id:
        return False

    try:
        from api.run_journal import read_run_events
        journal = read_run_events(session.session_id, stream_id)
    except Exception:
        logger.debug(
            "Session %s: failed to read run journal for stream %s",
            getattr(session, 'session_id', '?'),
            stream_id,
            exc_info=True,
        )
        return False

    events = [event for event in journal.get('events') or [] if isinstance(event, dict)]
    if not events:
        return False

    appended_any = False
    assistant_parts: list[str] = []
    reasoning_parts: list[str] = []
    assistant_started_at: float | None = None
    current_assistant_idx: int | None = None
    recovered_tool_calls: list[dict] = []
    initial_message_count = len(session.messages or [])
    claimed_existing_assistant_indexes: set[int] = set()

    def content_match_can_receive_reasoning(existing_idx: int) -> bool:
        messages = session.messages or []
        owner_idx = None
        for candidate_idx in range(existing_idx - 1, -1, -1):
            candidate = messages[candidate_idx]
            if isinstance(candidate, dict) and candidate.get('role') == 'user':
                owner_idx = candidate_idx
                break
        if owner_idx is None:
            return False

        pending_text = _normalize_journal_recovery_text(session.pending_user_message)
        if pending_text and not _message_matches_pending_checkpoint(
            messages[owner_idx],
            session.pending_user_message,
            session.pending_started_at,
            session.pending_user_source,
            session.pending_attachments,
        ):
            return False

        for candidate_idx in range(existing_idx + 1, initial_message_count):
            candidate = messages[candidate_idx]
            if not isinstance(candidate, dict) or candidate.get('role') != 'user':
                continue
            candidate_text = _normalize_journal_recovery_text(candidate.get('content'))
            candidate_matches_checkpoint = pending_text and _message_matches_pending_checkpoint(
                candidate,
                session.pending_user_message,
                session.pending_started_at,
                session.pending_user_source,
                session.pending_attachments,
            )
            if candidate_matches_checkpoint and candidate.get('_recovered'):
                continue
            if pending_text and candidate_text == pending_text:
                return False
            return False
        return True

    def append_context_projection(message: dict) -> None:
        context_projection = dict(message)
        context_projection.pop('reasoning', None)
        _append_recovered_turn_to_context(session, context_projection)

    def attach_display_reasoning(message: dict, reasoning: str) -> bool:
        if not reasoning:
            return False
        existing = str(message.get('reasoning') or '').strip()
        if existing:
            return False
        message['reasoning'] = reasoning
        return True

    def flush_assistant() -> int | None:
        nonlocal appended_any, assistant_parts, reasoning_parts
        nonlocal assistant_started_at, current_assistant_idx
        content = ''.join(assistant_parts).strip()
        reasoning = ''.join(reasoning_parts).strip()
        assistant_parts = []
        reasoning_parts = []
        if not content and not reasoning:
            return current_assistant_idx
        if dedupe_existing and content:
            search_excluded = set(claimed_existing_assistant_indexes)
            existing_idx = None
            while True:
                candidate_idx = _find_existing_assistant_for_journal_content(
                    session,
                    content,
                    max_index=initial_message_count,
                    excluded_indexes=search_excluded,
                )
                if candidate_idx is None:
                    break
                if not reasoning or content_match_can_receive_reasoning(candidate_idx):
                    existing_idx = candidate_idx
                    break
                search_excluded.add(candidate_idx)
            if existing_idx is not None:
                claimed_existing_assistant_indexes.add(existing_idx)
                current_assistant_idx = existing_idx
                assistant_started_at = None
                if 0 <= existing_idx < len(session.messages):
                    existing_message = session.messages[existing_idx]
                    append_context_projection(existing_message)
                    if attach_display_reasoning(existing_message, reasoning):
                        appended_any = True
                return existing_idx
        if dedupe_existing and reasoning and not content:
            for existing_idx in range(initial_message_count):
                if existing_idx in claimed_existing_assistant_indexes:
                    continue
                existing_message = session.messages[existing_idx]
                if not isinstance(existing_message, dict):
                    continue
                if (
                    existing_message.get('_recovered_from_run_journal')
                    and existing_message.get('_recovered_stream_id') == stream_id
                    and existing_message.get('role') == 'assistant'
                    and not str(existing_message.get('content') or '').strip()
                    and str(existing_message.get('reasoning') or '').strip() == reasoning
                ):
                    claimed_existing_assistant_indexes.add(existing_idx)
                    current_assistant_idx = existing_idx
                    assistant_started_at = None
                    return existing_idx
        timestamp = int(assistant_started_at or time.time())
        recovered_assistant = {
            'role': 'assistant',
            'content': content,
            'timestamp': timestamp,
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        }
        attach_display_reasoning(recovered_assistant, reasoning)
        session.messages.append(recovered_assistant)
        append_context_projection(recovered_assistant)
        current_assistant_idx = len(session.messages) - 1
        assistant_started_at = None
        appended_any = True
        return current_assistant_idx

    def ensure_assistant_anchor(created_at: float | None = None) -> int:
        nonlocal appended_any, current_assistant_idx
        idx = flush_assistant()
        if idx is not None:
            return idx
        # A stream can start with tools before any text. Keep those tools
        # visible after restart with an empty recovered assistant anchor instead
        # of inventing synthetic progress prose.
        #
        # Dedup guard (#3875): reuse an existing empty recovered anchor for THIS
        # stream instead of appending a fresh one. The lazy read-side retry path
        # (_retry_journal_recovery_in_place) re-runs this recovery on repeated
        # get_session() calls, and a tool-first stream that never emitted text
        # has no content to dedup on (flush_assistant() returns early on empty),
        # so without this guard each retry — and each distinct interrupted stream
        # over the session's life — appends another empty anchor. A session that
        # was interrupted-and-recovered many times then accumulates thousands of
        # empty content-less assistant rows, bloating the file and (combined with
        # the render path) painting the transcript blank. One anchor per stream
        # is all that's needed to host its recovered tool cards.
        for _existing_idx in range(len(session.messages) - 1, -1, -1):
            _m = session.messages[_existing_idx]
            if not isinstance(_m, dict):
                continue
            if (
                _m.get('_recovered_from_run_journal')
                and _m.get('_recovered_stream_id') == stream_id
                and _m.get('role') == 'assistant'
                and not str(_m.get('content') or '').strip()
                and not str(_m.get('reasoning') or '').strip()
            ):
                current_assistant_idx = _existing_idx
                return _existing_idx
        session.messages.append({
            'role': 'assistant',
            'content': '',
            'timestamp': int(created_at or time.time()),
            '_recovered_from_run_journal': True,
            '_recovered_stream_id': stream_id,
        })
        current_assistant_idx = len(session.messages) - 1
        appended_any = True
        return current_assistant_idx

    for event in events:
        event_name = str(event.get('event') or event.get('type') or '')
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        created_at = event.get('created_at') if isinstance(event.get('created_at'), (int, float)) else None
        if event_name == 'reasoning':
            text = str(
                payload.get('text') or payload.get('reasoning') or payload.get('thinking') or ''
            )
            if not text:
                continue
            if not assistant_parts and not reasoning_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            reasoning_parts.append(text)
            continue
        if event_name == 'token':
            text = str(payload.get('text') or '')
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            assistant_parts.append(text)
            continue
        if event_name == 'interim_assistant':
            if payload.get('already_streamed'):
                flush_assistant()
                continue
            text = str(payload.get('text') or '').strip()
            if not text:
                continue
            if not assistant_parts and assistant_started_at is None:
                assistant_started_at = created_at or time.time()
            if assistant_parts and not ''.join(assistant_parts).endswith(('\n', ' ')):
                assistant_parts.append('\n\n')
            assistant_parts.append(text)
            flush_assistant()
            continue
        if event_name == 'tool':
            anchor_idx = flush_assistant()
            if anchor_idx is None:
                anchor_idx = ensure_assistant_anchor(created_at)
            name = str(payload.get('name') or 'tool')
            preview = str(payload.get('preview') or '')
            if dedupe_existing and _journal_tool_already_present(
                session, name, preview, stream_id=stream_id,
            ):
                current_assistant_idx = anchor_idx
                continue
            recovered_tool_calls.append({
                'name': name,
                'preview': preview,
                'snippet': preview,
                'tid': f"journal-{event.get('seq') or len(recovered_tool_calls) + 1}",
                'assistant_msg_idx': anchor_idx,
                'args': _truncate_journal_tool_args(payload.get('args') or {}),
                'done': False,
                '_recovered_from_run_journal': True,
                '_recovered_stream_id': stream_id,
            })
            appended_any = True
            current_assistant_idx = anchor_idx
            continue
        if event_name == 'tool_complete':
            name = str(payload.get('name') or '')
            for tool_call in reversed(recovered_tool_calls):
                if tool_call.get('done'):
                    continue
                if not name or tool_call.get('name') == name:
                    tool_call['done'] = True
                    if payload.get('preview'):
                        tool_call['preview'] = str(payload.get('preview') or '')
                        tool_call['snippet'] = str(payload.get('preview') or '')
                    if payload.get('duration') is not None:
                        tool_call['duration'] = payload.get('duration')
                    tool_call['is_error'] = bool(payload.get('is_error', False))
                    break
            continue
        if event_name in {'done', 'stream_end', 'cancel', 'apperror', 'error'}:
            flush_assistant()

    flush_assistant()
    if recovered_tool_calls:
        session.tool_calls = list(session.tool_calls or []) + recovered_tool_calls
        appended_any = True
    return appended_any


# ── Lazy run-journal recovery (read-side self-heal) ─────────────────────────
#
# When sidecar repair runs before the run-journal for the dead stream is
# visible on disk (page-cache loss on WSL2 9p / DrvFs, an un-fsynced journal
# tail, a slow network FS, …), `_append_journaled_partial_output` returns
# False even though the journaled events will appear on disk shortly. Without
# the helpers below the repair path baked a permanent "no agent output was
# recovered" claim into the marker, and a later session read could never
# correct it.
#
# The contract is:
#
#   * Sidecar repair (`_apply_core_sync_or_error_marker`) writes a marker
#     with `_pending_journal_recovery=True` whenever it could not recover
#     visible output AND the stream id is known. Three retry-meta keys go
#     onto the marker: `_journal_retry_stream_id`, `_journal_retry_attempts`,
#     `_journal_retry_first_seen_ts`.
#   * Every `get_session()` call that returns the full session checks the
#     latest assistant marker; if the flag is set it re-runs
#     `_append_journaled_partial_output` with `dedupe_existing=True`. On
#     success the marker is promoted in place to the recovered-output
#     wording, the journaled rows are reordered to sit above the marker,
#     and all retry meta is stripped. If the journal is still missing or
#     zero-byte, the retry is a no-op and does not consume attempt budget.
#     Terminal/non-useful journals consume attempt budget and can demote
#     immediately at the max-attempt cap.
#   * After `_JOURNAL_RETRY_MAX_ATTEMPTS` failed retries or
#     `_JOURNAL_RETRY_GIVEUP_SECONDS` of wall-clock age, the marker is
#     demoted to the neutral wording ("Partial output may have been lost.")
#     so users do not see "reload to retry" prompts forever.
_JOURNAL_RETRY_MAX_ATTEMPTS = 12
_JOURNAL_RETRY_GIVEUP_SECONDS = 24 * 3600
_JOURNAL_RETRY_LOCKS: dict[str, threading.Lock] = {}
_JOURNAL_RETRY_LOCKS_GUARD = threading.Lock()


def _journal_retry_lock_for_sid(sid: str) -> threading.Lock:
    with _JOURNAL_RETRY_LOCKS_GUARD:
        return _JOURNAL_RETRY_LOCKS.setdefault(str(sid), threading.Lock())


def _build_recovery_marker_with_retry_hook(
    *, recovered_output: bool, stream_id: str | None, pending_started_at=None,
) -> dict:
    """Build an interrupted-turn marker, arming the lazy-retry hook when
    visible output was not recovered yet but a stream id is available."""
    if recovered_output:
        return _interrupted_recovery_marker(
            recovered_output=True,
            stream_id=stream_id,
            pending_started_at=pending_started_at,
        )
    if not stream_id:
        return _interrupted_recovery_marker(
            recovered_output=False,
            pending_started_at=pending_started_at,
        )
    marker = _interrupted_recovery_marker(
        pending_retry=True,
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )
    marker['_journal_retry_stream_id'] = str(stream_id)
    marker['_journal_retry_attempts'] = 0
    marker['_journal_retry_first_seen_ts'] = int(time.time())
    return marker


def _session_has_pending_journal_retry(session) -> bool:
    """Cheap short-circuit: scan from the tail until the most recent normal
    assistant turn. Any `_pending_journal_recovery` flag found before then
    means a retry is queued.
    """
    messages = getattr(session, 'messages', None) or []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get('_pending_journal_recovery'):
            return True
        if msg.get('role') == 'assistant' and not msg.get('_error'):
            # A normal assistant turn after any pending marker — nothing to
            # retry above this point.
            return False
    return False


def _strip_journal_retry_meta(marker: dict) -> None:
    marker.pop('_pending_journal_recovery', None)
    marker.pop('_journal_retry_stream_id', None)
    marker.pop('_journal_retry_attempts', None)
    marker.pop('_journal_retry_first_seen_ts', None)


def _reorder_journal_tail_above_marker(session, marker_idx: int) -> None:
    """Move `_recovered_from_run_journal=True` rows appended *after*
    ``marker_idx`` to sit immediately above the marker so chronological
    order is preserved (journaled output happened during the turn, marker
    annotates its end).
    """
    messages = session.messages
    if marker_idx < 0 or marker_idx >= len(messages):
        return
    tail = messages[marker_idx + 1 :]
    if not tail:
        return
    journaled = [
        m for m in tail
        if isinstance(m, dict) and m.get('_recovered_from_run_journal')
    ]
    if not journaled:
        return
    rest = [
        m for m in tail
        if not (isinstance(m, dict) and m.get('_recovered_from_run_journal'))
    ]
    marker = messages[marker_idx]
    new_messages = (
        messages[:marker_idx]
        + journaled
        + [marker]
        + rest
    )
    # Rebase any tool_calls.assistant_msg_idx values that pointed into the
    # journaled rows when they were appended at the tail.
    old_journaled_idx_base = marker_idx + 1
    new_journaled_idx_base = marker_idx
    shift = new_journaled_idx_base - old_journaled_idx_base  # = -1
    for tool_call in session.tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        idx = tool_call.get('assistant_msg_idx')
        if isinstance(idx, int) and idx >= old_journaled_idx_base \
                and idx < old_journaled_idx_base + len(journaled):
            tool_call['assistant_msg_idx'] = idx + shift
    session.messages = new_messages


def _try_retry_journal_recovery_in_place(session) -> bool:
    sid = str(getattr(session, 'session_id', '') or '')
    lock = _journal_retry_lock_for_sid(sid)
    if not lock.acquire(blocking=False):
        logger.debug("lazy journal-retry already running for session %s", sid)
        return False
    try:
        return _retry_journal_recovery_in_place(
            session, preserve_arriving_budget=True,
        )
    finally:
        lock.release()
        with _JOURNAL_RETRY_LOCKS_GUARD:
            if _JOURNAL_RETRY_LOCKS.get(sid) is lock:
                _JOURNAL_RETRY_LOCKS.pop(sid, None)


def _retry_journal_recovery_in_place(
    session,
    *,
    preserve_arriving_budget: bool = False,
) -> bool:
    """Re-attempt run-journal recovery for the most recent pending marker.

    Returns True if the marker was promoted to the recovered-output wording.
    Never raises — caller is best-effort.
    """
    try:
        messages = session.messages or []
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if not isinstance(msg, dict):
                continue
            if msg.get('role') == 'assistant' and not msg.get('_error') \
                    and not msg.get('_pending_journal_recovery'):
                # Walked past the pending marker without finding it.
                return False
            if not (
                msg.get('type') == 'interrupted'
                and msg.get('_pending_journal_recovery')
            ):
                continue
            stream_id = msg.get('_journal_retry_stream_id')
            first_seen = msg.get('_journal_retry_first_seen_ts') or 0
            attempts = int(msg.get('_journal_retry_attempts') or 0)
            now = time.time()
            give_up = (
                attempts >= _JOURNAL_RETRY_MAX_ATTEMPTS
                or (
                    first_seen
                    and now - float(first_seen) > _JOURNAL_RETRY_GIVEUP_SECONDS
                )
            )
            if not stream_id:
                # No stream id to retry against; demote immediately.
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while demoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                return False
            if give_up:
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while demoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                return False
            tail_len_before = len(session.messages)
            ok = _append_journaled_partial_output(
                session, stream_id, dedupe_existing=True,
            )
            if ok:
                msg['content'] = _INTERRUPTED_RECOVERED_WORDING
                _strip_journal_retry_meta(msg)
                # The journaled rows were appended at the end of messages;
                # only the rows past the previous tail count as "newly
                # journaled" and need to move above the marker.
                _ = tail_len_before  # informational; helper below scans
                _reorder_journal_tail_above_marker(session, idx)
                try:
                    session.save(touch_updated_at=False)
                except Exception:
                    logger.debug(
                        "save() failed while promoting marker for session %s",
                        getattr(session, 'session_id', '?'),
                        exc_info=True,
                    )
                logger.info(
                    "Session %s: lazy journal-recovery promoted marker for "
                    "stream %s after %d attempts",
                    getattr(session, 'session_id', '?'),
                    stream_id,
                    attempts,
                )
                return True
            if (
                preserve_arriving_budget
                and _journal_is_still_arriving(session, stream_id)
            ):
                logger.debug(
                    "Session %s: journal for stream %s still arriving; "
                    "preserving retry budget",
                    getattr(session, 'session_id', '?'),
                    stream_id,
                )
                return False
            next_attempts = attempts + 1
            if next_attempts >= _JOURNAL_RETRY_MAX_ATTEMPTS:
                msg['content'] = _INTERRUPTED_NEUTRAL_WORDING
                _strip_journal_retry_meta(msg)
            else:
                msg['_journal_retry_attempts'] = next_attempts
            try:
                session.save(touch_updated_at=False)
            except Exception:
                logger.debug(
                    "save() failed while updating retry counter for session %s",
                    getattr(session, 'session_id', '?'),
                    exc_info=True,
                )
            return False
        return False
    except Exception:
        logger.exception(
            "_retry_journal_recovery_in_place failed for session %s",
            getattr(session, 'session_id', '?'),
        )
        return False


def _apply_core_sync_or_error_marker(
    session,
    core_path,
    stream_id_for_recheck=None,
    *,
    require_stream_dead=True,
    touch_updated_at=True,
) -> bool:
    """Inner repair logic. Must be called with the per-session lock already held.

    Re-checks session state under the lock, then either syncs messages from the
    core transcript (if present and non-empty) or restores the pending user
    message as a recovered user turn and appends an error marker.

    stream_id_for_recheck: when provided, repair bails if session.active_stream_id
    changed (e.g. context compression rotated it).  The cache-miss repair path
    also requires the stream to be absent from active streams; the streaming
    thread's final fallback passes require_stream_dead=False because it runs
    before its own stream is removed from STREAMS.

    Returns True if repair was applied, False if the re-check bailed out.
    Must never raise — caller is responsible for exception handling.
    """
    sid = session.session_id
    # Bail if pending is unset — nothing to repair.
    if not session.pending_user_message:
        return False
    if stream_id_for_recheck is not None:
        # Bail if active_stream_id rotated between the pre-lock check and now.
        # Cache-miss repair must also skip if the stream is alive again, but the
        # streaming thread's final fallback runs before removing its own stream
        # from STREAMS and must be allowed to repair that same active stream.
        if session.active_stream_id != stream_id_for_recheck:
            return False
        if require_stream_dead and session.active_stream_id in _active_stream_ids():
            return False

    # When messages is already non-empty, do not overwrite history from any core
    # transcript. The pending user turn may still be the only durable copy of a
    # prompt submitted just before a server restart, so materialize it before
    # clearing runtime stream state.
    if len(session.messages) != 0:
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _already_checkpointed = _message_matches_pending_checkpoint(
            session.messages[-1],
            session.pending_user_message,
            _recovered_ts,
            session.pending_user_source,
            session.pending_attachments,
        )
        _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
            session.messages[-1],
            session.pending_user_message,
        )
        _stream_id = stream_id_for_recheck or session.active_stream_id
        _pending_started_at = session.pending_started_at
        if _run_journal_terminal_state(session, _stream_id) == 'completed':
            if not (_already_checkpointed or _latest_user_matches_pending_text(session.messages, session.pending_user_message)):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: cleared stale pending state for completed stream %s without error marker",
                sid,
                _stream_id,
            )
            return True
        if not _tail_user_already_checkpointed:
            _append_recovered_pending_turn(session, timestamp=_recovered_ts)
        else:
            recovered = {
                'role': 'user',
                'content': session.pending_user_message,
                'timestamp': _recovered_ts,
                '_recovered': True,
            }
            pending_source = getattr(session, 'pending_user_source', None)
            if pending_source and pending_source != 'webui':
                recovered['_source'] = pending_source
            if session.pending_attachments:
                recovered['attachments'] = list(session.pending_attachments)
            _append_recovered_turn_to_context(session, recovered)
        recovered_output = _append_journaled_partial_output(
            session,
            _stream_id,
        )
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        session.messages.append(
            _build_recovery_marker_with_retry_hook(
                recovered_output=recovered_output,
                stream_id=_stream_id,
                pending_started_at=_pending_started_at,
            )
        )
        session.save(touch_updated_at=touch_updated_at)
        logger.info(
            "Session %s: recovered pending user turn (messages non-empty), added error marker",
            sid,
        )
        return True

    # ── messages *is* empty ─ full repair ─────────────────────────────────

    if core_path.exists():
        with open(core_path, encoding='utf-8') as f:
            core = json.load(f)
        core_messages = core.get('messages', [])
        if core_messages:
            _stream_id = stream_id_for_recheck or session.active_stream_id
            session.messages = core_messages
            session.tool_calls = core.get('tool_calls', [])
            for field in ('input_tokens', 'output_tokens', 'estimated_cost'):
                if core.get(field) is not None:
                    setattr(session, field, core[field])
            _pending_text = _normalize_journal_recovery_text(session.pending_user_message)
            _recovered_ts = int(time.time())
            if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
                _recovered_ts = int(session.pending_started_at)
            _already_checkpointed = _message_matches_pending_checkpoint(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
                _recovered_ts,
                session.pending_user_source,
                session.pending_attachments,
            )
            _tail_user_already_checkpointed = _already_checkpointed or _message_matches_pending_text(
                session.messages[-1] if session.messages else None,
                session.pending_user_message,
            )
            if (
                _pending_text
                and not _tail_user_already_checkpointed
                and _run_journal_has_visible_output(session, _stream_id)
            ):
                _append_recovered_pending_turn(session, timestamp=_recovered_ts)
            recovered_output = _append_journaled_partial_output(
                session,
                _stream_id,
                dedupe_existing=True,
            )
            _pending_started_at = session.pending_started_at
            session.active_stream_id = None
            session.pending_user_message = None
            session.pending_attachments = []
            session.pending_started_at = None
            session.pending_user_source = None
            if recovered_output:
                session.messages.append(
                    _interrupted_recovery_marker(
                        recovered_output=True,
                        stream_id=_stream_id,
                        pending_started_at=_pending_started_at,
                    )
                )
            # NOTE: when the core transcript was synced in but the run journal
            # is not yet visible, intentionally do NOT append a lazy-retry
            # marker here. In this branch the canonical history is the core
            # transcript itself (which has already been written to s.messages
            # above) and the marker is purely advisory — the existing contract
            # is "marker only when there is a recovered partial turn to
            # annotate". Adding a pending-retry marker on every empty-journal
            # core-sync would surface a spurious "reload to retry" banner on
            # sessions whose journal is legitimately absent (e.g. archived
            # streams). The first and third branches handle the lost-response
            # case where the marker is the only signal the user gets.
            session.save(touch_updated_at=touch_updated_at)
            logger.info(
                "Session %s: synced %d messages from core transcript%s",
                sid,
                len(core_messages),
                " and recovered journaled output" if recovered_output else "",
            )
            return True

    # Core missing or empty — restore the pending user message as a recovered
    # user turn (preserving the draft), then append an error marker.
    if session.pending_user_message:
        # Use the original send time if available so the recovered turn
        # appears in the correct chronological position.
        _recovered_ts = int(time.time())
        if isinstance(session.pending_started_at, (int, float)) and session.pending_started_at > 0:
            _recovered_ts = int(session.pending_started_at)
        _append_recovered_pending_turn(session, timestamp=_recovered_ts)
    recovered_output = _append_journaled_partial_output(
        session,
        stream_id_for_recheck or session.active_stream_id,
    )
    _stream_id = stream_id_for_recheck or session.active_stream_id
    _pending_started_at = session.pending_started_at
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    session.pending_user_source = None
    session.messages.append(
        _build_recovery_marker_with_retry_hook(
            recovered_output=recovered_output,
            stream_id=_stream_id,
            pending_started_at=_pending_started_at,
        )
    )
    session.save(touch_updated_at=touch_updated_at)
    logger.info("Session %s: no core transcript found, added error marker", sid)
    return True


# ── _repair_stale_pending grace period (#1624) ─────────────────────────────
#
# Defense-in-depth against a narrow race between the streaming thread clearing
# pending_user_message and STREAMS.pop(stream_id). Without this guard, any
# fast turn (e.g. command approval) that exits the thread before the on-disk
# pending clear has flushed gets misdiagnosed as a crashed turn, producing a
# spurious "Response interrupted." marker.
#
# 30s covers the worst-case post-loop persistence window: LLM finishing a tool
# batch + lock contention with the checkpoint thread + a multi-MB session.save.
# A legitimately crashed turn whose pending_started_at is < 30s old will not
# repair on the first get_session() call, but WILL repair on the next call
# after the grace period elapses (typically the user's next interaction).
#
# Missing/falsy pending_started_at (legacy sidecars from before that field
# existed, or any path that forgot to set it) is treated as "old enough" so
# repair still recovers them — preserves current behavior for legacy data.
_REPAIR_STALE_PENDING_GRACE_SECONDS = 30


def _has_compression_continuation(session) -> bool:
    """Return True when ``session`` is an archived compression parent.

    Context compression rotates the live WebUI session id: the old sidecar is
    preserved for lineage while the new child owns the running/completed turn.
    Stale-pending repair must not append an interruption marker to that old
    parent just because its stream bookkeeping disappeared after the rotation.
    """
    sid = getattr(session, 'session_id', None)
    if not sid:
        return False

    def _row_is_continuation(row) -> bool:
        if not isinstance(row, dict):
            return False
        child_sid = row.get('session_id')
        if not child_sid or child_sid == sid:
            return False
        if row.get('parent_session_id') != sid:
            return False
        # Any child row is enough evidence that this pending state belongs to a
        # compression lineage, not a dead standalone turn. The child may itself
        # temporarily carry a bad pre_compression_snapshot flag from older code;
        # do not filter it out here or the guard misses the exact regression.
        return True

    try:
        with LOCK:
            for child in SESSIONS.values():
                if getattr(child, 'session_id', None) == sid:
                    continue
                if getattr(child, 'parent_session_id', None) == sid:
                    return True
    except Exception:
        pass

    try:
        if SESSION_INDEX_FILE.exists():
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
            if isinstance(entries, list) and any(_row_is_continuation(e) for e in entries):
                return True
    except Exception:
        logger.debug("Failed to inspect session index for compression continuation", exc_info=True)

    # Index rows can lag behind rapid compression/save races. Fall back to a
    # shallow JSON metadata scan; session files write parent_session_id before
    # the messages array, so this avoids loading multi-MB transcripts.
    try:
        needle = f'"parent_session_id": "{sid}"'
        for path in SESSION_DIR.glob('*.json'):
            if path.name.startswith('_') or path.stem == sid:
                continue
            try:
                # Preserve the old read_text()[:4096] CHARACTER-prefix semantics
                # with bounded I/O: a UTF-8 char is at most 4 bytes, so 4096 chars
                # fit in <=16384 bytes. Reading bytes then slicing to 4096 chars
                # avoids a regression where a multi-byte (e.g. emoji) compression
                # summary written before parent_session_id pushes the needle past a
                # 4096-BYTE cutoff even though it was within the old 4096-CHAR one.
                head = _read_file_head(path, max_prefix_bytes=16384)[:4096]
            except OSError:
                continue
            if needle in head:
                return True
    except Exception:
        logger.debug("Failed to scan session files for compression continuation", exc_info=True)

    return False


def _repair_stale_pending(session) -> bool:
    """Recover a sidecar stuck with messages=[] and stale pending state.

    Fires only when messages is empty, pending_user_message is set,
    active_stream_id is set, the stream is no longer alive, AND the turn is
    older than _REPAIR_STALE_PENDING_GRACE_SECONDS (#1624).

    Uses a non-blocking lock acquire so a caller that already holds the
    per-session lock (e.g. retry_last, undo_last, cancel_stream) cannot
    deadlock when get_session() triggers this on a cache miss.

    Returns True if repair was applied, False otherwise.
    Must never raise — all errors are caught and logged.
    """
    # Capture the stream id seen at pre-check time; the under-lock re-check in
    # _apply_core_sync_or_error_marker uses this to detect a rotated active_stream_id
    # (e.g. context compression) or a stream that came back alive.
    _seen_stream_id = session.active_stream_id
    if (not session.pending_user_message
            or not _seen_stream_id
            or _seen_stream_id in _active_stream_ids()):
        return False
    if getattr(session, 'pre_compression_snapshot', False):
        logger.debug(
            "_repair_stale_pending: skipping pre-compression snapshot %s",
            getattr(session, 'session_id', '?'),
        )
        return False
    if _has_compression_continuation(session):
        logger.debug(
            "_repair_stale_pending: skipping compression parent %s with continuation",
            getattr(session, 'session_id', '?'),
        )
        return False

    # Grace-period guard: bail if the turn is too fresh to be a real crash.
    # Falsy pending_started_at (None, 0, missing) means "old enough" — preserve
    # legacy-data recovery semantics for sessions that pre-date the field.
    _started = getattr(session, 'pending_started_at', None)
    if _started:
        try:
            _age = time.time() - float(_started)
        except (TypeError, ValueError):
            _age = float('inf')
        if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
            logger.debug(
                "_repair_stale_pending: skipping repair for session %s — "
                "pending_started_at age=%.1fs < %ds grace window",
                session.session_id, _age, _REPAIR_STALE_PENDING_GRACE_SECONDS,
            )
            return False
    else:
        # Treat missing/falsy pending_started_at as "old enough" (legacy data).
        _age = float('inf')

    sid = session.session_id
    if not is_safe_session_id(sid):
        return False

    try:
        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{sid}.json'

        lock = _get_session_agent_lock(sid)
        # Non-blocking acquire: bail immediately if the caller already holds this
        # lock (e.g. retry_last, undo_last, cancel_stream). Blocking would deadlock
        # because _get_session_agent_lock returns a non-reentrant threading.Lock.
        if not lock.acquire(blocking=False):
            logger.debug(
                "_repair_stale_pending: lock contended, skipping repair for session %s", sid,
            )
            return False
        try:
            # Telemetry (#1624): log legitimate repair firings so the next batch
            # of user reports tells us whether the underlying race still fires
            # post-fix. Rate-limit by age (Opus pre-release SHOULD-FIX): WARNING
            # for the diagnostically valuable race window (< 5 min — actual
            # leak-path candidates that slipped past the grace guard) and DEBUG
            # for the long-tail (orphaned sidecars from prior process lifetimes)
            # so reconnect loops on stuck sessions don't flood the log.
            _DIAG_WARN_WINDOW_SECONDS = 300  # 5 min
            _age_str = ('inf' if _age == float('inf') else f'{_age:.1f}s')
            _log = logger.warning if _age < _DIAG_WARN_WINDOW_SECONDS else logger.debug
            _log(
                "_repair_stale_pending firing: session=%s stream_id=%s pending_age=%s",
                sid, _seen_stream_id, _age_str,
            )
            return _apply_core_sync_or_error_marker(
                session, core_path, stream_id_for_recheck=_seen_stream_id,
            )
        finally:
            lock.release()
    except Exception:
        logger.exception("_repair_stale_pending failed for session %s", sid)
        return False


def _sync_sidecar_from_state_db_if_newer(session) -> bool:
    """Read-side self-heal when WebUI sidecar lags Hermes state.db.

    A WebUI stream can lose its terminal ``done``/``stream_end`` path while the
    underlying agent continues writing messages to ``state.db``. In that shape
    the browser briefly shows live SSE output, but a refresh reloads the stale
    sidecar JSON and the already-produced text appears to vanish. Reconcile the
    sidecar from state.db whenever the state transcript is visibly newer than
    the sidecar, even if the sidecar still carries an ``active_stream_id``.

    This deliberately reuses the existing append-only reconciler so workspace
    prefixes, timestamp drift, compaction watermarks, and tool metadata keep the
    same semantics as normal WebUI/state.db display merging.
    """
    if session is None or getattr(session, '_loaded_metadata_only', False):
        return False
    sid = getattr(session, 'session_id', None)
    if not sid or not is_safe_session_id(sid):
        return False
    seen_stream_id = getattr(session, 'active_stream_id', None)
    has_unfinished_sidecar_turn = bool(
        seen_stream_id or getattr(session, 'pending_user_message', None)
    )
    if not has_unfinished_sidecar_turn:
        return False
    # Never reconcile while the sidecar's stream is still a LIVE in-process
    # worker. A running turn owns the final writeback (it merges the agent
    # result and clears pending state itself); racing it here would drop its
    # active_stream_id mid-run and make the normal terminal writeback skip as
    # "stale". Only self-heal once the worker is gone from both the SSE
    # (STREAMS) and worker-lifecycle (ACTIVE_RUNS) registries.
    if seen_stream_id and seen_stream_id in _active_stream_ids():
        return False
    # Registration-window grace guard (mirrors _repair_stale_pending). A turn is
    # registered in STREAMS/ACTIVE_RUNS by the worker thread a moment AFTER the
    # request handler persists active_stream_id + pending_started_at to the
    # sidecar. Within that window the stream is legitimately in flight yet not
    # yet visible in the registries, so the liveness check above would
    # mis-classify it as a dead stream. A recent pending_started_at means "still
    # starting up" — bail. This also covers cross-process / gateway turns the
    # local registries cannot see. Falsy pending_started_at (None/0/missing) is
    # treated as "old enough" so legacy/orphaned sidecars still self-heal.
    if seen_stream_id:
        _started = getattr(session, 'pending_started_at', None)
        if _started:
            try:
                _age = time.time() - float(_started)
            except (TypeError, ValueError):
                _age = float('inf')
            if _age < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                return False

    try:
        state_summary = get_state_db_session_summary(
            sid,
            profile=getattr(session, 'profile', None),
        )
        state_count = int(state_summary.get('message_count') or 0)
        state_last = float(state_summary.get('last_message_at') or 0.0)
    except Exception:
        logger.debug("state.db summary check failed for session %s", sid, exc_info=True)
        return False
    if state_count <= 0:
        return False

    sidecar_messages = list(getattr(session, 'messages', None) or [])
    sidecar_count = len(sidecar_messages)
    sidecar_last = _last_message_timestamp(sidecar_messages) or 0.0

    # Fast negative (pre-lock): if state.db is not ahead by either count or
    # timestamp, do not pay for the lock. This keeps normal reads cheap.
    if state_count <= sidecar_count and state_last <= sidecar_last:
        return False

    # ── Under-lock critical section ──────────────────────────────────────────
    # The merge + sidecar write must hold the per-session lock so a concurrent
    # worker/checkpoint save can neither (a) be clobbered by a stale full-record
    # write here, nor (b) revive the stream between our liveness check and our
    # write. Non-blocking acquire: if a caller already holds the lock (retry_last,
    # undo_last, cancel_stream, the streaming worker's own finalize), bail rather
    # than deadlock — a later read will retry the self-heal.
    lock = _get_session_agent_lock(sid)
    if not lock.acquire(blocking=False):
        logger.debug(
            "state.db newer-sidecar sync: lock contended, skipping for session %s", sid,
        )
        return False
    try:
        # Re-load the authoritative on-disk session under the lock so we both
        # validate against (and write back) the very latest sidecar — never a
        # snapshot captured before the lock that could clobber a newer write.
        try:
            locked = Session.load(sid)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync: locked reload failed for session %s",
                sid, exc_info=True,
            )
            return False
        if locked is None:
            return False

        # Re-check liveness conditions against the freshly-loaded state: the
        # stream may have rotated (compression), come back alive, terminated and
        # cleared its own pending state, or had its turn finalized while we
        # waited. Any of these means there is nothing stale to repair.
        locked_stream_id = getattr(locked, 'active_stream_id', None)
        if locked_stream_id != seen_stream_id:
            return False
        if not (locked_stream_id or getattr(locked, 'pending_user_message', None)):
            return False
        if locked_stream_id and locked_stream_id in _active_stream_ids():
            return False
        if locked_stream_id:
            _lstarted = getattr(locked, 'pending_started_at', None)
            if _lstarted:
                try:
                    _lage = time.time() - float(_lstarted)
                except (TypeError, ValueError):
                    _lage = float('inf')
                if _lage < _REPAIR_STALE_PENDING_GRACE_SECONDS:
                    return False

        locked_messages = list(getattr(locked, 'messages', None) or [])
        locked_count = len(locked_messages)

        state_messages = get_state_db_session_messages(
            sid,
            profile=getattr(locked, 'profile', None),
        )
        if not state_messages:
            return False
        merged_messages = reconciled_state_db_messages_for_session(
            locked,
            state_messages=state_messages,
        )
        # The reconciler is append-only: a genuine state.db advance (output the
        # lost stream never wrote back) shows up as MORE rows than the sidecar.
        # A merged length not greater than the sidecar means nothing new to
        # recover — leave the sidecar untouched rather than rewriting in place.
        if len(merged_messages) <= locked_count:
            return False
        merged_context = reconciled_state_db_messages_for_session(
            locked,
            prefer_context=True,
            state_messages=state_messages,
        )

        # Mutate + persist the freshly-loaded, locked object. Because we hold the
        # lock and reloaded under it, this save cannot clobber a concurrent
        # writer's newer record.
        locked.messages = merged_messages
        locked.context_messages = merged_context
        locked.active_stream_id = None
        locked.pending_user_message = None
        locked.pending_attachments = []
        locked.pending_started_at = None
        locked.pending_user_source = None
        try:
            locked.save(touch_updated_at=True)
        except Exception:
            logger.debug(
                "state.db newer-sidecar sync save failed for session %s",
                sid, exc_info=True,
            )
            return False

        # Durable write succeeded — reflect the reconciled state on the caller's
        # shared/cached object so the in-flight read returns the recovered data.
        session.messages = merged_messages
        session.context_messages = merged_context
        session.active_stream_id = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        logger.info(
            "Session %s: synced sidecar from newer state.db transcript (%d -> %d messages)",
            sid,
            locked_count,
            len(merged_messages),
        )
        return True
    finally:
        lock.release()



def _last_non_tool_role(messages) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        role = _message_role(message)
        if role and role != 'tool':
            return role
    return ''


def _last_non_tool_message(messages):
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        role = _message_role(message)
        if role and role != 'tool':
            return message
    return None


def _message_content_text(message) -> str:
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return ''.join(parts)
    return ''


def _inactive_cache_tail_needs_disk_check(cached) -> bool:
    if cached is None:
        return False
    if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
        return False
    return _last_non_tool_role(getattr(cached, 'messages', None) or []) == 'user'


def _cache_has_stale_unsaved_user_tail(cached, disk_session) -> bool:
    """Return True when an inactive cached session has an unsaved user tail.

    A completed turn is saved to the sidecar before the browser reloads it.  In
    rare compaction/reconnect paths the in-process cache can retain a recovered
    or optimistic user row after the saved assistant tail even though the row was
    never persisted.  If /api/session serves that cache entry, the visible
    transcript appears to end on the old prompt and the saved assistant answer
    looks missing until a fork/reload resets the cache.
    """
    if cached is None or disk_session is None:
        return False
    if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
        return False
    cached_messages = getattr(cached, 'messages', None) or []
    disk_messages = getattr(disk_session, 'messages', None) or []
    if _last_non_tool_role(cached_messages) != 'user':
        return False
    if _last_non_tool_role(disk_messages) != 'assistant':
        return False
    if len(cached_messages) < len(disk_messages):
        return True
    if len(cached_messages) == len(disk_messages):
        # Same-length divergence is still stale: a completed assistant turn can
        # be persisted through a sibling Session object while this inactive LRU
        # entry still ends on the optimistic/recovered user row.
        #
        # Keep this narrow: only evict when the shared prefix is the same and
        # the cached user tail is not newer than the persisted assistant.  A
        # genuine just-submitted user message can exist briefly before the
        # stream id is attached, and that must not be replaced by older disk
        # state.
        cached_tail = _last_non_tool_message(cached_messages)
        disk_tail = _last_non_tool_message(disk_messages)
        cached_prefix = [
            (_message_role(message), _message_content_text(message))
            for message in cached_messages[:-1]
        ]
        disk_prefix = [
            (_message_role(message), _message_content_text(message))
            for message in disk_messages[:-1]
        ]
        if cached_prefix != disk_prefix:
            return False
        cached_tail_ts = _message_timestamp(cached_tail)
        disk_tail_ts = _message_timestamp(disk_tail)
        if cached_tail_ts is not None and disk_tail_ts is not None and cached_tail_ts > disk_tail_ts:
            return False
        return True

    cached_tail = _last_non_tool_message(cached_messages)
    previous_disk_user = None
    for message in reversed(disk_messages):
        if _message_role(message) == 'user':
            previous_disk_user = message
            break
    if previous_disk_user is None:
        return False

    # Only drop tails that look like a duplicated optimistic/recovered user row.
    # A genuinely new concurrent user edit must stay in memory so stale-session
    # guards can report and preserve it.
    return _message_content_text(cached_tail) == _message_content_text(previous_disk_user)


def _anchor_scene_index_from_records(records) -> dict:
    """Build the compact anchor-scene fingerprint {scene_key: updated_at} (#5854).

    This is the freshness signal the sidebar-poll comparison needs — scene keys
    plus each scene's ``updated_at`` — WITHOUT the 250-480KB bodies. Persisted in
    the metadata prefix (before ``messages``) so ``load_metadata_only`` and
    ``_persisted_session_meta_prefix`` stay cheap. Mirrors exactly what
    ``_anchor_scene_record_keys`` / ``_anchor_scene_records_updated_at`` read off
    the full records, so the fingerprint comparison is behavior-identical.
    """
    if not isinstance(records, dict):
        return {}
    index = {}
    for key, value in records.items():
        if not key or not isinstance(value, dict):
            continue
        try:
            updated_at = float(value.get('updated_at') or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        index[str(key)] = updated_at
    return index


def _disk_scene_fingerprint(disk_meta_prefix: dict):
    """Resolve the (scene_keys, max_updated_at) freshness signal from a parsed
    metadata prefix dict, preferring the modern ``anchor_scene_index`` and
    falling back to the full ``anchor_activity_scenes`` bodies for legacy files.

    Returns ``None`` when the prefix carries NEITHER field, so callers can tell
    "no scenes" (empty dict/index present) apart from "couldn't determine"
    (legacy file whose scenes serialize after ``messages`` and so aren't in the
    prefix) and fall through to the full metadata load instead of assuming zero.
    """
    if not isinstance(disk_meta_prefix, dict):
        return None
    if 'anchor_scene_index' in disk_meta_prefix:
        raw = disk_meta_prefix.get('anchor_scene_index')
        raw = raw if isinstance(raw, dict) else {}
        keys = {str(k) for k in raw}
        latest = 0.0
        for v in raw.values():
            try:
                fv = float(v or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    if 'anchor_activity_scenes' in disk_meta_prefix:
        records = disk_meta_prefix.get('anchor_activity_scenes')
        records = records if isinstance(records, dict) else {}
        keys = {str(k) for k, val in records.items() if k and isinstance(val, dict)}
        latest = 0.0
        for val in records.values():
            if not isinstance(val, dict):
                continue
            try:
                fv = float(val.get('updated_at') or 0)
            except (TypeError, ValueError):
                fv = 0.0
            if fv > latest:
                latest = fv
        return keys, latest
    return None


def _sidecar_stat_signature(path):
    """Stat signature for a sidecar path, or None if it can't be stat'd.

    Any edit (atomic-rename or in-place) changes at least one component, so a
    cached entry keyed by this signature is auto-invalidated on the next write.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))),
            int(st.st_size), int(getattr(st, 'st_ctime_ns', int(st.st_ctime * 1_000_000_000))))


def _legacy_sidecar_facts_get(sid):
    """Return cached authoritative facts for a LEGACY sidecar, or None (#5854).

    Only returns a hit when the file's current stat signature matches the cached
    one, so a stale entry can never be served after an edit.
    """
    if not is_safe_session_id(sid):
        return None
    sig = _sidecar_stat_signature(SESSION_DIR / f'{sid}.json')
    if sig is None:
        return None
    with _LEGACY_SIDECAR_FACTS_LOCK:
        hit = _LEGACY_SIDECAR_FACTS.get(sig)
        if hit is not None:
            _LEGACY_SIDECAR_FACTS.move_to_end(sig)
            return dict(hit)
    return None


def _legacy_sidecar_facts_put(sid, message_count, scene_index, *, expected_sig):
    """Cache authoritative facts for a legacy sidecar keyed by its stat signature.

    #5854 TOCTOU guard: ``expected_sig`` (MANDATORY) is the signature captured
    BEFORE the caller parsed the file. The facts were derived from that snapshot,
    so we only cache when the file's CURRENT signature still equals it —
    otherwise the file was atomically replaced during the parse and these facts
    describe the old content; caching them under the new signature would serve
    stale data. Pass ``None`` explicitly only if the caller genuinely has no
    snapshot (then this is a no-op, refusing to cache unverified facts).
    """
    if not is_safe_session_id(sid):
        return
    if expected_sig is None:
        return
    sig = _sidecar_stat_signature(SESSION_DIR / f'{sid}.json')
    if sig is None:
        return
    if sig != expected_sig:
        # File changed under us during the parse — do not cache stale facts.
        return
    entry = {"message_count": message_count,
             "scene_index": dict(scene_index) if isinstance(scene_index, dict) else {}}
    with _LEGACY_SIDECAR_FACTS_LOCK:
        _LEGACY_SIDECAR_FACTS[sig] = entry
        _LEGACY_SIDECAR_FACTS.move_to_end(sig)
        while len(_LEGACY_SIDECAR_FACTS) > _LEGACY_SIDECAR_FACTS_MAX:
            _LEGACY_SIDECAR_FACTS.popitem(last=False)


def _anchor_scene_record_keys(session) -> set[str]:
    records = getattr(session, 'anchor_activity_scenes', None)
    if not isinstance(records, dict):
        return set()
    return {str(key) for key, value in records.items() if key and isinstance(value, dict)}


def _anchor_scene_records_updated_at(session) -> float:
    records = getattr(session, 'anchor_activity_scenes', None)
    if not isinstance(records, dict):
        return 0.0
    latest = 0.0
    for record in records.values():
        if not isinstance(record, dict):
            continue
        try:
            updated_at = float(record.get('updated_at') or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at > latest:
            latest = updated_at
    return latest


def _session_scene_keys(session) -> set[str]:
    """Scene keys for a session object, fingerprint-aware (#5854).

    The ``_anchor_scene_index`` fingerprint is authoritative ONLY on a
    metadata-only stub (whose full ``anchor_activity_scenes`` are not
    materialized because they serialize after ``messages``). A FULLY-LOADED
    session carries a load-time fingerprint that goes stale the moment its
    records are mutated in place (the scene-persist path does exactly that
    without refreshing it), so for a full session we MUST read the real records.
    """
    if getattr(session, '_loaded_metadata_only', False):
        index = getattr(session, '_anchor_scene_index', None)
        if isinstance(index, dict):
            return {str(k) for k in index}
    return _anchor_scene_record_keys(session)


def _session_scene_updated_at(session) -> float:
    """Max scene ``updated_at`` for a session object, fingerprint-aware (#5854).

    Fingerprint is authoritative only on a metadata-only stub; a fully-loaded
    session always compares its real records (see ``_session_scene_keys``).
    """
    if getattr(session, '_loaded_metadata_only', False):
        index = getattr(session, '_anchor_scene_index', None)
        if isinstance(index, dict):
            latest = 0.0
            for v in index.values():
                try:
                    fv = float(v or 0)
                except (TypeError, ValueError):
                    fv = 0.0
                if fv > latest:
                    latest = fv
            return latest
    return _anchor_scene_records_updated_at(session)


def _cached_session_lags_disk(cached) -> bool:
    """Return True when a cached full session is older than its sidecar.

    Active/reconnect paths can update the persisted sidecar through another
    Session object while the LRU cache still holds an older object for the same
    id. Serving the cache then makes recent assistant results disappear from
    GET /api/session even though disk and _index.json are correct. Compare only
    cheap metadata here; full reload happens only if disk is strictly ahead.

    perf(webui/session-load-latency) cheap-first ordering: the function used to
    call Session.load_metadata_only(sid) on every cache hit, which parses the
    full sidecar JSON (~15-20ms even for 1.3MB sidecars on Celeron+ eMMC).
    For draft auto-saves that hit get_session() on every keystroke debounce
    (every ~400ms while typing), that 15-20ms multiplied out to ~75% of the
    request's wall time on the Chromebook. We now do a single fast check
    first: read only the JSON metadata prefix to compare message counts.
    The full Session.load_metadata_only() and its anchor-scene comparisons
    only run when the count check is inconclusive or when disk appears to be
    ahead of cache.
    """
    if cached is None:
        return False
    sid = getattr(cached, 'session_id', None)
    if not sid:
        return False
    cached_count = len(getattr(cached, 'messages', None) or [])
    # Fast path: prefix read of just the metadata header.
    disk_count = _persisted_message_count(sid)
    if disk_count is not None:
        if disk_count > cached_count:
            return True
        # Disk is at most as far as cache. Even when counts match, anchor scene
        # records can advance independently (api/routes.py saves a session
        # with `s.save(touch_updated_at=False, skip_index=True)` after editing
        # only the scene dict; message_count is len(messages) so it stays the
        # same). Greptile flagged this in PR review. Cheaply check the disk's
        # scene records from the same prefix we already read.
        cached_scenes = getattr(cached, 'anchor_activity_scenes', None) or {}
        if not isinstance(cached_scenes, dict):
            cached_scenes = {}
        # Track whether the cheap scene check was inconclusive — when it is,
        # we must fall through to the full metadata comparison instead of
        # returning False for inactive sessions with matching counts.
        _scene_check_inconclusive = False
        if cached_scenes:
            disk_meta_quick = _persisted_session_meta_prefix(sid)
            # #5854: modern prefixes carry only the anchor_scene_index
            # fingerprint (keys + updated_at), not the full scene bodies (those
            # now serialize after `messages`). _disk_scene_fingerprint resolves
            # the (keys, max_updated_at) signal from either the modern
            # fingerprint or a legacy file's inline bodies, and returns None
            # when the prefix carries NEITHER (a legacy large-scene file whose
            # scenes fall after the prefix) so we fall through rather than
            # assuming "no scenes".
            disk_fp = _disk_scene_fingerprint(disk_meta_quick) if disk_meta_quick is not None else None
            if disk_fp is not None:
                disk_keys, disk_latest = disk_fp
                if disk_keys:
                    # Directional: only reload when disk is strictly ahead
                    # of cache. Mirror master's subset comparison — cache
                    # that is ahead of disk must NOT force a reload, or
                    # un-persisted scene data is silently dropped.
                    cached_keys = _anchor_scene_record_keys(cached)
                    if not disk_keys.issubset(cached_keys):
                        return True
                    # Same key set (or disk is subset): check the latest
                    # updated_at timestamp.
                    if disk_latest > _anchor_scene_records_updated_at(cached):
                        return True
                # disk has no scenes, cache does -> cache is ahead; keep it.
            else:
                # Can't cheaply verify scene freshness from disk (prefix carried
                # neither fingerprint nor inline scenes, or the prefix read
                # failed while _persisted_message_count succeeded via index
                # fallback). Fall through to the full metadata load so we don't
                # serve stale scenes on the next equal-count inactive path.
                # Greptile P1.
                _scene_check_inconclusive = True
        else:
            # Cached session has no scene records. Check if disk has gained
            # the first scene record — without this the fast-path would miss
            # a newly persisted scene and return the stale cache. Greptile P1.
            disk_meta_quick = _persisted_session_meta_prefix(sid)
            disk_fp = _disk_scene_fingerprint(disk_meta_quick) if disk_meta_quick is not None else None
            if disk_fp is not None:
                disk_keys, _disk_latest = disk_fp
                if disk_keys:
                    return True
            else:
                # Prefix read failed / carried no scene signal (may still
                # succeed via index fallback for message count). Mark
                # inconclusive so we fall through to the full metadata
                # comparison instead of returning False with stale cache.
                # Greptile P1 (discussion_r3548650345).
                _scene_check_inconclusive = True
        if getattr(cached, 'active_stream_id', None) or getattr(cached, 'pending_user_message', None):
            # Active session: messages may be in flight; fall through to the
            # full check to be safe.
            pass
        elif _scene_check_inconclusive:
            # Could not cheaply verify scene freshness from disk; fall through
            # to the full metadata comparison rather than returning False
            # (which would serve a potentially stale cache). Greptile P1.
            pass
        else:
            # Inactive session, count matches, scene records match — cache is
            # at parity with disk.
            return False
    try:
        disk_meta = Session.load_metadata_only(sid)
    except Exception:
        return False
    if disk_meta is None:
        return False
    if disk_count is None:
        disk_count = _parse_nonnegative_int(getattr(disk_meta, '_metadata_message_count', None))
        if disk_count is None:
            disk_count = _lookup_index_message_count(sid)
        if disk_count is not None and disk_count > cached_count:
            return True
    if not getattr(cached, 'active_stream_id', None) and not getattr(cached, 'pending_user_message', None):
        # #5854: disk_meta is a metadata-only stub whose scenes now live after
        # `messages` and so are NOT materialized on it — read its scene freshness
        # from the _anchor_scene_index fingerprint. `cached` is ALWAYS a full
        # in-memory session (get_session never caches metadata-only stubs), and
        # its records are mutated in place by the scene-persist path without
        # refreshing the load-time fingerprint — so the cached side must read the
        # REAL records (master parity), never the fingerprint, or a parity cache
        # looks disk-behind after a scene write and forces a spurious full reload.
        #
        # A LEGACY stub (no anchor_scene_index fingerprint AND scenes not in the
        # prefix) carries no scene signal at all — comparing it blind would miss
        # a genuine disk-ahead scene change (stale worklog served). Full-load the
        # sidecar once to compare real scene records; the next save() rewrites
        # the modern layout so this legacy full-load doesn't recur.
        if (
            getattr(disk_meta, '_loaded_metadata_only', False)
            and getattr(disk_meta, '_anchor_scene_index', None) is None
        ):
            try:
                disk_full = Session.load(sid)
            except Exception:
                disk_full = None
            if disk_full is not None:
                disk_meta = disk_full
        cached_scene_keys = _anchor_scene_record_keys(cached)
        disk_scene_keys = _session_scene_keys(disk_meta)
        if disk_scene_keys and not disk_scene_keys.issubset(cached_scene_keys):
            return True
        if (
            disk_scene_keys
            and _session_scene_updated_at(disk_meta) > _anchor_scene_records_updated_at(cached)
        ):
            return True
    return False


def _persisted_message_count(sid) -> int | None:
    """Return the on-disk message count for *sid* without a full load (#4765).

    Reads only the sidecar metadata prefix (and falls back to the sidebar
    ``_index.json`` count) so the eviction safety check stays cheap even while
    the global ``LOCK`` is held. Returns ``None`` when the sidecar is missing or
    its count cannot be determined — callers treat that as "do not evict",
    because we must never drop an in-memory session we cannot prove is on disk.
    """
    if not is_safe_session_id(sid):
        return None
    p = SESSION_DIR / f'{sid}.json'
    if not p.exists():
        return None
    try:
        prefix = _read_metadata_json_prefix(p)
        if prefix:
            parsed = json.loads(prefix)
            count = _parse_nonnegative_int(parsed.get('message_count'))
            if count is not None:
                return count
            # #5854: a legacy sidecar (scenes-before-count) yields a prefix with
            # no message_count and no modern anchor_scene_index. The _index.json
            # count can lag behind external appends, so trusting it here risks
            # under-reporting (and dropping an unsaved tail on cache-replace).
            # Consult the authoritative legacy-facts cache (populated by a full
            # load) before giving up; only return None (→ caller full-loads) on
            # a miss. This keeps clean legacy sessions LRU-evictable without
            # trusting a stale index. A MODERN prefix always carries
            # message_count, so it returns above.
            if 'anchor_scene_index' not in parsed:
                _facts = _legacy_sidecar_facts_get(sid)
                if _facts is not None:
                    cached_count = _parse_nonnegative_int(_facts.get('message_count'))
                    if cached_count is not None:
                        return cached_count
                # Cache miss (never full-loaded yet, or the facts LRU evicted
                # this entry). Returning None here would make the session
                # non-evictable (the eviction check treats an unknown disk count
                # as "do not evict"), which can let new_session() evict its own
                # unsaved session and 404 the first send. Full-parse to get the
                # authoritative count (Session.load re-caches the facts for
                # legacy files under a TOCTOU-guarded signature). Re-verify the
                # stat signature is stable across the parse so we never return a
                # count from a file that was atomically replaced mid-read; retry
                # boundedly on mismatch, then fall through to None. Bounded
                # overall: the next save() rewrites the modern layout.
                for _attempt in range(3):
                    sig_before = _sidecar_stat_signature(p)
                    try:
                        _full = Session.load(sid)
                    except Exception:
                        _full = None
                    sig_after = _sidecar_stat_signature(p)
                    if _full is None:
                        return None
                    if sig_before is not None and sig_before == sig_after:
                        return len(getattr(_full, 'messages', None) or [])
                    # File changed during the parse — the count is uncertain;
                    # retry with a fresh snapshot.
                return None
    except Exception:
        # Fall through to the index-based fallback below.
        pass
    return _parse_nonnegative_int(_lookup_index_message_count(sid))


def _persisted_session_meta_prefix(sid) -> dict | None:
    """Return the parsed metadata prefix dict for *sid*, or None on error.

    Used by ``_cached_session_lags_disk`` to compare additional fields (e.g.
    anchor scene records) cheaply against the cached in-memory session without
    paying for a full ``Session.load_metadata_only`` parse. Returns the same
    shape as ``_persisted_message_count`` callers would expect — a dict that
    only contains the metadata-prefix fields, NOT ``messages`` / ``tool_calls``.
    """
    if not is_safe_session_id(sid):
        return None
    p = SESSION_DIR / f'{sid}.json'
    if not p.exists():
        return None
    try:
        prefix = _read_metadata_json_prefix(p)
        if not prefix:
            return None
        return json.loads(prefix)
    except Exception:
        return None


def _session_sidecar_exists(sid) -> bool | None:
    """Return whether *sid*'s sidecar file exists on disk.

    True  = the sidecar is confirmed present.
    False = the sidecar is confirmed absent (a truly never-persisted session).
    None  = existence is indeterminate (unsafe id, or the stat raised).

    ``_session_is_evictable`` uses this to distinguish a genuinely
    never-persisted empty shell (safe to grace-evict once abandoned) from a
    session whose count merely could not be read this pass (stay resident).
    """
    if not is_safe_session_id(sid):
        return None
    try:
        return (SESSION_DIR / f'{sid}.json').exists()
    except OSError:
        return None


# Grace window (seconds) during which a never-persisted, empty, draftless session
# shell is protected from LRU eviction. new_session() defers the first disk write
# only in the SESSIONS cache; evicting it there permanently 404s the chat (#6083).
# After this window a still-empty, still-draftless, never-saved shell is treated as
# abandoned and becomes evictable again, so these shells can't grow unbounded past
# the cache cap. Generous (30 min) so a user composing slowly is never dropped;
# a real draft persists to disk on the first keystroke and is reloadable anyway.
_UNSAVED_SHELL_GRACE_S = 1800


def _session_is_evictable(s) -> bool:
    """Return True only when *s* can be safely dropped from the LRU (#4765).

    Eviction must never lose data or interrupt a live turn. A session is
    evictable ONLY when ALL of the following hold:

      * It is not streaming (no ``active_stream_id``).
      * It has no in-flight/queued turn (no ``pending_user_message`` and no
        ``pending_started_at``).
      * Its full state is already persisted to the JSON sidecar, proven by the
        on-disk ``message_count`` being at least the in-memory message count.
        A metadata-only stub is inherently backed by disk, so it is evictable.

    The persistence requirement holds even for a session with ZERO messages,
    but only for a bounded grace window. ``new_session()`` deliberately does not
    touch disk until the first message (#1171), so between "New Conversation" and
    the first send this cache is the session's ONLY copy. Evicting it there
    discards an un-recreatable shell: ``get_session()`` has no recreate path and
    raises ``KeyError``, so the very next ``/api/session/draft`` or
    ``/api/chat/start`` 404s and the session can never be started (#6083). We
    therefore protect a never-persisted empty shell while it is fresh (the user
    just opened it and is composing) OR while it has an active composer draft.

    A never-persisted empty shell that is BOTH stale (older than
    ``_UNSAVED_SHELL_GRACE_S``) AND draftless is treated as an abandoned
    "New Conversation" tab the user opened and walked away from — it becomes
    evictable again so ``sessions_cache_max`` still bounds these shells and they
    cannot accumulate without limit (a slow leak / OOM on installs that open many
    empty chats). Anything the user is actually composing persists a draft via
    ``s.save()`` on the first keystroke, so it is disk-backed and reloadable well
    before the grace window expires; the window only covers the empty-and-untouched
    gap right after "New Conversation".

    Anything else we cannot positively prove is safe stays resident. Using
    slightly more RAM for a session we are unsure about is strictly better than
    evicting an active or unsaved session (task safety invariant: a half-done
    memory fix that loses a session is worse than none).
    """
    if s is None:
        return True  # nothing to protect; let the caller drop it
    if getattr(s, 'active_stream_id', None):
        return False
    if getattr(s, 'pending_user_message', None):
        return False
    if getattr(s, 'pending_started_at', None):
        return False
    sid = getattr(s, 'session_id', None)
    if not sid:
        return False
    # Metadata-only stubs never carry unsaved messages (messages=[] by design),
    # so they are always disk-backed and safe to drop.
    if getattr(s, '_loaded_metadata_only', False):
        return True
    in_memory_count = len(getattr(s, 'messages', None) or [])
    disk_count = _persisted_message_count(sid)
    if disk_count is None:
        # disk_count is None for TWO distinct reasons: the sidecar is confirmed
        # absent (truly never persisted → this cache is the only copy), OR the
        # sidecar exists but its count could not be read this pass (transient I/O,
        # mid-write). Only the CONFIRMED-ABSENT case is eligible for grace-based
        # shell eviction; an indeterminate existing-sidecar session stays resident
        # (conservative — never grace-evict something we cannot prove is gone).
        if in_memory_count > 0:
            return False  # holds unsaved messages → never drop
        composer_draft = getattr(s, 'composer_draft', None)
        if composer_draft:
            return False  # user is composing (draft present) → keep resident
        if _session_sidecar_exists(sid) is not False:
            # Sidecar present or existence indeterminate → not a never-persisted
            # shell; do not enter the abandoned-shell grace path.
            return False
        # Confirmed never-persisted empty draftless shell. Protect it while fresh
        # (the compose window right after "New Conversation"); once stale it is an
        # abandoned tab and becomes evictable so these shells cannot accumulate
        # unbounded past the cache cap (#6083 follow-up).
        created_at = getattr(s, 'created_at', None)
        if isinstance(created_at, (int, float)):
            if (time.time() - created_at) <= _UNSAVED_SHELL_GRACE_S:
                return False  # fresh empty shell → protect the compose window
            return True  # stale, empty, draftless, never-saved → abandoned, evictable
        # No usable created_at timestamp → be conservative, keep it resident.
        return False
    if in_memory_count == 0:
        # Persisted and empty → trivially clean, nothing to lose.
        return True
    return disk_count >= in_memory_count


def _evict_sessions_over_cap(cap: int | None = None) -> int:
    """Evict clean, persisted, non-active sessions until len(SESSIONS) <= cap.

    Replaces the previous blind ``SESSIONS.popitem(last=False)`` loops (#4765).
    The blind loops could evict the least-recently-used entry even if it was
    actively streaming or held unsaved messages, risking a dropped turn or lost
    conversation. This walks the LRU from oldest to newest and removes only
    entries that ``_session_is_evictable()`` proves are safe. An evicted session
    transparently lazily reloads from its sidecar on the next ``get_session()``.

    CALLER CONTRACT: the global ``LOCK`` MUST already be held (every call site
    mutates ``SESSIONS`` under ``LOCK``). This function never acquires ``LOCK``
    or any stream lock itself, so it cannot introduce a lock-ordering deadlock.

    Returns the number of sessions evicted. If every over-cap candidate is
    active/unsaved, the cache may temporarily exceed ``cap`` — that is the
    intended safe behavior (never lose an active/unsaved session).
    """
    if cap is None:
        try:
            cap = _cfg.get_sessions_cache_max()
        except Exception:
            cap = SESSIONS_MAX
    if not isinstance(cap, int) or cap < 1:
        cap = SESSIONS_MAX if isinstance(SESSIONS_MAX, int) and SESSIONS_MAX >= 1 else 1
    evicted = 0
    # Iterate over a snapshot of ids in LRU order (oldest first). We stop as
    # soon as we are at/below the cap. Skipping a non-evictable oldest entry and
    # moving on lets us reclaim a slightly-newer clean entry instead of blocking
    # eviction entirely behind one pinned active session.
    for sid in list(SESSIONS.keys()):
        if len(SESSIONS) <= cap:
            break
        candidate = SESSIONS.get(sid)
        if _session_is_evictable(candidate):
            SESSIONS.pop(sid, None)
            evicted += 1
    if len(SESSIONS) > cap:
        logger.debug(
            "SESSIONS cache above cap (%d > %d) after eviction pass: remaining "
            "entries are active or unsaved and were preserved (#4765)",
            len(SESSIONS), cap,
        )
    return evicted


def get_session_for_scan(sid):
    """Read a session for a one-pass scan without disturbing the LRU.

    The scan uses the full canonical resolver so stale sidecars, pending journal
    recovery, and newer state.db transcript rows remain searchable. It only
    differs from ``get_session`` in cache policy: no hit promotion, no cold-load
    insertion, and therefore no scan-triggered eviction.
    """
    try:
        return _resolve_session(sid, promote_cache=False, cache_on_miss=False)
    except Exception:
        logger.debug("scan load failed for session %s", sid, exc_info=True)
        return None


def _resolve_session(sid, metadata_only=False, *, promote_cache=True, cache_on_miss=True):
    """Resolve a session through the canonical freshness/recovery path.

    ``get_session_for_scan`` shares this resolver with normal reads so that
    content search sees the same disk freshness, journal-retry, and state.db
    recovery behavior. Its policy merely suppresses LRU promotion and cold-load
    caching; reconciliation still updates an already-resident entry when normal
    resolution replaces stale contents.
    """
    with LOCK:
        cached = SESSIONS.get(sid)
        if cached is not None and promote_cache:
            SESSIONS.move_to_end(sid)  # LRU: mark as recently used
    if cached is not None:
        # Defensive cache ownership check: compression/continuation and recovery
        # paths can temporarily juggle Session objects across lineage ids.  A
        # stale object stored under the wrong key makes GET /api/session return
        # a different transcript than the requested sid, which looks exactly
        # like a disappeared session.  Evict instead of trusting the LRU.
        if str(getattr(cached, 'session_id', '') or '') != str(sid):
            logger.warning(
                "evicting mismatched cached session: requested %s but cached object is %s",
                sid,
                getattr(cached, 'session_id', None),
            )
            with LOCK:
                if SESSIONS.get(sid) is cached:
                    SESSIONS.pop(sid, None)
            cached = None
    if cached is not None:
        if not metadata_only and _cached_session_lags_disk(cached):
            try:
                disk_session = Session.load(sid)
                with LOCK:
                    SESSIONS[sid] = disk_session
                    if promote_cache:
                        SESSIONS.move_to_end(sid)
                cached = disk_session
            except Exception:
                logger.debug(
                    "cached session disk-freshness check failed for session %s", sid, exc_info=True,
                )
        if not metadata_only and _inactive_cache_tail_needs_disk_check(cached):
            try:
                disk_session = Session.load(sid)
                if _cache_has_stale_unsaved_user_tail(cached, disk_session):
                    with LOCK:
                        SESSIONS[sid] = disk_session
                        if promote_cache:
                            SESSIONS.move_to_end(sid)
                    cached = disk_session
            except Exception:
                logger.debug(
                    "stale cached user-tail check failed for session %s", sid, exc_info=True,
                )
        if not metadata_only and _session_has_pending_journal_retry(cached):
            try:
                _try_retry_journal_recovery_in_place(cached)
            except Exception:
                logger.debug(
                    "lazy journal-retry failed on cache hit for session %s", sid, exc_info=True,
                )
        if not metadata_only:
            try:
                _sync_sidecar_from_state_db_if_newer(cached)
            except Exception:
                logger.debug(
                    "state.db newer-sidecar sync failed on cache hit for session %s", sid, exc_info=True,
                )
        return cached
    if metadata_only:
        s = Session.load_metadata_only(sid)
        if s:
            return s
    else:
        s = Session.load(sid)
    if s:
        if cache_on_miss:
            with LOCK:
                SESSIONS[sid] = s
                if promote_cache:
                    SESSIONS.move_to_end(sid)
                _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
        if not metadata_only:
            try:
                synced_from_state = _sync_sidecar_from_state_db_if_newer(s)
                repaired = False if synced_from_state else _repair_stale_pending(s)
                # If the stale-pending repair did not fire but the session
                # already carries a pending-journal-retry marker (e.g. set on
                # a previous repair pass), give the lazy-retry path one
                # chance to self-heal on this read.
                if not repaired and not synced_from_state and _session_has_pending_journal_retry(s):
                    try:
                        _try_retry_journal_recovery_in_place(s)
                    except Exception:
                        logger.debug(
                            "lazy journal-retry failed on cold load for session %s", sid, exc_info=True,
                        )
                # If repair had to bail because the per-session lock was held,
                # do not pin the still-stale sidecar in the LRU cache forever.
                # Leaving it cached would prevent future get_session() calls from
                # re-entering the cache-miss repair path after the lock holder exits.
                if cache_on_miss and not repaired and (len(s.messages) == 0
                        and s.pending_user_message
                        and s.active_stream_id
                        and s.active_stream_id not in _active_stream_ids()):
                    with LOCK:
                        if SESSIONS.get(sid) is s:
                            SESSIONS.pop(sid, None)
            except Exception:
                pass  # repair is best-effort
        return s
    raise KeyError(sid)


def get_session(sid, metadata_only=False):
    """Load a session, optionally with metadata only (skipping messages)."""
    return _resolve_session(sid, metadata_only=metadata_only)


_COMPRESSION_RECOVERY_PROFILE_UNSET = object()


def _compression_recovery_child_matches(
    session,
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
) -> bool:
    if source_profile is not _COMPRESSION_RECOVERY_PROFILE_UNSET:
        try:
            from api.profiles import _profiles_match
        except (ImportError, AttributeError):
            logger.debug("Failed to profile-check compression recovery session", exc_info=True)
            return False
        if not _profiles_match(getattr(session, "profile", None), source_profile):
            return False
    return (
        str(getattr(session, "compression_recovery_source_session_id", "") or "").strip() == source_session_id
        and str(getattr(session, "compression_recovery_action", "") or "").strip() == action
    )


def find_compression_recovery_session(
    source_session_id: str,
    action: str,
    source_profile=_COMPRESSION_RECOVERY_PROFILE_UNSET,
):
    """Return an existing focused recovery child for ``source_session_id``.

    The recovery-start endpoint is a retryable UI action. A persisted marker on
    the child session makes double-clicks, repeated calls, and cache reloads
    converge on the same continuation instead of creating duplicate siblings.
    """

    source_sid = str(source_session_id or "").strip()
    recovery_action = str(action or "").strip()
    if not source_sid or not recovery_action:
        return None

    matches = []
    seen_ids: set[str] = set()
    try:
        with LOCK:
            memory_sessions = list(SESSIONS.values())
        for session in memory_sessions:
            sid = str(getattr(session, "session_id", "") or "").strip()
            if sid:
                seen_ids.add(sid)
            if _compression_recovery_child_matches(session, source_sid, recovery_action, source_profile):
                matches.append(session)
    except Exception:
        logger.debug("Failed to scan cached compression recovery sessions", exc_info=True)

    try:
        persisted_ids = _persisted_session_ids_snapshot()
    except Exception:
        persisted_ids = frozenset()
    for sid in persisted_ids:
        if sid in seen_ids:
            continue
        try:
            meta = Session.load_metadata_only(sid)
        except Exception:
            logger.debug("Failed to inspect compression recovery session %s", sid, exc_info=True)
            continue
        if not meta or not _compression_recovery_child_matches(meta, source_sid, recovery_action, source_profile):
            continue
        try:
            matches.append(get_session(sid))
        except Exception:
            matches.append(meta)

    if not matches:
        return None

    def _sort_key(session):
        try:
            created_at = float(getattr(session, "created_at", 0) or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        try:
            updated_at = float(getattr(session, "updated_at", 0) or 0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return (created_at, updated_at, str(getattr(session, "session_id", "") or ""))

    return sorted(matches, key=_sort_key)[0]


def _profile_default_model_state(profile=None):
    """Return the default model/provider configured for *profile*."""
    default_model = ""
    default_provider = None
    try:
        from api.profiles import get_hermes_home_for_profile
        config_path = Path(get_hermes_home_for_profile(profile)) / "config.yaml"
        config_data = _cfg._load_yaml_config_file(config_path)
    except Exception:
        config_data = {}

    model_cfg = config_data.get("model", {}) if isinstance(config_data, dict) else {}
    if isinstance(model_cfg, str):
        default_model = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        default_model = str(model_cfg.get("default") or "").strip()
        default_provider = str(model_cfg.get("provider") or "").strip() or None

    return default_model or get_effective_default_model(), default_provider


def new_session(workspace=None, model=None, profile=None, model_provider=None, project_id=None, worktree_info=None, enabled_toolsets=None):
    """Create a new in-memory session.

    The session lives in the SESSIONS dict only — no disk write happens until
    the first message is appended (#1171 follow-up).  This avoids the
    "ghost Untitled session on disk" pile-up that occurred when users clicked
    New Conversation, reloaded the page, or completed onboarding without ever
    sending a message.  Subsequent code paths that populate state immediately
    (btw / background agent at api/routes.py) call ``s.save()`` themselves
    after setting title/messages, and ``_handle_chat_start`` saves the
    session as soon as the user actually sends a message — both are the
    natural first-write moments for a real session.

    Crash-safety: if the process exits between session creation and first
    message, the session is lost.  Since it had no messages, there is
    nothing to lose.  Worktree-backed sessions are the exception: they are
    saved immediately because creating the session also creates real
    filesystem state that must remain discoverable after restart.

    *profile* — when supplied by the caller (e.g. from the request body sent
    by the active browser tab), it is used directly so that concurrent clients
    on different profiles don't fight over a shared process-global.  If not
    supplied, we fall back to the process-level active profile (the pre-#798
    behaviour, preserved for calls that originate outside a request context).
    """
    if profile is None:
        # Fallback: read process-level global (single-client or startup path)
        try:
            from api.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except ImportError:
            profile = None
    if model:
        effective_model = model
        effective_model_provider = model_provider
    else:
        effective_model, effective_model_provider = _profile_default_model_state(profile)
        if model_provider:
            effective_model_provider = model_provider

    wt = worktree_info if isinstance(worktree_info, dict) else None
    workspace_path = (wt.get('path') if wt and wt.get('path') else workspace) if wt else workspace
    s = Session(
        workspace=workspace_path or get_last_workspace(),
        model=effective_model,
        model_provider=effective_model_provider,
        profile=profile,
        project_id=project_id,
        personality=None,
        worktree_path=wt.get('path') if wt else None,
        worktree_branch=wt.get('branch') if wt else None,
        worktree_repo_root=wt.get('repo_root') if wt else None,
        worktree_created_at=wt.get('created_at') if wt else None,
        enabled_toolsets=enabled_toolsets,
    )
    # #4985: defensive — auto-generated uuids don't collide with the
    # tombstone, but if a future caller ever passes an explicit id that
    # was previously pruned, clear the entry so the new session isn't
    # shadowed on the next poll. Wrapped because a tombstone failure
    # must never block new-session creation.
    try:
        _clear_webui_zero_message_orphan_tombstone(s.session_id)
        _clear_webui_deleted_session_tombstone(s.session_id)
    except Exception:
        logger.debug(
            "Failed to clear webui tombstone for %s",
            s.session_id,
            exc_info=True,
        )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        _evict_sessions_over_cap()  # #4765: safe LRU eviction (never active/unsaved)
    if wt:
        s.save()
    return s

def _hide_from_default_sidebar(session: dict, *, show_cron: bool = False, show_webhook: bool = False) -> bool:
    """Return True for internal/background sessions hidden from the default list."""
    sid = str(session.get('session_id') or '')
    source = (
        session.get('source_tag')
        or session.get('source')
        or session.get('raw_source')
        or session.get('session_source')
    )
    if not show_cron and (source == 'cron' or sid.startswith('cron_')):
        return True
    if not show_webhook and source == 'webhook':
        return True
    if bool(session.get('pre_compression_snapshot')):
        return not bool(session.get('_show_pre_compression_snapshot'))
    return False


def _sidebar_message_count(session: dict) -> int:
    for key in ('message_count', 'actual_message_count'):
        try:
            value = int(session.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _sidebar_lineage_root_id(session: dict, sessions_by_id: dict[str, dict]) -> str:
    sid = str(session.get('session_id') or '')
    explicit = str(session.get('_lineage_root_id') or '').strip()
    if explicit:
        return explicit
    relationship_type = str(session.get('relationship_type') or '').strip().lower()
    if relationship_type == 'child_session':
        return sid
    root = sid
    parent = session.get('parent_session_id')
    source = str(session.get('session_source') or '').strip().lower()
    seen = {sid}
    if source == 'fork':
        return root
    while parent and parent not in seen and parent in sessions_by_id:
        root = str(parent)
        seen.add(root)
        parent = sessions_by_id.get(root, {}).get('parent_session_id')
    return root


def _has_live_sidebar_state(session: dict) -> bool:
    return bool(
        session.get('active_stream_id')
        or session.get('has_pending_user_message')
        or session.get('pending_user_message')
    )


def _is_intentionally_background_sidebar_session(session: dict) -> bool:
    sid = str(session.get('session_id') or '')
    source = (
        session.get('source_tag')
        or session.get('source')
        or session.get('raw_source')
        or session.get('session_source')
    )
    return source in {'cron', 'webhook'} or sid.startswith('cron_')


def _include_project_hidden_background_sidebar_sessions(
    candidates: list[dict],
    visible: list[dict],
) -> list[dict]:
    """Keep project-assigned background sessions addressable by project chips.

    Cron and webhook sessions stay hidden from the default sidebar, but if they
    have a project assignment they must still be present in the client cache so
    their dedicated project chips can reveal them (#3019).
    """
    visible_ids = {
        str(session.get('session_id'))
        for session in visible
        if session.get('session_id')
    }
    out = list(visible)
    for session in candidates:
        sid = str(session.get('session_id') or '')
        if not sid or sid in visible_ids:
            continue
        if not _is_intentionally_background_sidebar_session(session):
            continue
        if not session.get('project_id'):
            continue
        if _sidebar_message_count(session) <= 0:
            continue
        row = dict(session)
        row['default_hidden'] = True
        out.append(row)
    return out


def _preserve_messageful_sidebar_discoverability(
    candidates: list[dict],
    visible: list[dict],
) -> list[dict]:
    """Keep at least one messageful row per non-background conversation visible.

    The normal sidebar filters intentionally hide empty drafts, cron/background
    rows, and duplicate pre-compression snapshots. They must not make the only
    messageful representative of a conversation disappear. If every visible row
    for a lineage was filtered out, rescue the best hidden messageful row and
    mark it so callers can surface or audit the degraded state.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in candidates
        if session.get('session_id')
    }
    covered_roots = {
        _sidebar_lineage_root_id(session, sessions_by_id)
        for session in visible
        if _sidebar_message_count(session) > 0
    }
    visible_ids = {
        str(session.get('session_id'))
        for session in visible
        if session.get('session_id')
    }
    rescue_by_root: dict[str, dict] = {}
    for session in candidates:
        sid = str(session.get('session_id') or '')
        if not sid or sid in visible_ids:
            continue
        if _sidebar_message_count(session) <= 0:
            continue
        if _is_intentionally_background_sidebar_session(session):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        if root in covered_roots:
            continue
        current = rescue_by_root.get(root)
        if current is None or (
            _sidebar_message_count(session), _session_sort_timestamp(session)
        ) > (
            _sidebar_message_count(current), _session_sort_timestamp(current)
        ):
            rescued = dict(session)
            rescued['discoverability_warning'] = 'rescued_messageful_hidden_session'
            rescue_by_root[root] = rescued
    if not rescue_by_root:
        return visible
    rescued_rows = sorted(
        rescue_by_root.values(),
        key=lambda session: (session.get('pinned', False), _session_sort_timestamp(session)),
        reverse=True,
    )
    return visible + rescued_rows


def _prefer_fuller_snapshots_for_sidebar(sessions: list[dict]) -> list[dict]:
    """Expose a hidden snapshot when it is the fuller transcript for a lineage.

    Pre-compression snapshots are normally hidden so archived compression
    segments do not duplicate the current continuation in the sidebar. If a
    snapshot row has more messages than the visible continuation for the same
    lineage, hiding it makes the conversation look truncated. In that case,
    show the fuller snapshot and suppress the shorter inactive continuation.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in sessions
        if session.get('session_id')
    }
    groups: dict[str, list[dict]] = {}
    for session in sessions:
        sid = str(session.get('session_id') or '')
        source = session.get('source_tag') or session.get('source')
        if source == 'cron' or sid.startswith('cron_'):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        groups.setdefault(root, []).append(session)

    snapshot_ids_to_show: set[str] = set()
    continuation_ids_to_hide: set[str] = set()
    for group in groups.values():
        visible = [session for session in group if not session.get('pre_compression_snapshot')]
        snapshots = [session for session in group if session.get('pre_compression_snapshot')]
        if not visible or not snapshots:
            continue
        if any(_has_live_sidebar_state(session) for session in visible):
            continue

        best_visible_count = max(_sidebar_message_count(session) for session in visible)
        best_snapshot = max(
            snapshots,
            key=lambda session: (_sidebar_message_count(session), _session_sort_timestamp(session)),
        )
        if _sidebar_message_count(best_snapshot) <= best_visible_count:
            continue

        newest_visible_ts = max(_session_sort_timestamp(session) for session in visible)
        snapshot_ts = _session_sort_timestamp(best_snapshot)
        snapshot_id = str(best_snapshot.get('session_id') or '')
        if not snapshot_id:
            continue

        snapshot_ids_to_show.add(snapshot_id)
        # If the continuation is newer, keep it visible too. That means the
        # lineage is split-brain-ish: the snapshot has more transcript rows, but
        # the continuation may still contain the newest post-compression turn.
        # Showing both is less tidy than hiding one, but it preserves every
        # reachable message. Tidy and wrong is how users start doubting reality.
        if newest_visible_ts > snapshot_ts:
            continue

        messageful_visible = [
            session for session in visible
            if _sidebar_message_count(session) > 0
        ]
        if len(messageful_visible) > 1:
            continue

        continuation_ids_to_hide.update(
            str(session.get('session_id'))
            for session in visible
            if session.get('session_id')
        )

    if not snapshot_ids_to_show and not continuation_ids_to_hide:
        return sessions

    out = []
    for session in sessions:
        sid = str(session.get('session_id') or '')
        if sid in continuation_ids_to_hide:
            continue
        if sid in snapshot_ids_to_show:
            session = dict(session)
            session['_show_pre_compression_snapshot'] = True
        out.append(session)
    return out


def _strip_sidebar_internal_flags(sessions: list[dict]) -> None:
    for session in sessions:
        session.pop('_show_pre_compression_snapshot', None)


def _looks_like_stale_zero_message_row(session: dict) -> bool:
    """Return True for indexed rows that likely need sidecar metadata repair."""
    return bool(
        int(session.get('message_count') or 0) == 0
        and int(session.get('user_message_count') or 0) > 0
    )


def _row_may_need_sidecar_metadata_refresh(
    session: dict,
    *,
    stale_snapshot_ids: set[str] | None = None,
) -> bool:
    """Return True when a row needs canonical sidecar runtime/snapshot metadata.

    Compression lineage fields are enriched from state.db in one batched query
    later in all_sessions(). Loading hundreds of lineage sidecars on every
    /api/sessions poll turns the sidebar into molasses, so keep this refresh
    limited to rows with transient runtime state, missing snapshot sidebar
    metadata, or a stale snapshot candidate that can affect the visibility
    decision for its lineage.
    """
    is_runtime_row = bool(
        session.get('active_stream_id')
        or session.get('has_pending_user_message')
        or session.get('pending_user_message')
    )
    if is_runtime_row:
        return True
    sid = str(session.get('session_id') or '')
    if not session.get('pre_compression_snapshot'):
        # Refresh a stale-indexed COMPRESSION CONTINUATION row from its sidecar.
        # Gate tightly: a plain /branch fork also carries parent_session_id
        # (#1342) but has no compression sidecar drift to correct, and its file
        # mtime routinely exceeds the indexed logical last_message_at — so
        # including forks here would call load_metadata_only() on every fork row
        # on every /api/sessions poll (the molasses #3770 guards against, per the
        # #3789 release gate). Exclude session_source == 'fork'
        # (the marker /api/session/branch stamps; see _is_continuation_session)
        # so only true continuations are eligible.
        if str(session.get('session_source') or '').strip().lower() == 'fork':
            return False
        if session.get('message_count') is None or session.get('last_message_at') is None:
            return True
        # Lineage fields are enriched from state.db in a batched pass later in
        # all_sessions(). A complete indexed lineage row must not be reloaded
        # from its sidecar merely because the filesystem mtime is newer than the
        # logical message timestamp; that pattern is common after compression
        # and turns each /api/sessions poll into hundreds of JSON prefix scans.
        # Keep the mtime repair path only for rows whose counters are known bad
        # or incomplete enough that the index cannot be trusted.
        lineage_shaped = bool(
            session.get('parent_session_id')
            or session.get('_lineage_root_id')
            or session.get('_compression_segment_count')
        )
        needs_mtime_check = bool(
            sid
            and (
                _looks_like_stale_zero_message_row(session)
                or (lineage_shaped and session.get('user_message_count') is None)
            )
        )
        if needs_mtime_check and _sidecar_mtime_after_index_timestamp(session):
            return True
        return False
    if (
        sid
        and _looks_like_stale_zero_message_row(session)
        and str(session.get('session_source') or '').strip().lower() != 'fork'
        and _sidecar_mtime_after_index_timestamp(session)
    ):
        return True
    if session.get('message_count') is None or session.get('last_message_at') is None:
        return True
    return bool(sid and stale_snapshot_ids and sid in stale_snapshot_ids)


def _sidecar_mtime_after_index_timestamp(session: dict) -> bool:
    sid = str(session.get('session_id') or '')
    if not sid or not is_safe_session_id(sid):
        return False
    try:
        sidecar_mtime = (SESSION_DIR / f'{sid}.json').stat().st_mtime
    except OSError:
        return False
    indexed_ts = _session_sort_timestamp(session)
    return sidecar_mtime > indexed_ts + 0.001


def _stale_snapshot_metadata_refresh_ids(sessions: list[dict]) -> set[str]:
    """Return pre-compression snapshots worth a sidecar metadata refresh.

    Most snapshot rows can be decided from the index: either their indexed count
    already beats the visible continuation, or they are normal older snapshots
    that should remain hidden. Only stat candidate sidecars when a hidden
    snapshot has a visible continuation in the same lineage and its indexed
    metadata would otherwise fail to expose it.
    """
    sessions_by_id = {
        str(session.get('session_id')): session
        for session in sessions
        if session.get('session_id')
    }
    groups: dict[str, list[dict]] = {}
    for session in sessions:
        sid = str(session.get('session_id') or '')
        source = session.get('source_tag') or session.get('source')
        if source == 'cron' or sid.startswith('cron_'):
            continue
        root = _sidebar_lineage_root_id(session, sessions_by_id)
        groups.setdefault(root, []).append(session)

    refresh_ids: set[str] = set()
    for group in groups.values():
        visible = [session for session in group if not session.get('pre_compression_snapshot')]
        snapshots = [session for session in group if session.get('pre_compression_snapshot')]
        if not visible or not snapshots:
            continue
        if any(_has_live_sidebar_state(session) for session in visible):
            continue
        best_visible_count = max(_sidebar_message_count(session) for session in visible)
        for snapshot in snapshots:
            sid = str(snapshot.get('session_id') or '')
            if not sid:
                continue
            if _sidebar_message_count(snapshot) > best_visible_count:
                continue
            # Modern index rows already carry enough sidebar summary data to
            # decide snapshot visibility. Only legacy/incomplete rows need the
            # sidecar mtime rescue; otherwise every historical snapshot whose
            # file mtime is newer than its logical timestamp is re-read on every
            # sidebar poll. Treat stale-zero-message rows as incomplete even
            # when user_message_count/last_message_at are present; their sidecar
            # may hold the real count that makes the snapshot visible.
            if (
                snapshot.get('user_message_count') is not None
                and int(snapshot.get('message_count') or 0) > 0
                and snapshot.get('last_message_at') is not None
            ):
                continue
            if _sidecar_mtime_after_index_timestamp(snapshot):
                refresh_ids.add(sid)
    return refresh_ids


def _refresh_index_rows_from_sidecar_metadata(
    sessions: list[dict],
    *,
    index_message_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Overlay fuller sidecar metadata onto stale sidebar index rows.

    ``_index.json`` is a cache and can lag behind the canonical session sidecar
    during compression/continuation writes. Keep this read-only and limited to
    lineage/runtime-shaped rows so ordinary sidebar refreshes do not scan every
    historical transcript.
    """
    out: list[dict] = []
    stale_snapshot_ids = _stale_snapshot_metadata_refresh_ids(sessions)
    for session in sessions:
        if not _row_may_need_sidecar_metadata_refresh(
            session,
            stale_snapshot_ids=stale_snapshot_ids,
        ):
            out.append(session)
            continue
        sid = session.get('session_id')
        if not sid:
            out.append(session)
            continue
        sidecar = Session.load_metadata_only(
            sid,
            index_message_counts=index_message_counts,
        )
        if not sidecar:
            out.append(session)
            continue
        compact = sidecar.compact(include_runtime=True)
        refreshed = dict(session)
        for key in (
            'message_count', 'updated_at', 'last_message_at', 'title', 'workspace',
            'model', 'model_provider', 'created_at', 'pinned', 'archived', 'project_id',
            'profile', 'pre_compression_snapshot', 'parent_session_id', 'source_tag',
            'raw_source', 'session_source', 'source_label', 'active_stream_id',
            'has_pending_user_message', 'pending_user_message', 'pending_started_at',
        ):
            value = compact.get(key)
            if value is not None:
                refreshed[key] = value
        try:
            refreshed['message_count'] = max(
                int(session.get('message_count') or 0),
                int(compact.get('message_count') or 0),
            )
        except (TypeError, ValueError):
            pass
        if _session_sort_timestamp(compact) > _session_sort_timestamp(session):
            refreshed['updated_at'] = compact.get('updated_at', refreshed.get('updated_at'))
            refreshed['last_message_at'] = compact.get('last_message_at', refreshed.get('last_message_at'))
        out.append(refreshed)
    return out


def state_db_has_session(sid: str) -> bool:
    """Return True when ``sid`` exists in the active state.db sessions table.

    Used by file-manager handlers to fall back to a state.db lookup when
    ``get_session`` raises ``KeyError`` because the session was created by
    Telegram/CLI (external) rather than the WebUI (issue #3280). The state.db
    schema stores only metadata (id/title/model/source/...), not a workspace
    path — the workspace is shared across session storage backends and is
    resolved separately via ``get_last_workspace()``.
    """
    if not sid:
        return False
    try:
        import sqlite3
    except ImportError:
        return False
    db_path = _active_state_db_path()
    if not db_path.exists():
        return False
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (str(sid),))
            return cur.fetchone() is not None
    except Exception:
        return False


class _ExternalSessionView:
    """Minimal session-shaped view for external (Telegram/CLI) sessions.

    Only exposes the fields file-manager handlers need (``session_id`` and
    ``workspace``). The workspace falls back to the WebUI's last-used
    workspace because state.db does not persist a per-session workspace path
    and the file browser is intentionally workspace-scoped, not
    session-storage-scoped (issue #3280).
    """

    __slots__ = ("session_id", "workspace")

    def __init__(self, session_id: str, workspace: str):
        self.session_id = session_id
        self.workspace = workspace


def get_session_for_file_ops(sid: str):
    """Return a profile-authorized session-like object for file-manager handlers.

    Tries ``get_session`` first (preserves all existing behavior for WebUI
    sessions) and only returns that session when its stored profile belongs to
    the active request profile.  If that lookup fails, checks state.db; when the
    session exists there, returns an ``_ExternalSessionView`` whose ``workspace``
    is the active WebUI workspace. If neither has the session, re-raises
    ``KeyError`` so callers continue to return their existing 404.
    """
    try:
        session = get_session(sid, metadata_only=True)
    except KeyError:
        if state_db_has_session(sid):
            return _ExternalSessionView(str(sid), str(get_last_workspace()))
        raise

    from api.profiles import _profiles_match, get_active_profile_name

    session_profile = getattr(session, 'profile', None)
    active_profile = get_active_profile_name()
    if not _profiles_match(session_profile, active_profile):
        logger.debug(
            "Rejected file-manager session for foreign profile: "
            "session_id=%s session_profile=%r active_profile=%r",
            sid,
            session_profile,
            active_profile,
        )
        raise KeyError(sid)
    return session


def _active_state_db_path() -> Path:
    """Return state.db for the active Hermes profile, degrading to HERMES_HOME."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    return hermes_home / 'state.db'


def _agent_state_db_path(*, profile=None) -> Path | None:
    """Return agent ``state.db`` for *profile*, or ``None`` when unavailable."""
    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return None
    return db_path


def agent_session_rows_existing(
    session_ids: list[str] | set[str] | frozenset[str],
    *,
    profile=None,
) -> frozenset[str]:
    """Return session ids confirmed present in the agent ``sessions`` table.

    Used by the sidebar orphan-prune path (#3238) to batch existence probes
    instead of opening one SQLite connection per candidate row.

    Degrades safely to ``frozenset(wanted)`` (assume all present) on any error,
    when the DB is missing, or when the ``sessions`` table is absent — matching
    ``agent_session_row_exists()`` so a transient failure never causes pruning.
    """
    wanted = {str(sid).strip() for sid in (session_ids or []) if str(sid or "").strip()}
    if not wanted:
        return frozenset()
    try:
        import sqlite3
    except ImportError:
        return frozenset(wanted)
    db_path = _agent_state_db_path(profile=profile)
    if db_path is None:
        return frozenset(wanted)
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            cols = {str(row[1]) for row in cur.fetchall()}
            if 'id' not in cols:
                return frozenset(wanted)
            existing: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"SELECT id FROM sessions WHERE id IN ({placeholders})",
                    chunk,
                )
                existing.update(str(row[0]).strip() for row in cur.fetchall())
            return frozenset(existing)
    except Exception:
        logger.debug(
            "agent_session_rows_existing probe failed for %d ids",
            len(wanted),
            exc_info=True,
        )
        return frozenset(wanted)


def agent_session_zero_message_sids(
    session_ids: list[str] | set[str] | frozenset[str],
    *,
    profile=None,
) -> frozenset[str]:
    """Return session ids confirmed to have zero rows in the agent ``messages`` table.

    Used by the sidebar orphan-prune path (#4985) to detect native-WebUI sessions
    whose backing agent row exists but was never written to (boot-time ``+`` click,
    profile switch that resets the active id, sidebar nav that opens a session then
    closes the tab before the first message commits). Such rows linger in the
    sidebar forever because the WebUI delete affordance is not exposed for them,
    and the existing #3238/#4591 orphan prune explicitly excludes webui sources.

    Mirrors ``agent_session_rows_existing``'s batched chunked probe, safe-degrade
    contract (returns ``frozenset()`` on any error so a transient failure NEVER
    causes a stale-prune data loss), and ``messages`` table absence handling.
    """
    wanted = {str(sid).strip() for sid in (session_ids or []) if str(sid or "").strip()}
    if not wanted:
        return frozenset()
    try:
        import sqlite3
    except ImportError:
        return frozenset()
    db_path = _agent_state_db_path(profile=profile)
    if db_path is None:
        return frozenset()
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            sessions_cols = {str(row[1]) for row in cur.fetchall()}
            if 'id' not in sessions_cols:
                return frozenset()
            cur.execute("PRAGMA table_info(messages)")
            messages_cols = {str(row[1]) for row in cur.fetchall()}
            if 'session_id' not in messages_cols:
                return frozenset()
            zero_message: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"SELECT s.id FROM sessions s "
                    f"WHERE s.id IN ({placeholders}) "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM messages m WHERE m.session_id = s.id"
                    f")",
                    chunk,
                )
                zero_message.update(str(row[0]).strip() for row in cur.fetchall())
            return frozenset(zero_message)
    except Exception:
        logger.debug(
            "agent_session_zero_message_sids probe failed for %d ids",
            len(wanted),
            exc_info=True,
        )
        return frozenset()


def agent_session_row_exists(session_id: str, *, profile=None) -> bool:
    """Return True if ``session_id`` still has a backing row in the agent state.db.

    Used to detect orphaned imported-CLI sidecars (#3238): the WebUI sidebar
    must NOT rely on the session's presence in ``get_cli_sessions()`` to decide
    whether its backing CLI row still exists, because that helper caps at
    ``CLI_VISIBLE_SESSION_LIMIT`` (20) rows — a still-existing session can fall
    out of the recent window and look "deleted." This is an exact, uncapped
    existence probe against the ``sessions`` table.

    Degrades safely to ``True`` (assume present) on any error or when the DB is
    unreadable, so a transient failure never causes a stale-pruning data loss.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return False
    return sid in agent_session_rows_existing([sid], profile=profile)


def _sidebar_title_is_generic_webui(title: str | None) -> bool:
    text = ' '.join(str(title or '').split())
    if text == 'Hermes WebUI':
        return True
    prefix = 'Hermes WebUI #'
    return text.startswith(prefix) and text[len(prefix):].isdigit()


def _read_state_db_sidebar_overrides(
    db_path: Path,
    session_ids: set[str],
    count_session_ids: set[str] | None = None,
) -> dict[str, dict]:
    """Return cheap state.db source/title overrides for sidebar rows.

    This intentionally does not chase lineage parents/children. It is used on
    the /api/sessions hot path before CLI filtering so state.db can correct
    stale JSON source flags without paying the full lineage-enrichment cost.

    Two-tier cost split (#5132): the ``sessions``-table lookup (source/title/
    message_count) is an indexed primary-key fetch and is run for ALL
    ``session_ids`` — its result feeds the source classification that
    ``/api/sessions`` filters on BEFORE the lazy lineage correction, so capping
    it would silently drop rows (e.g. a stale ``cli`` JSON row whose state.db
    source is ``webui``) from the default sidebar. The expensive part is the
    ``messages`` aggregation (``COUNT(*)``/``MAX(timestamp)`` GROUP BY), which is
    what blocked /api/sessions for 5-18s on power users; that scan is restricted
    to ``count_session_ids`` (the top-N paint-priority rows). When
    ``count_session_ids`` is None, both tiers cover the full set (caller opted
    out of the cap).
    """
    wanted = {str(sid) for sid in (session_ids or set()) if sid}
    if count_session_ids is None:
        count_wanted = set(wanted)
    else:
        count_wanted = {str(sid) for sid in count_session_ids if sid} & wanted
    if not wanted or not db_path.exists():
        return {}
    try:
        import sqlite3
    except ImportError:
        return {}
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(sessions)")
            session_cols = {row[1] for row in cur.fetchall()}
            if 'id' not in session_cols:
                return {}
            source_expr = 's.source' if 'source' in session_cols else 'NULL AS source'
            session_source_expr = 's.session_source' if 'session_source' in session_cols else 'NULL AS session_source'
            title_expr = 's.title' if 'title' in session_cols else 'NULL AS title'
            message_count_expr = 's.message_count' if 'message_count' in session_cols else 'NULL AS message_count'

            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'messages'")
            has_messages_table = cur.fetchone() is not None
            messages_has_session_id = False
            messages_has_timestamp = False
            messages_has_title_fields = False
            if has_messages_table:
                cur.execute("PRAGMA table_info(messages)")
                message_cols = {str(row[1]) for row in cur.fetchall()}
                messages_has_session_id = 'session_id' in message_cols
                messages_has_timestamp = 'timestamp' in message_cols
                messages_has_title_fields = {'session_id', 'role', 'content', 'timestamp'}.issubset(message_cols)

            overrides: dict[str, dict] = {}
            delegated_title_ids: set[str] = set()
            ids = list(wanted)
            chunk_size = 500
            for i in range(0, len(ids), chunk_size):
                chunk = ids[i:i + chunk_size]
                placeholders = ','.join('?' * len(chunk))
                cur.execute(
                    f"""
                    SELECT s.id, {source_expr}, {session_source_expr}, {title_expr}, {message_count_expr}
                    FROM sessions s
                    WHERE s.id IN ({placeholders})
                    """,
                    chunk,
                )
                for row in cur.fetchall():
                    sid = str(row['id'])
                    entry: dict[str, object] = {}
                    state_title = str(row['title'] or '').strip()
                    if state_title:
                        entry['_state_db_title'] = state_title
                    state_source = str(row['source'] or '').strip().lower()
                    if (
                        state_source == 'subagent'
                        and sid in count_wanted
                        and ' '.join(state_title.split()) == 'Subagent Session'
                    ):
                        delegated_title_ids.add(sid)
                    if state_source:
                        entry['_state_db_source'] = state_source
                        source_meta = normalize_agent_session_source(state_source)
                        entry['_state_db_source_tag'] = state_source
                        entry['_state_db_raw_source'] = source_meta.get('raw_source')
                        entry['_state_db_session_source'] = source_meta.get('session_source')
                        entry['_state_db_source_label'] = source_meta.get('source_label')
                    if row['message_count'] is not None:
                        try:
                            entry['_state_db_message_count'] = max(0, int(row['message_count'] or 0))
                        except (TypeError, ValueError):
                            pass
                    if entry:
                        overrides[sid] = entry
                if has_messages_table and messages_has_session_id:
                    count_chunk = [sid for sid in chunk if sid in count_wanted]
                    if not count_chunk:
                        continue
                    count_placeholders = ','.join('?' * len(count_chunk))
                    last_at_expr = "MAX(timestamp) AS last_message_at" if messages_has_timestamp else "NULL AS last_message_at"
                    cur.execute(
                        f"""
                        SELECT session_id, COUNT(*) AS actual_message_count, {last_at_expr}
                        FROM messages
                        WHERE session_id IN ({count_placeholders})
                        GROUP BY session_id
                        """,
                        count_chunk,
                    )
                    for row in cur.fetchall():
                        sid = str(row['session_id'])
                        entry = overrides.setdefault(sid, {})
                        try:
                            entry['_state_db_message_count'] = max(
                                int(entry.get('_state_db_message_count') or 0),
                                int(row['actual_message_count'] or 0),
                            )
                        except (TypeError, ValueError):
                            pass
                        if row['last_message_at'] is not None:
                            try:
                                entry['_state_db_last_message_at'] = float(row['last_message_at'] or 0)
                            except (TypeError, ValueError):
                                pass
            if messages_has_title_fields and delegated_title_ids:
                seen_user_messages: set[str] = set()
                delegated_title_ids = list(delegated_title_ids)
                for i in range(0, len(delegated_title_ids), chunk_size):
                    chunk = delegated_title_ids[i:i + chunk_size]
                    placeholders = ','.join('?' * len(chunk))
                    cur.execute(
                        f"""
                        SELECT session_id, role, content, timestamp
                        FROM messages
                        WHERE session_id IN ({placeholders}) AND role = 'user'
                        ORDER BY session_id, timestamp ASC
                        """,
                        chunk,
                    )
                    for row in cur.fetchall():
                        sid = str(row['session_id'])
                        if sid in seen_user_messages:
                            continue
                        display_title = title_from([dict(row)], fallback='')
                        if display_title:
                            seen_user_messages.add(sid)
                            overrides.setdefault(sid, {})['_state_db_display_title'] = display_title
            return overrides
    except Exception:
        return {}


def _apply_sidebar_state_db_overrides(sessions: list[dict]) -> None:
    """Apply state.db source/title overrides without full lineage enrichment.

    Source classification (source/title) is corrected for ALL rows because it
    feeds the CLI/WebUI sidebar filter that runs BEFORE the lazy lineage
    correction — capping it would silently drop rows whose stale JSON source
    disagrees with state.db (#5132 regression guard). Only the expensive
    ``messages`` count/last-message aggregation is capped to the top-N most
    recent (paint-priority) rows, which is what actually blocked /api/sessions
    for 5-18s on power users reading state.db for 2400+ rows on every
    concurrent poll (#5132). The cap is env-configurable and fails open; rows
    beyond it keep their JSON message-count/last-message until the history panel
    opens (lazily corrected, exactly as with the lineage cap #4638).
    """
    import os as _os
    try:
        _cap = int(_os.environ.get("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "300"))
    except (TypeError, ValueError):
        _cap = 300
    all_ids = {str(s.get('session_id')) for s in sessions if s.get('session_id')}
    if _cap > 0 and len(sessions) > _cap:
        count_ids = {str(s.get('session_id')) for s in sessions[:_cap] if s.get('session_id')}
    else:
        count_ids = None  # cap disabled / under cap -> count every row too
    try:
        metadata = _read_state_db_sidebar_overrides(
            _active_state_db_path(),
            all_ids,
            count_session_ids=count_ids,
        )
    except Exception:
        return
    _apply_sidebar_state_db_override_metadata(sessions, metadata)


def _apply_sidebar_state_db_override_metadata(sessions: list[dict], metadata: dict[str, dict]) -> None:
    for session in sessions:
        sid = session.get('session_id')
        if sid not in metadata:
            continue
        entry = dict(metadata[sid])
        state_db_title = entry.pop('_state_db_title', None)
        state_db_source = entry.pop('_state_db_source', None)
        state_db_source_tag = entry.pop('_state_db_source_tag', None)
        state_db_raw_source = entry.pop('_state_db_raw_source', None)
        state_db_session_source = entry.pop('_state_db_session_source', None)
        state_db_source_label = entry.pop('_state_db_source_label', None)
        state_db_message_count = entry.pop('_state_db_message_count', None)
        state_db_last_message_at = entry.pop('_state_db_last_message_at', None)
        state_db_display_title = entry.pop('_state_db_display_title', None)
        if state_db_source == 'webui':
            session['source_tag'] = state_db_source_tag
            session['raw_source'] = state_db_raw_source
            session['session_source'] = state_db_session_source
            session['source_label'] = state_db_source_label
            session['is_cli_session'] = False
        # Overlay the real state.db message count for WebUI-owned rows AND for
        # delegated subagent children (#5308). A subagent child
        # (state_db_source == 'subagent') is backed by the delegate runner's
        # state.db session, but its sidebar row is built from a stale sidecar
        # that often reports message_count == 0. Without overlaying the true
        # count, the front-end visibility predicate
        # (_sidebarRowHasVisibleMessages) drops the row and the subagent
        # session vanishes from the sidebar entirely (regression seam behind
        # #5308, same state.db-blind-metadata root as the #5307 transcript
        # recovery). The count overlay keeps the same conservative
        # anti-resurrection guard used for WebUI rows. The source-tag / title
        # reassignment above stays WebUI-only — a subagent child keeps its
        # subagent classification.
        if state_db_source in ('webui', 'subagent'):
            try:
                current_count = max(0, int(session.get('message_count') or 0))
                state_count = max(0, int(state_db_message_count or 0))
            except (TypeError, ValueError):
                current_count = 0
                state_count = 0
            try:
                current_last = max(
                    float(session.get('last_message_at') or 0),
                    float(session.get('updated_at') or 0),
                )
            except (TypeError, ValueError):
                current_last = 0.0
            try:
                state_last = float(state_db_last_message_at or 0)
            except (TypeError, ValueError):
                state_last = 0.0
            # ``current_last`` intentionally includes ``updated_at``: if a
            # sidecar metadata-only write happened after the state.db append,
            # keep the conservative anti-resurrection guard and wait for a
            # newer settled state.db message before overlaying counts again.
            if state_count > current_count and (state_last <= 0 or state_last > current_last):
                try:
                    existing_actual = max(0, int(session.get('actual_message_count') or 0))
                except (TypeError, ValueError):
                    existing_actual = 0
                session['message_count'] = state_count
                session['actual_message_count'] = max(state_count, existing_actual)
                if state_last > 0:
                    session['last_message_at'] = max(float(session.get('last_message_at') or 0), state_last)
                    session['updated_at'] = max(float(session.get('updated_at') or 0), state_last)
        title = session.get('title')
        if (
            state_db_title
            and state_db_title != title
            and _sidebar_title_is_generic_webui(title)
        ):
            session['_state_db_title'] = state_db_title
            session['display_title'] = state_db_title
        if (
            state_db_display_title
            and state_db_source == 'subagent'
            and ' '.join(str(title or '').split()) == 'Subagent Session'
        ):
            session['display_title'] = state_db_display_title


def _enrich_sidebar_lineage_metadata(sessions: list[dict]) -> None:
    """Attach state.db compression lineage metadata used by sidebar collapse.

    Cap the DB lookup to the top-N most recent sessions to bound wall-clock
    on power users with thousands of sessions. The sidebar paints chronologically
    newest first; older sessions almost never have visible lineage to collapse
    (parents are themselves stale and rarely surface in the same render).
    Lineage enrichment for those is loaded lazily when the user opens the
    history panel. Issue #38914 / 2026-06-21 triage: /api/sessions was spending
    4.9s on lineage_metadata across 2400+ rows.
    """
    # 2026-06-21: configurable via env to ease A/B and rollback without a redeploy.
    import os as _os
    try:
        _cap = int(_os.environ.get("HERMES_WEBUI_LINEAGE_TOP_N", "300"))
    except (TypeError, ValueError):
        _cap = 300
    if _cap > 0 and len(sessions) > _cap:
        candidates = sessions[:_cap]
    else:
        candidates = sessions
    try:
        metadata = read_session_lineage_metadata(
            _active_state_db_path(),
            {str(s.get('session_id')) for s in candidates if s.get('session_id')},
        )
    except Exception:
        return
    _apply_sidebar_state_db_override_metadata(sessions, metadata)
    for session in sessions:
        sid = session.get('session_id')
        if sid in metadata:
            entry = dict(metadata[sid])
            for key in (
                '_state_db_title',
                '_state_db_source',
                '_state_db_source_tag',
                '_state_db_raw_source',
                '_state_db_session_source',
                '_state_db_source_label',
            ):
                entry.pop(key, None)
            session.update(entry)


def _diag_stage(diag, name: str) -> None:
    if diag is not None:
        try:
            diag.stage(name)
        except Exception:
            pass


def all_sessions(diag=None, *, include_lineage_metadata: bool = True):
    _diag_stage(diag, "all_sessions.active_streams")
    active_stream_ids = _active_stream_ids()
    # Phase C: try index first for O(1) read; fall back to full scan
    _diag_stage(diag, "all_sessions.index_exists")
    if not SESSION_INDEX_FILE.exists():
        _diag_stage(diag, "all_sessions.start_index_rebuild")
        _start_session_index_rebuild_thread()
    if SESSION_INDEX_FILE.exists():
        try:
            _diag_stage(diag, "all_sessions.read_index")
            index = json.loads(SESSION_INDEX_FILE.read_bytes())
            _diag_stage(diag, "all_sessions.prune_index")
            with LOCK:
                in_memory_ids = set(SESSIONS.keys())
            persisted_ids = _persisted_session_ids_snapshot()
            if not index and _session_dir_has_persisted_session_files():
                raise ValueError("empty session index while session files exist")
            index = [
                s for s in index
                if (
                    str(s.get('session_id') or '') in in_memory_ids
                    or (
                        persisted_ids is not None
                        and str(s.get('session_id') or '') in persisted_ids
                    )
                    or (
                        persisted_ids is None
                        and _index_entry_exists(s.get('session_id'), in_memory_ids=in_memory_ids)
                    )
                )
            ]
            if not index and _session_dir_has_persisted_session_files():
                raise ValueError("session index has no live rows while session files exist")
            backfilled = []
            for i, s in enumerate(index):
                if 'last_message_at' not in s:
                    _diag_stage(diag, "all_sessions.backfill_load")
                    full = Session.load(s.get('session_id'))
                    if full:
                        index[i] = full.compact()
                        backfilled.append(full)
            if backfilled:
                try:
                    _diag_stage(diag, "all_sessions.backfill_write")
                    _write_session_index(updates=backfilled)
                except Exception:
                    logger.debug("Failed to persist last_message_at backfill")
            _diag_stage(diag, "all_sessions.mark_streaming")
            for s in index:
                s['is_streaming'] = _is_streaming_session(
                    s.get('active_stream_id'),
                    active_stream_ids,
                )
            # Overlay any in-memory sessions that may be newer than the index
            _diag_stage(diag, "all_sessions.overlay_lock")
            index_map = {s['session_id']: s for s in index}
            with LOCK:
                for s in SESSIONS.values():
                    index_map[s.session_id] = s.compact(
                        include_runtime=True,
                        active_stream_ids=active_stream_ids,
                    )
            missing_persisted_ids = []
            if persisted_ids is not None:
                indexed_ids = {str(sid) for sid in index_map.keys() if sid}
                missing_persisted_ids = sorted(
                    str(sid) for sid in persisted_ids
                    if sid and str(sid) not in indexed_ids
                )
            # #4985: the tombstone is intentionally NOT a blind-drop filter
            # on missing_persisted_ids. A tombstoned sid whose sidecar is
            # still on disk is recovered into the index here so the
            # post-recovery prune helper (``_prune_orphaned_webui_zero_message_sessions``
            # below) gets a chance to self-heal: if the row's state.db.messages
            # is still empty the helper leaves the tombstone in place (no
            # redundant re-prune); if state.db.messages now has rows the
            # helper clears the tombstone and the row stays visible. A
            # blind-drop here would be strictly worse than the orphan it
            # suppresses — it would silently swallow a legitimately-resurfaced
            # row forever, even after the user actually sent messages.
            recovered_sidecars = []
            if missing_persisted_ids:
                _diag_stage(diag, "all_sessions.recover_missing_index_sidecars")
                for sid in missing_persisted_ids:
                    try:
                        sidecar = Session.load_metadata_only(sid)
                    except Exception:
                        sidecar = None
                    if not sidecar:
                        continue
                    index_map[sidecar.session_id] = sidecar.compact(
                        include_runtime=True,
                        active_stream_ids=active_stream_ids,
                    )
                    recovered_sidecars.append(sidecar)
                if recovered_sidecars:
                    try:
                        _diag_stage(diag, "all_sessions.recover_missing_index_write")
                        _write_session_index(updates=recovered_sidecars)
                    except Exception:
                        logger.debug("Failed to persist recovered sidebar index rows")
            _diag_stage(diag, "all_sessions.refresh_sidecar_metadata")
            index_message_counts = _index_message_count_map(index)
            refreshed_index_rows = _refresh_index_rows_from_sidecar_metadata(
                list(index_map.values()),
                index_message_counts=index_message_counts,
            )
            index_map = {
                row['session_id']: row
                for row in refreshed_index_rows
                if row.get('session_id')
            }
            _diag_stage(diag, "all_sessions.sort_filter")
            result = sorted(index_map.values(), key=lambda s: (s.get('pinned', False), _session_sort_timestamp(s)), reverse=True)
            # Hide empty Untitled sessions from the UI entirely — they are ephemeral
            # scratch pads that only become real once the first message is sent (#1171).
            # No grace window: a 0-message Untitled session is never shown in the list
            # regardless of age. This means page refreshes and accidental New Conversation
            # clicks never leave orphan entries in the sidebar.
            #
            # Exception: sessions with active_stream_id set are actively streaming (#1327).
            # #1184 deferred the first save() until the first message, so during the
            # initial streaming turn the session still looks like Untitled+0-messages.
            # Without this exemption, navigating away during a long first turn causes
            # the session to vanish from the sidebar.
            result = [s for s in result if not (
                s.get('title', 'Untitled') == 'Untitled'
                and s.get('message_count', 0) == 0
                and not s.get('active_stream_id')
                and not s.get('has_pending_user_message')
                and not s.get('worktree_path')
            )]
            if include_lineage_metadata:
                _diag_stage(diag, "all_sessions.lineage_metadata")
                _enrich_sidebar_lineage_metadata(result)
            else:
                _diag_stage(diag, "all_sessions.state_db_overrides")
                _apply_sidebar_state_db_overrides(result)
                _diag_stage(diag, "all_sessions.lineage_metadata_skipped")
            result = _prefer_fuller_snapshots_for_sidebar(result)
            sidebar_candidates = result
            visible_result = [s for s in sidebar_candidates if not _hide_from_default_sidebar(s)]
            result = _preserve_messageful_sidebar_discoverability(sidebar_candidates, visible_result)
            result = _include_project_hidden_background_sidebar_sessions(sidebar_candidates, result)
            _strip_sidebar_internal_flags(result)
            # Backfill: sessions created before Sprint 22 have no profile tag.
            # Attribute them to 'default' so the client profile filter works correctly.
            for s in result:
                if not s.get('profile'):
                    s['profile'] = 'default'
            return result
        except Exception:
            logger.debug("Failed to load session index, falling back to full scan")
    # Full scan fallback
    _diag_stage(diag, "all_sessions.full_scan")
    out = []
    # #4985: the tombstone is intentionally NOT a blind-drop filter on the
    # full-scan fallback either. A tombstoned sid whose sidecar is still
    # on disk must be loaded here so the post-recovery prune helper
    # (``_prune_orphaned_webui_zero_message_sessions`` in api/routes) gets a
    # chance to self-heal: if state.db.messages is still empty the helper
    # leaves the tombstone in place; if state.db.messages now has rows the
    # helper clears the tombstone and the row stays visible. A blind-drop
    # here would be strictly worse than the orphan it suppresses — silently
    # swallowing a legitimately-resurfaced row forever.
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception:
            logger.debug("Failed to load session from %s", p)
    _diag_stage(diag, "all_sessions.full_scan_overlay")
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    _diag_stage(diag, "all_sessions.full_scan_sort_filter")
    out.sort(key=lambda s: (getattr(s, 'pinned', False), _session_sort_timestamp(s)), reverse=True)
    # Hide empty Untitled sessions from the UI entirely — kept consistent with the
    # index-path filter above. No grace window: a 0-message Untitled session is
    # never shown regardless of age (#1171).  Same streaming exemption as above (#1327).
    result = [s.compact(include_runtime=True, active_stream_ids=active_stream_ids) for s in out if not (
        s.title == 'Untitled'
        and len(s.messages) == 0
        and not s.active_stream_id
        and not s.pending_user_message
        and not getattr(s, 'worktree_path', None)
    )]  # fmt: skip
    if include_lineage_metadata:
        _diag_stage(diag, "all_sessions.lineage_metadata")
        _enrich_sidebar_lineage_metadata(result)
    else:
        _diag_stage(diag, "all_sessions.state_db_overrides")
        _apply_sidebar_state_db_overrides(result)
        _diag_stage(diag, "all_sessions.lineage_metadata_skipped")
    result = _prefer_fuller_snapshots_for_sidebar(result)
    sidebar_candidates = result
    visible_result = [s for s in sidebar_candidates if not _hide_from_default_sidebar(s)]
    result = _preserve_messageful_sidebar_discoverability(sidebar_candidates, visible_result)
    result = _include_project_hidden_background_sidebar_sessions(sidebar_candidates, result)
    _strip_sidebar_internal_flags(result)
    for s in result:
        if not s.get('profile'):
            s['profile'] = 'default'
    return result


def _strip_attached_files_marker(text: str) -> str:
    return re.sub(r"\n\n\[Attached files: [^\]]+\]$", "", str(text or "")).strip()


def title_from(messages, fallback: str='Untitled'):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if c is None:
                continue
            if isinstance(c, list):
                c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
            text = _strip_attached_files_marker(str(c))
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

_PROJECTS_MIGRATION_LOCK = threading.Lock()
_projects_migrated = False


def _backfill_project_profiles_if_needed(projects: list) -> bool:
    """Tag any legacy untagged projects (`profile` missing) with a sensible default.

    Strategy:
      1. For each untagged project, look at the sessions assigned to it via
         the session index. If any session carries a profile, take that
         profile.  Most installs are single-profile so this picks up the
         right answer for everyone.
      2. Otherwise default to 'default'.

    Returns True if any project was mutated. Safe to call repeatedly — once
    every project is tagged, this is a no-op. Runs at most once per process
    (cached via the module-level _projects_migrated flag) but the result is
    persisted so it's a one-time write.
    """
    untagged = [p for p in projects if not p.get('profile')]
    if not untagged:
        return False

    # Build session_id -> profile map for the untagged project_ids.
    session_profile_by_project: dict[str, str] = {}
    if SESSION_INDEX_FILE.exists():
        try:
            entries = json.loads(SESSION_INDEX_FILE.read_bytes())
            untagged_ids = {p['project_id'] for p in untagged if p.get('project_id')}
            for e in entries:
                pid = e.get('project_id')
                if pid in untagged_ids and e.get('profile'):
                    # First session profile wins for the project.
                    session_profile_by_project.setdefault(pid, e['profile'])
        except Exception:
            logger.debug("Failed to read session index for project profile backfill")

    mutated = False
    for p in untagged:
        inferred = session_profile_by_project.get(p.get('project_id'), 'default')
        p['profile'] = inferred
        mutated = True
    return mutated


def load_projects(*, _migrate: bool = True) -> list:
    """Load project list from disk. Returns list of project dicts.

    On first call, runs a one-time migration to back-fill the `profile` field
    on legacy untagged projects (#1614). Disable via `_migrate=False` for
    callsites that want the raw on-disk shape (test fixtures, e.g.).
    """
    global _projects_migrated
    if not PROJECTS_FILE.exists():
        return []
    try:
        projects = json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    if _migrate and not _projects_migrated:
        with _PROJECTS_MIGRATION_LOCK:
            # Re-check inside the lock — another thread may have raced.
            if _projects_migrated:
                # Per Opus advisor on stage-293: another thread completed
                # migration and wrote new state to disk while we waited for
                # the lock. Our `projects` snapshot is the pre-migration
                # version; re-read so the caller doesn't see stale untagged
                # rows (which a mutation route could then write back,
                # silently overwriting the migration).
                try:
                    return json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
                except Exception:
                    return projects
            if _backfill_project_profiles_if_needed(projects):
                try:
                    save_projects(projects)
                    _projects_migrated = True
                except Exception:
                    logger.debug("Failed to persist project profile backfill")
                    # Leave _projects_migrated False so a future call retries.
            else:
                # Nothing to migrate — already tagged.
                _projects_migrated = True
    return projects

def save_projects(projects) -> None:
    """Write project list to disk."""
    PROJECTS_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding='utf-8')


CRON_PROJECT_NAME = 'Cron Jobs'
_CRON_PROJECT_LOCK = threading.Lock()


def ensure_cron_project(create: bool = True) -> str | None:
    """Return the project_id of the system "Cron Jobs" project for the active profile.

    Each profile gets its own "Cron Jobs" project so cron-spawned sessions in
    profile A don't surface under the cron chip of profile B (#1614). Lookup
    keys on (name, profile) — a legacy untagged "Cron Jobs" project (no
    `profile` field) is treated as belonging to whichever profile first calls
    this in a given install, then re-tagged.

    When `create` is False, only an EXISTING per-profile cron project is
    resolved (exact tag, renamed-root alias, or legacy-untagged back-tag);
    no new project is minted and None is returned instead. Callers gate
    `create` on `_profile_has_user_projects()` so cron sessions don't force
    a "Cron Jobs" chip onto installs that never opted into project
    organization (#5379). Direct callers that omit `create` keep today's
    unconditional-create behavior.

    Thread-safe and idempotent.  Returns a 12-char hex project_id string, or
    None if `create` is False and no existing cron project resolves.
    """
    from api.profiles import get_active_profile_name, _is_root_profile

    active = get_active_profile_name() or 'default'
    with _CRON_PROJECT_LOCK:
        projects = load_projects()
        # Look for an existing per-profile cron project. Match either an exact
        # profile tag or the renamed-root alias (a 'default'-tagged project
        # under a renamed root, or a renamed-root-tagged project under
        # 'default'). _is_root_profile is the canonical alias check.
        for p in projects:
            if p.get('name') != CRON_PROJECT_NAME:
                continue
            row_profile = p.get('profile')
            if row_profile == active:
                return p['project_id']
            if _is_root_profile(row_profile or 'default') and _is_root_profile(active):
                return p['project_id']
        # Reuse a legacy untagged cron project — back-tag it to the active profile.
        for p in projects:
            if p.get('name') == CRON_PROJECT_NAME and not p.get('profile'):
                p['profile'] = active
                save_projects(projects)
                return p['project_id']
        if not create:
            return None
        # Otherwise create a new one tagged with the active profile.
        project_id = uuid.uuid4().hex[:12]
        projects.append({
            'project_id': project_id,
            'name': CRON_PROJECT_NAME,
            'color': '#6366f1',
            'profile': active,
            'created_at': time.time(),
        })
        save_projects(projects)
        return project_id


WEBHOOK_PROJECT_NAME = 'Webhooks'
_WEBHOOK_PROJECT_LOCK = threading.Lock()


def ensure_webhook_project() -> str:
    """Return the project_id of the system "Webhooks" project for the active profile."""
    from api.profiles import get_active_profile_name, _is_root_profile

    active = get_active_profile_name() or 'default'
    with _WEBHOOK_PROJECT_LOCK:
        projects = load_projects()
        for p in projects:
            if p.get('name') != WEBHOOK_PROJECT_NAME:
                continue
            row_profile = p.get('profile')
            if row_profile == active:
                return p['project_id']
            if _is_root_profile(row_profile or 'default') and _is_root_profile(active):
                return p['project_id']
        for p in projects:
            if p.get('name') == WEBHOOK_PROJECT_NAME and not p.get('profile'):
                p['profile'] = active
                save_projects(projects)
                return p['project_id']
        project_id = uuid.uuid4().hex[:12]
        projects.append({
            'project_id': project_id,
            'name': WEBHOOK_PROJECT_NAME,
            'color': '#0ea5e9',
            'profile': active,
            'created_at': time.time(),
        })
        save_projects(projects)
        return project_id


def _profile_has_user_projects() -> bool:
    """True if the active profile already has at least one real (non-system) project.

    "Opted into project organization" means `load_projects()` contains a
    project whose name is not a reserved system name (`CRON_PROJECT_NAME`,
    `WEBHOOK_PROJECT_NAME`), tagged to the active profile or its renamed-root
    alias. Profile/alias matching mirrors `ensure_cron_project`'s own lookup
    so the two never disagree about which profile a project belongs to.

    Read-only: never mutates projects.json, safe to call as often as needed.
    """
    from api.profiles import get_active_profile_name, _is_root_profile

    active = get_active_profile_name() or 'default'
    reserved = {CRON_PROJECT_NAME, WEBHOOK_PROJECT_NAME}
    for p in load_projects():
        if p.get('name') in reserved:
            continue
        row_profile = p.get('profile')
        if row_profile == active:
            return True
        if _is_root_profile(row_profile or 'default') and _is_root_profile(active):
            return True
    return False


def is_cron_session(session_id: str, source_tag: str | None = None) -> bool:
    """Return True if a session originates from a cron job."""
    if source_tag == 'cron':
        return True
    sid = str(session_id or '')
    return sid.startswith('cron_')


def is_webhook_session(session_id: str, source_tag: str | None = None) -> bool:
    """Return True if a session originates from a webhook route."""
    return str(source_tag or '').strip().lower() == 'webhook'



def import_cli_session(
    session_id: str,
    title: str,
    messages,
    model: str='unknown',
    profile=None,
    created_at=None,
    updated_at=None,
    parent_session_id=None,
):
    """Create a new WebUI session populated with CLI/agent messages.

    Preserve parent_session_id from state.db so imported continuation segments
    keep their lineage in the WebUI store and sidebar instead of reappearing as
    detached orphan chats.
    """
    s = Session(
        session_id=session_id,
        title=title,
        workspace=get_last_workspace(),
        model=model,
        messages=messages,
        profile=profile,
        created_at=created_at,
        updated_at=updated_at,
        parent_session_id=parent_session_id,
    )
    # #4985: import_cli_session uses an explicit sid (the CLI sidecar's id).
    # If that sid was previously tombstoned as a webui zero-message orphan,
    # clear the tombstone entry so the freshly-imported session is visible
    # on the next poll. Wrapped because a tombstone failure must never block
    # an import.
    try:
        _clear_webui_zero_message_orphan_tombstone(s.session_id)
        _clear_webui_deleted_session_tombstone(s.session_id)
    except Exception:
        logger.debug(
            "Failed to clear webui tombstone for %s",
            s.session_id,
            exc_info=True,
        )
    s.save(touch_updated_at=False)
    return s


# ── CLI session bridge ──────────────────────────────────────────────────────

CLAUDE_CODE_SOURCE = 'claude_code'
CLAUDE_CODE_SOURCE_LABEL = 'Claude Code'
CLAUDE_CODE_MAX_FILES = 200
CLAUDE_CODE_MAX_FILE_BYTES = 10 * 1024 * 1024
CLAUDE_CODE_MAX_MESSAGES_PER_FILE = 1000
CLAUDE_CODE_MAX_CONTENT_CHARS = 200_000


def _normalize_cli_session_source_filter(source_filter) -> str | None:
    normalized = str(source_filter or '').strip().lower()
    if not normalized or normalized in {'all', 'any', '*'}:
        return None
    if normalized == 'claude-code':
        return CLAUDE_CODE_SOURCE
    return normalized


def _default_claude_code_projects_dir() -> Path | None:
    """Resolve the Claude Code projects directory without touching real home in tests."""
    override = os.getenv('HERMES_WEBUI_CLAUDE_PROJECTS_DIR')
    if override:
        return Path(override).expanduser()
    if os.getenv('HERMES_WEBUI_TEST_STATE_DIR'):
        return None
    return Path.home() / '.claude' / 'projects'


def _claude_code_session_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.expanduser().resolve()).encode('utf-8')).hexdigest()[:24]
    return f'{CLAUDE_CODE_SOURCE}_{digest}'


def _parse_claude_code_timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _extract_claude_code_text(content) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content[:CLAUDE_CODE_MAX_CONTENT_CHARS]
    if isinstance(content, list):
        parts = []
        used = 0
        for item in content:
            text = ''
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content') or ''
            if not text:
                continue
            text = str(text)
            remaining = CLAUDE_CODE_MAX_CONTENT_CHARS - used
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            used += len(parts[-1])
        return '\n'.join(parts)
    if isinstance(content, dict):
        return _extract_claude_code_text(content.get('text') or content.get('content'))
    return str(content)[:CLAUDE_CODE_MAX_CONTENT_CHARS]


def _parse_claude_code_jsonl(path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE) -> tuple[list[dict], str | None, float | None, float | None]:
    messages: list[dict] = []
    summary_title = None
    first_ts = None
    last_ts = None
    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if len(messages) >= max_messages:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                if not summary_title:
                    summary = raw.get('summary') or raw.get('title')
                    if isinstance(summary, str) and summary.strip():
                        summary_title = ' '.join(summary.split())[:80]
                records = raw.get('messages') if isinstance(raw.get('messages'), list) else None
                if records is None:
                    records = [raw.get('message') if isinstance(raw.get('message'), dict) else raw]
                for record in records:
                    if len(messages) >= max_messages:
                        break
                    if not isinstance(record, dict):
                        continue
                    msg = record.get('message') if isinstance(record.get('message'), dict) else record
                    role = str(msg.get('role') or record.get('role') or raw.get('role') or raw.get('type') or '').strip().lower()
                    if role == 'human':
                        role = 'user'
                    if role not in {'user', 'assistant', 'system', 'tool'}:
                        continue
                    content = _extract_claude_code_text(msg.get('content') if 'content' in msg else record.get('content'))
                    if not content.strip():
                        continue
                    ts = _parse_claude_code_timestamp(
                        msg.get('timestamp')
                        or record.get('timestamp')
                        or raw.get('timestamp')
                        or raw.get('created_at')
                    )
                    if ts is not None:
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    item = {'role': role, 'content': content}
                    if ts is not None:
                        item['timestamp'] = ts
                    messages.append(item)
    except Exception:
        return [], None, None, None
    return messages, summary_title, first_ts, last_ts


def _parse_claude_code_jsonl_cached(
    path: Path, *, max_messages: int = CLAUDE_CODE_MAX_MESSAGES_PER_FILE
) -> tuple[list[dict], str | None, float | None, float | None]:
    """``_parse_claude_code_jsonl`` memoized by the file's (path, mtime_ns, size, ctime_ns).

    The transcript files under ``~/.claude/projects`` are global and rarely
    change between sidebar builds, but parsing them dominates the cold
    /api/sessions latency (and repeats on every profile switch). Caching the
    parse result keyed by the file's stat signature collapses the warm cost to a
    single ``os.stat`` per file. A genuine append/edit bumps ``mtime_ns``/``size``
    /``ctime_ns`` and misses the cache, so staleness is impossible without
    re-parsing.

    ``max_messages`` is part of the key so a caller asking for a different cap
    never reads a result truncated to a smaller one.
    """
    try:
        st = path.stat()
        # Key on mtime_ns + size + ctime_ns: size is the strong discriminator for
        # append-only JSONL (any write changes it), and ctime_ns guards the rare
        # same-size, same-mtime in-place edit so a content change can never serve
        # a stale parse. A spurious ctime bump only costs one harmless re-parse.
        key = (str(path), st.st_mtime_ns, st.st_size, st.st_ctime_ns, int(max_messages))
    except OSError:
        # Can't stat -> fall back to a direct (uncached) parse; it will also
        # likely fail and return the empty tuple, matching prior behavior.
        return _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        hit = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if hit is not None:
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            messages, summary_title, first_ts, last_ts = hit
            # Return a shallow copy of the message list so a caller mutating it
            # can't corrupt the cached entry; the per-message dicts are treated
            # as read-only by all current callers.
            return list(messages), summary_title, first_ts, last_ts

    parsed = _parse_claude_code_jsonl(path, max_messages=max_messages)

    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        existing = _CLAUDE_CODE_PARSE_CACHE.get(key)
        if existing is None:
            _CLAUDE_CODE_PARSE_CACHE[key] = parsed
            _CLAUDE_CODE_PARSE_CACHE.move_to_end(key)
            while len(_CLAUDE_CODE_PARSE_CACHE) > _CLAUDE_CODE_PARSE_CACHE_MAX:
                _CLAUDE_CODE_PARSE_CACHE.popitem(last=False)
    messages, summary_title, first_ts, last_ts = parsed
    return list(messages), summary_title, first_ts, last_ts


def clear_claude_code_parse_cache() -> None:
    """Drop all memoized Claude Code transcript parses (test/lifecycle hook)."""
    with _CLAUDE_CODE_PARSE_CACHE_LOCK:
        _CLAUDE_CODE_PARSE_CACHE.clear()


def _iter_claude_code_jsonl_files(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES):
    root = Path(projects_dir).expanduser() if projects_dir is not None else _default_claude_code_projects_dir()
    if root is None:
        return
    try:
        if root.is_symlink():
            return
        root = root.resolve(strict=False)
        if not root.exists() or not root.is_dir():
            return
        yielded = 0
        for project_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if yielded >= max_files:
                return
            try:
                if project_dir.is_symlink() or not project_dir.is_dir():
                    continue
                for path in sorted(project_dir.iterdir(), key=lambda p: p.name):
                    if yielded >= max_files:
                        return
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.jsonl':
                        continue
                    try:
                        if path.stat().st_size > max_file_bytes:
                            continue
                    except OSError:
                        continue
                    yielded += 1
                    yield path
            except OSError:
                continue
    except OSError:
        return


def _claude_code_title(messages: list[dict], summary_title: str | None) -> str:
    if summary_title:
        return summary_title
    for msg in messages:
        if msg.get('role') == 'user':
            text = ' '.join(str(msg.get('content') or '').split())
            if text:
                return text[:80]
    return 'Claude Code Session'


def get_claude_code_sessions(projects_dir: Path | str | None = None, *, max_files: int = CLAUDE_CODE_MAX_FILES, max_file_bytes: int = CLAUDE_CODE_MAX_FILE_BYTES) -> list:
    """Read Claude Code JSONL sessions as read-only external-agent rows.

    The bridge is additive and defensive: it skips symlinks, oversized files,
    malformed lines, and per-file errors rather than crashing WebUI session
    listing. Tests pass ``projects_dir`` fixtures so Michael's real ~/.claude is
    never read during test runs.
    """
    sessions = []
    # ``get_last_workspace()`` is loop-invariant (the same active workspace for
    # every Claude Code row) but internally stats config.yaml + probes terminal
    # cwd, so calling it once per row was ~200 redundant stat()s on the cold
    # sidebar build (#4718). Resolve it a single time.
    cc_workspace = str(get_last_workspace())
    for path in _iter_claude_code_jsonl_files(projects_dir, max_files=max_files, max_file_bytes=max_file_bytes) or []:
        messages, summary_title, first_ts, last_ts = _parse_claude_code_jsonl_cached(path)
        if not messages:
            continue
        sid = _claude_code_session_id(path)
        # Match the truthiness fallback used in the assignments below: the old
        # inline code was ``first_ts or last_ts or path.stat().st_mtime``, which
        # also fell back to mtime for a falsy-but-not-None ``0.0`` timestamp
        # (epoch-0 / 1970 transcripts). An identity (``is None``) guard would
        # leave those rows with ``None`` instead of the file mtime, so use the
        # same ``not`` test the assignments use to stay bug-for-bug compatible.
        if not first_ts and not last_ts:
            try:
                _mtime = path.stat().st_mtime
            except OSError:
                _mtime = 0.0
        else:
            _mtime = None
        created_at = first_ts or last_ts or _mtime
        updated_at = last_ts or first_ts or _mtime
        sessions.append({
            'session_id': sid,
            'title': _claude_code_title(messages, summary_title),
            'workspace': cc_workspace,
            'model': 'claude-code',
            'message_count': len(messages),
            'created_at': created_at,
            'updated_at': updated_at,
            'last_message_at': updated_at,
            'pinned': False,
            'archived': False,
            'project_id': None,
            'profile': None,
            'source_tag': CLAUDE_CODE_SOURCE,
            'raw_source': CLAUDE_CODE_SOURCE,
            'session_source': 'external_agent',
            'source_label': CLAUDE_CODE_SOURCE_LABEL,
            'is_cli_session': True,
            'read_only': True,
        })
    sessions.sort(key=lambda s: s.get('last_message_at') or s.get('updated_at') or 0, reverse=True)
    return sessions


def get_claude_code_session_messages(sid, projects_dir: Path | str | None = None) -> list:
    """Return messages for one read-only Claude Code JSONL session."""
    sid = str(sid or '')
    if not sid.startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return []
    for path in _iter_claude_code_jsonl_files(projects_dir) or []:
        if _claude_code_session_id(path) != sid:
            continue
        messages, _summary_title, _first_ts, _last_ts = _parse_claude_code_jsonl_cached(path)
        return messages
    return []


def clear_cli_sessions_cache() -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        global _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        _CLI_SESSIONS_CACHE_INVALIDATION_VERSION += 1
        _CLI_SESSIONS_CACHE.clear()
    # The sidecar-metadata projection cache is stat-keyed (self-invalidating on
    # any file change), but clear it alongside the CLI cache so an explicit
    # reset — a mutating sidebar action or test isolation — starts fully cold.
    clear_sidecar_metadata_cache()


def _copy_cli_sessions(sessions: list) -> list:
    return copy.deepcopy(sessions)


def _cli_sessions_cache_invalidation_stamp() -> int:
    with _CLI_SESSIONS_CACHE_LOCK:
        return int(_CLI_SESSIONS_CACHE_INVALIDATION_VERSION)


def _cli_sessions_cache_claim_rebuild(cache_key: tuple) -> tuple[threading.Event, bool]:
    with _CLI_SESSIONS_CACHE_LOCK:
        current = _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key)
        if current is not None:
            return current, False
        event = threading.Event()
        _CLI_SESSIONS_CACHE_INFLIGHT[cache_key] = event
        return event, True


def _cli_sessions_cache_done(cache_key: tuple, event: threading.Event | None) -> None:
    with _CLI_SESSIONS_CACHE_LOCK:
        if event is None:
            return
        if _CLI_SESSIONS_CACHE_INFLIGHT.get(cache_key) is event:
            _CLI_SESSIONS_CACHE_INFLIGHT.pop(cache_key, None)
    if event is not None:
        event.set()


def _cache_cli_sessions_if_current(
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    sessions: list,
) -> bool:
    with _CLI_SESSIONS_CACHE_LOCK:
        if _CLI_SESSIONS_CACHE_INVALIDATION_VERSION != invalidation_stamp:
            return False
        _CLI_SESSIONS_CACHE[cache_key] = (
            time.monotonic() + ttl,
            invalidation_stamp,
            _copy_cli_sessions(sessions),
        )
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        while len(_CLI_SESSIONS_CACHE) > _CLI_SESSIONS_CACHE_MAX_ENTRIES:
            _CLI_SESSIONS_CACHE.popitem(last=False)
    return True


def _copy_fresh_cli_sessions_cache_entry(cache_key: tuple):
    with _CLI_SESSIONS_CACHE_LOCK:
        cached_entry = _CLI_SESSIONS_CACHE.get(cache_key)
        if cached_entry is None:
            return None
        if len(cached_entry) == 3:
            cached_expires_at, cached_stamp, cached_sessions = cached_entry
        else:
            cached_expires_at, cached_sessions = cached_entry
            cached_stamp = _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
        if cached_stamp != _CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
            _CLI_SESSIONS_CACHE.pop(cache_key, None)
            return None
        if cached_expires_at <= time.monotonic():
            return None
        # LRU: a fresh hit is the most-recently-used entry.
        _CLI_SESSIONS_CACHE.move_to_end(cache_key)
        return _copy_cli_sessions(cached_sessions)


def _load_and_cache_cli_sessions(
    *,
    cache_key: tuple,
    ttl: float,
    invalidation_stamp: int,
    load_sessions,
    stale_sessions,
    stale_stamp,
    all_profiles: bool,
    db_path,
) -> list:
    try:
        sessions = load_sessions()
    except Exception as _cli_err:
        logger.warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            "all profiles" if all_profiles else db_path, _cli_err,
        )
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        return []
    _cache_cli_sessions_if_current(
        cache_key,
        ttl,
        invalidation_stamp,
        sessions,
    )
    return _copy_cli_sessions(sessions)


def _reload_cli_sessions_after_inflight(
    *,
    cache_key: tuple,
    ttl: float,
    stale_sessions,
    stale_stamp,
    load_sessions,
    all_profiles: bool,
    db_path: str,
) -> list:
    while True:
        event, is_owner = _cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            break
        wait_finished = False
        try:
            wait_finished = bool(
                event.wait(
                    _CLI_SESSIONS_CACHE_STALE_WAIT_SECONDS
                    if stale_sessions is not None
                    else _CLI_SESSIONS_CACHE_WAIT_SECONDS
                )
            )
        except Exception:
            pass
        cached_sessions = _copy_fresh_cli_sessions_cache_entry(cache_key)
        if cached_sessions is not None:
            return cached_sessions
        if stale_sessions is not None and stale_stamp == _cli_sessions_cache_invalidation_stamp():
            return stale_sessions
        if not wait_finished:
            fallback_invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
            return _load_and_cache_cli_sessions(
                cache_key=cache_key,
                ttl=ttl,
                invalidation_stamp=fallback_invalidation_stamp,
                load_sessions=load_sessions,
                stale_sessions=stale_sessions,
                stale_stamp=stale_stamp,
                all_profiles=all_profiles,
                db_path=db_path,
            )
    try:
        invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
        return _load_and_cache_cli_sessions(
            cache_key=cache_key,
            ttl=ttl,
            invalidation_stamp=invalidation_stamp,
            load_sessions=load_sessions,
            stale_sessions=stale_sessions,
            stale_stamp=stale_stamp,
            all_profiles=all_profiles,
            db_path=db_path,
        )
    finally:
        _cli_sessions_cache_done(cache_key, event)


def _cli_sessions_cache_ttl_seconds() -> float:
    # #4842: widen the freshness window while a turn is streaming so the fixed
    # streaming poll cadence doesn't force a rebuild on every poll. Paired
    # with the streaming-freeze cache key (so the key is stable across polls
    # mid-stream), this bounds the heavy CLI/cron projection to one rebuild per
    # streaming-TTL window instead of one per poll. Mirrors the route-level
    # #4808 TTL widening.
    try:
        if _cli_sessions_streaming_freeze_marker() is not None:
            return max(0.0, float(_CLI_SESSIONS_CACHE_STREAMING_TTL_SECONDS))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(_CLI_SESSIONS_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return 5.0


def _path_cache_key(path) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except Exception:
        return str(path)


def _path_stat_cache_key(path):
    if path is None:
        return None
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _callable_accepts_include_claude_code(callable_obj) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    if 'include_claude_code' in signature.parameters:
        return True
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _sqlite_content_fingerprint(db_path: Path):
    """Return a commit-reliable content fingerprint for a state.db.

    The stat-only key below (mtime_ns + size of the .db/-wal/-shm files) is NOT
    reliable for cache invalidation: in WAL mode a commit lands in the -wal file,
    and under fast sequential writes the (mtime_ns, size) of the sidecars can
    COLLIDE with a previously cached stamp (same nanosecond bucket + a WAL frame
    that lands at the same offset/size after a prior checkpoint truncation), so a
    freshly-committed gateway/CLI session is intermittently served from the stale
    Python cache. PRAGMA data_version does NOT help here either — read from a
    fresh per-request connection it always reports that connection's own initial
    value and never advances (verified). A cheap content fingerprint over the
    sessions/messages tables, read on a fresh connection, DOES advance on every
    commit (incl. external gateway writes) and is immune to mtime granularity.
    Cost is a pair of indexed COUNT/MAX queries (sub-ms), far cheaper than the
    full uncached session scan this key gates.
    """
    try:
        if not Path(db_path).exists():
            return None
    except OSError:
        return None
    try:
        import sqlite3
        # Read-only + a tiny busy timeout: a fingerprint read must NEVER stall the
        # /api/sessions hot path when state.db is briefly locked by a writer.
        # On lock (or any error) we return None and the caller falls back to the
        # cheap file-stat stamp, so correctness degrades gracefully to the prior
        # behavior rather than blocking for the default multi-second busy timeout.
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=0.05
            )
        except Exception:
            return None
        try:
            conn.execute("PRAGMA busy_timeout=50")
            parts = []
            for table in ("sessions", "messages"):
                try:
                    # MAX(rowid) is an O(1) index lookup (no table scan) and
                    # advances on every INSERT. Pair it with the table's largest
                    # rowid + a count-free total: we deliberately avoid COUNT(*)
                    # which forces a full SCAN on large messages tables (~tens of
                    # ms per sidebar refresh on a big store). MAX(rowid) misses a
                    # pure DELETE-without-insert, but the file-stat fallback in
                    # _sqlite_file_stat_cache_key still moves on a delete commit,
                    # and a delete never makes a MISSING row appear (the flake we
                    # fix is an ADDED row not showing up). It also misses a plain
                    # `UPDATE sessions SET title/message_count` with no message
                    # insert (state_sync.py sync) — those fall back to the stat
                    # stamp + 5s TTL, i.e. the prior behavior (a title-only rename
                    # can lag <=5s); no regression vs the old stat-only key.
                    row = conn.execute(
                        f"SELECT MAX(rowid) FROM {table}"
                    ).fetchone()
                    parts.append(row[0] if row else None)
                except Exception:
                    parts.append(None)
            return tuple(parts)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return None


def _sqlite_file_stat_cache_key(db_path: Path):
    """Return a commit-reliable invalidation key for a SQLite DB.

    Combines a content fingerprint (the authoritative signal — advances on every
    commit, immune to mtime-granularity collisions that flaked the gateway_sync
    test) with the cheap file stat stamps as a belt-and-suspenders fallback for
    the case where the fingerprint can't be read.
    """
    return (
        _sqlite_content_fingerprint(db_path),
        _path_stat_cache_key(db_path),
        _path_stat_cache_key(Path(f"{db_path}-wal")),
        _path_stat_cache_key(Path(f"{db_path}-shm")),
    )


def _cli_sessions_streaming_freeze_marker():
    """Return a stable cache-key marker while any turn is actively streaming.

    The CLI/cron sidebar projection (``_load_cli_sessions_uncached``) is gated by
    ``_CLI_SESSIONS_CACHE``, whose key folds in ``_sqlite_file_stat_cache_key`` →
    ``_sqlite_content_fingerprint`` (``MAX(rowid) FROM messages``). During an
    active chat turn the gateway/CLI writes a message row per streamed delta, so
    that fingerprint advances on essentially every ``/api/sessions`` poll — busting
    the CLI cache and re-running the expensive candidate-join + projection (and the
    lineage-metadata pass) on every poll, while contending for the same SQLite/global
    lock the streaming worker holds. That is the multi-second ``get_cli_sessions``
    in #4842 (and #4672/#4808).

    The route-level session-list cache already freezes its own key during streaming
    (#4808 ``_session_list_cache_streaming_freeze_marker``), but that freeze never
    reached this *inner* CLI-sessions cache, so the heavy CLI/cron query still
    re-ran whenever the outer cache validated. This marker mirrors the route-level
    one: keyed only on the *set* of active stream ids, it is constant while the same
    turn(s) stream (so the projection is reused across polls) and changes the instant
    a stream starts/stops (so the just-finished turn's rows are picked up promptly).
    A streaming session's own CLI/cron title/count is not what this projection
    returns (the streaming session is overlaid live by the route layer), and any
    in-app structural mutation invalidates the cache directly via
    ``clear_cli_sessions_cache``. Externally-driven changes that don't fire that
    listener (a scheduled cron completing, an external CLI writing rows) surface
    after the streaming TTL expires and a subsequent refresh occurs rather than
    instantly — a bounded, self-healing lag that is the deliberate latency/CPU
    trade-off of the freeze. (#4842)
    """
    try:
        active = _active_stream_ids()
    except Exception:
        return None
    if not active:
        return None
    try:
        return ("streaming", tuple(sorted(str(x) for x in active)))
    except Exception:
        return ("streaming",)


def _resolve_cli_sessions_context(source_filter=None, include_claude_code: bool = True):
    # Use the active WebUI profile's HERMES_HOME to find state.db.
    # The active profile is determined by what the user has selected in the UI
    # (stored in the server's runtime config). This means:
    #   - default profile  -> ~/.hermes/state.db
    #   - named profile X  -> ~/.hermes/profiles/X/state.db
    # We resolve the active profile's home directory rather than just using
    # HERMES_HOME (which is the server's launch profile, not necessarily the
    # active one after a profile switch).
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()

    try:
        from api.profiles import get_active_profile_name
        cli_profile = get_active_profile_name()
    except Exception:
        cli_profile = None

    db_path = hermes_home / 'state.db'
    projects_dir = _default_claude_code_projects_dir()
    # #4842: while a turn streams, freeze the volatile state.db component of the
    # key so per-message writes don't bust the CLI cache and re-run the heavy
    # CLI/cron projection on every poll (mirrors the route-level #4808 freeze).
    # The wider streaming TTL in get_cli_sessions() still permits a periodic
    # rebuild after that window, and structural mutations invalidate via
    # clear_cli_sessions_cache().
    _streaming_marker = _cli_sessions_streaming_freeze_marker()
    db_state_key = _streaming_marker if _streaming_marker is not None else _sqlite_file_stat_cache_key(db_path)
    cache_key = (
        str(hermes_home),
        str(cli_profile or ''),
        str(db_path),
        str(source_filter or ''),
        db_state_key,
        bool(include_claude_code),
        _path_cache_key(projects_dir),
        _path_stat_cache_key(projects_dir),
        _path_stat_cache_key(SESSION_INDEX_FILE),
    )
    return hermes_home, db_path, cli_profile, cache_key


def _all_profiles_cli_contexts() -> tuple[list[tuple[Path, Path, str | None]], tuple]:
    """Return per-profile CLI scan contexts plus a cache key fragment."""
    try:
        from api.profiles import (
            _profiles_root,
            get_active_profile_name,
            get_hermes_home_for_profile,
            list_profiles_api,
        )
    except Exception:
        return [], ()

    contexts: list[tuple[Path, Path, str | None]] = []
    cache_entries: list[tuple[str, str, object]] = []
    seen_homes: set[str] = set()

    def _add_context(profile_name) -> None:
        try:
            hermes_home = Path(get_hermes_home_for_profile(profile_name)).expanduser().resolve()
        except Exception:
            return
        home_key = _path_cache_key(hermes_home)
        if not home_key or home_key in seen_homes:
            return
        seen_homes.add(home_key)
        db_path = hermes_home / 'state.db'
        profile_value = str(profile_name or 'default').strip() or 'default'
        contexts.append((hermes_home, db_path, profile_value))
        cache_entries.append((home_key, profile_value, _sqlite_file_stat_cache_key(db_path)))

    try:
        _add_context(get_active_profile_name())
    except Exception:
        pass
    try:
        for row in list_profiles_api():
            if not isinstance(row, dict):
                continue
            _add_context(row.get('name'))
    except Exception:
        logger.debug("All-profiles CLI context enumeration failed", exc_info=True)
    try:
        for entry in _profiles_root().iterdir():
            if not entry.is_dir():
                continue
            _add_context(entry.name)
    except Exception:
        logger.debug("All-profiles CLI directory enumeration failed", exc_info=True)

    return contexts, tuple(cache_entries)


def clear_sidecar_metadata_cache() -> None:
    """Drop all memoized sidebar-projection sidecar metadata (test/lifecycle hook)."""
    with _SIDECAR_METADATA_CACHE_LOCK:
        _SIDECAR_METADATA_CACHE.clear()


def _state_projection_sidecar_metadata(sid: str) -> dict:
    """Return UI-owned metadata (title + archived) for a state.db-projected row.

    Memoized by the sidecar file's (path, mtime_ns, size, ctime_ns) stat
    signature so the sidebar projection — which calls this once per row in both
    the visible pass and the up-to-200-row cron pass — pays a single os.stat per
    file on a warm build instead of an open() + 64KB read + JSON-key scan
    (#4842). A rename/archive/edit bumps the signature and invalidates just that
    entry, so a stale title/archived flag is impossible without re-reading.
    Returns a COPY so callers can't mutate the cached dict.

    NOTE: this stat-gates on ``SESSION_DIR / f'{sid}.json'`` because that file is
    ``Session.load_metadata_only``'s sole source for title+archived. If that ever
    stops being true (metadata moves to another store), this gate would short-
    circuit before the real source — update both together.
    """
    default = {"title": None, "archived": False}
    if not is_safe_session_id(sid):
        return dict(default)
    p = SESSION_DIR / f'{sid}.json'
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size, st.st_ctime_ns)
    except OSError:
        # No sidecar file (the common case for a pure state.db row) or it
        # vanished mid-build — nothing to project, and nothing worth caching.
        return dict(default)

    with _SIDECAR_METADATA_CACHE_LOCK:
        hit = _SIDECAR_METADATA_CACHE.get(key)
        if hit is not None:
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            return dict(hit)

    metadata = dict(default)
    try:
        webui_meta = Session.load_metadata_only(sid)
    except Exception:
        webui_meta = None
    if webui_meta:
        title = getattr(webui_meta, 'title', None)
        if title:
            metadata["title"] = title
        metadata["archived"] = bool(getattr(webui_meta, 'archived', False))

    with _SIDECAR_METADATA_CACHE_LOCK:
        # Re-check under lock in case a concurrent build populated it; either
        # entry is equally valid for the same stat signature.
        if key not in _SIDECAR_METADATA_CACHE:
            _SIDECAR_METADATA_CACHE[key] = metadata
            _SIDECAR_METADATA_CACHE.move_to_end(key)
            while len(_SIDECAR_METADATA_CACHE) > _SIDECAR_METADATA_CACHE_MAX:
                _SIDECAR_METADATA_CACHE.popitem(last=False)
    return dict(metadata)


def _load_cli_sessions_uncached(
    hermes_home: Path,
    db_path: Path,
    _cli_profile,
    source_filter=None,
    *,
    visible_session_limit: int | None = None,
    cron_project_limit: int | None | bool = CRON_PROJECT_CHIP_LIMIT,
    webhook_project_limit: int | None | bool = WEBHOOK_PROJECT_CHIP_LIMIT,
    include_claude_code: bool = True,
) -> list:
    cli_sessions = []
    if source_filter in (None, CLAUDE_CODE_SOURCE) and include_claude_code:
        try:
            cli_sessions.extend(get_claude_code_sessions())
        except Exception:
            logger.debug("Claude Code session scan failed", exc_info=True)

    if source_filter == CLAUDE_CODE_SOURCE:
        return cli_sessions


    if not db_path.exists():
        return cli_sessions

    # Memoize the cron project ID for this scan so we don't pay a lock-acquire +
    # disk-read of projects.json per cron session in the loop below.
    # Resolved lazily on the first cron session we encounter.
    # [resolved, project_id_or_None] — a plain `[None]` sentinel can't tell
    # "not yet resolved" apart from "resolved to None" (the gated-closed
    # case), which would re-pay the load_projects() read on every cron row
    # in a cron-heavy zero-user-project scan — the exact I/O blowup #4842
    # fixed, reintroduced by this gate if left as a bare None check.
    _cron_pid_cache: list = [False, None]
    def _cron_pid():
        if not _cron_pid_cache[0]:
            _cron_pid_cache[0] = True
            _cron_pid_cache[1] = ensure_cron_project(create=_profile_has_user_projects())
        return _cron_pid_cache[1]

    # Memoize the cron jobs.json job_id -> name map for this scan. The two row
    # loops below each looked up a cron job's friendly name by re-reading and
    # re-parsing hermes_home/cron/jobs.json PER untitled cron row — up to ~200
    # full-file JSON parses on a cron-heavy profile (#4842). Parse it once,
    # lazily, on the first untitled cron row we hit. {} when absent/unreadable.
    _cron_job_names_cache: list = [None]  # list-as-cell; None = not yet resolved
    def _cron_job_names():
        if _cron_job_names_cache[0] is None:
            names: dict[str, str] = {}
            try:
                _jobs_path = hermes_home / 'cron' / 'jobs.json'
                if _jobs_path.exists():
                    _jobs_data = json.loads(_jobs_path.read_text(encoding='utf-8'))
                    for _j in _jobs_data.get('jobs', []):
                        _jid = _j.get('id')
                        _jname = _j.get('name')
                        if _jid and _jname:
                            names[str(_jid)] = _jname
            except Exception:
                pass  # degrade gracefully — fall back to the generic title
            _cron_job_names_cache[0] = names
        return _cron_job_names_cache[0]

    def _cron_title_from_jobs(sid: str):
        """Friendly cron job name for a cron_{job_id}_{ts} sid, or None."""
        if not sid.startswith('cron_'):
            return None
        parts = sid.split('_')
        if len(parts) < 3:
            return None
        return _cron_job_names().get(parts[1])

    # get_last_workspace() reads up to two files + an is_dir()/remote probe and
    # returns the SAME active workspace for every projected row, so calling it
    # per row was redundant I/O on the cold sidebar build (#4842; mirrors the
    # #4718 hoist on the Claude Code path). Resolve it once for this scan.
    _cli_workspace_cache: list = [None]  # list-as-cell; None = not yet resolved
    def _cli_workspace():
        if _cli_workspace_cache[0] is None:
            _cli_workspace_cache[0] = str(get_last_workspace())
        return _cli_workspace_cache[0]

    _webhook_pid_cache: list[str | None] = [None]
    def _webhook_pid():
        if _webhook_pid_cache[0] is None:
            _webhook_pid_cache[0] = ensure_webhook_project()
        return _webhook_pid_cache[0]

    def _state_row_project_id(sid: str, source: str | None) -> str | None:
        if is_cron_session(sid, source):
            return _cron_pid()
        if is_webhook_session(sid, source):
            return _webhook_pid()
        return None

    profile_value = _cli_profile or 'default'
    # A deleted WebUI session is tombstoned (see _record_webui_deleted_session_tombstone)
    # so recovery/audit/claim treat it as gone. The sidebar's own state.db projection
    # must honor the same tombstone, or a deleted WebUI session reappears here as an
    # "Agent" ghost the moment non-WebUI sessions are shown (#5498, second path). Only
    # suppress genuine WebUI rows with no live sidecar — a re-created/re-imported sid
    # (live {sid}.json) always beats a stale tombstone.
    try:
        _deleted_webui_tombstone = _load_webui_deleted_session_tombstone()
    except Exception:
        _deleted_webui_tombstone = frozenset()
    for row in read_importable_agent_session_rows(
        db_path,
        limit=visible_session_limit if visible_session_limit is not None else (
            CRON_PROJECT_CHIP_LIMIT if source_filter == 'cron'
            else WEBHOOK_PROJECT_CHIP_LIMIT if source_filter == 'webhook'
            else CLI_VISIBLE_SESSION_LIMIT
        ),
        log=logger,
        exclude_sources=("cron", "webhook") if source_filter is None else None,
        include_sources=None if source_filter is None else (source_filter,),
    ):
        sid = row['id']
        raw_ts = row['last_activity'] or row['started_at']
        # Prefer the CLI session's own profile from the DB; fall back to
        # the active CLI profile so sidebar filtering works either way.
        profile = profile_value  # CLI DB has no profile column; use active profile

        _source = row['source'] or 'cli'
        # Honor the deleted-WebUI tombstone: a WebUI row the user deleted must
        # not resurface in this projection (the #5498 ghost). Live sidecar wins.
        if (
            _source == 'webui'
            and sid in _deleted_webui_tombstone
            and not (SESSION_DIR / f"{sid}.json").exists()
        ):
            continue
        _source_meta = normalize_agent_session_source(_source)
        _title = row['title']
        if not _title and _source == 'cron':
            # Look up the human-friendly cron job name (cron_{job_id}_{ts}) from
            # the once-parsed jobs.json map instead of re-reading the file here.
            _title = _cron_title_from_jobs(sid) or _title
        # If a WebUI JSON file exists for this session (e.g. previously
        # imported or renamed in the sidebar), prefer its UI-owned metadata over
        # the state.db projection. This keeps archived cron/tool/API runs hidden
        # even when all_sessions() omits the hidden sidecar and the state row is
        # re-injected from Hermes state.db (#4397).
        _sidecar_meta = _state_projection_sidecar_metadata(sid)
        if _sidecar_meta.get('title'):
            _title = _sidecar_meta['title']
        _archived = bool(_sidecar_meta.get('archived'))
        _display_title = _title or f'{_source.title()} Session'
        cli_sessions.append({
            'session_id': sid,
            'title': _display_title,
            'workspace': _cli_workspace(),
            'model': row['model'] or None,
            'message_count': row['message_count'] or row['actual_message_count'] or 0,
            'created_at': row['started_at'],
            'updated_at': raw_ts,
            'pinned': False,
            'archived': _archived,
            'project_id': _state_row_project_id(sid, _source),
            'profile': profile,
            'source_tag': _source,
            'raw_source': row.get('raw_source') or _source_meta.get('raw_source'),
            'user_id': row.get('user_id'),
            'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
            'chat_type': row.get('chat_type'),
            'thread_id': row.get('thread_id'),
            'session_key': row.get('session_key'),
            'platform': row.get('platform'),
            'session_source': row.get('session_source') or _source_meta.get('session_source'),
            'source_label': row.get('source_label') or _source_meta.get('source_label'),
            'parent_session_id': row.get('parent_session_id'),
            'parent_title': row.get('parent_title'),
            'parent_source': row.get('parent_source'),
            'relationship_type': row.get('relationship_type'),
            '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
            'end_reason': row.get('end_reason'),
            'actual_message_count': row.get('actual_message_count'),
            'user_message_count': row.get('actual_user_message_count'),
            '_lineage_root_id': row.get('_lineage_root_id'),
            '_lineage_tip_id': row.get('_lineage_tip_id'),
            '_compression_segment_count': row.get('_compression_segment_count'),
            'is_cli_session': is_cli_session_row({**row, **_source_meta}),
        })

    if source_filter is not None:
        return cli_sessions

    # --- Second pass: fetch cron sessions that may have been squeezed out
    # of the default window by more-recent non-cron sessions.
    # The normal sidebar query caps at CLI_VISIBLE_SESSION_LIMIT (20) rows;
    # once 20 newer sessions exist, older cron runs vanish from the payload
    # before _include_project_hidden_background_sidebar_sessions can rescue
    # them (#3172).  A separate, higher-capped cron-only pass ensures they
    # stay addressable under their project chip.
    if cron_project_limit is not False:
        existing_sids = {s['session_id'] for s in cli_sessions}
        try:
            for row in read_importable_agent_session_rows(
                db_path,
                limit=cron_project_limit,
                log=logger,
                exclude_sources=None,
                include_sources=("cron",),
            ):
                sid = row['id']
                if sid in existing_sids:
                    continue
                _source = row['source'] or 'cli'
                if _source != 'cron':
                    continue
                raw_ts = row['last_activity'] or row['started_at']
                _title = row['title']
                if not _title:
                    # Friendly cron job name from the once-parsed jobs.json map.
                    _title = _cron_title_from_jobs(sid) or _title
                _sidecar_meta = _state_projection_sidecar_metadata(sid)
                if _sidecar_meta.get('title'):
                    _title = _sidecar_meta['title']
                _archived = bool(_sidecar_meta.get('archived'))
                _display_title = _title or 'Cron Session'
                cli_sessions.append({
                    'session_id': sid,
                    'title': _display_title,
                    'workspace': _cli_workspace(),
                    'model': row['model'] or None,
                    'message_count': row['message_count'] or row['actual_message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': _archived,
                    'project_id': _cron_pid(),
                    'profile': profile_value,
                    'source_tag': 'cron',
                    'raw_source': row.get('raw_source'),
                    'user_id': row.get('user_id'),
                    'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
                    'chat_type': row.get('chat_type'),
                    'thread_id': row.get('thread_id'),
                    'session_key': row.get('session_key'),
                    'platform': row.get('platform'),
                    'session_source': row.get('session_source'),
                    'source_label': row.get('source_label'),
                    'parent_session_id': row.get('parent_session_id'),
                    'parent_title': row.get('parent_title'),
                    'parent_source': row.get('parent_source'),
                    'relationship_type': row.get('relationship_type'),
                    '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
                    'end_reason': row.get('end_reason'),
                    'actual_message_count': row.get('actual_message_count'),
                    'user_message_count': row.get('actual_user_message_count'),
                    '_lineage_root_id': row.get('_lineage_root_id'),
                    '_lineage_tip_id': row.get('_lineage_tip_id'),
                    '_compression_segment_count': row.get('_compression_segment_count'),
                    'is_cli_session': is_cli_session_row(row),
                })
                existing_sids.add(sid)
        except Exception:
            logger.debug("Cron project-chip second pass failed", exc_info=True)

    # --- Second pass: fetch webhook sessions that may have been squeezed out
    # of the default window. They stay hidden from the default sidebar but must
    # remain addressable under the Webhooks project chip.
    if webhook_project_limit is not False:
        existing_sids = {s['session_id'] for s in cli_sessions}
        try:
            for row in read_importable_agent_session_rows(
                db_path,
                limit=webhook_project_limit,
                log=logger,
                exclude_sources=None,
                include_sources=("webhook",),
            ):
                sid = row['id']
                if sid in existing_sids:
                    continue
                _source = row['source'] or 'webhook'
                if _source != 'webhook':
                    continue
                _source_meta = normalize_agent_session_source(_source)
                raw_ts = row['last_activity'] or row['started_at']
                _title = row['title']
                _sidecar_meta = _state_projection_sidecar_metadata(sid)
                if _sidecar_meta.get('title'):
                    _title = _sidecar_meta['title']
                _archived = bool(_sidecar_meta.get('archived'))
                _display_title = _title or 'Webhook Session'
                cli_sessions.append({
                    'session_id': sid,
                    'title': _display_title,
                    'workspace': str(get_last_workspace()),
                    'model': row['model'] or None,
                    'message_count': row['message_count'] or row['actual_message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': _archived,
                    'project_id': _webhook_pid(),
                    'profile': profile_value,
                    'source_tag': 'webhook',
                    'raw_source': row.get('raw_source') or _source_meta.get('raw_source'),
                    'user_id': row.get('user_id'),
                    'chat_id': row.get('chat_id') or row.get('origin_chat_id'),
                    'chat_type': row.get('chat_type'),
                    'thread_id': row.get('thread_id'),
                    'session_key': row.get('session_key'),
                    'platform': row.get('platform'),
                    'session_source': row.get('session_source') or _source_meta.get('session_source'),
                    'source_label': row.get('source_label') or _source_meta.get('source_label'),
                    'parent_session_id': row.get('parent_session_id'),
                    'parent_title': row.get('parent_title'),
                    'parent_source': row.get('parent_source'),
                    'relationship_type': row.get('relationship_type'),
                    '_parent_lineage_root_id': row.get('_parent_lineage_root_id'),
                    'end_reason': row.get('end_reason'),
                    'actual_message_count': row.get('actual_message_count'),
                    'user_message_count': row.get('actual_user_message_count'),
                    '_lineage_root_id': row.get('_lineage_root_id'),
                    '_lineage_tip_id': row.get('_lineage_tip_id'),
                    '_compression_segment_count': row.get('_compression_segment_count'),
                    'is_cli_session': is_cli_session_row({**row, **_source_meta}),
                })
                existing_sids.add(sid)
        except Exception:
            logger.debug("Webhook project-chip second pass failed", exc_info=True)

    return cli_sessions


def get_cli_sessions(
    source_filter=None,
    *,
    all_profiles: bool = False,
    include_claude_code: bool = True,
) -> list:
    """Read CLI sessions from the agent's SQLite store and return them as
    dicts in a format the WebUI sidebar can render alongside local sessions.

    Returns empty list if the SQLite DB is missing or any error occurs -- the
    bridge is purely additive and never crashes the WebUI.
    """
    source_filter = _normalize_cli_session_source_filter(source_filter)
    if all_profiles:
        contexts, context_cache_key = _all_profiles_cli_contexts()
        db_path = "all profiles"
        # #4842: freeze the volatile per-profile state.db component while
        # streaming so a streamed message row in one profile doesn't bust the
        # all-profiles CLI cache and re-run every profile's heavy projection.
        _streaming_marker = _cli_sessions_streaming_freeze_marker()
        if _streaming_marker is not None:
            context_cache_key = ('streaming-frozen', _streaming_marker)
        cache_key = (
            'all_profiles',
            source_filter or '',
            bool(include_claude_code),
            context_cache_key,
            _path_cache_key(_default_claude_code_projects_dir()),
            _path_stat_cache_key(_default_claude_code_projects_dir()),
            _path_stat_cache_key(SESSION_INDEX_FILE),
        )
    else:
        resolve_kwargs = {}
        resolve_supports_include_claude_code = _callable_accepts_include_claude_code(
            _resolve_cli_sessions_context
        )
        if resolve_supports_include_claude_code:
            resolve_kwargs['include_claude_code'] = include_claude_code
        hermes_home, db_path, cli_profile, cache_key = _resolve_cli_sessions_context(
            source_filter,
            **resolve_kwargs,
        )
        if not resolve_supports_include_claude_code:
            cache_key = cache_key + (bool(include_claude_code),)
    ttl = _cli_sessions_cache_ttl_seconds()
    now = time.monotonic()

    def _load_sessions():
        loader_supports_include_claude_code = _callable_accepts_include_claude_code(
            _load_cli_sessions_uncached
        )
        if all_profiles:
            merged: list[dict] = []
            for idx, (ctx_home, ctx_db_path, ctx_profile) in enumerate(contexts):
                load_kwargs = {
                    'source_filter': source_filter,
                    'visible_session_limit': None,
                    'cron_project_limit': None,
                    'webhook_project_limit': None,
                }
                if loader_supports_include_claude_code:
                    load_kwargs['include_claude_code'] = include_claude_code and idx == 0
                merged.extend(
                    _load_cli_sessions_uncached(
                        ctx_home,
                        ctx_db_path,
                        ctx_profile,
                        **load_kwargs,
                    )
                )
            return merged
        load_kwargs = {'source_filter': source_filter}
        if loader_supports_include_claude_code:
            load_kwargs['include_claude_code'] = include_claude_code
        return _load_cli_sessions_uncached(
            hermes_home,
            db_path,
            cli_profile,
            **load_kwargs,
        )

    if ttl > 0:
        stale_sessions = None
        stale_stamp = None
        with _CLI_SESSIONS_CACHE_LOCK:
            cached_entry = _CLI_SESSIONS_CACHE.get(cache_key)
            if cached_entry is not None:
                if len(cached_entry) == 3:
                    cached_expires_at, cached_stamp, cached_sessions = cached_entry
                else:
                    cached_expires_at, cached_sessions = cached_entry
                    cached_stamp = _CLI_SESSIONS_CACHE_INVALIDATION_VERSION
                if cached_stamp != _CLI_SESSIONS_CACHE_INVALIDATION_VERSION:
                    _CLI_SESSIONS_CACHE.pop(cache_key, None)
                elif cached_expires_at > now:
                    # LRU: a fresh hit is the most-recently-used entry.
                    _CLI_SESSIONS_CACHE.move_to_end(cache_key)
                    return _copy_cli_sessions(cached_sessions)
                else:
                    stale_sessions = _copy_cli_sessions(cached_sessions)
                    stale_stamp = cached_stamp
        event, is_owner = _cli_sessions_cache_claim_rebuild(cache_key)
        if is_owner:
            try:
                invalidation_stamp = _cli_sessions_cache_invalidation_stamp()
                return _load_and_cache_cli_sessions(
                    cache_key=cache_key,
                    ttl=ttl,
                    invalidation_stamp=invalidation_stamp,
                    load_sessions=_load_sessions,
                    stale_sessions=stale_sessions,
                    stale_stamp=stale_stamp,
                    all_profiles=all_profiles,
                    db_path=db_path,
                )
            finally:
                _cli_sessions_cache_done(cache_key, event)
        return _reload_cli_sessions_after_inflight(
            cache_key=cache_key,
            ttl=ttl,
            stale_sessions=stale_sessions,
            stale_stamp=stale_stamp,
            load_sessions=_load_sessions,
            all_profiles=all_profiles,
            db_path=db_path,
        )

    try:
        return _load_sessions()
    except Exception as _cli_err:
        logger.warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            "all profiles" if all_profiles else db_path, _cli_err,
        )
        return []

def _json_loads_if_string(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return value


def get_state_db_session_messages(
    sid,
    *,
    stitch_continuations: bool = False,
    profile=None,
    since_timestamp=None,
    include_inactive: bool = False,
    limit=None,
) -> list:
    """Read messages for a Hermes session from state.db.

    When *profile* is supplied, reads from that profile's state.db; otherwise
    falls back to the active profile's state.db.  This generic reader works for
    any session source, including WebUI-origin sessions that were later updated
    through another Hermes surface such as the Gateway API Server.  When
    ``stitch_continuations`` is true it preserves the historical CLI/external-agent
    behavior of walking compatible compression/close parent segments before reading
    messages.

    ``since_timestamp`` is an optional display-path optimization.  It limits the
    raw state.db scan to rows at or after a sidecar-derived timestamp floor while
    preserving the caller's normal merge/window logic.  Full-history callers must
    leave it unset.

    ``limit`` is an optional defensive row cap (applied after ORDER BY as a SQL
    LIMIT). It is a BACKSTOP against a pathological/huge state.db materializing
    unbounded rows into a Python list, NOT a semantic window: the display path
    counts visible rows post-reconciliation, so a true window LIMIT here would
    corrupt the sidecar/state.db merge (see _state_db_since_timestamp_for_limited_display,
    which deliberately does NOT SQL-LIMIT raw rows for that reason). Callers that
    need the full history for model-context reconstruction leave this unset.

    When the messages table exposes an ``active`` column, inactive rows are
    compacted/archived history and are intentionally excluded by default. WebUI
    reconciliation feeds this reader straight into the next model context; pulling
    ``active=0`` archive rows back in resurrects pre-compaction history and can
    make every later turn re-trigger compression. Pass ``include_inactive=True``
    only for explicit recovery/audit views.
    """
    try:
        import sqlite3
    except ImportError:
        return []

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return []

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            required = {'role', 'content', 'timestamp'}
            if not required.issubset(available):
                return []
            optional = [
                'tool_call_id',
                'tool_calls',
                'tool_name',
                'reasoning',
                'reasoning_details',
                'codex_reasoning_items',
                'reasoning_content',
                'codex_message_items',
            ]
            id_col = ['id'] if 'id' in available else []
            selected = id_col + ['role', 'content', 'timestamp'] + [c for c in optional if c in available]

            session_chain = [str(sid)]
            if stitch_continuations:
                cur.execute("PRAGMA table_info(sessions)")
                session_cols = {str(row['name']) for row in cur.fetchall()}
                if {'parent_session_id', 'end_reason', 'started_at', 'source'}.issubset(session_cols):
                    cur.execute(
                        """
                        SELECT id, source, started_at, parent_session_id, ended_at, end_reason
                        FROM sessions
                        WHERE id = ?
                        """,
                        (sid,),
                    )
                    rows_by_id = {}
                    row = cur.fetchone()
                    if row:
                        rows_by_id[str(row['id'])] = dict(row)
                        current_id = str(row['id'])
                        seen = {current_id}
                        for _ in range(20):
                            current = rows_by_id.get(current_id)
                            parent_id = current.get('parent_session_id') if current else None
                            if not parent_id or parent_id in seen:
                                break
                            cur.execute(
                                """
                                SELECT id, source, started_at, parent_session_id, ended_at, end_reason
                                FROM sessions
                                WHERE id = ?
                                """,
                                (parent_id,),
                            )
                            parent_row = cur.fetchone()
                            if not parent_row:
                                break
                            parent_dict = dict(parent_row)
                            rows_by_id[str(parent_row['id'])] = parent_dict
                            if not _is_continuation_session(parent_dict, current):
                                break
                            session_chain.insert(0, str(parent_row['id']))
                            current_id = str(parent_row['id'])
                            seen.add(current_id)

            placeholders = ', '.join('?' for _ in session_chain)
            params = list(session_chain)
            since_clause = ""
            if since_timestamp is not None:
                try:
                    since_ts = float(since_timestamp)
                except (TypeError, ValueError):
                    since_ts = None
                if since_ts is not None:
                    since_clause = " AND (timestamp IS NULL OR timestamp >= ?)"
                    params.append(since_ts)
            active_clause = ""
            if 'active' in available and not include_inactive:
                active_clause = " AND (active IS NULL OR active != 0)"
            # Defensive row cap (backstop only — see docstring). Applied as a
            # SQL LIMIT bound parameter (?) so the tail (newest) rows are
            # retained and a pathological state.db can't materialize unbounded
            # rows. None = unchanged full-history read for model-context callers.
            limit_clause = ""
            if limit is not None:
                try:
                    limit_int = max(1, int(limit))
                except (TypeError, ValueError):
                    limit_int = None
                if limit_int is not None:
                    # The query orders ASC (oldest first); to keep the NEWEST
                    # rows under the cap, take a descending-ordered subquery and
                    # re-sort ascending — a plain LIMIT would keep the oldest.
                    limit_clause = " ORDER BY timestamp DESC, id DESC LIMIT ?"
                    params.append(limit_int)
            if limit_clause:
                cur.execute(f"""
                    SELECT * FROM (
                        SELECT {', '.join(selected)}, session_id
                        FROM messages
                        WHERE session_id IN ({placeholders})
                        {since_clause}
                        {active_clause}
                        {limit_clause}
                    ) ORDER BY timestamp ASC, id ASC
                """, params)
            else:
                cur.execute(f"""
                    SELECT {', '.join(selected)}, session_id
                    FROM messages
                    WHERE session_id IN ({placeholders})
                    {since_clause}
                    {active_clause}
                    ORDER BY timestamp ASC, id ASC
                """, params)
            msgs = []
            for row in cur.fetchall():
                msg = {
                    'role': row['role'],
                    'content': row['content'],
                    'timestamp': row['timestamp'],
                }
                for col in optional:
                    if col not in row.keys():
                        continue
                    value = row[col]
                    if value in (None, ''):
                        continue
                    if col in {'tool_calls', 'reasoning_details', 'codex_reasoning_items', 'codex_message_items'}:
                        value = _json_loads_if_string(value)
                    msg[col] = value
                if msg.get('role') == 'tool' and msg.get('tool_name') and not msg.get('name'):
                    msg['name'] = msg['tool_name']
                msgs.append(msg)
    except Exception:
        return []
    return msgs


def get_state_db_session_message_prefix_summary(
    sid,
    before_timestamp,
    *,
    profile=None,
) -> dict | None:
    """Return prefix timestamp counts, or ``None`` when they cannot be proven.

    The projection intentionally avoids message content and tool-call columns so
    callers can reject impossible prefix matches before materializing visible
    identities. Missing databases are an authoritative empty prefix and are not
    created by this read path.
    """
    try:
        import sqlite3
    except ImportError:
        return None

    if not sid:
        return None
    try:
        before_ts = float(before_timestamp)
    except (TypeError, ValueError):
        return None

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return {"count": 0, "null_timestamp_count": 0}

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if not {'session_id', 'timestamp'}.issubset(available):
                return None
            active_clause = ""
            if 'active' in available:
                active_clause = " AND (active IS NULL OR active != 0)"
            cur.execute(
                f"""
                SELECT
                    COUNT(CASE
                        WHEN timestamp IS NOT NULL AND timestamp < ? THEN 1
                    END) AS count,
                    COUNT(CASE WHEN timestamp IS NULL THEN 1 END) AS null_timestamp_count
                FROM messages
                WHERE session_id = ?
                {active_clause}
                """,
                (before_ts, str(sid)),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "count": int(row["count"]),
                "null_timestamp_count": int(row["null_timestamp_count"]),
            }
    except Exception:
        return None


def get_state_db_session_message_keys_before_timestamp(
    sid,
    before_timestamp,
    *,
    profile=None,
) -> list[tuple] | None:
    """Return visible-identity keys before ``before_timestamp`` in DB order.

    Missing timestamps are intentionally excluded because the bounded reader
    keeps them with ``timestamp IS NULL OR timestamp >= ?``.  The caller uses
    this as a conservative prefix-identity guard before taking the optimized
    tail-read path, so schemas that cannot prove the merge-visible identity
    force a full read.
    """
    try:
        import sqlite3
    except ImportError:
        return None

    if not sid:
        return None
    try:
        before_ts = float(before_timestamp)
    except (TypeError, ValueError):
        return None

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not db_path.exists():
        return []

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if not {'id', 'session_id', 'role', 'content', 'timestamp', 'tool_calls'}.issubset(available):
                return None
            cur.execute(
                """
                SELECT
                    COALESCE(role, '') AS role,
                    COALESCE(content, '') AS content,
                    tool_calls
                FROM messages
                WHERE session_id = ? AND timestamp IS NOT NULL AND timestamp < ?
                ORDER BY timestamp ASC, id ASC
                """,
                (str(sid), before_ts),
            )
            return [
                _session_message_visible_key(
                    {
                        "role": row["role"],
                        "content": row["content"],
                        "tool_calls": _json_loads_if_string(row["tool_calls"]),
                    }
                )
                for row in cur.fetchall()
            ]
    except Exception:
        return None


def get_state_db_session_summary(sid, *, profile=None) -> dict:
    """Return a cheap message count/timestamp summary for one state.db session."""
    try:
        import sqlite3
    except ImportError:
        return {"message_count": 0, "last_message_at": 0.0}

    if isinstance(profile, str) and profile:
        db_path = _get_profile_home(profile) / 'state.db'
        if not db_path.exists():
            db_path = _active_state_db_path()
    else:
        db_path = _active_state_db_path()
    if not sid or not db_path.exists():
        return {"message_count": 0, "last_message_at": 0.0}

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(messages)")
            available = {str(row['name']) for row in cur.fetchall()}
            if 'session_id' not in available:
                return {"message_count": 0, "last_message_at": 0.0}
            if 'timestamp' in available:
                cur.execute(
                    "SELECT COUNT(*) AS message_count, MAX(timestamp) AS last_message_at "
                    "FROM messages WHERE session_id = ?",
                    (str(sid),),
                )
                row = cur.fetchone()
                if not row:
                    return {"message_count": 0, "last_message_at": 0.0}
                return {
                    "message_count": max(0, int(row["message_count"] or 0)),
                    "last_message_at": float(row["last_message_at"] or 0) if row["last_message_at"] is not None else 0.0,
                }
            cur.execute("SELECT COUNT(*) AS message_count FROM messages WHERE session_id = ?", (str(sid),))
            row = cur.fetchone()
            return {
                "message_count": max(0, int(row["message_count"] or 0)) if row else 0,
                "last_message_at": 0.0,
            }
    except Exception:
        return {"message_count": 0, "last_message_at": 0.0}


def _normalized_message_timestamp_for_key(value):
    if value is None or value == "":
        return ""
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Truncate to second-level granularity so that sub-second drift between
    # the sidecar JSON write and the state.db created_at write does not cause
    # the legacy dedup key to differ for the same logical message.
    return str(int(timestamp))


def _message_timestamp_as_float(msg):
    if not isinstance(msg, dict):
        return None
    value = msg.get("timestamp")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _session_message_merge_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    message_identity = msg.get("id") or msg.get("message_id")
    if message_identity:
        return ("message_id", str(message_identity))
    # Include tool_calls so assistant messages that invoke different tools
    # (but share identical empty content and same-second timestamp) are not
    # collapsed by the merge-key guard at line ~4216.  Without this,
    # all tool-calling messages map to the same legacy key and the
    # timestamp<=max_sidecar_timestamp blanket-skip at line ~4218 drops
    # every state.db tool-call after the first one registered by the sidecar.
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        "legacy",
        str(msg.get("role") or ""),
        str(msg.get("content") or ""),
        _normalized_message_timestamp_for_key(msg.get("timestamp")),
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
        _tc_key,
    )


def _session_messages_have_prefix(messages, prefix) -> bool:
    messages = list(messages or [])
    prefix = list(prefix or [])
    if len(prefix) > len(messages):
        return False
    for idx, expected in enumerate(prefix):
        if _session_message_merge_key(messages[idx]) != _session_message_merge_key(expected):
            return False
    return True


_SESSION_MESSAGE_DISPLAY_METADATA_KEYS = (
    "_turnDuration",
    "_turnTps",
    "_turnUsage",
    "_firstTokenMs",
    "_usedModel",
    "_gatewayRouting",
    "_statusCard",
    "_anchor_stream_id",
    "_anchor_activity_scene",
)


def _message_display_metadata_value_present(value) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (dict, list, tuple, set)) and not value:
        return False
    return True


def _merge_session_display_metadata(target: dict | None, source: dict | None) -> None:
    """Preserve display-only turn metadata when duplicate transcript rows merge."""
    if not isinstance(target, dict) or not isinstance(source, dict):
        return
    for key in _SESSION_MESSAGE_DISPLAY_METADATA_KEYS:
        if _message_display_metadata_value_present(target.get(key)):
            continue
        value = source.get(key)
        if _message_display_metadata_value_present(value):
            target[key] = copy.deepcopy(value)


def _session_message_dedup_key(msg: dict):
    """Like _session_message_merge_key but preserves full-precision timestamp.

    Two messages are true duplicates only if role, content, AND exact
    timestamp all match.  Sub-second timestamp differences indicate
    legitimately distinct messages (e.g. two assistant turns within the
    same wall-clock second).
    """
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    message_identity = msg.get("id") or msg.get("message_id")
    if message_identity:
        return ("message_id", str(message_identity))
    # Include tool_calls in the key so assistant messages that carry
    # different tool invocations (but identical empty content/timestamp)
    # are never collapsed into one.  (#3346 regression)
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        "legacy",
        str(msg.get("role") or ""),
        str(msg.get("content") or ""),
        str(msg.get("timestamp") or ""),
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
        _tc_key,
    )


def _normalized_session_message_content(msg: dict) -> str:
    if not isinstance(msg, dict):
        return repr(msg)
    return " ".join(str(msg.get("content") or "").split())


def _loose_session_message_content(value: str) -> str:
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def _session_message_content_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    role = str(msg.get("role") or "")
    content = _normalized_session_message_content(msg)
    if role == "user":
        # WebUI sends the model a workspace-prefixed user_message
        # ("[Workspace::v1: /path]\n<text>") while the visible/optimistic
        # bubble and the WebUI sidecar row carry only the bare "<text>". The
        # streaming dedup identity (_message_identity in api/streaming.py)
        # strips this prefix for user turns, so this reconciliation key must
        # do the same. Otherwise a state.db row (prefixed) and a sidecar row
        # (bare) key DIFFERENTLY, the alignment loop in
        # state_db_delta_after_context fails to match them, treats the
        # state.db copy as a NEW row, and appends a duplicate user turn. The
        # agent then merges the two adjacent user rows into a permanent
        # composite -- the post-restart stale-user-prepend bug (#5339). Reuse
        # the SAME helper as the streaming side (imported lazily to avoid a
        # circular import; api.streaming imports api.models at module load) so
        # the two dedup layers can't drift apart again.
        from api.streaming import _strip_workspace_prefix

        content = " ".join(
            _strip_workspace_prefix(content, include_legacy=True).split()
        )
    return (
        role,
        content,
        str(msg.get("tool_call_id") or ""),
        str(msg.get("tool_name") or msg.get("name") or ""),
    )


def _session_message_visible_key(msg: dict):
    if not isinstance(msg, dict):
        return ("non_dict", repr(msg))
    # Include tool_calls so assistant messages that invoke different tools
    # (but share identical empty content) are not collapsed by sidecar
    # prefix matching.  Without this, all tool-calling messages map to
    # ("assistant", "") and the merge treats state.db rows as replays.
    _tc = msg.get("tool_calls")
    _tc_key = json.dumps(_tc, sort_keys=True, default=str) if _tc else ""
    return (
        str(msg.get("role") or ""),
        _normalized_session_message_content(msg),
        _tc_key,
    )


def _build_visible_duplicate_lookup(visible_keys: set[tuple]) -> dict:
    by_role = {}
    for key in visible_keys:
        try:
            role = key[0]
            content = key[1]
        except (TypeError, IndexError):
            continue
        if not content:
            continue
        by_role.setdefault(role, []).append(key)
    # Keep loose_by_key lazy.  Some transcripts contain multi-megabyte tool
    # outputs; eagerly casefolding + regex-tokenizing every visible key on every
    # duplicate probe made /api/session take 10s+ and blocked /api/sessions.
    return {"keys": visible_keys, "by_role": by_role, "loose_by_key": {}}


def _matching_visible_duplicate(visible_key: tuple, visible_keys: set[tuple], lookup: dict | None = None):
    if visible_key in visible_keys:
        return visible_key
    role = visible_key[0]
    content = visible_key[1] if len(visible_key) > 1 else ""
    if not content:
        return None
    if lookup is None:
        lookup = _build_visible_duplicate_lookup(visible_keys)
    loose_content = None
    loose_by_key = lookup.setdefault("loose_by_key", {})
    for existing_key in lookup.get("by_role", {}).get(role, []):
        existing_role = existing_key[0]
        existing_content = existing_key[1] if len(existing_key) > 1 else ""
        if role != existing_role or not existing_content:
            continue
        # Exact visible-key equality was checked above. For very large payloads
        # (tool logs / request dumps), Python-in substring and fuzzy-token
        # comparisons are both expensive and low-value; doing them repeatedly
        # made session loading block the whole WebUI for many seconds. Keep
        # fuzzy matching for normal chat-sized text, but do exact-only matching
        # for giant payloads.
        if max(len(content), len(existing_content)) > 200_000:
            continue
        if content in existing_content or existing_content in content:
            return existing_key
        if loose_content is None:
            loose_content = _loose_session_message_content(content)
        loose_existing = loose_by_key.get(existing_key)
        if loose_existing is None:
            loose_existing = _loose_session_message_content(existing_content)
            loose_by_key[existing_key] = loose_existing
        if loose_content and loose_existing and (
            loose_content in loose_existing or loose_existing in loose_content
        ):
            return existing_key
    return None


def _has_visible_duplicate(visible_key: tuple, visible_keys: set[tuple]) -> bool:
    return _matching_visible_duplicate(visible_key, visible_keys) is not None


def _sidecar_has_terminal_partial_error(sidecar_messages: list) -> bool:
    """Return True when WebUI already owns an interrupted live partial turn.

    After a cancelled/error terminal event, the WebUI sidecar contains the
    user prompt, the streamed partial assistant prose/tool snapshot, and the
    explicit terminal carrier. state.db may still contain the same run's raw
    assistant/tool replay rows; appending those rows makes Compact Worklog show
    duplicated process prose after cancel. In that shape, the sidecar is the
    display owner.
    """
    messages = [msg for msg in (sidecar_messages or []) if isinstance(msg, dict)]
    latest_error_idx = None
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "assistant":
            continue
        if msg.get("_error"):
            latest_error_idx = idx
    if latest_error_idx is None:
        return False
    for msg in messages[latest_error_idx + 1 :]:
        if str(msg.get("role") or "").lower() in ("user", "assistant"):
            return False
    segment_start = 0
    for idx in range(latest_error_idx - 1, -1, -1):
        if str(messages[idx].get("role") or "").lower() == "user":
            segment_start = idx + 1
            break
    for msg in messages[segment_start:latest_error_idx]:
        if str(msg.get("role") or "").lower() == "assistant" and msg.get("_partial"):
            return True
    return False


def state_db_delta_after_context(sidecar_context: list, state_messages: list) -> list:
    """Return only state.db rows that are newer than model-facing context.

    `context_messages` is the authoritative model-facing prefix. state.db may
    contain a mirrored copy of that prefix with fresh timestamps, especially for
    LCM/continuation sessions. Appending the whole state transcript to a clean
    sidecar context replays old context into the next runtime prompt.
    """
    sidecar_context = list(sidecar_context or [])
    state_messages = list(state_messages or [])
    if not sidecar_context or not state_messages:
        return state_messages

    # Recovered interrupted turns are special: the visible interruption marker
    # is synthetic, so the recovered user turn should still count as a mirrored
    # prefix when it is the actual aligned prefix row.
    allow_single_row_prefix = bool(
        isinstance(sidecar_context[0], dict)
        and sidecar_context[0].get('_recovered')
        and str(sidecar_context[0].get('role') or '') == 'user'
    )

    sidecar_keys = [_session_message_content_key(m) for m in sidecar_context]
    state_keys = [_session_message_content_key(m) for m in state_messages]
    max_offset = min(len(sidecar_keys), len(state_keys))
    best_len = 0
    best_offset = 0
    for offset in range(max_offset):
        length = 0
        while (
            offset + length < len(sidecar_keys)
            and length < len(state_keys)
            and sidecar_keys[offset + length] == state_keys[length]
        ):
            length += 1
        if length > best_len:
            best_len = length
            best_offset = offset

    # Require at least two mirrored rows. A single repeated short user message
    # is not enough evidence that state.db starts with a mirrored context
    # segment, but small recovered contexts often contain only a compact summary
    # and one follow-up row; those should still use the delta path.
    if best_len < (1 if allow_single_row_prefix and best_offset == 0 else 2):
        return state_messages

    # Drop only rows that can be aligned with the remaining sidecar context in
    # order. This still tolerates stale state-only rows between mirrored context
    # rows, but once the sidecar context is exhausted every later state row is a
    # real delta, even if it repeats a short earlier message.
    sidecar_index = best_len
    state_index = best_len
    while sidecar_index < len(sidecar_keys) and state_index < len(state_keys):
        if state_keys[state_index] == sidecar_keys[sidecar_index]:
            sidecar_index += 1
        state_index += 1
    if sidecar_index == len(sidecar_keys):
        return state_messages[state_index:]
    return state_messages[best_len:]


def _normalized_compression_anchor_text(value) -> str:
    return " ".join(str(value or "").split()).strip()[:160]


def _compression_anchor_timestamp_as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _context_messages_include_compression_marker(messages: list) -> bool:
    for message in messages or []:
        if not is_context_compression_marker(message):
            continue
        text = _message_content_text(message).lower().lstrip()
        # Only prompt compaction summaries require fail-closed state.db replay.
        # Other compression-adjacent summaries, such as Session Arc Summary,
        # keep the existing prefix-delta behavior so fresh follow-ups survive.
        if text.startswith("[context compaction") or text.startswith("context compaction"):
            return True
    return False


def _state_db_anchor_index(state_messages: list, anchor_key) -> int | None:
    if not isinstance(anchor_key, dict):
        return None

    anchor_role = str(anchor_key.get("role") or "").strip().lower()
    anchor_text = _normalized_compression_anchor_text(anchor_key.get("text"))
    anchor_attachments = anchor_key.get("attachments")
    anchor_ts = _compression_anchor_timestamp_as_float(anchor_key.get("ts"))

    if not anchor_role:
        return None

    # Do not attempt text-only fallback when timestamp is unavailable. Text-based
    # fallback can match stale legacy rows if the anchor timestamp was lost,
    # which can re-introduce old state.db rows after compaction.
    if anchor_ts is None:
        return None

    if anchor_attachments in (None, ""):
        expected_attachments = 0
    else:
        try:
            expected_attachments = int(anchor_attachments)
        except (TypeError, ValueError):
            expected_attachments = 0

    exact_timestamp_matches = []
    for idx, message in enumerate(state_messages or []):
        if _message_role(message) != anchor_role:
            continue

        attachments = message.get("attachments") if isinstance(message, dict) else None
        attach_count = len(attachments) if isinstance(attachments, list) else 0
        if attach_count != expected_attachments:
            continue

        # Attachment-only or timestamp-only anchors have no stable text payload.
        # In that shape the timestamp + role + attachment count is the boundary;
        # apply text comparison only when the anchor actually captured text.
        message_text = _normalized_compression_anchor_text(_message_content_text(message))
        if anchor_text and message_text != anchor_text:
            continue

        message_ts = _compression_anchor_timestamp_as_float(
            message.get("timestamp") if isinstance(message, dict) else None
        )
        if message_ts is None:
            continue
        if abs(message_ts - anchor_ts) <= 1e-6:
            exact_timestamp_matches.append(idx)
            continue

    if exact_timestamp_matches:
        return exact_timestamp_matches[-1]
    return None


def _tool_call_assistant_should_precede_content_assistant(existing: dict, msg: dict) -> bool:
    return (
        isinstance(existing, dict)
        and isinstance(msg, dict)
        and str(msg.get("role") or "").lower() == "assistant"
        and bool(msg.get("tool_calls"))
        and not _message_content_text(msg).strip()
        and str(existing.get("role") or "").lower() == "assistant"
        and not existing.get("tool_calls")
        and bool(_message_content_text(existing).strip())
    )


def _insert_state_message_chronologically(messages: list, msg: dict) -> bool:
    """Insert a state.db-only row before newer sidecar rows when safe.

    Returns False when the only chronological slot would resurrect an old state
    row before the sidecar/context begins. This keeps no-watermark compression
    display paths from reintroducing rows that were already compacted out.
    """
    timestamp = _message_timestamp_as_float(msg)
    if timestamp is None:
        messages.append(msg)
        return True
    idx = 0
    while idx < len(messages):
        existing = messages[idx]
        existing_timestamp = _message_timestamp_as_float(existing)
        should_insert = existing_timestamp is not None and (
            existing_timestamp > timestamp
            or (
                existing_timestamp == timestamp
                and (
                    (
                        msg.get("role") == "user"
                        and existing.get("role") == "assistant"
                    )
                    or _tool_call_assistant_should_precede_content_assistant(existing, msg)
                )
            )
        )
        if not should_insert:
            idx += 1
            continue
        if idx == 0 and existing_timestamp is not None and existing_timestamp > timestamp:
            # With no surviving sidecar/context row before this slot, a real
            # interruption rescue is indistinguishable from a compacted-out old
            # prompt; prefer avoiding no-watermark resurrection in that shape.
            return False
        # Advance the insertion point past two kinds of slots that must not be
        # split, applied to a fixpoint so they compose in any order at an
        # equal-timestamp collision:
        #   (a) an assistant(tool_calls) -> tool result block — inserting inside
        #       it would split the tool call from its result (provider 400 /
        #       corrupt tool context);
        #   (b) a slot whose left neighbour shares this message's role at the
        #       same timestamp — inserting there would re-order an already-matched
        #       same-role turn (e.g. user, <inserted user>, assistant). The agent
        #       core merges adjacent users before send, so (b) is benign in
        #       practice, but advancing keeps the merged transcript correctly
        #       ordered and alternation-clean regardless.
        # Looping to a fixpoint guarantees the same-role guard can't strand the
        # insert back inside a tool-pair (and vice versa).
        while True:
            advanced = False
            # (a) Skip past a complete assistant(tool_calls) -> tool result
            # block. Advance over ALL contiguous tool rows that belong to the
            # preceding assistant's tool_calls, not just the first — a multi-tool
            # turn has several adjacent tool results, and inserting between any of
            # them splits the block (assistant, tool, <insert>, tool).
            if (
                idx < len(messages)
                and messages[idx].get("role") == "tool"
                and idx > 0
                and messages[idx - 1].get("role") == "assistant"
                and messages[idx - 1].get("tool_calls")
            ):
                while idx < len(messages) and messages[idx].get("role") == "tool":
                    idx += 1
                    advanced = True
            # (b) Skip past an equal-timestamp run whose left neighbour shares
            # this message's role — inserting there would re-order an
            # already-matched same-role turn (user, <inserted user>, assistant).
            # The agent core merges adjacent users before send, so this is benign
            # in practice, but advancing keeps the merged transcript ordered.
            while (
                idx < len(messages)
                and idx > 0
                and messages[idx - 1].get("role") == msg.get("role")
                and _message_timestamp_as_float(messages[idx]) == timestamp
                and not _tool_call_assistant_should_precede_content_assistant(messages[idx], msg)
            ):
                idx += 1
                advanced = True
            if not advanced:
                break
        messages.insert(idx, msg)
        return True
    messages.append(msg)
    return True


def merge_session_messages_append_only(
    sidecar_messages: list,
    state_messages: list,
    *,
    truncation_watermark=None,
    truncation_boundary=None,
) -> list:
    """Merge sidecar/context and state.db messages without deleting local rows.

    ``truncation_boundary``: the original truncate cutoff — the
    timestamp of the last message kept by the truncate operation.  When the
    watermark is later advanced (new turn committed), this boundary is preserved
    so the empty-sidecar recovery can distinguish a legitimate prefix from a
    deleted suffix instead of guessing by dropping one turn pair.
    """
    sidecar_messages = list(sidecar_messages or [])
    state_messages = list(state_messages or [])
    # Per-invocation cache keyed by message identity. Sidecar/state message objects
    # are retained for this call, and this function does not mutate key-defining
    # fields before each helper call.
    _MESSAGE_CACHE_MISSING = object()
    _cached_msg_prepared: dict[int, dict[str, object]] = {}
    _cached_msg_keys: dict[tuple[int, str], object] = {}

    _message_key_helpers = {
        "merge": _session_message_merge_key,
        "dedup": _session_message_dedup_key,
        "content": _session_message_content_key,
        "visible": _session_message_visible_key,
    }

    def _cached_message_key(msg, kind):
        if not isinstance(msg, dict):
            return _message_key_helpers[kind](msg)

        cache_key = (id(msg), kind)
        value = _cached_msg_keys.get(cache_key, _MESSAGE_CACHE_MISSING)
        if value is not _MESSAGE_CACHE_MISSING:
            return value

        helper = _message_key_helpers[kind]
        msg_cache_key = id(msg)
        prepared_msg = _cached_msg_prepared.get(msg_cache_key)

        if kind in {"merge", "dedup"}:
            if prepared_msg is None:
                value = helper(msg)
                # If this is a legacy message key, keep the already-stringified
                # content payload for downstream helper calls.
                if isinstance(value, tuple) and value and value[0] == "legacy":
                    prepared_msg = dict(msg)
                    prepared_msg["content"] = value[2]
                    _cached_msg_prepared[msg_cache_key] = prepared_msg
            else:
                value = helper(prepared_msg)
            _cached_msg_keys[cache_key] = value
            return value

        if prepared_msg is None:
            # For non-ID messages this is the canonical merge path.
            merge_key = _cached_message_key(msg, "merge")
            prepared_msg = _cached_msg_prepared.get(msg_cache_key)
            if prepared_msg is None:
                prepared_msg = dict(msg)
                prepared_msg["content"] = (
                    merge_key[2]
                    if isinstance(merge_key, tuple)
                    and len(merge_key) > 2
                    and merge_key[0] == "legacy"
                    else str(msg.get("content") or "")
                )
                _cached_msg_prepared[msg_cache_key] = prepared_msg

        value = helper(prepared_msg)
        _cached_msg_keys[cache_key] = value
        return value

    watermark_timestamp = _message_timestamp_as_float({"timestamp": truncation_watermark})
    if not state_messages:
        return sidecar_messages
    if not sidecar_messages:
        if watermark_timestamp is None:
            # No watermark — keep everything, just dedup.
            filtered = state_messages
        elif watermark_timestamp == 0:
            # Truncate-to-empty sentinel (#2914) — block all replay.
            return []
        else:
            # Positive watermark after edit/retry/undo (#4767).  Without a
            # sidecar there's no seen_content_keys to check against, so we
            # reconstruct the correct transcript from state.db alone.
            #
            # `at_or_after` (ts >= watermark) is legitimate POST-EDIT content
            # ONLY when the watermark was ADVANCED strictly past the original
            # truncate cutoff — i.e. a new turn was committed after the edit, so
            # truncation_boundary (the original cutoff) is strictly below the
            # advanced watermark.  In that state we keep the legitimate prefix
            # (ts <= boundary) plus the post-edit tail (ts >= watermark) and drop
            # the deleted (boundary, watermark) suffix.
            #
            # In every OTHER state the content above the watermark is the deleted
            # suffix, NOT post-edit content, so keeping it would resurrect deleted
            # turns (the exact data-loss this fix exists to kill):
            #   * boundary == watermark — just truncated, no new turn committed
            #     yet (e.g. crash/cold-load with metadata-vs-sidecar divergence);
            #   * boundary is None — legacy session saved before this field
            #     existed.  In the pre-#4767 model committing a turn CLEARED the
            #     watermark to None, so a persisted positive watermark always
            #     meant "frozen at cutoff, not advanced".
            # For all of those, fall back to the conservative pre-#4767 filter
            # `ts <= watermark`, which never resurrects a deleted suffix.
            boundary_ts = _message_timestamp_as_float({"timestamp": truncation_boundary})
            if boundary_ts is not None and boundary_ts < watermark_timestamp:
                pre_legitimate = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts <= boundary_ts
                ]
                at_or_after = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts >= watermark_timestamp
                ]
                filtered = pre_legitimate + at_or_after
            else:
                filtered = [
                    m for m in state_messages
                    if (ts := _message_timestamp_as_float(m)) is not None
                    and ts <= watermark_timestamp
                ]

        # Deduplicate true duplicates (same role, content, exact timestamp)
        # without collapsing legitimately-repeated identical turns (#3346).
        seen = set()
        seen_messages = {}
        deduped = []
        for msg in filtered:
            key = _cached_message_key(msg, "dedup")
            if key not in seen:
                seen.add(key)
                seen_messages[key] = msg
                deduped.append(msg)
            else:
                _merge_session_display_metadata(seen_messages.get(key), msg)
        return deduped

    merged_messages = []
    seen_message_keys = set()
    seen_dedup_keys = set()
    seen_content_keys = set()
    seen_visible_keys = set()
    sidecar_visible_sequence = []
    sidecar_visible_messages = []
    sidecar_visible_keys = set()
    sidecar_visible_counts = {}
    merged_by_message_key = {}
    merged_by_dedup_key = {}
    merged_by_visible_key = {}
    max_sidecar_timestamp = None

    def _remember_merged_message(message):
        if not isinstance(message, dict):
            return
        merged_by_message_key.setdefault(_cached_message_key(message, "merge"), message)
        merged_by_dedup_key.setdefault(_cached_message_key(message, "dedup"), message)
        merged_by_visible_key.setdefault(_cached_message_key(message, "visible"), message)

    for msg in sidecar_messages:
        timestamp = _message_timestamp_as_float(msg)
        if timestamp is not None:
            max_sidecar_timestamp = timestamp if max_sidecar_timestamp is None else max(max_sidecar_timestamp, timestamp)
        key = _cached_message_key(msg, "merge")
        seen_message_keys.add(key)
        seen_dedup_keys.add(_cached_message_key(msg, "dedup"))
        content_key = _cached_message_key(msg, "content")
        seen_content_keys.add(content_key)
        visible_key = _cached_message_key(msg, "visible")
        seen_visible_keys.add(visible_key)
        sidecar_visible_keys.add(visible_key)
        sidecar_visible_counts[visible_key] = sidecar_visible_counts.get(visible_key, 0) + 1
        sidecar_visible_sequence.append(visible_key)
        sidecar_visible_messages.append(msg)
        merged_messages.append(msg)
        _remember_merged_message(msg)
    if _sidecar_has_terminal_partial_error(sidecar_messages):
        return merged_messages
    sidecar_visible_lookup = _build_visible_duplicate_lookup(sidecar_visible_keys)
    state_replay_idx = 0
    skipped_state_visible_counts = {}
    # Loop-invariant: a session whose original truncate cutoff (truncation_boundary)
    # is strictly below the watermark is genuinely ADVANCED (a new turn was
    # committed after the edit). In that state post-watermark state.db rows are
    # legitimate post-edit content, even when the sidecar's newest row only
    # EQUALS the watermark (the post-edit user is checkpointed but its assistant
    # reply exists only in state.db). Conservative for boundary None / == watermark.
    boundary_ts = _message_timestamp_as_float({"timestamp": truncation_boundary})
    watermark_advanced_by_boundary = (
        watermark_timestamp is not None
        and boundary_ts is not None
        and boundary_ts < watermark_timestamp
    )
    for msg in state_messages:
        timestamp = _message_timestamp_as_float(msg)
        key = _cached_message_key(msg, "merge")
        dedup_key = _cached_message_key(msg, "dedup")
        visible_key = _cached_message_key(msg, "visible")
        content_key = _cached_message_key(msg, "content")
        replays_sidecar_prefix = False
        replay_target = None
        if state_replay_idx < len(sidecar_visible_sequence):
            expected_visible_key = sidecar_visible_sequence[state_replay_idx]
            if visible_key == expected_visible_key or _has_visible_duplicate(
                visible_key, {expected_visible_key}
            ):
                replays_sidecar_prefix = True
                replay_target = sidecar_visible_messages[state_replay_idx]
                state_replay_idx += 1
        if replays_sidecar_prefix:
            _merge_session_display_metadata(replay_target, msg)
            matched_visible_key = _matching_visible_duplicate(
                visible_key,
                sidecar_visible_keys,
                sidecar_visible_lookup,
            )
            if matched_visible_key is not None:
                skipped_state_visible_counts[matched_visible_key] = (
                    skipped_state_visible_counts.get(matched_visible_key, 0) + 1
                )
            # Record dedup key so later duplicates of this replayed message
            # are caught by the dedup guard (#3346).
            seen_dedup_keys.add(dedup_key)
            continue
        # Skip rows ABOVE the watermark only while the sidecar has NOT advanced
        # past the watermark. Because Session.save() no longer auto-clears the
        # watermark, an unconditional `timestamp > watermark` skip would become
        # permanent and silently drop legitimate future state.db-only recovery
        # rows once the session moves forward past the edit boundary. Once the
        # sidecar's own max timestamp is beyond the watermark (the session has
        # advanced), allow state rows newer than the sidecar tail to merge.
        #
        # The sidecar's max timestamp can also EQUAL the watermark when the new
        # post-edit USER turn has been checkpointed into the sidecar (its
        # timestamp == the advanced watermark) but its ASSISTANT reply exists
        # only in state.db (recovery before the sidecar tail advances). In that
        # state truncation_boundary < watermark proves the session is genuinely
        # advanced, so the post-watermark state-only reply is legitimate
        # post-edit content and must merge through (not be dropped as a replaced
        # tail). The conservative skip still applies for boundary is None and
        # boundary == watermark (not-advanced / legacy).
        #
        # CRITICAL: the boundary-advanced signal may only bypass the skip AFTER
        # state replay has consumed the sidecar's visible checkpoint
        # (state_replay_idx >= len(sidecar_visible_sequence)). A deleted suffix
        # row with ts > watermark that appears in state.db BEFORE the edited
        # checkpoint must still be skipped — otherwise the advanced signal would
        # resurrect it. The sidecar-max-timestamp signal needs no such gate (a
        # sidecar tail beyond the watermark is itself proof the checkpoint has
        # advanced).
        checkpoint_consumed = state_replay_idx >= len(sidecar_visible_sequence)
        sidecar_advanced_past_watermark = (
            watermark_timestamp is not None
            and (
                (max_sidecar_timestamp is not None
                 and max_sidecar_timestamp > watermark_timestamp)
                or (watermark_advanced_by_boundary and checkpoint_consumed)
            )
        )
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp > watermark_timestamp
            and key not in seen_message_keys
            and (
                not sidecar_advanced_past_watermark
                or (max_sidecar_timestamp is not None and timestamp <= max_sidecar_timestamp)
            )
        ):
            continue
        # When a truncation watermark is active, state.db may contain original
        # messages that were replaced by Edit (old content with old timestamp).
        # The timestamp-based filter above catches messages AFTER the watermark,
        # but messages BEFORE it (like the original pre-edit content) slip through.
        # If a state.db message's content is not present in the sidecar and its
        # timestamp is before the watermark, it's a replaced/stale row — skip it.
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp < watermark_timestamp
            and key not in seen_message_keys
            and content_key not in seen_content_keys
        ):
            continue
        # Same-second edit: if timestamp equals the watermark and the message
        # content is not in the sidecar, it's a replaced message edited at the
        # same second — skip it.  The edited version (same timestamp, different
        # content) is in the sidecar and survives this check.
        #
        # Only apply the same-second guard to user messages.  An assistant reply
        # (or tool message) at the same second as the watermark is a legitimate
        # post-edit recovery row — the sidecar holds only the edited user
        # checkpoint, so the assistant reply's content won't be in it and would
        # be silently dropped without this role guard.
        if (
            watermark_timestamp is not None
            and timestamp is not None
            and timestamp == watermark_timestamp
            and key not in seen_message_keys
            and content_key not in seen_content_keys
            and str(msg.get("role", "")).lower() == "user"
        ):
            continue
        # Check for true duplicates using full-precision timestamp (#3346).
        # Must run before the merge-key guards so that legitimately distinct
        # sub-second messages with the same second-level merge key are not
        # collapsed.  The merge key truncates to seconds; the dedup key does
        # not.
        if dedup_key in seen_dedup_keys:
            _merge_session_display_metadata(merged_by_dedup_key.get(dedup_key), msg)
            continue
        if max_sidecar_timestamp is not None and timestamp is not None and timestamp <= max_sidecar_timestamp:
            # For message_id keys the merge key is authoritative — skip if
            # already seen.  For legacy keys the dedup check above already
            # handled true duplicates; same-second distinct messages must
            # fall through.
            if key in seen_message_keys and key[0] == "message_id":
                _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                continue
            if not (isinstance(key, tuple) and key[:1] == ("message_id",)):
                # Legacy key within sidecar timestamp range — only skip if
                # this exact merge_key was already registered by the sidecar.
                # Different tool_calls produce different merge_keys even with
                # identical content/timestamp, so an unchecked continue here
                # would drop legitimately distinct turns.  (#3346 / PR #3665)
                if key in seen_message_keys:
                    _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                    continue
        if key in seen_message_keys and key[0] == "message_id":
            _merge_session_display_metadata(merged_by_message_key.get(key), msg)
            continue
        matched_visible_key = _matching_visible_duplicate(
            visible_key,
            sidecar_visible_keys,
            sidecar_visible_lookup,
        )
        if matched_visible_key is not None:
            skipped_count = skipped_state_visible_counts.get(matched_visible_key, 0)
            sidecar_count = sidecar_visible_counts.get(matched_visible_key, 0)
            if skipped_count < sidecar_count:
                skipped_state_visible_counts[matched_visible_key] = skipped_count + 1
                _merge_session_display_metadata(merged_by_visible_key.get(matched_visible_key), msg)
                continue
        # State rows at or before the newest sidecar timestamp are normally
        # assumed to have already been observed by the sidecar. The <= gate
        # preserves sidecar-only ordering/metadata for equal timestamps and
        # prevents duplicate legacy rows when timestamp precision differs
        # between stores. State rows whose visible content already exists in
        # the sidecar are also skipped even if state.db restamped them later
        # during compaction/recovery; otherwise old prompts can be appended
        # after the assistant tail and make /api/session look like the answer
        # vanished. Explicit message ids are authoritative for distinct rows
        # only when their visible content is not already present.
        if (
            key[0] != "message_id"
            and max_sidecar_timestamp is not None
            and timestamp is not None
            and timestamp <= max_sidecar_timestamp
        ):
            # When a truncation watermark is active and the sidecar holds only
            # the edited user checkpoint, state.db may contain an assistant/tool
            # reply at the same timestamp that is NOT in the sidecar.  This
            # block would normally skip it ("sidecar already has this message"),
            # but the sidecar doesn't — it's a genuine state-only recovery row.
            # Let it through (CORE-B, #4767).
            #
            # Only AFTER the sidecar's visible checkpoint has been consumed
            # (checkpoint_consumed) — a same-second row appearing in state.db
            # BEFORE the edited user replay is a deleted/replaced row, not the
            # post-edit reply, and must stay skipped.
            if (
                watermark_timestamp is not None
                and timestamp == watermark_timestamp
                and checkpoint_consumed
                and str(msg.get("role", "")).lower() != "user"
                and content_key not in seen_content_keys
            ):
                pass  # fall through to append below
            else:
                # Legacy key within sidecar timestamp range.  Normally skip — the
                # sidecar already has this message.  Exception: if the state.db
                # message has tool_calls that DIFFER from the sidecar version
                # (same content_key but different dedup_key because tool_calls
                # differ), preserve it — distinct tool_calls must not be collapsed.
                _tc = msg.get("tool_calls")
                if _tc:
                    _ck = content_key
                    if _ck in seen_content_keys and dedup_key not in seen_dedup_keys:
                        # Different tool_calls from sidecar — preserve, but keep
                        # the row in timestamp order. Falling through to the
                        # generic append path would move older tool-call-only
                        # assistant rows after the settled final answer.
                        if _insert_state_message_chronologically(merged_messages, msg):
                            seen_message_keys.add(key)
                            seen_dedup_keys.add(dedup_key)
                            seen_content_keys.add(content_key)
                            seen_visible_keys.add(visible_key)
                            _remember_merged_message(msg)
                        continue
                    else:
                        _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                        continue
                else:
                    if msg.get("role") == "user" and content_key not in seen_content_keys:
                        if _insert_state_message_chronologically(merged_messages, msg):
                            seen_message_keys.add(key)
                            seen_dedup_keys.add(dedup_key)
                            seen_content_keys.add(content_key)
                            seen_visible_keys.add(visible_key)
                            _remember_merged_message(msg)
                        continue
                    _merge_session_display_metadata(merged_by_message_key.get(key), msg)
                    continue
        seen_message_keys.add(key)
        seen_dedup_keys.add(dedup_key)
        seen_content_keys.add(content_key)
        seen_visible_keys.add(visible_key)
        merged_messages.append(msg)
        _remember_merged_message(msg)
    return merged_messages


def reconciled_state_db_messages_for_session(
    session, *, prefer_context: bool = False, state_messages: list | None = None
) -> list:
    """Return append-only messages reconciled with state.db for a WebUI session."""
    if session is None:
        return []
    local_messages = []
    using_context_messages = False
    if prefer_context:
        context_messages = getattr(session, 'context_messages', None)
        if isinstance(context_messages, list) and context_messages:
            local_messages = context_messages
            using_context_messages = True
    if not local_messages:
        local_messages = getattr(session, 'messages', None) or []
    if state_messages is None:
        state_messages = get_state_db_session_messages(getattr(session, 'session_id', None))
    if prefer_context and local_messages:
        if using_context_messages:
            sidecar_messages = getattr(session, 'messages', None) or []
            if (
                getattr(session, 'is_cli_session', False)
                and not getattr(session, 'read_only', False)
                and sidecar_messages
                and len(sidecar_messages) > len(local_messages)
                and _session_messages_have_prefix(sidecar_messages, local_messages)
            ):
                # A claimed CLI sidecar can carry a stale context prefix while the
                # stitched CLI transcript already landed in session.messages. On the
                # first WebUI follow-up, prefer that longer authoritative transcript
                # unless context_messages intentionally diverged via compaction or
                # another non-prefix transform.
                local_messages = sidecar_messages
                using_context_messages = False
            if using_context_messages:
                compressed_context = _context_messages_include_compression_marker(local_messages)
                anchor_key = getattr(session, "compression_anchor_message_key", None)
                if compressed_context:
                    if not anchor_key:
                        logger.debug(
                            "Compressed context for session %s has no compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    anchor_index = _state_db_anchor_index(state_messages, anchor_key)
                    if anchor_index is None:
                        logger.debug(
                            "Compressed context for session %s has an unverifiable compression anchor; using context_messages only",
                            getattr(session, "session_id", None),
                        )
                        return list(local_messages)
                    state_messages = list(state_messages or [])[anchor_index + 1 :]
        state_messages = state_db_delta_after_context(local_messages, state_messages)
    return merge_session_messages_append_only(
        local_messages,
        state_messages,
        truncation_watermark=getattr(session, "truncation_watermark", None),
        truncation_boundary=getattr(session, "truncation_boundary", None),
    )


def get_cli_session_messages(sid, *, profile=None) -> list:
    """Read messages for a single CLI/external-agent session.

    Preserve tool-call/result and reasoning metadata from the agent state.db so
    CLI-origin transcripts render with the same tool cards as WebUI-native
    sessions. When the requested session is the tip of a compression/CLI-close
    continuation chain, return the stitched full transcript across all segments
    in chronological order. Returns empty list on any error.
    """
    if str(sid or '').startswith(f'{CLAUDE_CODE_SOURCE}_'):
        return get_claude_code_session_messages(sid)
    return get_state_db_session_messages(sid, stitch_continuations=True, profile=profile)


def count_conversation_rounds(sid: str, since: float | None = None) -> int:
    """Count conversation rounds for a session from state.db.

    A "round" = one user message + one agent reply.  Consecutive user
    messages are merged into a single round so that multi-part questions
    don't inflate the count.

    Parameters
    ----------
    sid : str
        Gateway session ID (e.g. ``20260430_151231_7209a0``).
    since : float | None
        Unix timestamp.  If provided, only messages **after** this
        timestamp are counted.

    Returns
    -------
    int
        Number of complete conversation rounds.
    """
    import os, sqlite3, datetime

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return 0

    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT role, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (sid,),
            )
            rows = cur.fetchall()
    except Exception:
        return 0

    rounds = 0
    seen_user = False          # have we seen a user msg in the current round?
    seen_agent_after_user = False  # have we seen an agent reply after that user msg?

    for row in rows:
        role = (row['role'] or '').strip().lower()
        ts_raw = row['timestamp']

        # Parse timestamp and apply the ``since`` filter.
        if since is not None and ts_raw is not None:
            try:
                if isinstance(ts_raw, (int, float)):
                    ts_val = float(ts_raw)
                else:
                    # ISO-8601 string
                    ts_val = datetime.datetime.fromisoformat(
                        str(ts_raw).replace('Z', '+00:00')
                    ).timestamp()
                if ts_val <= since:
                    continue
            except Exception:
                pass

        if role == 'user':
            if seen_user and not seen_agent_after_user:
                # Consecutive user message — merge into current round.
                pass
            elif seen_user and seen_agent_after_user:
                # Previous round completed, starting a new one.
                rounds += 1
                seen_agent_after_user = False
            seen_user = True
        elif role == 'assistant':
            if seen_user:
                seen_agent_after_user = True

    # Close the last round if it was completed.
    if seen_user and seen_agent_after_user:
        rounds += 1

    return rounds


CONVERSATION_ROUND_THRESHOLD = 10


@contextmanager
def _cleanup_manifest_process_lock(hermes_home):
    """Serialize cleanup across WebUI worker processes for one profile.

    Keep the lock file in place permanently: unlinking it while another process
    is waiting can split later callers across different inodes and defeat the
    lock. POSIX uses ``flock``; native Windows locks the first byte with
    ``msvcrt.locking``. If neither primitive exists, fail closed rather than
    running destructive cleanup without cross-process serialization.
    """
    lock_path = Path(hermes_home) / ".session_cleanup.lock"
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

        raise RuntimeError("cross-process session cleanup locking is unavailable")


_cleanup_manifest_locks_guard = threading.Lock()
_cleanup_manifest_locks = {}


def _cleanup_manifest_thread_lock(hermes_home):
    """Return the in-process cleanup lock for one resolved profile home."""
    key = os.fspath(Path(hermes_home))
    with _cleanup_manifest_locks_guard:
        lock = _cleanup_manifest_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _cleanup_manifest_locks[key] = lock
        return lock


def delete_cli_session(sid) -> bool:
    """Delete a CLI session while serializing manifest and DB cleanup."""
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        logger.warning("Failed to resolve active profile for session delete", exc_info=True)
        return False
    try:
        with _cleanup_manifest_thread_lock(hermes_home):
            with _cleanup_manifest_process_lock(hermes_home):
                return _delete_cli_session_locked(sid, hermes_home)
    except Exception:
        logger.warning("Failed to delete CLI session %s from state.db", sid, exc_info=True)
        return False


def _delete_cli_session_locked(sid, hermes_home) -> bool:
    """Delete a CLI session from state.db using Hermes' session semantics.

    A scoped transaction implements the Agent invariant while giving branch and
    compression evidence precedence over inherited delegate metadata. Current
    Hermes Agent's canonical helper does not yet make that precedence guarantee,
    so this destructive path fails closed instead of delegating to it.

    Returns True when the requested state is absent after cleanup, False on an
    operational error.
    """
    try:
        import sqlite3
    except ImportError:
        return False

    # Process any leftover cleanup manifests from a previous failed run.
    # This runs before the DB-existence check so pending artifact
    # removals get another chance even when the current session ID
    # is unrelated.
    stale_cleanup_complete = _process_stale_cleanup_manifests(hermes_home)

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return False

    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            # SQLite does not enforce foreign keys by default; enabling
            # PRAGMA foreign_keys makes the ON DELETE CASCADE clauses on
            # session_model_usage and telegram_dm_topic_bindings fire
            # automatically.  Compression locks have no FK, so they are
            # cleaned explicitly below.
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if not {"id", "parent_session_id"}.issubset(columns):
                return False

            selected = ["id", "parent_session_id"]
            for column in (
                "model_config", "source", "end_reason", "started_at", "ended_at"
            ):
                selected.append(column if column in columns else f"NULL AS {column}")
            rows = conn.execute(f"SELECT {', '.join(selected)} FROM sessions").fetchall()

            # _delegate_from is authoritative. source=subagent is the legacy
            # compatibility signal for rows created before that marker existed,
            # but legacy branch/compression continuations must remain intact.
            records = []
            for row in rows:
                model_config = {}
                raw_model_config = row["model_config"]
                model_config_known = raw_model_config in (None, "")
                if not model_config_known:
                    try:
                        parsed_model_config = json.loads(raw_model_config)
                    except (TypeError, ValueError):
                        parsed_model_config = None
                    if isinstance(parsed_model_config, dict):
                        model_config = parsed_model_config
                        model_config_known = True
                records.append(
                    {
                        "id": row["id"],
                        "parent_id": row["parent_session_id"],
                        "delegate_from": model_config.get("_delegate_from"),
                        "branched_from": model_config.get("_branched_from"),
                        "model_config_known": model_config_known,
                        "source": row["source"],
                        "end_reason": row["end_reason"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                    }
                )
            records_by_id = {record["id"]: record for record in records}

            def _timestamp_value(value):
                """Return a comparable UTC timestamp, or None when ambiguous."""
                if isinstance(value, bool) or value in (None, ""):
                    return None
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric if math.isfinite(numeric) else None
                if isinstance(value, datetime.datetime):
                    parsed = value
                elif isinstance(value, str):
                    raw = value.strip()
                    if not raw:
                        return None
                    try:
                        numeric = float(raw)
                        return numeric if math.isfinite(numeric) else None
                    except ValueError:
                        try:
                            parsed = datetime.datetime.fromisoformat(
                                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                            )
                        except ValueError:
                            return None
                else:
                    return None
                if parsed.tzinfo is None:
                    return None
                try:
                    numeric = parsed.timestamp()
                except (OverflowError, OSError, ValueError):
                    return None
                return numeric if math.isfinite(numeric) else None

            def _must_preserve(record, parent):
                """Fail closed when branch/compression evidence is ambiguous."""
                if record["branched_from"] is not None:
                    return True
                end_reason = parent.get("end_reason")
                if end_reason == "compression":
                    return True
                if end_reason != "branched":
                    return False
                started_at = _timestamp_value(record["started_at"])
                parent_ended_at = _timestamp_value(parent.get("ended_at"))
                if started_at is None or parent_ended_at is None:
                    return True
                return started_at >= parent_ended_at

            def _lineage_parent(record):
                """Return the parent that supplies this row's lineage edge.

                ``_delegate_from`` is authoritative when present. Falling back
                to the physical parent is only valid for legacy rows without
                that marker; otherwise a compression continuation whose physical
                parent differs from its lineage parent could be deleted.
                """
                delegate_from = record["delegate_from"]
                if delegate_from is not None:
                    return records_by_id.get(delegate_from) or {}
                return records_by_id.get(record["parent_id"]) or {}

            # Once a branch/compression continuation is preserved, its physical
            # child tree is outside the delete lineage. Stale inherited delegate
            # markers must not let traversal re-enter that retained subtree.
            preserved_ids = {
                record["id"]
                for record in records
                if record["id"] != sid
                and _must_preserve(record, _lineage_parent(record))
            }
            while True:
                descendants = {
                    record["id"]
                    for record in records
                    if record["id"] != sid
                    and record["parent_id"] in preserved_ids
                }
                new_ids = descendants - preserved_ids
                if not new_ids:
                    break
                preserved_ids.update(new_ids)

            found = {sid}
            frontier = {sid}
            while frontier:
                next_frontier = set()
                for record in records:
                    row_id = record["id"]
                    if row_id in found or row_id in preserved_ids:
                        continue
                    parent_id = record["parent_id"]
                    delegate_from = record["delegate_from"]
                    linked_to_frontier = (
                        delegate_from in frontier or parent_id in frontier
                    )
                    if not linked_to_frontier:
                        continue
                    parent = _lineage_parent(record)
                    if _must_preserve(record, parent):
                        continue
                    # Explicit _delegate_from is authoritative even when a
                    # migrated legacy row lacks source='subagent'. Branch and
                    # compression evidence above still wins and preserves it.
                    if delegate_from is not None:
                        if delegate_from in frontier:
                            next_frontier.add(row_id)
                        continue
                    # Only marker-less legacy inference requires the historical
                    # source tag plus a compatible physical-parent edge.
                    if record["source"] != "subagent":
                        continue
                    if (
                        parent_id in frontier
                        and record["model_config_known"]
                    ):
                        next_frontier.add(row_id)

                found.update(next_frontier)
                frontier = next_frontier

            delegate_ids = sorted(found - {sid})
            all_removed_ids = [sid, *delegate_ids]
            placeholders = ",".join("?" * len(all_removed_ids))

            # Delete delegate children first (messages, then orphan their
            # children, then the rows themselves).
            for child_id in delegate_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (child_id,))
            for child_id in delegate_ids:
                conn.execute(
                    "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                    (child_id,),
                )
            for child_id in delegate_ids:
                conn.execute("DELETE FROM sessions WHERE id = ?", (child_id,))

            # Preserve every remaining child as an independent row, matching
            # current SessionDB.delete_session().
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id = ?",
                (sid,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

            # Referential-integrity cleanup for session-owned rows that are
            # not covered by ON DELETE CASCADE.  session_model_usage and
            # telegram_dm_topic_bindings have FK CASCADE (enforced by
            # PRAGMA foreign_keys = ON above), but compression_locks has no
            # foreign key, so stale rows would survive.  Gate each table
            # on existence so this works on older schemas too.
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "compression_locks" in table_names:
                conn.execute(
                    f"DELETE FROM compression_locks WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )
            # Belt-and-suspenders: also explicitly clear FK tables in case
            # PRAGMA foreign_keys is OFF on an older SQLite build or a
            # future schema drops the CASCADE clause.
            if "session_model_usage" in table_names:
                conn.execute(
                    f"DELETE FROM session_model_usage WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )
            if "telegram_dm_topic_bindings" in table_names:
                conn.execute(
                    f"DELETE FROM telegram_dm_topic_bindings WHERE session_id IN ({placeholders})",
                    all_removed_ids,
                )

            # Persist a cleanup manifest BEFORE the commit so artifact
            # removal is idempotent and retryable.  Each call uses a unique
            # manifest filename (atomic temp-file + rename) so concurrent
            # deletes never clobber each other's retry records.
            sessions_dir = hermes_home / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            manifest_basename = f".cleanup_manifest_{uuid.uuid4().hex}"
            manifest_path = sessions_dir / f"{manifest_basename}.json"
            manifest_tmp = sessions_dir / f"{manifest_basename}.tmp"
            try:
                manifest_tmp.write_text(
                    json.dumps(sorted(str(i) for i in all_removed_ids)),
                    encoding="utf-8",
                )
                manifest_tmp.rename(manifest_path)
            except OSError:
                logger.warning("Failed to write cleanup manifest", exc_info=True)
                try:
                    manifest_tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Failed to remove incomplete cleanup manifest %s",
                        manifest_tmp,
                        exc_info=True,
                    )
                # Publishing the retry record is a prerequisite for making the
                # DB deletion durable. Without it, committed rows could vanish
                # while transcript artifacts remain forever and the UI reports
                # a false success.
                conn.rollback()
                return False

            conn.commit()

            # Post-commit artifact cleanup.  Scan ALL outstanding manifests
            # (including the one just written) and retry every pending ID.
            # Each manifest entry is re-checked against the DB so a stale
            # manifest from a failed commit never unlinks a live session's
            # artifacts.
            artifact_cleanup_failed = False

            def _is_session_alive(conn, sid):
                """True when *sid* still has a row in the sessions table.
                On any query failure, assume alive (fail closed) — an
                uncertain liveness check must never trigger artifact
                deletion on this unrecoverable state.db path.
                """
                try:
                    cursor = conn.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (sid,)
                    )
                    return cursor.fetchone() is not None
                except Exception:
                    return True

            def _clean_artifacts_for_id(sessions_dir, removed_id):
                """Remove on-disk transcript files for one session ID.
                Returns True when every artifact is gone (or was absent).
                """
                if not is_safe_session_id(removed_id):
                    return False
                ok = True
                for suffix in (".json", ".jsonl"):
                    artifact = sessions_dir / f"{removed_id}{suffix}"
                    if not artifact.exists():
                        continue
                    try:
                        artifact.unlink(missing_ok=True)
                    except OSError:
                        ok = False
                        logger.warning(
                            "Failed to remove session artifact %s%s",
                            removed_id,
                            suffix,
                            exc_info=True,
                        )
                try:
                    for path in list(
                        sessions_dir.glob(f"request_dump_{removed_id}_*.json")
                    ):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            ok = False
                            logger.warning(
                                "Failed to remove request dump %s",
                                path,
                                exc_info=True,
                            )
                except OSError:
                    ok = False
                    logger.warning(
                        "Failed to enumerate request dumps for %s",
                        removed_id,
                        exc_info=True,
                    )
                return ok

            for mp in sorted(sessions_dir.glob(".cleanup_manifest_*.json")):
                try:
                    raw = mp.read_text(encoding="utf-8")
                    pending_ids = json.loads(raw)
                except (OSError, ValueError, TypeError):
                    # The IDs are unknown, so deleting the manifest would lose
                    # the only retry record for potentially orphaned artifacts.
                    artifact_cleanup_failed = True
                    continue
                if not isinstance(pending_ids, list) or not all(
                    isinstance(item, str) for item in pending_ids
                ):
                    artifact_cleanup_failed = True
                    continue
                still_pending = []
                for removed_id in pending_ids:
                    # Transaction-outcome guard: never unlink artifacts for
                    # a session that still exists in the DB.  A stale
                    # manifest from a failed commit must not delete data
                    # belonging to a live conversation.
                    if _is_session_alive(conn, removed_id):
                        still_pending.append(removed_id)
                        continue
                    if not _clean_artifacts_for_id(sessions_dir, removed_id):
                        still_pending.append(removed_id)
                if still_pending:
                    # Atomic rewrite using temp-file + rename.
                    tmp = mp.with_suffix(".tmp")
                    try:
                        tmp.write_text(json.dumps(still_pending), encoding="utf-8")
                        tmp.rename(mp)
                    except OSError:
                        logger.warning(
                            "Failed to rewrite manifest %s", mp, exc_info=True,
                        )
                    artifact_cleanup_failed = True
                else:
                    mp.unlink(missing_ok=True)

            return stale_cleanup_complete and not artifact_cleanup_failed
    except Exception:
        logger.warning("Failed to delete CLI session %s from state.db", sid, exc_info=True)
        return False

# ---------------------------------------------------------------------------
# ``delete_cli_session`` nests each profile's cross-process file lock inside
# that profile's in-process lock so stale-manifest read, DB transaction, and
# post-commit cleanup remain one critical section without blocking unrelated
# profiles.
# ---------------------------------------------------------------------------
def _process_stale_cleanup_manifests(hermes_home) -> bool:
    """Process any leftover cleanup manifests outside a DB transaction.

    Called at the start of each delete_cli_session run, before the
    regular transaction, so a previous run whose DB commit succeeded
    but artifact cleanup failed gets another chance — even when the
    session ID being deleted this time is unrelated.

    Serialized by the caller's per-profile thread and process locks so that the
    manifest read → DB transaction → post-commit cleanup triad is
    never interleaved across concurrent delete calls. Returns ``True`` only
    when every discovered retry record was processed completely.
    """
    try:
        import sqlite3
    except ImportError:
        return False
    db_path = hermes_home / "state.db"
    sessions_dir = hermes_home / "sessions"
    if not sessions_dir.exists():
        return True
    manifests = sorted(sessions_dir.glob(".cleanup_manifest_*.json"))
    if not manifests:
        return True
    # Missing state.db is not proof that a manifested session is dead. It may
    # have been temporarily renamed, unmounted, or made inaccessible. Preserve
    # every manifest and artifact until a successful query proves absence.
    if not db_path.exists():
        return False
    cleanup_complete = True
    for mp in manifests:
        try:
            raw = mp.read_text(encoding="utf-8")
            pending_ids = json.loads(raw)
        except (OSError, ValueError, TypeError):
            # Preserve malformed/unreadable retry records. Their pending IDs
            # are unknowable, so silently deleting them would turn an
            # incomplete cleanup into a false success.
            cleanup_complete = False
            continue
        if not isinstance(pending_ids, list) or not all(
            isinstance(item, str) for item in pending_ids
        ):
            cleanup_complete = False
            continue
        pending_ids = list(pending_ids)
        if not pending_ids:
            mp.unlink(missing_ok=True)
            continue
        # Require a successful read-only query to prove absence. Opening via a
        # read-only URI also prevents SQLite from creating a fresh empty DB if
        # state.db disappears between the existence check and connect().
        try:
            db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(db_uri, uri=True)) as conn:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE id IN ({})".format(
                        ",".join("?" * len(pending_ids))
                    ),
                    pending_ids,
                )
                alive = {row[0] for row in cursor.fetchall()}
        except Exception:
            # Liveness query failed (missing DB, lock, timeout, I/O error).
            # Fail closed: preserve the manifest file on disk and skip it this
            # round. A later call can retry when DB state is queryable.
            cleanup_complete = False
            continue

        still_pending = []
        for removed_id in pending_ids:
            if removed_id in alive:
                # Session still exists — the previous commit never
                # reached the DB, so this manifest entry is stale.
                # Drop it silently: never propagate a stale manifest
                # into the post-commit cleanup loop where it would
                # cause the current call to report a false failure.
                continue
            if not _clean_pending_artifact(sessions_dir, removed_id):
                still_pending.append(removed_id)
        if still_pending:
            tmp = mp.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(still_pending), encoding="utf-8")
                tmp.rename(mp)
            except OSError:
                cleanup_complete = False
        else:
            try:
                mp.unlink(missing_ok=True)
            except OSError:
                cleanup_complete = False
    return cleanup_complete


def _clean_pending_artifact(sessions_dir, removed_id):
    """Remove on-disk transcript files for one session ID, outside a
    DB transaction.  Returns True when every artifact is gone (or absent).
    """
    if not is_safe_session_id(removed_id):
        return False
    ok = True
    for suffix in (".json", ".jsonl"):
        artifact = sessions_dir / f"{removed_id}{suffix}"
        if not artifact.exists():
            continue
        try:
            artifact.unlink(missing_ok=True)
        except OSError:
            ok = False
    try:
        for path in list(sessions_dir.glob(f"request_dump_{removed_id}_*.json")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                ok = False
    except OSError:
        ok = False
    return ok
