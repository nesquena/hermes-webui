"""Regression tests for SessionDB FD-leak fixes (PR #1421) plus the
subagent shared-handle race (close-under-live-subagents).

History
-------
PR #1421: `_run_agent_streaming` created a new `SessionDB` per request and
replaced the cached agent's `_session_db` without closing the old one.
After ~73 messages on a long-lived agent, leaked FDs exhausted the 256 FD
default limit causing `EMFILE` crashes. Fix: close the previous handle
when it is safe to replace it.

Follow-up (this change): always-close-before-replace is *not* safe when
background subagents still hold a reference to the same SessionDB object
(delegate_tool copies ``parent._session_db`` by ref). A server-side wakeup
/ new turn for the parent session was closing the shared handle mid-child-
run, producing:

    Session DB append_message failed: 'NoneType' object has no attribute 'execute'

Policy now (``_adopt_session_db_for_cached_agent``):
- existing handle still open → keep it, close the unused *new* handle
- existing handle missing/closed → adopt the new handle (close dead one)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── 1: source-level pin: cached-agent reuse uses the adopt helper ──────────


def test_cached_agent_reuse_uses_adopt_helper():
    """Cached-agent reuse must go through `_adopt_session_db_for_cached_agent`
    so a still-open SessionDB is reused (subagent-safe) and only a dead handle
    is closed+replaced (still EMFILE-safe)."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    reuse_idx = src.find("Refresh per-turn callbacks")
    assert reuse_idx != -1, "cached-agent reuse block missing"
    block = src[reuse_idx : reuse_idx + 2500]

    assert "_adopt_session_db_for_cached_agent" in block, (
        "cached-agent reuse path must call _adopt_session_db_for_cached_agent "
        "instead of unconditionally closing agent._session_db. Unconditional "
        "close breaks background subagents that share the handle by reference."
    )
    assert "agent._session_db = _session_db" in block, (
        "reuse path must still assign the adopted SessionDB onto the agent"
    )
    # The old unconditional-close pattern must not remain in the reuse block.
    assert "agent._session_db.close()" not in block, (
        "unconditional agent._session_db.close() in the reuse path is the "
        "subagent race; close is now owned by _adopt_session_db_for_cached_agent"
    )


def test_adopt_and_is_open_helpers_exist():
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "def _session_db_is_open(" in src
    assert "def _adopt_session_db_for_cached_agent(" in src
    # self-heal path must also refuse to close a still-open handle
    replace_idx = src.find("def _replace_session_db_in_kwargs")
    assert replace_idx != -1
    block = src[replace_idx : replace_idx + 1200]
    assert "_session_db_is_open" in block, (
        "_replace_session_db_in_kwargs must guard on _session_db_is_open so "
        "credential self-heal cannot close a handle live subagents share"
    )
    # adopt helper must log failed closes (not bare `pass`) so EMFILE pressure
    # from a failed close is diagnosable — matches _replace_session_db_in_kwargs.
    adopt_idx = src.find("def _adopt_session_db_for_cached_agent")
    assert adopt_idx != -1
    adopt_block = src[adopt_idx : adopt_idx + 1800]
    assert 'Failed to close unused session_db handle in adopt helper' in adopt_block
    assert "logger.debug" in adopt_block


# ── 2: source-level pin: LRU eviction path still closes _session_db ────────


def test_lru_eviction_closes_evicted_agent_session_db():
    """SAME LEAK SHAPE on the LRU eviction path: when SESSION_AGENT_CACHE
    grows beyond SESSION_AGENT_CACHE_MAX (default 25), the LRU agent gets popped
    via `popitem(last=False)`. Without explicit close, its `_session_db` waits
    on GC finalization which may never run on a long-lived server.

    Fix: capture the evicted entry, close its agent's `_session_db` before
    dropping the reference. (Eviction is a true session boundary — no live
    subagents are expected to still be writing into that agent.)
    """
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    eviction_idx = src.find("Evicted LRU agent from cache")
    assert eviction_idx != -1, "LRU eviction debug log missing"
    block = src[max(0, eviction_idx - 1500) : eviction_idx + 200]

    assert "evicted_sid, _ = SESSION_AGENT_CACHE.popitem" not in block, (
        "LRU eviction must capture the evicted entry so the agent's "
        "_session_db can be closed. The `evicted_sid, _ = ...` discard form "
        "is the original bug shape."
    )

    assert "_close_evicted_agent_at_session_boundary(_evicted_sid, _evicted_agent)" in block, (
        "LRU eviction must route the evicted agent through the session-boundary "
        "close helper."
    )
    helper_start = src.index("def _close_evicted_agent_at_session_boundary")
    helper_end = src.index("\ndef _refresh_cached_agent_runtime", helper_start)
    helper_block = src[helper_start:helper_end]
    assert "session_db.close()" in helper_block, (
        "LRU eviction helper must close the evicted agent's _session_db. "
        "(Opus pre-release follow-up to PR #1421.)"
    )


# ── 3: behavioral: SessionDB.close() is idempotent + safe ──────────────────


def test_session_db_close_is_idempotent():
    """`SessionDB.close()` must be safe to call multiple times."""
    import importlib.util
    if importlib.util.find_spec("hermes_state") is None:
        pytest.skip("hermes_state not on import path (CI-only — agent repo not present)")
    from hermes_state import SessionDB  # type: ignore
    import tempfile

    with tempfile.TemporaryDirectory() as tmpd:
        db_path = Path(tmpd) / "test.db"
        db = SessionDB(db_path=db_path)
        with db._lock:
            db._conn.execute("SELECT 1")
        db.close()
        assert db._conn is None
        db.close()
        assert db._conn is None
        db.close()


# ── 4: behavioral: adopt helper keeps open handles, closes dead ones ───────


class _MockSessionDB:
    def __init__(self, name, open_=True):
        self.name = name
        self.close_calls = 0
        # Mirror SessionDB: open → _conn is truthy; closed → _conn is None.
        self._conn = object() if open_ else None

    def close(self):
        self.close_calls += 1
        self._conn = None


class _MockAgent:
    def __init__(self, db):
        self._session_db = db
        self.stream_delta_callback = None
        self.tool_progress_callback = None
        self._api_call_count = 0
        self._interrupted = False
        self._interrupt_message = None


def _import_adopt_helpers():
    """Import the production helpers.

    No source-slicing / exec fallback: that path breaks as soon as the helpers
    reference module-level names (e.g. ``logger.debug``) or another def is
    inserted between the markers. Prefer a real import; skip the behavioral
    suite when the package cannot be imported (CI without full deps).
    Source-level pins above still catch reverts without importing streaming.
    """
    try:
        from api.streaming import (  # type: ignore
            _adopt_session_db_for_cached_agent,
            _session_db_is_open,
        )
    except Exception as exc:
        pytest.skip(f"api.streaming helpers not importable: {exc}")
    return _session_db_is_open, _adopt_session_db_for_cached_agent


def test_adopt_reuses_open_session_db_and_closes_new():
    """Live (open) existing handle must be kept; unused new handle closed.

    This is the subagent-safe path: children hold a reference to `old_db`.
    """
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=True)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)

    result = adopt(agent, new_db)

    assert result is old_db
    assert agent._session_db is old_db
    assert old_db.close_calls == 0, "must not close the live shared handle"
    assert new_db.close_calls == 1, "unused per-request handle must be closed (FD leak)"
    assert _is_open(old_db) is True
    assert _is_open(new_db) is False


def test_adopt_replaces_closed_session_db():
    """Dead existing handle is closed (idempotent) and replaced with the new one."""
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=False)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)

    result = adopt(agent, new_db)

    assert result is new_db
    assert agent._session_db is new_db
    assert old_db.close_calls == 1
    assert new_db.close_calls == 0


def test_adopt_handles_missing_existing():
    _is_open, adopt = _import_adopt_helpers()
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(None)
    agent._session_db = None

    result = adopt(agent, new_db)

    assert result is new_db
    assert agent._session_db is new_db
    assert new_db.close_calls == 0


def test_cached_agent_reuse_calls_adopt_semantics():
    """End-to-end mirror of the production reuse block using the real helper."""
    _is_open, adopt = _import_adopt_helpers()
    old_db = _MockSessionDB("old", open_=True)
    new_db = _MockSessionDB("new", open_=True)
    agent = _MockAgent(old_db)
    _session_db = new_db

    # Mirror production:
    if _session_db is not None:
        _session_db = adopt(agent, _session_db)
        agent._session_db = _session_db

    assert agent._session_db is old_db
    assert old_db.close_calls == 0
    assert new_db.close_calls == 1


# ── 5: behavioral: LRU eviction with mock agents ────────────────────────────


def test_lru_eviction_closes_evicted_session_db():
    """End-to-end: simulate LRU eviction and verify the evicted agent's
    SessionDB.close() is called."""
    import collections

    cache = collections.OrderedDict()
    db1, db2, db3 = _MockSessionDB("a"), _MockSessionDB("b"), _MockSessionDB("c")
    cache["sid-a"] = (_MockAgent(db1), "sig1")
    cache["sid-b"] = (_MockAgent(db2), "sig2")
    cache["sid-c"] = (_MockAgent(db3), "sig3")

    MAX = 2
    while len(cache) > MAX:
        evicted_sid, evicted_entry = cache.popitem(last=False)
        try:
            _evicted_agent = evicted_entry[0] if isinstance(evicted_entry, tuple) else None
            if _evicted_agent is not None and getattr(_evicted_agent, "_session_db", None) is not None:
                _evicted_agent._session_db.close()
        except Exception:
            pass

    assert "sid-a" not in cache
    assert db1.close_calls == 1, "evicted agent's SessionDB must be closed exactly once"
    assert db2.close_calls == 0, "remaining agents' SessionDBs must not be touched"
    assert db3.close_calls == 0


# ── 6: self-heal path must not reuse a CLOSED handle when the rebuild fails ──


def _import_replace_helper():
    """Import the real credential-self-heal SessionDB replacer."""
    try:
        from api.streaming import _replace_session_db_in_kwargs  # type: ignore
    except Exception as exc:
        pytest.skip(f"api.streaming not importable: {exc}")
    return _replace_session_db_in_kwargs


def test_replace_degrades_to_none_when_rebuild_fails_and_old_is_closed(monkeypatch):
    """Credential self-heal regression (Codex gate finding on PR #6143).

    When ``_build_session_db_for_stream`` returns None (rebuild failed) AND the
    prior handle is already CLOSED, ``_replace_session_db_in_kwargs`` must leave
    ``agent_kwargs['session_db'] = None`` — as master did — so the rebuilt agent
    lazily reinitialises. Retaining the closed handle (the pre-fix behaviour)
    makes every persist/search fail with
    ``'NoneType' object has no attribute 'execute'`` while the chat continues.
    """
    import api.streaming as streaming

    _replace = _import_replace_helper()
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _p: None)

    old_db = _MockSessionDB("old", open_=False)  # already closed
    kwargs = {"session_db": old_db}
    result = _replace(kwargs, "/tmp/does-not-matter.db")

    assert result is None, "must not hand back a closed handle when rebuild fails"
    assert kwargs["session_db"] is None, "kwargs must degrade to None (clean lazy reinit)"


def test_replace_keeps_open_handle_when_rebuild_fails(monkeypatch):
    """Inverse: a still-OPEN prior handle (held by live subagents) is retained
    when the rebuild fails — do not orphan a live shared connection."""
    import api.streaming as streaming

    _replace = _import_replace_helper()
    monkeypatch.setattr(streaming, "_build_session_db_for_stream", lambda _p: None)

    old_db = _MockSessionDB("old", open_=True)  # still live (subagents hold it)
    kwargs = {"session_db": old_db}
    result = _replace(kwargs, "/tmp/does-not-matter.db")

    assert result is old_db, "a live handle must be kept when the rebuild fails"
    assert kwargs["session_db"] is old_db
    assert old_db.close_calls == 0, "must not close a live shared handle"
