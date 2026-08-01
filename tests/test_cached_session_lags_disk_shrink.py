"""Regression coverage: cached session must detect an external transcript SHRINK.

Root cause: `_cached_session_lags_disk` only treated the disk sidecar as fresher
when its message_count was GREATER than the cached object's (growth), or when
anchor-scene fingerprints advanced. An external squash/truncate rewrites the
sidecar with FEWER messages and a newer updated_at; the running server kept
serving the stale pre-shrink cached object (thousands of messages, multi-MB
payloads) until LRU eviction or an in-process mutation — the browser then sat
on "Loading conversation..." while it parsed the resurrected transcript.

The fix: for an idle (no active stream, no pending user message) cached
session, a strictly smaller disk message_count combined with a strictly newer
disk updated_at proves an external shrink and forces a reload. In-process
shrinks mutate the same cached object before saving, so they can never produce
that combination; active/pending sessions are never reloaded by this path.
"""
import json
import time

import api.models as M
import pytest


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    sdir = tmp_path / "sessions"
    monkeypatch.setattr(M, "SESSION_DIR", sdir)
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _persist(session_store, sid, n_messages, updated_at):
    msgs = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n_messages)
    ]
    s = M.Session(session_id=sid, title="T", workspace=str(session_store.parent),
                  model="glm", messages=msgs)
    s.updated_at = updated_at
    s.save(touch_updated_at=False)
    return s


def _rewrite_shrunk(session_store, sid, n_messages, updated_at):
    """Simulate the external squash: fewer messages, strictly newer updated_at."""
    return _persist(session_store, sid, n_messages, updated_at)


def test_idle_cache_reloads_when_disk_shrunk_newer(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 50, base)
    _rewrite_shrunk(session_store, "s1", 1, base + 50)
    assert M._cached_session_lags_disk(cached) is True


def test_idle_cache_kept_when_disk_smaller_but_older(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 50, base)
    # Disk behind the cached snapshot (stale file) must NOT clobber memory.
    _rewrite_shrunk(session_store, "s1", 1, base - 50)
    assert M._cached_session_lags_disk(cached) is False


def test_active_stream_cache_never_reloaded_on_external_shrink(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 50, base)
    cached.active_stream_id = "stream-1"
    _rewrite_shrunk(session_store, "s1", 1, base + 50)
    assert M._cached_session_lags_disk(cached) is False


def test_pending_user_message_cache_never_reloaded_on_external_shrink(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 50, base)
    cached.pending_user_message = "hello"
    _rewrite_shrunk(session_store, "s1", 1, base + 50)
    assert M._cached_session_lags_disk(cached) is False


def test_growth_still_reloads(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 2, base)
    _persist(session_store, "s1", 5, base + 50)
    assert M._cached_session_lags_disk(cached) is True


def test_equal_count_parity_kept(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 4, base)
    assert M._cached_session_lags_disk(cached) is False


def test_shrink_equal_updated_at_kept(session_store):
    base = time.time() - 100
    cached = _persist(session_store, "s1", 50, base)
    # Same updated_at: cannot prove the shrink is newer than the snapshot.
    _rewrite_shrunk(session_store, "s1", 1, base)
    assert M._cached_session_lags_disk(cached) is False
