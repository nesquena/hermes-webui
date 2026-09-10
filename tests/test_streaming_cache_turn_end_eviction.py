"""Turn-end eviction belt for the streaming merge/redact display caches.

The streaming merge cache and (Task 5) streaming redact memo speak only for
repeat polls of an UNCHANGED in-memory tail. Once a turn settles -- done,
cancel, or apperror -- the tail changed, so any cached entry for the session
is dead weight at best. streaming._evict_display_caches_for_session is the
belt-and-suspenders drop that runs at every terminal stream event; these
tests exercise the helper directly (no live streams).
"""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def routes_env(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.streaming as streaming

    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(state_dir))
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    with routes._display_streaming_merge_cache_lock:
        routes._display_streaming_merge_cache.clear()
    yield SimpleNamespace(
        config=config, models=models, routes=routes, streaming=streaming
    )
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    with routes._display_streaming_merge_cache_lock:
        routes._display_streaming_merge_cache.clear()


SID = "20260101_000000_turnend1"
STATE_SIG = ("target-session-revision-v1", 7)
MSG_LIMIT = 50


def _write_sidecar(routes_env, sid=SID, n=3):
    rows = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}",
            "timestamp": 1000.0 + i,
            "id": f"row-{i}",
        }
        for i in range(n)
    ]
    path = routes_env.routes.SESSION_DIR / f"{sid}.json"
    path.write_text(json.dumps({"session_id": sid, "messages": rows}))
    return rows


def _make_session(messages, *, sid=SID, active=True):
    return SimpleNamespace(
        session_id=sid,
        profile=None,
        messages=list(messages),
        active_stream_id="stream-live" if active else None,
        pending_user_message=None,
        truncation_watermark=None,
        truncation_boundary=None,
    )


def _patch_state_sig(routes_env, monkeypatch, sig=STATE_SIG):
    def _fake(session_id, profile=None):
        return sig

    monkeypatch.setattr(routes_env.routes, "_state_db_session_signature", _fake)


def _store_entry(routes_env, monkeypatch, sid=SID):
    """Populate the streaming merge cache the same way the GET poll path does."""
    routes = routes_env.routes
    rows = _write_sidecar(routes_env, sid=sid)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows, sid=sid)
    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )
    assert sid in routes._display_streaming_merge_cache
    return s


def test_turn_end_evicts_streaming_merge_entries(routes_env, monkeypatch):
    """(a) A stored streaming entry is gone after the turn-end eviction."""
    routes = routes_env.routes
    s = _store_entry(routes_env, monkeypatch)
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is not None

    routes_env.streaming._evict_display_caches_for_session(SID)

    assert SID not in routes._display_streaming_merge_cache
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None


def test_eviction_without_entry_is_noop(routes_env, monkeypatch):
    """(b) Evicting a session with no cached entry must not raise."""
    routes = routes_env.routes
    _store_entry(routes_env, monkeypatch)

    # Unknown sid: no entry, no exception.
    routes_env.streaming._evict_display_caches_for_session("20260101_000000_unknown")
    assert SID in routes._display_streaming_merge_cache

    # Empty/missing sid resolves to no-op rather than a KeyError/AttributeError.
    routes_env.streaming._evict_display_caches_for_session(None)
    routes_env.streaming._evict_display_caches_for_session("")
    assert SID in routes._display_streaming_merge_cache


def test_helper_calls_both_routes_level_evictions(routes_env, monkeypatch):
    """(c) The centralized helper forwards to BOTH routes-level evictions."""
    routes = routes_env.routes
    calls = []
    monkeypatch.setattr(
        routes, "evict_streaming_merge_entries",
        lambda sid: calls.append(("merge", sid)),
    )
    monkeypatch.setattr(
        routes, "evict_streaming_redact_entry",
        lambda sid: calls.append(("redact", sid)),
    )

    routes_env.streaming._evict_display_caches_for_session(SID)
    assert calls == [("merge", SID), ("redact", SID)]

    # A session-like object resolves to its session_id.
    s = _make_session([], sid=SID)
    routes_env.streaming._evict_display_caches_for_session(s)
    assert calls == [("merge", SID), ("redact", SID)] * 2


def test_real_redact_forward_is_tolerated_pre_task5(routes_env, monkeypatch):
    """The unmocked redact forward is a graceful no-op until Task 5 lands.

    evict_streaming_redact_entry lazily resolves
    api.helpers._session_redact_cache_pop via getattr; while that helper is
    absent the whole chain must still complete silently.
    """
    routes = routes_env.routes
    _store_entry(routes_env, monkeypatch)

    routes_env.streaming._evict_display_caches_for_session(SID)

    assert SID not in routes._display_streaming_merge_cache


def test_eviction_failures_are_swallowed(routes_env, monkeypatch):
    """Best-effort contract: an evictor blowing up must not raise out of the belt."""

    def _boom(sid):
        raise RuntimeError("eviction boom")

    monkeypatch.setattr(routes_env.routes, "evict_streaming_merge_entries", _boom)

    routes_env.streaming._evict_display_caches_for_session(SID)  # no raise
