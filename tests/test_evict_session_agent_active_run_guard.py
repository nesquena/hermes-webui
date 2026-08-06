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

import api.config as config


class _FakeSessionDB:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeAgent:
    def __init__(self):
        self._session_db = _FakeSessionDB()


def _seed_cache(session_id, agent):
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE[session_id] = (agent, "sig")


def _clear_active_runs():
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()


def test_evict_skips_session_db_close_when_run_active(monkeypatch):
    """A live run on the session => the cache handle drops but the DB stays open."""
    sid = "live-session-evict-guard"
    agent = _FakeAgent()
    _seed_cache(sid, agent)
    _clear_active_runs()
    config.register_active_run("stream-xyz", session_id=sid)
    try:
        config._evict_session_agent(sid)
        # The agent handle is removed from the cache (harmless — the worker
        # holds its own local ref) ...
        with config.SESSION_AGENT_CACHE_LOCK:
            assert sid not in config.SESSION_AGENT_CACHE
        # ... but the live worker's SessionDB must NOT be closed.
        assert agent._session_db.closed is False
    finally:
        _clear_active_runs()
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.pop(sid, None)


def test_evict_closes_session_db_when_no_run_active(monkeypatch):
    """No live run => normal eviction closes the SessionDB (idle path unchanged)."""
    sid = "idle-session-evict-guard"
    agent = _FakeAgent()
    _seed_cache(sid, agent)
    _clear_active_runs()

    # Neutralize the lifecycle commit machinery so the test isolates the
    # ACTIVE_RUNS guard + close decision (no uncommitted work => should_close).
    monkeypatch.setattr("api.session_lifecycle.has_uncommitted_work", lambda *_a, **_k: False)
    monkeypatch.setattr("api.session_lifecycle.unregister_agent", lambda *_a, **_k: None)
    monkeypatch.setattr("api.session_lifecycle.discard_session", lambda *_a, **_k: None)
    try:
        config._evict_session_agent(sid)
        with config.SESSION_AGENT_CACHE_LOCK:
            assert sid not in config.SESSION_AGENT_CACHE
        assert agent._session_db.closed is True
    finally:
        _clear_active_runs()
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.pop(sid, None)
