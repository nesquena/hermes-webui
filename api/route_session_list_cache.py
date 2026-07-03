"""Session-list cache helpers extracted from api.routes."""

import os
import copy
import threading
import time
from collections import OrderedDict
from pathlib import Path

from api.config import LOCK, SESSION_DIR, SESSIONS, SETTINGS_FILE
from api.models import _active_state_db_path, _active_stream_ids
from api.profiles import _profiles_match


_SESSIONS_CACHE_TTL_SECONDS = 2.5
# #4808: while a turn is actively streaming the frontend polls /api/sessions on a
# fixed cadence (static/sessions.js `_streamingPollMs` = 5000ms). With the idle TTL
# of 2.5s, every streaming poll lands in a fresh window and forces a full
# all_sessions() rebuild on the hot path under the global store LOCK — pinning CPU
# and starving token rendering on large stores (recurrence of #4672). Hold the
# sidebar cache steady for longer than one poll interval while streaming; live
# runtime state (active stream, sort order, pending flags) is overlaid on every
# response regardless of cache, and structural/settings changes still evict
# immediately via the source stamp.
_SESSIONS_CACHE_STREAMING_TTL_SECONDS = 10.0
_SESSIONS_CACHE_MAX_ENTRIES = 64
_SESSIONS_CACHE_WAIT_SECONDS = 0.25
_SESSIONS_CACHE_STALE_WAIT_SECONDS = 0.10
_SESSIONS_CACHE: OrderedDict[tuple, tuple[float, tuple, dict]] = OrderedDict()
_SESSIONS_CACHE_LOCK = threading.RLock()
_SESSIONS_CACHE_INFLIGHT: dict[tuple, threading.Event] = {}
_SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION = 0
_SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION = 0
_SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION: dict[str, int] = {}


def _session_list_cache_session_dir() -> Path:
    try:
        import api.routes as _routes

        value = getattr(_routes, "SESSION_DIR", SESSION_DIR)
        return Path(value)
    except Exception:
        return SESSION_DIR


def _session_list_cache_settings_file() -> Path:
    try:
        import api.routes as _routes

        value = getattr(_routes, "SETTINGS_FILE", SETTINGS_FILE)
        return Path(value)
    except Exception:
        return SETTINGS_FILE


def _session_list_cache_state_db_path():
    try:
        import api.routes as _routes

        override = getattr(_routes, "_active_state_db_path", None)
        if callable(override) and override is not _session_list_cache_state_db_path:
            return override()
    except Exception:
        pass
    return _active_state_db_path()


def _session_list_cache_gateway_session_metadata_path() -> Path:
    try:
        import api.routes as _routes

        override = getattr(_routes, "_gateway_session_metadata_path", None)
        if callable(override) and override is not _session_list_cache_gateway_session_metadata_path:
            return Path(override())
    except Exception:
        pass

    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().resolve()
    return hermes_home / "sessions" / "sessions.json"


def _session_list_cache_active_stream_ids():
    try:
        import api.routes as _routes

        override = getattr(_routes, "_active_stream_ids", None)
        if callable(override) and override is not _session_list_cache_active_stream_ids:
            return override()
    except Exception:
        pass
    return _active_stream_ids()


def _session_list_cache_resolved_source_stamp(key: tuple):
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_source_stamp", None)
        if callable(override) and override is not _session_list_cache_source_stamp:
            return override(key)
    except Exception:
        pass
    return _session_list_cache_source_stamp(key)


def _session_list_cache_profile_scope(profile: str | None) -> str:
    normalized = str(profile or "").strip() or "default"
    if _profiles_match(normalized, "default"):
        return "default"
    return normalized


def _session_list_cache_key(
    active_profile: str | None,
    all_profiles: bool,
    show_cli_sessions: bool,
    show_previous_messaging_sessions: bool,
    show_cron_sessions: bool,
    include_archived: bool = False,
    exclude_hidden: bool = False,
    visible_only: bool = False,
    show_webhook_sessions: bool = False,
    source_filter: str | None = None,
    sidebar_source: str | None = None,
    archived_limit: int | None = None,
    archived_offset: int = 0,
) -> tuple:
    normalized_archived_limit = None
    if archived_limit is not None:
        try:
            normalized_archived_limit = max(0, int(archived_limit))
        except (TypeError, ValueError):
            normalized_archived_limit = None
    try:
        normalized_archived_offset = max(0, int(archived_offset or 0))
    except (TypeError, ValueError):
        normalized_archived_offset = 0
    return (
        _session_list_cache_profile_scope(active_profile),
        bool(all_profiles),
        bool(show_cli_sessions),
        bool(show_previous_messaging_sessions),
        bool(show_cron_sessions),
        bool(include_archived),
        bool(exclude_hidden),
        bool(visible_only),
        bool(show_webhook_sessions),
        source_filter,
        sidebar_source,
        normalized_archived_limit,
        normalized_archived_offset,
    )


def _session_list_cache_get(
    key: tuple,
    allow_stale: bool = False,
) -> tuple[dict | None, bool]:
    now = time.monotonic()
    current_stamp = _session_list_cache_resolved_source_stamp(key)
    with _SESSIONS_CACHE_LOCK:
        entry = _SESSIONS_CACHE.get(key)
        if not entry:
            return None, False
        ts, stamp, payload = entry
        if stamp != current_stamp:
            if allow_stale:
                _SESSIONS_CACHE.move_to_end(key)
                return copy.deepcopy(payload), False
            _SESSIONS_CACHE.pop(key, None)
            return None, False
        # #4808: widen the freshness window while a turn is streaming so the fixed
        # 5s streaming poll cadence doesn't force a full rebuild on every poll.
        ttl = _SESSIONS_CACHE_TTL_SECONDS
        if _session_list_cache_streaming_freeze_marker() is not None:
            ttl = _SESSIONS_CACHE_STREAMING_TTL_SECONDS
        fresh = (now - ts) < ttl
        if fresh:
            _SESSIONS_CACHE.move_to_end(key)
            return copy.deepcopy(payload), True
        if allow_stale:
            _SESSIONS_CACHE.move_to_end(key)
            return copy.deepcopy(payload), False
        _SESSIONS_CACHE.pop(key, None)
        return None, False


def _session_list_cache_stale_reason(key: tuple) -> str | None:
    """Return why an existing cache entry is stale, if it is stale."""
    now = time.monotonic()
    current_stamp = _session_list_cache_resolved_source_stamp(key)
    with _SESSIONS_CACHE_LOCK:
        entry = _SESSIONS_CACHE.get(key)
        if not entry:
            return None
        ts, stamp, _payload = entry
        if stamp != current_stamp:
            return "source"
        ttl = _SESSIONS_CACHE_TTL_SECONDS
        if _session_list_cache_streaming_freeze_marker() is not None:
            ttl = _SESSIONS_CACHE_STREAMING_TTL_SECONDS
        if (now - ts) >= ttl:
            return "age"
        return None


def _session_list_cache_set(key: tuple, payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    stamp = _session_list_cache_resolved_source_stamp(key)
    with _SESSIONS_CACHE_LOCK:
        _SESSIONS_CACHE[key] = (time.monotonic(), stamp, copy.deepcopy(payload))
        _SESSIONS_CACHE.move_to_end(key)
        while len(_SESSIONS_CACHE) > _SESSIONS_CACHE_MAX_ENTRIES:
            _SESSIONS_CACHE.popitem(last=False)


def _session_list_cache_clear(profile: str | None = None) -> None:
    normalized_profile = _session_list_cache_profile_scope(profile) if profile else None
    with _SESSIONS_CACHE_LOCK:
        global _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
        global _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION
        if not profile:
            _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION += 1
            _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION += 1
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.clear()
            _SESSIONS_CACHE.clear()
            return
        _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION += 1
        _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION[normalized_profile] = (
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.get(normalized_profile, 0) + 1
        )
        for cache_key in list(_SESSIONS_CACHE.keys()):
            cache_profile, cache_all_profiles, *_rest = cache_key
            if cache_all_profiles:
                _SESSIONS_CACHE.pop(cache_key, None)
                continue
            if _profiles_match(cache_profile, normalized_profile):
                _SESSIONS_CACHE.pop(cache_key, None)


def _clear_session_list_cache(profile: str | None = None) -> None:
    _session_list_cache_clear(profile=profile)


def _session_list_cache_invalidation_stamp(key: tuple) -> tuple[int, int]:
    cache_profile, cache_all_profiles, *_rest = key
    with _SESSIONS_CACHE_LOCK:
        global_version = _SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
        if cache_all_profiles:
            return (
                global_version,
                _SESSIONS_CACHE_ALL_PROFILES_INVALIDATION_VERSION,
            )
        return (
            global_version,
            _SESSIONS_CACHE_PROFILE_INVALIDATION_VERSION.get(cache_profile, 0),
        )


def _session_list_cache_path_stamp(path: Path | None) -> tuple[int, int]:
    try:
        if path is None:
            return (0, 0)
        st = Path(path).stat()
        return (int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))), int(st.st_size))
    except Exception:
        return (0, 0)


def _session_list_cache_streaming_freeze_marker():
    """Return a hold-down marker while any session is actively streaming, else None.

    During an active chat turn the gateway/CLI writes message rows to state.db
    continuously. Each write advances the WAL stat and the content fingerprint
    (``MAX(rowid)`` of ``messages``) that ``_session_list_cache_source_stamp``
    folds in, so the source stamp changes on essentially every ``/api/sessions``
    poll — popping the cache and forcing a full ``all_sessions()`` rebuild
    mid-stream. That rebuild then contends for the global ``LOCK`` the streaming
    worker holds while writing, which is what drags token output down to
    ~2 tok/s and produces the multi-second (and occasional ~15s) ``/api/sessions``
    latencies in issue #4672.

    The marker is keyed only on the *set* of active stream ids, not on any
    per-write state, so:
      * while the same turn(s) stream, the marker is constant → the cache holds
        steady and rebuilds are bounded to the TTL cadence (one per
        ``_SESSIONS_CACHE_TTL_SECONDS``) instead of one per poll;
      * the instant a stream starts or stops, the active set changes → the
        marker changes → the cache re-validates and the just-finished turn's
        final title/message_count is picked up immediately.

    Structural sidebar mutations (new/deleted/renamed/imported sessions,
    attention, cron completion) do NOT rely on this stamp — they invalidate the
    cache directly through the ``publish_session_list_changed`` listener — so the
    only thing that can lag under the hold-down is a streaming session's own
    title/message_count, which already tolerates a <=TTL refresh delay.
    """
    try:
        active = _session_list_cache_active_stream_ids()
    except Exception:
        return None
    if not active:
        return None
    try:
        return ("streaming", tuple(sorted(str(x) for x in active)))
    except Exception:
        return ("streaming",)


def _session_list_cache_state_db_fingerprint(state_db_path: Path | None):
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_state_db_fingerprint", None)
        if callable(override) and override is not _session_list_cache_state_db_fingerprint:
            return override(state_db_path)
    except Exception:
        pass
    return _session_list_cache_state_db_fingerprint_impl(state_db_path)


def _session_list_cache_state_db_fingerprint_impl(state_db_path: Path | None):
    if state_db_path is None:
        return None
    try:
        from api.models import _sqlite_content_fingerprint

        return _sqlite_content_fingerprint(state_db_path)
    except Exception:
        return None


def _session_list_cache_source_stamp(key: tuple) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int], object, int]:
    _cache_profile, _cache_all_profiles, _cache_show_cli_sessions, *_rest = key
    try:
        swv = _session_list_cache_settings_write_version()
    except Exception:
        swv = 0
    # WebUI-origin sessions can also receive settled rows in state.db when the
    # official Hermes Desktop App continues the same agent session.  The sidebar
    # therefore watches state.db even when the CLI/external-session tab is hidden.
    #
    # Streaming hold-down (#4672): while a turn is in flight, collapse the
    # volatile state.db-derived components (db/WAL stat, gateway metadata, index
    # stat, content fingerprint) to a marker that only changes when a stream
    # starts or stops. This stops per-token message writes from busting the
    # cache and triggering LOCK-contending rebuilds on every poll. The TTL still
    # forces a periodic rebuild so the streaming session's own count/title stay
    # fresh within the TTL window, and settings_file + the settings write
    # version stay live so user-initiated sidebar/setting toggles invalidate
    # immediately. Skipping the fingerprint's SQLite connect here also makes the
    # streaming-path stamp strictly cheaper than the idle path.
    streaming_marker = _session_list_cache_streaming_freeze_marker()
    if streaming_marker is not None:
        return (
            streaming_marker,
            streaming_marker,
            streaming_marker,
            streaming_marker,
            _session_list_cache_path_stamp(_session_list_cache_settings_file()),
            streaming_marker,
            swv,
        )
    try:
        state_db_path = Path(_session_list_cache_state_db_path())
    except Exception:
        state_db_path = None
    try:
        state_db_wal_path = state_db_path.with_name(f"{state_db_path.name}-wal") if state_db_path is not None else None
    except Exception:
        state_db_wal_path = None
    try:
        gateway_metadata_path = _session_list_cache_gateway_session_metadata_path()
    except Exception:
        gateway_metadata_path = None
    try:
        session_index_path = _session_list_cache_session_dir() / "_index.json"
    except Exception:
        session_index_path = None
    return (
        _session_list_cache_path_stamp(state_db_path),
        _session_list_cache_path_stamp(state_db_wal_path),
        _session_list_cache_path_stamp(gateway_metadata_path),
        _session_list_cache_path_stamp(session_index_path),
        _session_list_cache_path_stamp(_session_list_cache_settings_file()),
        # Commit-reliable content fingerprint of state.db — the file-stat stamps
        # above can collide under WAL-mode writes (same mtime_ns bucket + WAL
        # frame size), so without this a freshly-committed CLI/gateway session
        # could be served stale for the cache TTL. Mirrors the models-layer fix.
        _session_list_cache_state_db_fingerprint(state_db_path),
        swv,
    )


def _session_list_cache_settings_write_version() -> int:
    try:
        import api.routes as _routes

        override = getattr(_routes, "_session_list_cache_settings_write_version", None)
        if callable(override) and override is not _session_list_cache_settings_write_version:
            return int(override())
    except Exception:
        pass
    try:
        from api.config import _SETTINGS_WRITE_VERSION

        return int(_SETTINGS_WRITE_VERSION)
    except Exception:
        return 0


def _session_list_cache_overlay_runtime_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    try:
        active_stream_ids = _session_list_cache_active_stream_ids()
    except Exception:
        active_stream_ids = set()
    session_ids = [
        str(row.get("session_id") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("session_id") or "").strip()
    ]
    live_sessions = {}
    if session_ids:
        with LOCK:
            for sid in session_ids:
                live = SESSIONS.get(sid)
                if live is not None:
                    live_sessions[sid] = live
    overlaid = []
    for row in rows:
        item = dict(row) if isinstance(row, dict) else {}
        sid = str(item.get("session_id") or "").strip()
        live = live_sessions.get(sid)
        if live is not None:
            live_stream_id = getattr(live, "active_stream_id", None)
            item["active_stream_id"] = live_stream_id or None
            item["has_pending_user_message"] = bool(
                getattr(live, "pending_user_message", None)
            )
            for key in ("pending_started_at", "updated_at", "last_message_at"):
                current = _session_list_row_numeric_value(item.get(key))
                raw_live_value = getattr(live, key, None)
                live_value = _session_list_row_numeric_value(raw_live_value)
                if live_value > current:
                    item[key] = raw_live_value
        stream_id = item.get("active_stream_id")
        item["is_streaming"] = bool(stream_id and stream_id in active_stream_ids)
        overlaid.append(item)
    overlaid.sort(key=_session_list_runtime_sort_key, reverse=True)
    return overlaid


def _session_list_row_numeric_value(value) -> float:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0 else 0.0


def _session_list_row_timestamp(row: dict) -> float:
    if not isinstance(row, dict):
        return 0.0
    # Match the frontend `_sessionSortTimestampMs` semantics exactly (#4688 review):
    # the idle base is the FIRST truthy of last_message_at -> updated_at -> created_at
    # (NOT a flat max over all of them — a renamed/metadata-touched idle chat bumps
    # updated_at without new messages and must not outrank a newer chatted session),
    # then pending_started_at is overlaid only as the runtime promotion.
    base = 0.0
    for key in ("last_message_at", "updated_at", "created_at"):
        base = _session_list_row_numeric_value(row.get(key))
        if base > 0:
            break
    pending = _session_list_row_numeric_value(row.get("pending_started_at"))
    return max(base, pending)


def _session_list_row_is_runtime_active(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("is_streaming"):
        return True
    return bool(row.get("active_stream_id") and row.get("has_pending_user_message"))


def _session_list_runtime_sort_key(row: dict) -> tuple[int, float]:
    return (
        1 if _session_list_row_is_runtime_active(row) else 0,
        _session_list_row_timestamp(row),
    )


def _session_list_cache_claim_rebuild(key: tuple) -> tuple[threading.Event, bool]:
    with _SESSIONS_CACHE_LOCK:
        current = _SESSIONS_CACHE_INFLIGHT.get(key)
        if current is not None:
            return current, False
        event = threading.Event()
        _SESSIONS_CACHE_INFLIGHT[key] = event
        return event, True


def _session_list_cache_done(key: tuple, event: threading.Event | None) -> None:
    with _SESSIONS_CACHE_LOCK:
        if event is None:
            return
        if _SESSIONS_CACHE_INFLIGHT.get(key) is event:
            _SESSIONS_CACHE_INFLIGHT.pop(key, None)
    if event is not None:
        event.set()
