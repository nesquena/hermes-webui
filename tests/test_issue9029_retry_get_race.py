"""Regression coverage for #9029.

``retry_last`` / ``undo_last`` truncate the cached session object under
``LOCK``, release it, and only then ``save()``. A full ``get_session()`` that
lands in that window saw a cached object with fewer messages than the sidecar,
took it for a cache lagging the disk, and reloaded the pre-truncation file into
``SESSIONS``. The retry then persisted the shorter transcript, but the cache
kept the reloaded old one: the removed reply came back and the next reader
could write it to disk again.

The interleaving is forced inside the truncation's own ``save`` call: before
the write proceeds, a real ``get_session`` runs on another thread and joins.
That is exactly the issue's window (after the in-memory truncation, before the
save) with no wall-clock delay.
"""

from __future__ import annotations

import json
import threading

import pytest

import api.models as models
from api.models import SESSIONS, Session
from api.session_ops import (
    _stamp_intentional_shrink_generation,
    retry_last,
    truncate_session_at_keep,
    undo_last,
)

WAIT = 10.0  # failure guard for the joined GET, never a synchronization delay

Q1 = "q1"
A1 = "a1"
PARTIAL = "[partial]"
Q2 = "q2"
OLD_A2 = "OLD-A2"


def _msg(role: str, content: str, ts: float, mid: str, **extra) -> dict:
    row = {"id": mid, "role": role, "content": content, "timestamp": ts}
    row.update(extra)
    return row


def _history() -> list[dict]:
    """q1, a1, [partial], q2, OLD-A2 — the issue's reproduction shape."""
    return [
        _msg("user", Q1, 1.0, "u1"),
        _msg("assistant", A1, 2.0, "a1"),
        _msg("assistant", PARTIAL, 2.5, "p1", _partial=True),
        _msg("user", Q2, 3.0, "u2"),
        _msg("assistant", OLD_A2, 4.0, "a2"),
    ]


SHRUNK = [Q1, A1, PARTIAL]


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    # Reading paths must not add unrelated repairs/syncs to this race.
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda _s: False)
    monkeypatch.setattr(models, "_repair_stale_pending", lambda _s: False)
    monkeypatch.setattr(models, "_session_has_pending_journal_retry", lambda _s: False)
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


def _seed_and_cache(session_dir, sid: str) -> Session:
    seed = Session(
        session_id=sid,
        title="issue 9029",
        workspace=str(session_dir.parent),
        messages=_history(),
        context_messages=_history(),
    )
    seed.save()
    SESSIONS.clear()
    cached = models.get_session(sid)  # cold load; 5 messages
    assert len(cached.messages) == 5
    return cached


def _raw_disk(session_dir, sid: str) -> dict:
    return json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))


def _load_disk(session_dir, sid: str) -> list[dict]:
    return _raw_disk(session_dir, sid)["messages"]


def _install_get_between_truncation_and_save(monkeypatch, sid: str, seen: list):
    """Run a full GET on another thread just before the retry's save writes.

    The hook fires on the first ``Session.save`` after arming, which is the
    retry/undo save: the in-memory truncation has already happened and ``LOCK``
    has been released, exactly the window #9029 describes.
    """
    real_save = Session.save
    state = {"armed": False, "fired": False}

    def save(self, *args, **kwargs):
        if state["armed"] and not state["fired"] and self.session_id == sid:
            state["fired"] = True

            def _get():
                seen.append(models.get_session(sid))

            thread = threading.Thread(target=_get, name="get-9029", daemon=True)
            thread.start()
            thread.join(WAIT)
            assert not thread.is_alive(), "the interleaved GET never returned"
            assert len(seen) == 1
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", save)
    return state


@pytest.mark.parametrize(
    "operation", [retry_last, undo_last], ids=["retry", "undo"]
)
def test_get_between_truncation_and_save_cannot_restore_removed_reply(
    tmp_path, monkeypatch, operation
):
    session_dir = tmp_path / "sessions"
    sid = f"issue9029-{operation.__name__}"
    _seed_and_cache(session_dir, sid)

    seen: list[Session] = []
    state = _install_get_between_truncation_and_save(monkeypatch, sid, seen)
    state["armed"] = True
    try:
        operation(sid)
    finally:
        state["armed"] = False

    # The GET landed in the truncated-not-yet-saved window and must have seen
    # the shorter in-memory object, not the pre-truncation file.
    assert len(seen) == 1
    assert [row["content"] for row in seen[0].messages] == SHRUNK

    # The operation itself persisted the shrink.
    assert [row["content"] for row in _load_disk(session_dir, sid)] == SHRUNK
    # The pending marker is in-memory only: the sidecar schema is untouched.
    assert not any(
        "intentional_shrink_pending" in key for key in _raw_disk(session_dir, sid)
    )

    # The next full read is a cache hit and still sees the shrink.
    after = models.get_session(sid)
    assert [row["content"] for row in after.messages] == SHRUNK

    # Persisting the object the next reader got (as chat/start does) must not
    # put the removed reply back on disk.
    after.save()
    assert [row["content"] for row in _load_disk(session_dir, sid)] == SHRUNK


def test_checkpointed_disk_ahead_of_cache_still_reloads(tmp_path, monkeypatch):
    """Guard: a genuinely newer sidecar must still replace the cache.

    The #9029 marker only covers a truncation that is pending its own save.
    A stream checkpoint that appended a message to the same sidecar is real
    disk progress and must keep reloading.
    """
    session_dir = tmp_path / "sessions"
    sid = "issue9029-checkpoint-ahead"
    _seed_and_cache(session_dir, sid)

    writer = Session.load(sid)
    writer.messages.append(_msg("assistant", "checkpointed", 5.0, "a3"))
    writer.save()

    fresh = models.get_session(sid)
    assert [row["content"] for row in fresh.messages] == [Q1, A1, PARTIAL, Q2, OLD_A2, "checkpointed"]
    with models.LOCK:
        assert SESSIONS[sid] is fresh


def test_pending_shrink_marker_does_not_pin_cache_when_disk_moves_past_snapshot(
    tmp_path, monkeypatch
):
    """Guard: the pending-shrink marker is not a permanent cache pin.

    If another writer moves the sidecar past the pre-shrink snapshot while the
    truncation is still unsaved, disk is newer than both states and the normal
    freshness check must win.
    """
    session_dir = tmp_path / "sessions"
    sid = "issue9029-shrink-then-disk-ahead"
    cached = _seed_and_cache(session_dir, sid)

    # The retry/undo window: the cached object is already truncated, unsaved,
    # and carries the in-memory pending-shrink count.
    truncate_session_at_keep(cached, 3)
    assert getattr(cached, "_intentional_shrink_pending_count", None) == 5
    assert [row["content"] for row in cached.messages] == SHRUNK

    writer = Session.load(sid)
    writer.messages.append(_msg("assistant", "checkpointed", 5.0, "a3"))
    writer.save()

    fresh = models.get_session(sid)
    assert [row["content"] for row in fresh.messages] == [Q1, A1, PARTIAL, Q2, OLD_A2, "checkpointed"]
    assert getattr(fresh, "_intentional_shrink_pending_count", None) is None


def test_stamp_records_pending_count_only_on_shrink():
    session = Session(session_id="issue9029-stamp", messages=_history())
    assert _stamp_intentional_shrink_generation(session, 5, 5) is False
    assert getattr(session, "_intentional_shrink_pending_count", None) is None
    assert _stamp_intentional_shrink_generation(session, 5, 3) is True
    assert session._intentional_shrink_pending_count == 5
