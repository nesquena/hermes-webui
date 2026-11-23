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


@pytest.mark.parametrize("operation", [retry_last, undo_last], ids=["retry", "undo"])
def test_get_parked_after_count_snapshot_does_not_drop_new_marker(
    tmp_path, monkeypatch, operation
):
    """Greptile P1 on #9273: the count snapshot must not be lock-free.

    A GET that read ``cached_count`` BEFORE the shrink and then reads the marker
    AFTER it sees ``disk_count == cached_count`` (the stale count) and clears
    the fresh marker as if the shrink's save had landed. A second GET then
    reloads the pre-truncation file and the removed reply comes back.
    Snapshotting the count and marker under LOCK, and clearing only the
    generation the snapshot observed, keeps the new marker alive.
    """
    session_dir = tmp_path / "sessions"
    sid = f"issue9029-stale-count-{operation.__name__}"
    _seed_and_cache(session_dir, sid)

    parked = threading.Event()
    resume = threading.Event()
    inside_freshness = {"value": False}
    real_lags = models._cached_session_lags_disk
    real_count = models._persisted_message_count

    def lags(cached):
        inside_freshness["value"] = True
        try:
            return real_lags(cached)
        finally:
            inside_freshness["value"] = False

    def count(session_id):
        # Park ONLY the freshness check's disk-count read, after it has taken
        # its count snapshot and before it compares the marker.
        if session_id == sid and inside_freshness["value"] and not parked.is_set():
            parked.set()
            assert resume.wait(WAIT), "the parked GET was never resumed"
        return real_count(session_id)

    monkeypatch.setattr(models, "_cached_session_lags_disk", lags)
    monkeypatch.setattr(models, "_persisted_message_count", count)

    first = {}

    def _first_get():
        try:
            first["session"] = models.get_session(sid)
        except BaseException as exc:  # surfaced on the main thread below
            first["error"] = exc

    getter = threading.Thread(target=_first_get, name="get-9029-parked", daemon=True)
    getter.start()
    assert parked.wait(WAIT), "the GET never reached the disk-count read"

    # The GET holds its pre-shrink snapshot (5 messages, no marker) and is
    # parked before reading the count. Run the real retry/undo: its save hook
    # resumes the GET in the truncated-not-yet-saved window, then lands a
    # second full GET before the save itself.
    real_save = Session.save
    state = {"armed": True, "second": None}

    def save(self, *args, **kwargs):
        if state["armed"] and self.session_id == sid:
            state["armed"] = False
            resume.set()
            getter.join(WAIT)
            assert not getter.is_alive(), "the interleaved GET never returned"
            assert "error" not in first, first.get("error")
            state["second"] = models.get_session(sid)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "save", save)
    try:
        operation(sid)
    finally:
        state["armed"] = False
        resume.set()
        getter.join(WAIT)

    # The parked GET resumed after the in-memory shrink and must not have
    # resurrected the old transcript for itself...
    assert [row["content"] for row in first["session"].messages] == SHRUNK
    # ...nor for the second GET that landed before the save.
    second = state["second"]
    assert second is not None
    assert [row["content"] for row in second.messages] == SHRUNK

    after = models.get_session(sid)
    assert [row["content"] for row in after.messages] == SHRUNK
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


@pytest.mark.parametrize(
    "operation", [retry_last, undo_last], ids=["retry", "undo"]
)
def test_get_between_truncation_and_save_keeps_unanswered_user_prefix(
    tmp_path, monkeypatch, operation
):
    session_dir = tmp_path / "sessions"
    sid = f"issue9029-unanswered-{operation.__name__}"
    messages = [
        _msg("user", "unanswered", 1.0, "u1"),
        _msg("user", "retry-me", 2.0, "u2"),
        _msg("assistant", "removed-reply", 3.0, "a1"),
    ]
    seed = Session(
        session_id=sid,
        title="issue 9029 unanswered",
        workspace=str(session_dir.parent),
        messages=messages,
        context_messages=messages,
    )
    seed.save()
    SESSIONS.clear()
    cached = models.get_session(sid)
    assert len(cached.messages) == 3

    seen: list[Session] = []
    state = _install_get_between_truncation_and_save(monkeypatch, sid, seen)
    state["armed"] = True
    try:
        operation(sid)
    finally:
        state["armed"] = False

    # The interleaved GET landed before the save and saw the shorter prefix.
    assert len(seen) == 1
    assert [row["content"] for row in seen[0].messages] == ["unanswered"]

    # The operation itself persisted the shrink.
    assert [row["content"] for row in _load_disk(session_dir, sid)] == ["unanswered"]

    # The next full read is a cache hit and still sees the shrink.
    after = models.get_session(sid)
    assert [row["content"] for row in after.messages] == ["unanswered"]

    # Persisting the object the next reader got must keep only the shrink on disk.
    after.save()
    assert [row["content"] for row in _load_disk(session_dir, sid)] == ["unanswered"]


def test_unsaved_user_tail_without_pending_shrink_still_reloads(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    sid = "issue9029-control-unsaved-user-tail"
    disk_messages = [
        _msg("user", "unanswered", 1.0, "u1"),
        _msg("user", "retry-me", 2.0, "u2"),
        _msg("assistant", "reply", 3.0, "a1"),
    ]
    disk = Session(
        session_id=sid,
        title="issue 9029 control",
        workspace=str(session_dir.parent),
        messages=disk_messages,
        context_messages=disk_messages,
    )
    disk.save()

    cached_messages = [_msg("user", "unanswered", 1.0, "u1")]
    cached = Session(
        session_id=sid,
        title="issue 9029 control",
        workspace=str(session_dir.parent),
        messages=cached_messages,
        context_messages=cached_messages,
    )
    assert getattr(cached, "_intentional_shrink_pending_count", None) is None

    assert models._cache_has_stale_unsaved_user_tail(cached, disk) is True

    with models.LOCK:
        SESSIONS[sid] = cached

    reloaded = models.get_session(sid)
    assert [row["content"] for row in reloaded.messages] == ["unanswered", "retry-me", "reply"]
    with models.LOCK:
        assert SESSIONS[sid] is reloaded
        assert SESSIONS[sid] is not cached


@pytest.mark.parametrize("operation", [retry_last, undo_last], ids=["retry", "undo"])
def test_get_load_before_save_keeps_the_shrink(tmp_path, monkeypatch, operation):
    """The other arrival order: the GET loads the old sidecar first.

    The GET is parked between ``Session.load`` and the stale-tail decision, the
    retry/undo then lands its save and clears the pending marker, and only then
    does the decision run. Snapshotting the marker at decision time reads "no
    shrink" and replaces the shorter cache with the three-row copy the load read
    earlier, so the next reader writes the removed reply back to disk.
    """
    session_dir = tmp_path / "sessions"
    sid = f"issue9029-split-order-{operation.__name__}"
    messages = [
        _msg("user", "unanswered", 1.0, "u1"),
        _msg("user", "retry-me", 2.0, "u2"),
        _msg("assistant", "removed-reply", 3.0, "a1"),
    ]
    seed = Session(
        session_id=sid,
        title="issue 9029 split order",
        workspace=str(session_dir.parent),
        messages=messages,
        context_messages=messages,
    )
    seed.save()
    SESSIONS.clear()
    cached = models.get_session(sid)
    assert len(cached.messages) == 3

    loaded = threading.Event()
    released = threading.Event()
    seen: dict = {}
    threads: list = []
    save_state = {"armed": False}
    load_state = {"armed": False}
    real_load = Session.load
    real_save = Session.save

    def load(cls, session_id, *args, **kwargs):
        session = real_load(session_id, *args, **kwargs)
        if load_state["armed"] and not load_state.get("fired") and session_id == sid:
            load_state["fired"] = True
            seen["disk_at_load"] = [row["content"] for row in session.messages]
            loaded.set()
            assert released.wait(WAIT), "the parked GET was never released"
        return session

    def save(self, *args, **kwargs):
        if save_state["armed"] and self.session_id == sid:
            save_state["armed"] = False
            load_state["armed"] = True

            def _get():
                try:
                    seen["session"] = models.get_session(sid)
                except BaseException as exc:  # surfaced on the main thread
                    seen["error"] = exc

            thread = threading.Thread(target=_get, name="get-9029-split", daemon=True)
            threads.append(thread)
            thread.start()
            assert loaded.wait(WAIT), "the GET never loaded the sidecar"
            # The save lands; the caller (retry/undo) clears the marker right
            # after save() returns -- the GET stays parked through both.
            return real_save(self, *args, **kwargs)
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Session, "load", classmethod(load))
    monkeypatch.setattr(Session, "save", save)
    save_state["armed"] = True
    try:
        operation(sid)
    finally:
        save_state["armed"] = False
        load_state["armed"] = False
        released.set()
        for thread in threads:
            thread.join(WAIT)
            assert not thread.is_alive(), "the parked GET never returned"

    assert "error" not in seen, seen.get("error")
    # The load read the pre-shrink file...
    assert seen["disk_at_load"] == ["unanswered", "retry-me", "removed-reply"]
    # ...but the decision ran after the shrink's own save cleared the marker, so
    # it must keep the shorter cache instead of restoring the copy it loaded.
    assert [row["content"] for row in seen["session"].messages] == ["unanswered"]
    assert [row["content"] for row in _load_disk(session_dir, sid)] == ["unanswered"]

    after = models.get_session(sid)
    assert [row["content"] for row in after.messages] == ["unanswered"]
    after.save()
    assert [row["content"] for row in _load_disk(session_dir, sid)] == ["unanswered"]


@pytest.mark.parametrize("keep", [0, 2], ids=["clear", "truncate"])
def test_truncate_save_does_not_pin_the_cache_against_newer_disk(
    tmp_path, monkeypatch, keep
):
    """The truncate/clear route's own save must end the pending-shrink window.

    Those routes truncate and save, but nothing cleared the marker afterwards.
    While the marker lived, ``_cached_session_lags_disk`` treated every sidecar
    up to the pre-shrink snapshot as "older than the cache", so a genuinely
    newer disk was not reloaded until it reached parity with the cache again.
    """
    session_dir = tmp_path / "sessions"
    sid = f"issue9029-truncate-marker-{keep}"
    cached = _seed_and_cache(session_dir, sid)
    expected = [Q1, A1][:keep]

    # /api/session/truncate and /api/session/clear both do exactly these two
    # calls (truncate_session_at_keep + save).
    truncate_session_at_keep(cached, keep)
    cached.save()
    # The in-flight window is over once the truncated transcript is persisted.
    assert getattr(cached, "_intentional_shrink_pending_count", None) is None

    # A real writer moves the sidecar past the truncated state.
    writer = Session.load(sid)
    writer.messages = list(writer.messages) + [_msg("assistant", "newer", 9.0, "n1")]
    writer.save()

    fresh = models.get_session(sid)
    assert [row["content"] for row in fresh.messages] == expected + ["newer"]
    with models.LOCK:
        assert SESSIONS[sid] is fresh


def test_owner_record_save_under_lock_does_not_block_on_the_clear(tmp_path, monkeypatch):
    """A caller that already holds ``LOCK`` must not wait on it inside ``save``.

    ``api/routes.py`` materializes the first-draft owner record with
    ``draft_session.save(touch_updated_at=False, skip_index=True)`` while it
    holds both the per-session persist lock and ``LOCK``. ``LOCK`` is a plain
    ``threading.Lock``, so a save that re-acquires it to close the pending-shrink
    window blocks on itself forever, and every request that needs ``LOCK`` with
    it. The owner-record save skips the sidebar index for exactly that reason,
    and the clear has to follow the same rule.

    A fresh plain lock stands in for ``api.config.LOCK`` so that a regression
    wedges only this test instead of every later acquisition in the session.
    """
    session_dir = tmp_path / "sessions"
    sid = "issue9029-owner-record-under-lock"
    monkeypatch.setattr(models, "LOCK", threading.Lock())
    draft = Session(
        session_id=sid,
        title="owner record",
        workspace=str(session_dir.parent),
        messages=[],
        context_messages=[],
    )
    # The state the reviewer asked about: a generation stamped on the object
    # that this owner-record save then persists.
    draft.intentional_shrink_generation = "generation-owner-record"
    draft._intentional_shrink_pending_count = 4
    finished = threading.Event()
    errors: list = []

    def _save_under_lock():
        try:
            with models._session_persist_lock(sid), models.LOCK:
                draft.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:  # surfaced on the test thread
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(
        target=_save_under_lock, name="owner-record-9029", daemon=True
    )
    thread.start()
    try:
        assert finished.wait(WAIT), (
            "Session.save(skip_index=True) blocked on LOCK while its caller held it"
        )
    finally:
        thread.join(WAIT)
    assert not errors, errors[0]
    assert not thread.is_alive(), "the owner-record save never returned"
    assert (session_dir / f"{sid}.json").exists()


def test_generation_read_with_the_rows_it_describes(tmp_path, monkeypatch):
    """The clear's token has to be read with the rows this save captured.

    ``_save_locked`` captured the rows and only then read
    ``intentional_shrink_generation``. A shrink landing between the two made the
    save persist the OLD rows while clearing the NEW shrink's marker, so the next
    reader reloaded the removed rows.
    """
    session_dir = tmp_path / "sessions"
    sid = "issue9029-generation-with-rows"
    cached = _seed_and_cache(session_dir, sid)
    real_dedup = models._deduplicate_exact_stable_messages
    state = {"armed": True}

    def dedup(messages, *args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            # The shrink lands after the rows were handed to this save.
            cached.messages = list(cached.messages)[:3]
            cached.intentional_shrink_generation = "generation-mid-capture"
            cached._intentional_shrink_pending_count = len(messages)
        return real_dedup(messages, *args, **kwargs)

    monkeypatch.setattr(models, "_deduplicate_exact_stable_messages", dedup)
    cached.save()

    # Control: this save persisted the rows it had captured (the old five).
    assert len(_load_disk(session_dir, sid)) == 5
    # The token travelled with those rows, so the sidecar neither claims the new
    # generation nor the truncated transcript.
    assert _raw_disk(session_dir, sid)["intentional_shrink_generation"] is None
    # And the marker the late shrink stamped is still the pending authority.
    assert cached.intentional_shrink_generation == "generation-mid-capture"
    assert getattr(cached, "_intentional_shrink_pending_count", None) == 5

