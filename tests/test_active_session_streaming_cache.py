"""Active-session (streaming) merge cache: key builder + store/probe/evict.

The display-merge cache shipped gated to INACTIVE sessions only (upstream
#7212). This suite covers the streaming twin for ACTIVE (streaming/pending)
sessions: a fail-closed composite key over every input the merge consumed
(sidecar stat sig, lineage parent sigs, in-memory tail marker, target-session
state.db revision, truncation fields, msg_limit), a TTL-bounded LRU
store/probe pair, and eviction helpers. Any unresolvable component must yield
None (never a stale transcript).
"""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def routes_env(tmp_path, monkeypatch):
    import api.config as config
    import api.models as models
    import api.routes as routes

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
    yield SimpleNamespace(config=config, models=models, routes=routes)
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()
    with routes._display_streaming_merge_cache_lock:
        routes._display_streaming_merge_cache.clear()


SID = "20260101_000000_stream1"
STATE_SIG = ("target-session-revision-v1", 42)
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
    """Pin the target-session state.db signature (no real state.db in tests)."""
    seen = []

    def _fake(session_id, profile=None):
        seen.append((session_id, profile))
        return sig

    monkeypatch.setattr(routes_env.routes, "_state_db_session_signature", _fake)
    return seen


def test_key_builder_fail_closed_without_sidecar(routes_env, monkeypatch):
    """No sidecar file on disk -> stat sig unresolvable -> key is None."""
    routes = routes_env.routes
    seen = _patch_state_sig(routes_env, monkeypatch)
    s = _make_session([{"role": "user", "content": "hi", "timestamp": 1.0, "id": "a"}])

    assert routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT) is None
    assert seen == []  # failed closed before ever touching the state.db signature


def test_key_builder_returns_full_composite(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    seen = _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)

    key = routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT)

    assert key is not None
    assert key[0] == ("streaming-active",)
    assert key[1] == routes_env.models._sidecar_stat_signature(
        routes.SESSION_DIR / f"{SID}.json"
    )
    assert key[2] == ()  # no lineage parents recorded for this sid
    assert key[3] == len(rows)
    assert key[4] == (rows[-1]["timestamp"], rows[-1]["id"])
    assert key[5] == STATE_SIG
    assert seen == [(SID, None)]
    assert key[6] is None  # truncation_watermark
    assert key[7] is None  # truncation_boundary
    assert key[8] == MSG_LIMIT


def test_key_builder_carries_truncation_fields(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    s.truncation_watermark = 17
    s.truncation_boundary = "boundary-x"

    key = routes._display_streaming_merge_cache_key(s, msg_limit=MSG_LIMIT)

    assert key[6] == 17
    assert key[7] == "boundary-x"


def test_store_is_noop_when_key_unbuildable(routes_env, monkeypatch):
    routes = routes_env.routes
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session([{"role": "user", "timestamp": 1.0, "id": "a"}])  # no sidecar

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )

    assert routes._display_streaming_merge_cache == {}


def test_store_probe_roundtrip_returns_shallow_copies(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    merged = [
        {"role": "user", "content": "m0", "timestamp": 1.0, "id": "a"},
        {"role": "assistant", "content": "m1", "timestamp": 2.0, "id": "b"},
    ]

    routes.store_streaming_merge_entry(s, msg_limit=MSG_LIMIT, messages=merged)
    assert SID in routes._display_streaming_merge_cache

    hit = routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT)
    assert hit == merged
    # Mutating the served copy must not corrupt the cached rows.
    hit[0]["content"] = "MUTATED"
    hit.append({"role": "user", "content": "extra"})

    again = routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT)
    assert again[0]["content"] == "m0"
    assert len(again) == 2
    # msg_limit is part of the composite key: a different window must miss.
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT + 10) is None


def test_probe_skips_inactive_session(routes_env, monkeypatch):
    """Inactive sessions belong to the legacy display-merge cache, not this one."""
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    active = _make_session(rows, active=True)
    routes.store_streaming_merge_entry(
        active, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )

    inactive = _make_session(rows, active=False)
    assert routes.probe_streaming_merge_entry(inactive, msg_limit=MSG_LIMIT) is None


def test_probe_misses_after_ttl_and_evicts_entry(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)
    now = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is not None

    now[0] += routes._DISPLAY_MERGE_STREAMING_TTL_SECONDS + 0.01
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None
    assert SID not in routes._display_streaming_merge_cache  # expired entry evicted

    # Clock back inside the TTL: still a miss -- the entry is gone for good.
    now[0] = 1001.0
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None


def test_evict_streaming_merge_entries(routes_env, monkeypatch):
    routes = routes_env.routes
    rows = _write_sidecar(routes_env)
    _patch_state_sig(routes_env, monkeypatch)
    s = _make_session(rows)

    routes.store_streaming_merge_entry(
        s, msg_limit=MSG_LIMIT, messages=[{"role": "user", "content": "m0"}]
    )
    assert SID in routes._display_streaming_merge_cache

    routes.evict_streaming_merge_entries(SID)
    assert routes.probe_streaming_merge_entry(s, msg_limit=MSG_LIMIT) is None
    assert SID not in routes._display_streaming_merge_cache
    routes.evict_streaming_merge_entries("20260101_000000_unknown")  # no raise


def test_evict_streaming_redact_entry_calls_helpers_pop(routes_env, monkeypatch):
    """Turn-end belt hook forwards to api.helpers._session_redact_cache_pop.

    That helper lands in Task 5; until then the getattr guard makes this a
    silent no-op, so the hook is wired here via monkeypatch (raising=False
    covers both the pre-Task-5 absence and the post-Task-5 real attribute).
    """
    import api.helpers as helpers

    routes = routes_env.routes
    calls = []
    monkeypatch.setattr(
        helpers, "_session_redact_cache_pop", lambda sid: calls.append(sid),
        raising=False,
    )

    routes.evict_streaming_redact_entry(SID)
    assert calls == [SID]

    routes.evict_streaming_redact_entry("")  # empty sid: no-op, no raise
    assert calls == [SID]
