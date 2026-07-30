from unittest.mock import MagicMock


def test_evicted_agent_lifecycle_commits_unregisters_and_shutdowns(monkeypatch):
    import api.streaming as streaming

    events = []

    def fake_commit(session_id, *, agent=None, wait=False):
        events.append(("commit", session_id, agent, wait))
        return True

    def fake_has_uncommitted_work(session_id):
        events.append(("has_uncommitted", session_id))
        return False

    def fake_unregister(session_id):
        events.append(("unregister", session_id))

    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", fake_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", fake_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", fake_unregister)

    session_db = MagicMock()
    agent = MagicMock()
    agent._session_db = session_db
    agent._session_messages = [{"role": "user", "content": "hello"}]

    streaming._close_evicted_agent_at_session_boundary("old-session", agent)

    assert ("commit", "old-session", agent, True) in events
    assert ("has_uncommitted", "old-session") in events
    assert ("unregister", "old-session") in events
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    session_db.close.assert_called_once()


def test_evicted_agent_lifecycle_shutdown_uses_empty_messages_when_missing(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", lambda *a, **kw: True)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lambda session_id: False)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", MagicMock())

    agent = MagicMock()
    agent._session_db = MagicMock()

    streaming._close_evicted_agent_at_session_boundary("old-session", agent)

    agent.shutdown_memory_provider.assert_called_once_with([])
    agent._session_db.close.assert_called_once()


def test_cached_agent_entry_lifecycle_extracts_agent_from_cache_tuple(monkeypatch):
    import api.streaming as streaming

    closed = []
    monkeypatch.setattr(
        streaming,
        "_close_evicted_agent_at_session_boundary",
        lambda session_id, agent: closed.append((session_id, agent)) or True,
    )

    agent = MagicMock()

    assert streaming._close_cached_agent_entry_at_session_boundary("old-session", (agent, "sig")) is True
    assert closed == [("old-session", agent)]


def test_evicted_agent_lifecycle_keeps_provider_alive_when_commit_still_dirty(monkeypatch):
    import api.streaming as streaming

    def fake_commit(session_id, *, agent=None, wait=False):
        return True

    def fake_has_uncommitted_work(session_id):
        return True

    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", fake_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", fake_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", MagicMock())

    agent = MagicMock()
    agent._session_db = MagicMock()

    streaming._close_evicted_agent_at_session_boundary("dirty-session", agent)

    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_identity_mismatch_cache_evictions_close_entries_outside_cache_lock():
    src = open("api/streaming.py", encoding="utf-8").read()

    expected_markers = [
        "_identity_mismatch_entry = SESSION_AGENT_CACHE.pop(session_id, None)",
        "_stale_runtime_entry = SESSION_AGENT_CACHE.pop(session_id, None)",
        "_skipped_agent_migration_entry = _cached_entry",
        "evicted_cached_entry = _cfg.SESSION_AGENT_CACHE.pop(sid, None)",
        "_evicted_entry = SESSION_AGENT_CACHE.pop(session_id, None)",
    ]
    for marker in expected_markers:
        assert marker in src

    close_markers = [
        "_close_cached_agent_entry_at_session_boundary(session_id, _identity_mismatch_entry)",
        "_close_cached_agent_entry_at_session_boundary(session_id, _stale_runtime_entry)",
        "_close_cached_agent_entry_at_session_boundary(old_sid, _skipped_agent_migration_entry)",
        "_close_cached_agent_entry_at_session_boundary(sid, evicted_cached_entry)",
        "_close_cached_agent_entry_at_session_boundary(session_id, _evicted_entry)",
    ]
    lines = src.splitlines()
    for marker in close_markers:
        close_idx = next(i for i, line in enumerate(lines) if marker in line)
        lock_idx = max(i for i, line in enumerate(lines[:close_idx]) if "with SESSION_AGENT_CACHE_LOCK:" in line)
        lock_indent = len(lines[lock_idx]) - len(lines[lock_idx].lstrip())
        between = lines[lock_idx + 1:close_idx]
        assert any(
            line.strip()
            and not line.lstrip().startswith("#")
            and len(line) - len(line.lstrip()) <= lock_indent
            for line in between
        ), f"{marker} still appears inside the SESSION_AGENT_CACHE_LOCK block"
