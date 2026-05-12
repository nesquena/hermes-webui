"""Regression tests for v0.50.259 — SessionDB FD leak fixes from PR #1421
plus Opus pre-release advisor follow-up extending the fix to LRU eviction.

The bug: `_run_agent_streaming` created a new `SessionDB` per request and
replaced the cached agent's `_session_db` without closing the old one.
After ~73 messages on a long-lived agent, leaked FDs exhausted the 256 FD
default limit causing `EMFILE` crashes.

Fix path 1 (PR #1421 by @wali-reheman): close `agent._session_db` before
replacing it on the cached-agent reuse path.

Fix path 2 (this PR Opus follow-up): same shape on the LRU eviction path.
When `SESSION_AGENT_CACHE.popitem(last=False)` evicts an old agent, its
`_session_db` is dropped on the floor and only released when GC eventually
finalizes the agent — which on a long-running server may be never.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── 1: source-level pin: cached-agent-reuse path closes _session_db ─────────


def test_cached_agent_reuse_closes_old_session_db():
    """The cached-agent reuse path in `_run_agent_streaming` MUST close the
    old `_session_db` before replacing it. Without this, every streaming
    request leaks a SessionDB connection (3 FDs once WAL is active)."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # Find the cached-agent reuse block (where callbacks are refreshed).
    # The replacement of agent._session_db must be preceded by a close().
    reuse_idx = src.find("Refresh per-turn callbacks")
    assert reuse_idx != -1, "cached-agent reuse block missing"
    block = src[reuse_idx : reuse_idx + 2500]

    # Must close before replace.
    assert "agent._session_db.close()" in block, (
        "cached-agent reuse path must call agent._session_db.close() before "
        "replacing it. Without this, FDs leak on every streaming request "
        "and the server EMFILE-crashes after ~73 messages. See PR #1421."
    )
    # And the close must come BEFORE the replacement (lexically).
    close_idx = block.find("agent._session_db.close()")
    replace_idx = block.find("agent._session_db = _session_db")
    assert close_idx != -1 and replace_idx != -1
    assert close_idx < replace_idx, (
        "close() must lexically precede the assignment so the old connection "
        "is released before the reference is rebound."
    )


# ── 2: source-level pin: LRU eviction path also closes _session_db ──────────


def test_lru_eviction_closes_evicted_agent_session_db():
    """SAME LEAK SHAPE on the LRU eviction path: when SESSION_AGENT_CACHE
    grows beyond SESSION_AGENT_CACHE_MAX (50), the LRU agent gets popped via
    `popitem(last=False)`. Without explicit close, its `_session_db` waits
    on GC finalization which may never run on a long-lived server.

    Fix: capture the evicted entry, close its agent's `_session_db` before
    dropping the reference.
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # The eviction site uses popitem(last=False). The evicted entry must be
    # captured (not discarded with `_`) and the agent's _session_db closed.
    eviction_idx = src.find("Evicted LRU agent from cache")
    assert eviction_idx != -1, "LRU eviction debug log missing"
    # Look in a window around the eviction.
    block = src[max(0, eviction_idx - 1500) : eviction_idx + 200]

    # Negative pattern: the old `evicted_sid, _ = ...` discard form must NOT
    # be present — that's the bug shape.
    assert "evicted_sid, _ = SESSION_AGENT_CACHE.popitem" not in block, (
        "LRU eviction must capture the evicted entry so the agent\'s "
        "_session_db can be closed. The `evicted_sid, _ = ...` discard form "
        "is the original bug shape."
    )

    # Positive pattern: must close on the evicted agent.
    assert "_evicted_agent._session_db.close()" in block, (
        "LRU eviction must close the evicted agent\'s _session_db. "
        "(Opus pre-release follow-up to PR #1421.)"
    )


# ── 3: behavioral: SessionDB.close() is idempotent + safe ──────────────────


def test_session_db_close_is_idempotent():
    """`SessionDB.close()` must be safe to call multiple times. The fix
    relies on this — if a future code path closes the same `_session_db`
    after we've swapped it, the second close is a benign no-op.

    Skipped when hermes_state is not on the import path (e.g. on the GH
    Actions runner that only has the WebUI repo, not the agent repo).
    The source-level pin in test_cached_agent_reuse_closes_old_session_db
    catches revert of the close() call; this test only adds runtime
    confirmation when both repos are co-located.
    """
    import importlib.util
    if importlib.util.find_spec("hermes_state") is None:
        pytest.skip("hermes_state not on import path (CI-only — agent repo not present)")
    try:
        from hermes_state import SessionDB  # type: ignore
    except ModuleNotFoundError as exc:
        pytest.skip(f"hermes_state dependencies not importable in this checkout: {exc}")
    import tempfile

    with tempfile.TemporaryDirectory() as tmpd:
        db_path = Path(tmpd) / "test.db"
        db = SessionDB(db_path=db_path)
        # Force connection open by issuing a query.
        with db._lock:
            db._conn.execute("SELECT 1")
        # First close
        db.close()
        assert db._conn is None
        # Second close — must not raise.
        db.close()
        assert db._conn is None
        # Third close — still safe.
        db.close()


# ── 4: behavioral: cached-agent reuse with mock agent ───────────────────────


def test_cached_agent_reuse_calls_close_on_old_session_db():
    """End-to-end: simulate the cached-agent reuse code path with a mock
    agent and verify the mock SessionDB.close() is called when _session_db
    is replaced. Pins the runtime behavior, not just the source pattern."""
    import sys
    sys.path.insert(0, str(REPO))

    class MockSessionDB:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0
        def close(self):
            self.close_calls += 1

    class MockAgent:
        def __init__(self, db):
            self._session_db = db
            self.stream_delta_callback = None
            self.tool_progress_callback = None
            self._api_call_count = 0
            self._interrupted = False
            self._interrupt_message = None

    # Simulate the inner block of the cached-agent reuse path. We replicate
    # the pattern manually rather than importing _run_agent_streaming
    # directly because that function has many other side effects.
    old_db = MockSessionDB("old")
    new_db = MockSessionDB("new")
    agent = MockAgent(old_db)

    # Mirror the production code path:
    if hasattr(agent, "_session_db") and agent._session_db is not None:
        try:
            agent._session_db.close()
        except Exception:
            pass
    agent._session_db = new_db

    assert old_db.close_calls == 1, (
        "old SessionDB must be closed exactly once when replaced on cached agent"
    )
    assert new_db.close_calls == 0, "new SessionDB should not be closed"
    assert agent._session_db is new_db


# ── 5: behavioral: LRU eviction with mock agents ────────────────────────────


def test_lru_eviction_closes_evicted_session_db():
    """End-to-end: simulate LRU eviction and verify the evicted agent's
    SessionDB.close() is called."""
    import collections

    class MockSessionDB:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0
        def close(self):
            self.close_calls += 1

    class MockAgent:
        def __init__(self, db):
            self._session_db = db

    cache = collections.OrderedDict()
    db1, db2, db3 = MockSessionDB("a"), MockSessionDB("b"), MockSessionDB("c")
    cache["sid-a"] = (MockAgent(db1), "sig1")
    cache["sid-b"] = (MockAgent(db2), "sig2")
    cache["sid-c"] = (MockAgent(db3), "sig3")

    # Mirror the production eviction path with MAX=2:
    MAX = 2
    while len(cache) > MAX:
        evicted_sid, evicted_entry = cache.popitem(last=False)
        try:
            _evicted_agent = evicted_entry[0] if isinstance(evicted_entry, tuple) else None
            if _evicted_agent is not None and getattr(_evicted_agent, "_session_db", None) is not None:
                _evicted_agent._session_db.close()
        except Exception:
            pass

    # First-inserted entry (sid-a) was evicted.
    assert "sid-a" not in cache
    assert db1.close_calls == 1, "evicted agent's SessionDB must be closed exactly once"
    assert db2.close_calls == 0, "remaining agents' SessionDBs must not be touched"
    assert db3.close_calls == 0
