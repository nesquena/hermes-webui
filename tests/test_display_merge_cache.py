"""The display merge for paginated loads must be memoized for inactive sessions.

Before the fix, GET /api/session re-ran merge_session_messages_append_only on
every request (~2-3s for multi-thousand-message transcripts). The cache must:

- return the same merged transcript as a direct merge (correctness),
- serve copies (caller mutation cannot corrupt the cache),
- invalidate when the sidecar file changes on disk,
- invalidate when the state.db rows change,
- never cache ACTIVE sessions (active_stream_id / pending_user_message).
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
    yield SimpleNamespace(config=config, models=models, routes=routes)
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


def test_active_session_never_cached(routes_env):
    routes = routes_env.routes
    s = _make_session(routes_env, sid="20260101_000000_cache5")
    s.active_stream_id = "stream-live"
    rows = _state_rows(s.messages[-1]["timestamp"])

    routes._display_merge_cache.clear()
    routes._limited_webui_messages_for_display_with_sidecar(s, None, rows)
    assert s.session_id not in routes._display_merge_cache
