"""Regression: _evict_session_agent must not close a live session's _session_db.

Bug-D follow-up (#5096): truncate/clear/model-switch all call
api.config._evict_session_agent(). The worker assigns agent._session_db at run
start, so eviction must consult ACTIVE_RUNS (the authoritative liveness signal,
same as the worker's own LRU-eviction guard) and skip the lifecycle commit +
_session_db.close() while a run is in flight on that session. Otherwise a
truncate racing an in-flight turn on the same session (reachable via a second
client / direct API; the UI gates it behind S.busy) closes the SessionDB the
running worker is still persisting through.
"""

from unittest.mock import MagicMock

import pytest

import api.config as config


@pytest.fixture(autouse=True)
def clear_agent_liveness_registries():
    import api.streaming as streaming

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        streaming._DEFERRED_EVICTION_TEARDOWNS.clear()
        streaming._DEFERRED_EVICTION_TEARDOWN_IN_PROGRESS.clear()
    yield
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        streaming._DEFERRED_EVICTION_TEARDOWNS.clear()
        streaming._DEFERRED_EVICTION_TEARDOWN_IN_PROGRESS.clear()


class _FakeSessionDB:
    def __init__(self):
        self.closed = False
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.closed = True


class _FakeAgent:
    def __init__(self, session_id="live-session-evict-guard"):
        self.session_id = session_id
        self._session_db = _FakeSessionDB()
        self._session_messages = [{"role": "user", "content": "hello"}]
        self.shutdown_memory_provider = MagicMock()


def _seed_cache(session_id, agent):
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE[session_id] = (agent, "sig")


def _clear_active_runs():
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()


def _patch_streaming_lifecycle(monkeypatch, streaming, *, dirty=False):
    lifecycle_commit = MagicMock(return_value=True)
    lifecycle_has_uncommitted_work = MagicMock(return_value=dirty)
    lifecycle_unregister = MagicMock()
    lifecycle_discard = MagicMock()
    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", lifecycle_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lifecycle_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", lifecycle_unregister)
    monkeypatch.setattr(streaming, "_lifecycle_discard_session", lifecycle_discard)
    return lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard


def test_evict_defers_session_db_close_when_run_active(monkeypatch):
    """A live run may lose the cache handle, but its DB stays open until idle."""
    import api.streaming as streaming

    sid = "live-session-evict-guard"
    agent = _FakeAgent(sid)
    _seed_cache(sid, agent)
    _clear_active_runs()
    _patch_streaming_lifecycle(monkeypatch, streaming)
    config.register_active_run("stream-xyz", session_id=sid)
    try:
        config._evict_session_agent(sid)
        with config.SESSION_AGENT_CACHE_LOCK:
            assert sid not in config.SESSION_AGENT_CACHE
        assert agent._webui_deferred_eviction_teardown_session_id == sid
        assert agent._session_db.closed is False

        _clear_active_runs()
        assert streaming._drain_deferred_evicted_agent_teardowns() == 1
        assert agent._session_db.close_count == 1
    finally:
        _clear_active_runs()
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.pop(sid, None)


def test_evict_closes_session_db_when_no_run_active(monkeypatch):
    """No live run => normal eviction closes the SessionDB (idle path unchanged)."""
    import api.streaming as streaming

    sid = "idle-session-evict-guard"
    agent = _FakeAgent(sid)
    _seed_cache(sid, agent)
    _clear_active_runs()

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_streaming_lifecycle(monkeypatch, streaming)
    )
    closed_entries = []
    original_close = streaming._close_cached_agent_entry_at_session_boundary

    def spy_close(closed_sid, cache_entry):
        closed_entries.append((closed_sid, cache_entry))
        return original_close(closed_sid, cache_entry)

    monkeypatch.setattr(streaming, "_close_cached_agent_entry_at_session_boundary", spy_close)
    try:
        config._evict_session_agent(sid)
        with config.SESSION_AGENT_CACHE_LOCK:
            assert sid not in config.SESSION_AGENT_CACHE
        assert closed_entries == [(sid, (agent, "sig"))]
        lifecycle_commit.assert_called_once_with(sid, agent=agent, wait=True)
        lifecycle_has_uncommitted_work.assert_called_once_with(sid)
        lifecycle_unregister.assert_called_once_with(sid)
        lifecycle_discard.assert_called_once_with(sid)
        agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
        assert agent._session_db.closed is True
    finally:
        _clear_active_runs()
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.pop(sid, None)


def test_evict_defers_wrong_key_entry_when_agent_actual_identity_has_active_run(monkeypatch):
    import api.streaming as streaming

    old_sid = "old-cache-key"
    new_sid = "actual-live-session"
    agent = _FakeAgent(new_sid)
    _seed_cache(old_sid, agent)
    _patch_streaming_lifecycle(monkeypatch, streaming)
    config.register_active_run("stream-actual", session_id=new_sid)

    config._evict_session_agent(old_sid)

    with config.SESSION_AGENT_CACHE_LOCK:
        assert old_sid not in config.SESSION_AGENT_CACHE
    assert set(agent._webui_deferred_eviction_teardown_session_ids) == {old_sid, new_sid}
    assert agent._session_db.closed is False


def test_evict_pop_race_defers_to_shared_streaming_teardown(monkeypatch):
    import api.streaming as streaming

    old_sid = "stale-cache-key"
    new_sid = "actual-session-after-compression"
    agent = _FakeAgent(new_sid)
    _seed_cache(old_sid, agent)
    _clear_active_runs()
    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_streaming_lifecycle(monkeypatch, streaming)
    )

    original_has_cache_owner = streaming._evicted_agent_has_cache_owner
    injected_active_run = False

    def register_run_after_config_precheck(checked_agent):
        nonlocal injected_active_run
        if checked_agent is agent and not injected_active_run:
            injected_active_run = True
            config.register_active_run("stream-after-precheck", session_id=new_sid)
        return original_has_cache_owner(checked_agent)

    monkeypatch.setattr(streaming, "_evicted_agent_has_cache_owner", register_run_after_config_precheck)

    config._evict_session_agent(old_sid)

    with config.SESSION_AGENT_CACHE_LOCK:
        assert old_sid not in config.SESSION_AGENT_CACHE
    assert injected_active_run is True
    assert agent._webui_deferred_eviction_teardown_session_id == new_sid
    assert set(agent._webui_deferred_eviction_teardown_session_ids) == {old_sid, new_sid}
    assert agent._session_db.closed is False
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()

    _clear_active_runs()

    assert streaming._drain_deferred_evicted_agent_teardowns() == 1
    lifecycle_commit.assert_called_once_with(new_sid, agent=agent, wait=True)
    lifecycle_has_uncommitted_work.assert_called_once_with(new_sid)
    lifecycle_unregister.assert_called_once_with(new_sid)
    lifecycle_discard.assert_called_once_with(new_sid)
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    assert agent._session_db.close_count == 1


def test_evict_idle_matching_entry_uses_shared_streaming_teardown(monkeypatch):
    import api.streaming as streaming

    sid = "idle-matching-session"
    agent = _FakeAgent(sid)
    _seed_cache(sid, agent)
    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_streaming_lifecycle(monkeypatch, streaming)
    )
    closed_entries = []
    original_close = streaming._close_cached_agent_entry_at_session_boundary

    def spy_close(closed_sid, cache_entry):
        closed_entries.append((closed_sid, cache_entry))
        return original_close(closed_sid, cache_entry)

    monkeypatch.setattr(streaming, "_close_cached_agent_entry_at_session_boundary", spy_close)

    config._evict_session_agent(sid)

    with config.SESSION_AGENT_CACHE_LOCK:
        assert sid not in config.SESSION_AGENT_CACHE
    assert closed_entries == [(sid, (agent, "sig"))]
    lifecycle_commit.assert_called_once_with(sid, agent=agent, wait=True)
    lifecycle_has_uncommitted_work.assert_called_once_with(sid)
    lifecycle_unregister.assert_called_once_with(sid)
    lifecycle_discard.assert_called_once_with(sid)
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    assert agent._session_db.close_count == 1
