import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def clear_agent_lifecycle_registries():
    import api.config as config
    import api.streaming as streaming

    def reset_deferred_teardowns():
        with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
            retry_timer = streaming._DEFERRED_EVICTION_RETRY_TIMER
        if retry_timer is not None:
            retry_timer.cancel()
            retry_timer.join(timeout=1)
        with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
            streaming._DEFERRED_EVICTION_TEARDOWNS.clear()
            streaming._DEFERRED_EVICTION_TEARDOWN_IN_PROGRESS.clear()
            streaming._DEFERRED_EVICTION_RETRY_TIMER = None
            streaming._DEFERRED_EVICTION_RETRY_NEXT_DELAY_SECONDS = (
                streaming._DEFERRED_EVICTION_RETRY_BASE_DELAY_SECONDS
            )

    def reset_runtime_registries():
        with config.STREAMS_LOCK:
            config.STREAMS.clear()
            config.CANCEL_FLAGS.clear()
            config.AGENT_INSTANCES.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.clear()

    reset_runtime_registries()
    reset_deferred_teardowns()
    yield
    reset_runtime_registries()
    reset_deferred_teardowns()


def _patch_lifecycle(monkeypatch, streaming, *, dirty=False):
    lifecycle_commit = MagicMock(return_value=True)
    lifecycle_has_uncommitted_work = MagicMock(return_value=dirty)
    lifecycle_unregister = MagicMock()
    lifecycle_discard = MagicMock()
    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", lifecycle_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lifecycle_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", lifecycle_unregister)
    monkeypatch.setattr(streaming, "_lifecycle_discard_session", lifecycle_discard)
    return lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard


def _agent(session_id="real-session"):
    session_db = MagicMock()
    agent = MagicMock()
    agent.session_id = session_id
    agent._session_db = session_db
    agent._session_messages = [{"role": "user", "content": "still running"}]
    return agent


def test_evicted_agent_lifecycle_defers_teardown_when_exact_agent_is_live(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")

    live_stream_id = "live-stream-for-real-session"
    with config.STREAMS_LOCK:
        config.AGENT_INSTANCES[live_stream_id] = agent

    assert streaming._close_evicted_agent_at_session_boundary("wrong-cache-key", agent) is False
    assert agent._webui_deferred_eviction_teardown_session_id == "real-session"
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_evicted_agent_lifecycle_defers_when_cancel_detached_active_run_owns_session(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["detached-stream"] = {
            "stream_id": "detached-stream",
            "session_id": "real-session",
            "phase": "cancelling",
        }

    assert streaming._close_evicted_agent_at_session_boundary("wrong-cache-key", agent) is False
    assert agent._webui_deferred_eviction_teardown_session_id == "real-session"
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_blocked_evicted_agent_drains_if_worker_clears_liveness_before_mark(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")
    with config.STREAMS_LOCK:
        config.AGENT_INSTANCES["live-stream"] = agent
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["live-stream"] = {
            "stream_id": "live-stream",
            "session_id": "real-session",
            "phase": "running",
        }

    original_mark = streaming._mark_deferred_evicted_agent_teardown

    def clear_liveness_then_mark(session_id, marked_agent):
        with config.STREAMS_LOCK:
            config.AGENT_INSTANCES.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        original_mark(session_id, marked_agent)

    monkeypatch.setattr(streaming, "_mark_deferred_evicted_agent_teardown", clear_liveness_then_mark)

    assert streaming._close_evicted_agent_at_session_boundary("stale-cache-key", agent) is False
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_id")
    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        assert id(agent) not in streaming._DEFERRED_EVICTION_TEARDOWNS
        assert id(agent) not in streaming._DEFERRED_EVICTION_TEARDOWN_IN_PROGRESS
    lifecycle_commit.assert_called_once_with("real-session", agent=agent, wait=True)
    lifecycle_has_uncommitted_work.assert_called_once_with("real-session")
    lifecycle_unregister.assert_called_once_with("real-session")
    lifecycle_discard.assert_called_once_with("real-session")
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    agent._session_db.close.assert_called_once()


def test_evicted_agent_defers_when_requested_key_alias_is_active_after_identity_rotation(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    old_sid = "pre-compression-session"
    new_sid = "post-compression-session"
    agent = _agent(new_sid)
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["compressing-stream"] = {
            "stream_id": "compressing-stream",
            "session_id": old_sid,
            "phase": "cancelling",
        }

    assert streaming._close_evicted_agent_at_session_boundary(old_sid, agent) is False

    assert agent._webui_deferred_eviction_teardown_session_id == new_sid
    assert set(agent._webui_deferred_eviction_teardown_session_ids) == {old_sid, new_sid}
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()

    assert streaming._drain_deferred_evicted_agent_teardowns() == 1
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_id")
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_ids")
    lifecycle_commit.assert_called_once_with(new_sid, agent=agent, wait=True)
    lifecycle_has_uncommitted_work.assert_called_once_with(new_sid)
    lifecycle_unregister.assert_called_once_with(new_sid)
    lifecycle_discard.assert_called_once_with(new_sid)
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    agent._session_db.close.assert_called_once()


def test_global_deferred_evicted_agent_drain_closes_orphan_after_successor_finishes(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    old_agent = _agent("real-session")
    successor_agent = _agent("real-session")

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS["successor-stream"] = {
            "stream_id": "successor-stream",
            "session_id": "real-session",
            "phase": "running",
        }

    assert streaming._close_evicted_agent_at_session_boundary("stale-cache-key", old_agent) is False
    assert old_agent._webui_deferred_eviction_teardown_session_id == "real-session"

    assert streaming._drain_deferred_evicted_agent_teardowns() == 0
    assert old_agent._webui_deferred_eviction_teardown_session_id == "real-session"
    lifecycle_commit.assert_not_called()
    old_agent.shutdown_memory_provider.assert_not_called()
    old_agent._session_db.close.assert_not_called()

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()

    assert streaming._drain_deferred_evicted_agent_teardowns() == 1
    assert not hasattr(old_agent, "_webui_deferred_eviction_teardown_session_id")
    lifecycle_commit.assert_called_once_with("real-session", agent=old_agent, wait=True)
    lifecycle_has_uncommitted_work.assert_called_once_with("real-session")
    lifecycle_unregister.assert_called_once_with("real-session")
    lifecycle_discard.assert_called_once_with("real-session")
    old_agent.shutdown_memory_provider.assert_called_once_with(old_agent._session_messages)
    old_agent._session_db.close.assert_called_once()

    assert streaming._drain_deferred_evicted_agent_teardowns() == 0
    lifecycle_commit.assert_called_once()
    old_agent.shutdown_memory_provider.assert_called_once()
    old_agent._session_db.close.assert_called_once()
    successor_agent.shutdown_memory_provider.assert_not_called()


def test_global_deferred_evicted_agent_drain_claims_once_under_concurrency(monkeypatch):
    import api.streaming as streaming

    agent = _agent("idle-session")
    commit_started = threading.Event()
    release_commit = threading.Event()
    commit_calls = []
    commit_lock = threading.Lock()

    def fake_commit(session_id, *, agent=None, wait=False):
        with commit_lock:
            commit_calls.append((session_id, agent, wait))
        commit_started.set()
        assert release_commit.wait(timeout=5)
        return True

    lifecycle_has_uncommitted_work = MagicMock(return_value=False)
    lifecycle_unregister = MagicMock()
    lifecycle_discard = MagicMock()
    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", fake_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lifecycle_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", lifecycle_unregister)
    monkeypatch.setattr(streaming, "_lifecycle_discard_session", lifecycle_discard)

    streaming._mark_deferred_evicted_agent_teardown("idle-session", agent)
    results = []

    def drain():
        results.append(streaming._drain_deferred_evicted_agent_teardowns())

    first = threading.Thread(target=drain)
    first.start()
    assert commit_started.wait(timeout=5)

    second = threading.Thread(target=drain)
    second.start()
    second.join(timeout=5)
    assert not second.is_alive()

    release_commit.set()
    first.join(timeout=5)
    assert not first.is_alive()

    assert sorted(results) == [0, 1]
    assert commit_calls == [("idle-session", agent, True)]
    lifecycle_has_uncommitted_work.assert_called_once_with("idle-session")
    lifecycle_unregister.assert_called_once_with("idle-session")
    lifecycle_discard.assert_called_once_with("idle-session")
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    agent._session_db.close.assert_called_once()


def test_global_deferred_drain_does_not_reclaim_when_close_recheck_blocks(monkeypatch):
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")
    liveness_checks = []

    def fake_live_blocked(session_id, checked_agent):
        liveness_checks.append((session_id, checked_agent))
        return len(liveness_checks) >= 2

    monkeypatch.setattr(streaming, "_evicted_agent_teardown_is_live_blocked", fake_live_blocked)

    streaming._mark_deferred_evicted_agent_teardown("real-session", agent)

    assert streaming._drain_deferred_evicted_agent_teardowns() == 0
    assert liveness_checks == [("real-session", agent), ("real-session", agent)]
    assert agent._webui_deferred_eviction_teardown_session_id == "real-session"
    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        assert streaming._DEFERRED_EVICTION_TEARDOWNS[id(agent)] is agent
        assert id(agent) not in streaming._DEFERRED_EVICTION_TEARDOWN_IN_PROGRESS
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_deferred_evicted_agent_teardown_does_not_close_cache_owned_duplicate(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")
    streaming._mark_deferred_evicted_agent_teardown("real-session", agent)
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE["real-session"] = (agent, "sig")

    assert streaming._drain_deferred_evicted_agent_teardowns() == 0
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_id")
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


def test_evicted_agent_lifecycle_does_not_close_when_same_agent_is_still_cache_owned(monkeypatch):
    import api.config as config
    import api.streaming as streaming

    lifecycle_commit, lifecycle_has_uncommitted_work, lifecycle_unregister, lifecycle_discard = (
        _patch_lifecycle(monkeypatch, streaming)
    )
    agent = _agent("real-session")
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE["real-session"] = (agent, "sig")

    assert streaming._close_evicted_agent_at_session_boundary("old-cache-key", agent) is False
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_id")
    lifecycle_commit.assert_not_called()
    lifecycle_has_uncommitted_work.assert_not_called()
    lifecycle_unregister.assert_not_called()
    lifecycle_discard.assert_not_called()
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()


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


def test_evicted_agent_lifecycle_retries_until_commit_is_clean(monkeypatch):
    import api.streaming as streaming

    lifecycle_commit = MagicMock(return_value=True)
    lifecycle_has_uncommitted_work = MagicMock(side_effect=[True, False])
    lifecycle_unregister = MagicMock()
    lifecycle_discard = MagicMock()
    monkeypatch.setattr(streaming, "_lifecycle_commit_session_memory", lifecycle_commit)
    monkeypatch.setattr(streaming, "_lifecycle_has_uncommitted_work", lifecycle_has_uncommitted_work)
    monkeypatch.setattr(streaming, "_lifecycle_unregister_agent", lifecycle_unregister)
    monkeypatch.setattr(streaming, "_lifecycle_discard_session", lifecycle_discard)
    monkeypatch.setattr(streaming, "_DEFERRED_EVICTION_RETRY_BASE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(streaming, "_DEFERRED_EVICTION_RETRY_MAX_DELAY_SECONDS", 0.02)
    monkeypatch.setattr(streaming, "_DEFERRED_EVICTION_RETRY_NEXT_DELAY_SECONDS", 0.01)

    agent = _agent("dirty-session")

    assert streaming._close_evicted_agent_at_session_boundary("dirty-session", agent) is False

    assert agent._webui_deferred_eviction_teardown_session_id == "dirty-session"
    agent.shutdown_memory_provider.assert_not_called()
    agent._session_db.close.assert_not_called()

    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        retry_timer = streaming._DEFERRED_EVICTION_RETRY_TIMER
    assert retry_timer is not None
    retry_timer.join(timeout=2)
    assert not retry_timer.is_alive()
    assert not hasattr(agent, "_webui_deferred_eviction_teardown_session_id")
    assert lifecycle_commit.call_count == 2
    assert lifecycle_has_uncommitted_work.call_count == 2
    lifecycle_unregister.assert_called_once_with("dirty-session")
    lifecycle_discard.assert_called_once_with("dirty-session")
    agent.shutdown_memory_provider.assert_called_once_with(agent._session_messages)
    agent._session_db.close.assert_called_once()

    with streaming._DEFERRED_EVICTION_TEARDOWN_LOCK:
        assert streaming._DEFERRED_EVICTION_RETRY_TIMER is None
        assert not streaming._DEFERRED_EVICTION_TEARDOWNS
    assert lifecycle_commit.call_count == 2
    agent._session_db.close.assert_called_once()


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


def test_worker_finally_keeps_run_unregisters_inside_streams_lock_before_global_deferred_drain():
    lines = open("api/streaming.py", encoding="utf-8").read().splitlines()
    reset_idx = next(i for i, line in enumerate(lines) if "_reset_turn_session_identity(_turn_session_identity_tokens)" in line)
    lock_idx = next(i for i in range(reset_idx, len(lines)) if "with STREAMS_LOCK:" in lines[i])
    lock_indent = len(lines[lock_idx]) - len(lines[lock_idx].lstrip())

    block_end = next(
        i
        for i in range(lock_idx + 1, len(lines))
        if lines[i].strip()
        and not lines[i].lstrip().startswith("#")
        and len(lines[i]) - len(lines[i].lstrip()) <= lock_indent
    )

    ordered_markers = [
        "AGENT_INSTANCES.pop(stream_id, None)",
        "unregister_active_run(stream_id)",
        "unregister_stream_owner(stream_id)",
        "clear_session_writeback_owner_if_owned(session_id, stream_id)",
    ]
    marker_indexes = []
    for marker in ordered_markers:
        marker_idx = next(i for i in range(lock_idx, block_end) if marker in lines[i])
        marker_indexes.append(marker_idx)
        assert len(lines[marker_idx]) - len(lines[marker_idx].lstrip()) > lock_indent

    assert marker_indexes == sorted(marker_indexes)
    drain_idx = next(i for i in range(block_end, len(lines)) if "_drain_deferred_evicted_agent_teardowns()" in lines[i])
    assert drain_idx > block_end
    assert len(lines[drain_idx]) - len(lines[drain_idx].lstrip()) <= lock_indent + 4
