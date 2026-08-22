"""Coverage for skipping the state.db load on a display-merge cache hit.

GET /api/session used to materialise every state.db row for a session even when
the merged transcript was already memoized -- ~2.3s of pure waste on a 36k-row
session. The rows are only consumed by the merge, so a cache hit can skip the
load entirely.

That is only sound because the cache key no longer depends on the loaded rows.
These tests pin the two properties that make the shortcut safe:
  1. a hit returns EXACTLY what the full load+merge path returns;
  2. anything uncertain (active session, unbuildable key, changed data) falls
     back to the full load rather than serving a stale transcript.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import routes  # noqa: E402

SID = "20260301_090000_cache1"


class _Session:
    """Minimal session stand-in for the cache-probe contract."""

    def __init__(self, sid=SID, active=None, pending=None):
        self.session_id = sid
        self.profile = None
        self.active_stream_id = active
        self.pending_user_message = pending
        self.truncation_watermark = None
        self.truncation_boundary = None


@pytest.fixture(autouse=True)
def _clean_cache():
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    yield
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()


@pytest.fixture()
def stable_key(monkeypatch):
    """Make the cache key deterministic without touching the filesystem."""
    monkeypatch.setattr(
        "api.models._sidecar_stat_signature", lambda p: ("sig", 1, 2, 3), raising=False)
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: "SIG-A")
    return "SIG-A"


def _seed(session, sidecar, merged):
    key = routes._display_merge_cache_key(session, sidecar, None)
    assert key is not None
    with routes._display_merge_cache_lock:
        routes._display_merge_cache[session.session_id] = {
            "key": key,
            "messages": merged,
            "stored_at": time.monotonic(),
        }
    return key


# --------------------------------------------------------------------------
# The shortcut must be exact.
# --------------------------------------------------------------------------

def test_cache_hit_returns_the_memoized_transcript(stable_key):
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    merged = [
        {"role": "user", "content": "hi", "timestamp": 10.0},
        {"role": "assistant", "content": "hello", "timestamp": 11.0},
    ]
    _seed(session, sidecar, merged)

    got = routes._display_merge_cached_messages(session, sidecar)
    assert got == merged


def test_cache_hit_returns_copies_not_shared_rows(stable_key):
    """Callers attach display metadata; the cached rows must not be mutated."""
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    merged = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, merged)

    got = routes._display_merge_cached_messages(session, sidecar)
    got[0]["injected"] = True

    again = routes._display_merge_cached_messages(session, sidecar)
    assert "injected" not in again[0], "cache entry was mutated by a caller"


def test_shortcut_matches_the_full_merge_path(monkeypatch, stable_key):
    """The whole point: identical output with and without the shortcut.

    Populate the cache through the real merge helper, then assert the probe
    returns the same transcript the helper would have recomputed.
    """
    session = _Session()
    sidecar = [{"role": "user", "content": "one", "timestamp": 1.0}]
    rows = [
        {"role": "user", "content": "one", "timestamp": 1.0},
        {"role": "assistant", "content": "two", "timestamp": 2.0},
    ]

    full = routes._limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar,
        rows,
        state_db_signature=stable_key,
    )
    probed = routes._display_merge_cached_messages(session, sidecar)

    assert probed is not None, "merge helper did not populate the cache"
    assert probed == full


# --------------------------------------------------------------------------
# Fail-closed: never serve a stale transcript.
# --------------------------------------------------------------------------

def test_active_stream_never_uses_the_cache(stable_key):
    """An active session's in-memory tail can be ahead of its disk signature."""
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    session.active_stream_id = "stream-123"
    assert routes._display_merge_cached_messages(session, sidecar) is None


def test_pending_user_message_never_uses_the_cache(stable_key):
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    session.pending_user_message = {"content": "not yet on disk"}
    assert routes._display_merge_cached_messages(session, sidecar) is None


def test_live_memory_session_blocks_hit_from_stale_idle_object(stable_key):
    """A stale disk object must not bypass the canonical in-memory active queue."""
    stale = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    merged = [{"role": "user", "content": "cached", "timestamp": 10.0}]
    _seed(stale, sidecar, merged)
    live = _Session(active="stream-live")

    with routes.LOCK:
        previous = routes.SESSIONS.get(stale.session_id)
        routes.SESSIONS[stale.session_id] = live
    try:
        assert routes._display_merge_cached_messages(stale, sidecar) is None
    finally:
        with routes.LOCK:
            if previous is None:
                routes.SESSIONS.pop(stale.session_id, None)
            else:
                routes.SESSIONS[stale.session_id] = previous


def test_changed_state_db_signature_misses(monkeypatch, stable_key):
    """New state.db rows must invalidate the entry, not be silently skipped."""
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: "SIG-B")
    assert routes._display_merge_cached_messages(session, sidecar) is None


def test_streaming_freeze_key_hits_before_ttl(monkeypatch, stable_key):
    marker = ("streaming", ("run-1",))
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: marker)
    now = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    merged = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, merged)

    now[0] += routes._DISPLAY_MERGE_STREAMING_TTL_SECONDS - 0.1
    assert routes._display_merge_cached_messages(session, sidecar) == merged


def test_streaming_freeze_key_expires_after_ttl(monkeypatch, stable_key):
    marker = ("streaming", ("run-1",))
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: marker)
    now = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    now[0] += routes._DISPLAY_MERGE_STREAMING_TTL_SECONDS + 0.1
    assert routes._display_merge_cached_messages(session, sidecar) is None


def test_changed_sidecar_tail_misses(stable_key):
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    grown = sidecar + [{"role": "assistant", "content": "new", "timestamp": 12.0}]
    assert routes._display_merge_cached_messages(session, grown) is None


def test_empty_cache_misses(stable_key):
    assert routes._display_merge_cached_messages(_Session(), []) is None


def test_msg_before_pagination_never_uses_tail_cache(stable_key):
    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "cached tail", "timestamp": 10.0}])

    assert routes._display_merge_cached_messages(
        session,
        sidecar,
        msg_before=100,
    ) is None


def test_probe_fails_closed_when_bounded_signature_unavailable(monkeypatch):
    """Without the bounded signature the key would need the rows we skipped.

    Fingerprinting the absent rows would key on an empty row set and could match
    an entry built from real rows -- so the probe must refuse instead.
    """
    monkeypatch.setattr(
        "api.models._sidecar_stat_signature", lambda p: ("sig", 1, 2, 3), raising=False)
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: "SIG-A")

    session = _Session()
    sidecar = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    _seed(session, sidecar, [{"role": "user", "content": "hi", "timestamp": 10.0}])

    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: None)
    assert routes._display_merge_cached_messages(session, sidecar) is None


def test_cache_key_refuses_none_rows_without_bounded_signature(monkeypatch):
    """Direct contract test for the probe-path guard in the key builder."""
    monkeypatch.setattr(
        "api.models._sidecar_stat_signature", lambda p: ("sig", 1, 2, 3), raising=False)
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *a, **k: None)

    key = routes._display_merge_cache_key(_Session(), [{"timestamp": 1.0}], None)
    assert key is None


def test_unsafe_session_id_is_refused(stable_key):
    bad = _Session(sid="../../etc/passwd")
    assert routes._display_merge_cached_messages(bad, []) is None


def test_loader_refuses_signature_when_commit_occurs_during_read(monkeypatch):
    signatures = iter(("SIG-A", "SIG-B"))
    monkeypatch.setattr(
        routes,
        "_state_db_session_signature",
        lambda *args, **kwargs: next(signatures),
    )
    rows = [{"role": "assistant", "content": "snapshot"}]
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: rows)

    loaded, stable = routes._load_state_db_messages_with_stable_signature(
        SID,
        profile=None,
        reader_kwargs={},
    )

    assert loaded == rows
    assert stable is None


def test_loader_returns_signature_when_read_window_is_stable(monkeypatch):
    monkeypatch.setattr(routes, "_state_db_session_signature", lambda *args, **kwargs: "SIG-A")
    rows = [{"role": "assistant", "content": "snapshot"}]
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *args, **kwargs: rows)

    loaded, stable = routes._load_state_db_messages_with_stable_signature(
        SID,
        profile=None,
        reader_kwargs={},
    )

    assert loaded == rows
    assert stable == "SIG-A"


def test_merge_does_not_publish_when_signature_changes_before_store(monkeypatch):
    monkeypatch.setattr(
        "api.models._sidecar_stat_signature", lambda path: ("sig", 1, 2, 3), raising=False
    )
    signatures = iter(("SIG-A", "SIG-B"))
    monkeypatch.setattr(
        routes,
        "_state_db_session_signature",
        lambda *args, **kwargs: next(signatures),
    )
    session = _Session()
    sidecar = [{"role": "user", "content": "one", "timestamp": 1.0}]
    rows = [{"role": "assistant", "content": "two", "timestamp": 2.0}]

    merged = routes._limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar,
        rows,
        state_db_signature="SIG-A",
    )

    assert len(merged) == 2
    with routes._display_merge_cache_lock:
        assert session.session_id not in routes._display_merge_cache
