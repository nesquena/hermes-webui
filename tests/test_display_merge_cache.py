"""The display merge for paginated loads must be memoized for inactive sessions.

Before the fix, GET /api/session re-ran merge_session_messages_append_only on
every request (~2-3s for multi-thousand-message transcripts). The cache must:

- return the same merged transcript as a direct merge (correctness),
- serve copies (caller mutation cannot corrupt the cache),
- invalidate when the sidecar file changes on disk,
- invalidate when the state.db rows change,
- never cache an ACTIVE session whose stream this process cannot see
  (active_stream_id / pending_user_message without a registered in-process
  stream — cross-process gateway/CLI turn, fail closed),
- for an ACTIVE session WITH a registered in-process stream, reuse the memoized
  merge only under the streaming freeze marker and only within
  ``_DISPLAY_MERGE_STREAMING_TTL_SECONDS`` (RC1), while ``msg_before`` paging
  keeps bypassing the cache entirely.
"""

import time
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
    streams_before = set(config.STREAMS.keys())
    yield SimpleNamespace(config=config, models=models, routes=routes)
    # Remove only the stream registry entries this module's tests added.
    for stream_id in list(config.STREAMS.keys()):
        if stream_id not in streams_before:
            config.STREAMS.pop(stream_id, None)
    with routes._display_merge_cache_lock:
        routes._display_merge_cache.clear()


def _make_session(routes_env, sid="20260101_000000_cache1", n=6):
    models = routes_env.models
    s = models.Session(session_id=sid)
    now = time.time()
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        s.messages.append({"role": role, "content": f"turn {i}", "timestamp": now + i})
    s.save()
    return s


def _state_rows(base_ts, n=2):
    return [
        {"role": "assistant", "content": f"state row {i}", "timestamp": base_ts + 100 + i}
        for i in range(n)
    ]


def test_merge_cached_and_equal_to_direct_merge(routes_env):
    routes = routes_env.routes
    s = _make_session(routes_env)
    rows = _state_rows(s.messages[-1]["timestamp"])

    first = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert routes._display_merge_cache, "expected a cache entry for an inactive session"
    second = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert [m.get("content") for m in first] == [m.get("content") for m in second]
    assert any(m.get("content") == "state row 0" for m in second)


def test_cache_hit_returns_copies(routes_env):
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache2")
    rows = _state_rows(s.messages[-1]["timestamp"])

    first = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    first[0]["content"] = "MUTATED"
    second = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert second[0]["content"] != "MUTATED"


def test_cache_invalidated_by_sidecar_write(routes_env):
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache3")
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    s.messages.append({
        "role": "assistant", "content": "new turn after write",
        "timestamp": time.time() + 50,
    })
    s.save()
    merged = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert any(m.get("content") == "new turn after write" for m in merged)


def test_cache_invalidated_by_state_rows_change(routes_env):
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache4")
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    rows2 = rows + [{
        "role": "assistant", "content": "brand new state row",
        "timestamp": rows[-1]["timestamp"] + 5,
    }]
    merged = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows2)
    assert any(m.get("content") == "brand new state row" for m in merged)


def test_active_session_without_registered_stream_is_never_cached(routes_env):
    """Fail-closed: a stream this process cannot see must not be cached.

    An in-memory tail from a cross-process (gateway/CLI) turn cannot be
    verified against any signature this process can build, so without a
    registered in-process stream the merge always recomputes.
    """
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache5")
    s.active_stream_id = "stream-live"
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert s.session_id not in routes._display_merge_cache


def _register_stream(routes_env, stream_id="stream-live"):
    """Make ``stream_id`` visible to the in-process active-stream registry."""
    import api.config as config

    routes_env.config.STREAMS[stream_id] = {"session_id": "test"}
    return stream_id


def test_active_session_with_registered_stream_uses_streaming_cache(routes_env):
    """RC1: an in-process active stream may reuse the memoized merge.

    The entry must be stored under the streaming freeze marker so
    ``_display_merge_cache_entry_usable`` bounds it to the streaming TTL, and
    repeated tail loads within the TTL must not re-run the O(history) merge.
    """
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache6")
    s.active_stream_id = "stream-live"
    _register_stream(routes_env)
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    merge_calls = {"n": 0}
    real_merge = routes.merge_session_messages_append_only

    def counting_merge(*args, **kwargs):
        merge_calls["n"] += 1
        return real_merge(*args, **kwargs)

    routes.merge_session_messages_append_only = counting_merge
    try:
        first = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
        second = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    finally:
        routes.merge_session_messages_append_only = real_merge

    with routes._display_merge_cache_lock:
        entry = routes._display_merge_cache.get(s.session_id)
    assert entry is not None, "expected a streaming cache entry"
    state_key = entry["key"][4]
    assert isinstance(state_key, tuple) and state_key[0] == "streaming", (
        "active-session entries must be keyed on the streaming freeze marker"
    )
    assert merge_calls["n"] == 1, "second tail load within the TTL re-ran the merge"
    assert [m.get("content") for m in first] == [m.get("content") for m in second]
    assert any(m.get("content") == "state row 0" for m in second)


def test_streaming_cache_entry_expires_after_ttl(routes_env, monkeypatch):
    """The streaming hold-down is TTL-bounded: after 5s the merge recomputes."""
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache7")
    s.active_stream_id = "stream-live"
    _register_stream(routes_env)
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    merge_calls = {"n": 0}
    real_merge = routes.merge_session_messages_append_only

    def counting_merge(*args, **kwargs):
        merge_calls["n"] += 1
        return real_merge(*args, **kwargs)

    routes.merge_session_messages_append_only = counting_merge
    now = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])
    try:
        routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
        now[0] += routes._DISPLAY_MERGE_STREAMING_TTL_SECONDS + 0.1
        routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    finally:
        routes.merge_session_messages_append_only = real_merge

    assert merge_calls["n"] == 2, "expired streaming entry must recompute the merge"


def test_new_in_memory_row_invalidates_streaming_cache(routes_env):
    """A new message row must be visible on the next tail load immediately."""
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache8")
    s.active_stream_id = "stream-live"
    _register_stream(routes_env)
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    s.messages.append({
        "role": "assistant",
        "content": "live tail row",
        "timestamp": time.time() + 50,
    })
    merged = routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert any(m.get("content") == "live tail row" for m in merged)


def test_msg_before_pagination_bypasses_streaming_cache(routes_env):
    """RC1 must not change the msg_before contract: paging never reuses the
    initial tail merge (uncapped read scope; the tail entry may have been built
    from a since-bounded state.db read, so reusing it could hide older rows)."""
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache9")
    s.active_stream_id = "stream-live"
    _register_stream(routes_env)
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    merge_calls = {"n": 0}
    real_merge = routes.merge_session_messages_append_only

    def counting_merge(*args, **kwargs):
        merge_calls["n"] += 1
        return real_merge(*args, **kwargs)

    routes.merge_session_messages_append_only = counting_merge
    try:
        routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
        assert merge_calls["n"] == 1
        paged = routes._limited_webui_messages_for_display_with_sidecar(
            s, None, rows, msg_before=2)
    finally:
        routes.merge_session_messages_append_only = real_merge

    assert merge_calls["n"] == 2, "msg_before paging must recompute, not reuse the tail entry"
    assert [m.get("content") for m in paged] == [
        "turn 0", "turn 1", "turn 2", "turn 3", "turn 4", "turn 5",
        "state row 0", "state row 1",
    ], "paged read must cover the uncapped merge scope"
