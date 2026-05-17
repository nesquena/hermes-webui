"""Behavioral tests for WebUI memory-provider session lifecycle.

Batch-extraction memory providers such as OpenViking and Holographic need a
clear lifecycle contract: only completed turns are committable, repeated commits
must be no-ops when nothing new happened, and a commit finishing late must not
erase work completed while it was in flight.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _fresh_lifecycle():
    """Import/reload lifecycle module and clear process-global test state."""
    lifecycle = importlib.import_module("api.session_lifecycle")
    lifecycle = importlib.reload(lifecycle)
    reset = getattr(lifecycle, "_reset_for_tests", None)
    if callable(reset):
        reset()
    return lifecycle


class RecordingAgent:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.failures_remaining = 0
        self._session_db: object | None = None

    def commit_memory_session(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("commit failed")


class CloseTrackingDB:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_commit_finishing_late_does_not_clear_newly_completed_turn():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    sid = "rapid-reopen"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    commit_thread = threading.Thread(target=lambda: lifecycle.commit_session_memory(sid))
    commit_thread.start()
    assert agent.entered.wait(timeout=2)

    # A later turn completes while the previous commit is still running.  The
    # previous commit must not erase this newer generation when it finishes.
    lifecycle.mark_turn_completed(sid, agent=agent)
    agent.release.set()
    commit_thread.join(timeout=2)

    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is True

    assert lifecycle.commit_session_memory(sid) is True
    assert agent.calls == 2
    assert lifecycle.has_uncommitted_work(sid) is False


def test_failed_commit_preserves_uncommitted_work_and_agent_handle():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    agent.failures_remaining = 1
    sid = "commit-failure"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    assert lifecycle.commit_session_memory(sid) is False
    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is True

    assert lifecycle.commit_session_memory(sid) is True
    assert agent.calls == 2
    assert lifecycle.has_uncommitted_work(sid) is False


def test_failed_explicit_agent_commit_preserves_agent_handle_for_retry():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    agent.failures_remaining = 1
    sid = "explicit-agent-failure"

    lifecycle.mark_turn_completed(sid)

    assert lifecycle.commit_session_memory(sid, agent=agent) is False
    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is True

    # Retry without passing an agent must still find the explicit agent supplied
    # to the failed commit attempt.
    assert lifecycle.commit_session_memory(sid) is True
    assert agent.calls == 2
    assert lifecycle.has_uncommitted_work(sid) is False


def test_dirty_failed_commit_handle_survives_replacement_registration():
    lifecycle = _fresh_lifecycle()
    old_agent = RecordingAgent()
    old_agent.release.set()
    old_agent.failures_remaining = 1
    new_agent = RecordingAgent()
    new_agent.release.set()
    sid = "preserve-failed-handle"

    lifecycle.register_agent(sid, old_agent)
    lifecycle.mark_turn_completed(sid, agent=old_agent)

    assert lifecycle.commit_session_memory(sid) is False
    assert old_agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is True

    lifecycle.register_agent(sid, new_agent)

    assert lifecycle.commit_session_memory(sid) is True
    assert old_agent.calls == 2
    assert new_agent.calls == 0
    assert lifecycle.has_uncommitted_work(sid) is False

    # Once the dirty generation is clean, the pending replacement may become the
    # active handle for future completed work.
    lifecycle.mark_turn_completed(sid)
    assert lifecycle.commit_session_memory(sid) is True
    assert new_agent.calls == 1


def test_explicit_new_agent_commit_cannot_clear_old_dirty_segment():
    lifecycle = _fresh_lifecycle()
    old_agent = RecordingAgent()
    old_agent.release.set()
    old_agent.failures_remaining = 1
    new_agent = RecordingAgent()
    new_agent.release.set()
    sid = "explicit-new-cannot-steal-old"

    lifecycle.register_agent(sid, old_agent)
    lifecycle.mark_turn_completed(sid, agent=old_agent)
    assert lifecycle.commit_session_memory(sid) is False

    lifecycle.register_agent(sid, new_agent)
    lifecycle.mark_turn_completed(sid, agent=new_agent)

    # Even an explicit commit with the replacement agent must flush the oldest
    # dirty segment with its preserved owner first.
    assert lifecycle.commit_session_memory(sid, agent=new_agent) is True
    assert old_agent.calls == 2
    assert new_agent.calls == 0
    assert lifecycle.has_uncommitted_work(sid) is True

    assert lifecycle.commit_session_memory(sid) is True
    assert new_agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is False


def test_registered_session_without_completed_turn_is_not_committed():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    sid = "registered-only"

    lifecycle.register_agent(sid, agent)

    assert lifecycle.commit_session_memory(sid) is False
    assert agent.calls == 0
    assert lifecycle.has_uncommitted_work(sid) is False


def test_shutdown_drain_includes_dirty_sessions_even_if_pending_registry_was_cleared():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    sid = "dirty-cached-only"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)
    lifecycle.unregister_agent(sid)

    # A lifecycle registry miss should not make completed work undiscoverable if
    # the agent is still supplied/cached by the caller.
    assert lifecycle.commit_session_memory(sid, agent=agent) is True
    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is False


def test_shutdown_drain_waits_for_inflight_commit_and_flushes_new_generation():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    sid = "shutdown-waits"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)
    first_commit = threading.Thread(target=lambda: lifecycle.commit_session_memory(sid))
    first_commit.start()
    assert agent.entered.wait(timeout=2)

    lifecycle.mark_turn_completed(sid, agent=agent)

    drain_thread = threading.Thread(target=lifecycle.drain_all_on_shutdown)
    drain_thread.start()
    drain_thread.join(timeout=0.05)
    assert drain_thread.is_alive()
    assert agent.calls == 1

    agent.release.set()
    first_commit.join(timeout=2)
    drain_thread.join(timeout=2)

    assert agent.calls == 2
    assert lifecycle.has_uncommitted_work(sid) is False


def test_frontend_new_session_sends_previous_session_id_boundary():
    src = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
    start = src.index("async function newSession")
    end = src.index("const data=await api('/api/session/new'", start)
    body = src[start:end]

    assert "prev_session_id" in body
    assert "S.session" in body and "session_id" in body


# ── Follow-up review fix tests ──────────────────────────────────────────────


def test_evict_session_agent_commits_before_dropping():
    """_evict_session_agent() must attempt a lifecycle commit before dropping
    the cached agent handle, so batch-extraction providers can extract pending
    work before the handle is lost."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    sid = "evict-commit"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    import api.config as cfg

    with cfg.SESSION_AGENT_CACHE_LOCK:
        cfg.SESSION_AGENT_CACHE.clear()
        cfg.SESSION_AGENT_CACHE[sid] = (agent, "sig")

    cfg._evict_session_agent(sid)

    assert agent.calls == 1, "evict should have committed before dropping"
    assert lifecycle.has_uncommitted_work(sid) is False
    with cfg.SESSION_AGENT_CACHE_LOCK:
        assert sid not in cfg.SESSION_AGENT_CACHE

    # Successful eviction should unregister the cached agent handle. If the
    # handle leaked, a future mark without supplying an agent could commit.
    lifecycle.mark_turn_completed(sid)
    assert lifecycle.commit_session_memory(sid) is False


def test_evict_session_agent_waits_for_inflight_commit_before_closing_db():
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    db = CloseTrackingDB()
    agent._session_db = db
    sid = "evict-waits-for-inflight"

    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)
    first_commit = threading.Thread(target=lambda: lifecycle.commit_session_memory(sid))
    first_commit.start()
    assert agent.entered.wait(timeout=2)

    # A newer completed generation appears while the first commit is still in
    # flight. Eviction must wait for the first commit and flush the newer one
    # before closing the agent's DB resource.
    lifecycle.mark_turn_completed(sid, agent=agent)

    import api.config as cfg
    with cfg.SESSION_AGENT_CACHE_LOCK:
        cfg.SESSION_AGENT_CACHE.clear()
        cfg.SESSION_AGENT_CACHE[sid] = (agent, "sig")

    evict_thread = threading.Thread(target=lambda: cfg._evict_session_agent(sid))
    evict_thread.start()
    evict_thread.join(timeout=0.05)
    assert evict_thread.is_alive()
    assert db.close_calls == 0

    agent.release.set()
    first_commit.join(timeout=2)
    evict_thread.join(timeout=2)

    assert agent.calls == 2
    assert lifecycle.has_uncommitted_work(sid) is False
    assert db.close_calls == 1


def test_lru_eviction_commits_outside_cache_lock():
    """LRU eviction must collect under SESSION_AGENT_CACHE_LOCK and commit only
    after leaving that lock; provider extraction can be slow I/O."""
    import api.streaming as streaming_mod

    src = Path(streaming_mod.__file__).read_text(encoding="utf-8")
    marker = "_evicted_items = []"
    collect_start = src.index(marker)
    lock_start = src.index("with SESSION_AGENT_CACHE_LOCK:", collect_start)
    lock_end = src.index("# Commit and close evicted agents outside the cache lock", lock_start)
    locked_section = src[lock_start:lock_end]
    outside_section = src[lock_end:src.index("logger.debug('[webui] Created new agent", lock_end)]

    assert "commit_session_memory" not in locked_section
    assert "_lifecycle_commit" not in locked_section
    assert "SESSION_AGENT_CACHE.popitem" in locked_section
    assert "_lifecycle_commit" in outside_section
    assert "wait=True" in outside_section
    assert "outside the cache lock" in outside_section


def test_clear_session_evicts_outside_session_lock():
    """Clearing a session must not hold the per-session mutation lock while
    evicting its cached agent, because eviction can run provider commit I/O."""
    import api.routes as routes_mod
    src = Path(routes_mod.__file__).read_text(encoding="utf-8")

    route_start = src.index('if parsed.path == "/api/session/clear"')
    route_end = src.index('if parsed.path == "/api/session/truncate"', route_start)
    route_block = src[route_start:route_end]

    lock_start = route_block.index("with _get_session_agent_lock(sid):")
    lock_end = route_block.index("# Evict cached agent outside the per-session lock", lock_start)
    locked_section = route_block[lock_start:lock_end]
    outside_section = route_block[lock_end:]

    assert "_evict_session_agent" not in locked_section
    assert "s.save()" in locked_section
    assert "_evict_session_agent(sid)" in outside_section
    assert "provider" in outside_section and "I/O" in outside_section


def test_post_turn_lifecycle_marks_completion_without_commit():
    """Source-adjacent test: verify post-turn lifecycle only calls
    mark_turn_completed and does NOT call commit_session_memory.  Per
    CLI-parity semantics, completed turns are marked dirty/uncommitted;
    actual extraction/commit happens only at session boundaries
    (new session, LRU eviction, shutdown drain)."""
    import api.streaming as streaming_mod
    src = Path(streaming_mod.__file__).read_text(encoding="utf-8")

    save_pos = src.index("s.save()")
    lifecycle_marker = src.index("mark_turn_completed(s.session_id, agent=agent)", save_pos)
    cancel_check = src.index("cancel_event.is_set()", save_pos)
    completed_journal = src.index('"completed"', save_pos)
    sync_to_state_db = src.index("# Sync to state.db", save_pos)

    assert lifecycle_marker > cancel_check, (
        "mark_turn_completed must appear after the cancellation check"
    )
    assert lifecycle_marker > completed_journal, (
        "mark_turn_completed must appear after the completed-turn journal event"
    )
    assert lifecycle_marker < sync_to_state_db

    # The post-turn block must contain mark_turn_completed but NOT
    # commit_session_memory — extraction is a boundary concern.
    block_start = src.rindex("if not ephemeral:", save_pos, lifecycle_marker)
    block_end_pos = src.index("# Sync to state.db", save_pos)
    post_turn_block = src[block_start:block_end_pos]
    assert "mark_turn_completed" in post_turn_block
    assert "commit_session_memory" not in post_turn_block
    assert "per-session writeback lock" in post_turn_block


def test_multiple_completed_turns_coalesce_into_single_boundary_commit():
    """Behavioral test: repeated completed turns accumulate without commit,
    and a single boundary commit flushes the coalesced segment once."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()
    sid = "coalesce-boundary"

    lifecycle.register_agent(sid, agent)

    # Multiple turns complete — all are marked but NOT committed.
    lifecycle.mark_turn_completed(sid, agent=agent)
    assert lifecycle.has_uncommitted_work(sid) is True

    lifecycle.mark_turn_completed(sid, agent=agent)
    assert lifecycle.has_uncommitted_work(sid) is True

    lifecycle.mark_turn_completed(sid, agent=agent)
    assert lifecycle.has_uncommitted_work(sid) is True

    # A single boundary commit flushes all accumulated work.
    assert lifecycle.commit_session_memory(sid) is True
    assert agent.calls == 1, "one boundary commit should coalesce all turns"
    assert lifecycle.has_uncommitted_work(sid) is False

    # A further boundary commit is a no-op when nothing new happened.
    assert lifecycle.commit_session_memory(sid) is False
    assert agent.calls == 1


def test_empty_session_id_is_safe_noop():
    """All lifecycle API functions must no-op safely for falsy session IDs."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()

    assert lifecycle.register_agent("", agent) is None
    assert lifecycle.unregister_agent("") is None
    assert lifecycle.mark_turn_completed("") == 0
    assert lifecycle.has_uncommitted_work("") is False
    assert lifecycle.commit_session_memory("") is False
    assert lifecycle.commit_session_memory("", agent=agent) is False

    assert lifecycle.register_agent(None, agent) is None
    assert lifecycle.unregister_agent(None) is None
    assert lifecycle.mark_turn_completed(None) == 0
    assert lifecycle.has_uncommitted_work(None) is False
    assert lifecycle.commit_session_memory(None) is False
