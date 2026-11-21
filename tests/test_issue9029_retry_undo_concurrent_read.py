"""Regression tests for #9029: a concurrent full GET between an intentional
shrink (retry/undo/truncate/clear) and its save reloads the OLD sidecar over
the shrunk cached object, putting the removed reply back.

The window is hit deterministically: Session.save is patched so the
concurrent full read runs at the exact moment the in-memory object has been
truncated but the sidecar has not been rewritten yet.

The second half of the file covers the REVERSED order: a full read that
finishes AFTER retry/undo saved and republished still held the old sidecar
snapshot and republished it over the shortened object.
"""
from __future__ import annotations

import threading


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _five_message_sidecar(session_id: str):
    """The issue's shape: q1, a1, [partial], q2, OLD-A2."""
    from api.models import Session

    return Session(
        session_id=session_id,
        messages=[
            _msg("user", "q1", 1.0, "u1"),
            _msg("assistant", "a1", 2.0, "a1"),
            _msg("assistant", "partial", 2.5, "p1"),
            _msg("user", "q2", 3.0, "u2"),
            _msg("assistant", "OLD-A2", 4.0, "a2"),
        ],
    )


def _prepare_session_dir(monkeypatch, tmp_path, session_id: str):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _patch_save_with_concurrent_full_read(monkeypatch, models, session_id: str):
    """Run one full get_session() inside save(), i.e. after the in-memory
    truncation but before the sidecar is rewritten — the issue's race window,
    without relying on timing."""
    original_save = models.Session.save
    seen = []
    in_window = False

    def save_with_concurrent_read(self, *args, **kwargs):
        nonlocal in_window
        if str(getattr(self, "session_id", "")) == session_id and not in_window:
            in_window = True
            try:
                seen.append(models.get_session(session_id))
            finally:
                in_window = False
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", save_with_concurrent_read)
    return seen


def test_retry_concurrent_full_read_in_window_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """#9029 core repro: retry_last truncates the cached object, then saves.
    A full GET landing in between must not reload the old sidecar back into
    the cache — the removed reply OLD-A2 must not come back."""
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "retryrace")
    session = _five_message_sidecar("retryrace")
    session.save()
    models.SESSIONS["retryrace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "retryrace")

    session_ops.retry_last("retryrace")

    # Disk must hold the truncated transcript (OLD-A2 gone).
    on_disk = models.Session.load("retryrace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert "OLD-A2" not in disk_contents, (
        f"retry did not persist the truncation: {disk_contents}"
    )
    assert disk_contents == ["q1", "a1", "partial"]

    # The NEXT full read must still see the truncated transcript — the
    # concurrent read inside the window must not have reloaded the old file.
    after = models.get_session("retryrace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_undo_concurrent_full_read_in_window_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """Same shape for undo_last (#9029)."""
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "undorace")
    session = _five_message_sidecar("undorace")
    session.save()
    models.SESSIONS["undorace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "undorace")

    session_ops.undo_last("undorace")

    on_disk = models.Session.load("undorace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert disk_contents == ["q1", "a1", "partial"], (
        f"undo did not persist the truncation: {disk_contents}"
    )

    after = models.get_session("undorace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache after undo: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_truncate_endpoint_concurrent_read_in_window_does_not_restore_messages(
    monkeypatch, tmp_path
):
    """Same window on the real /api/session/truncate route (#9029).

    The truncate route already republishes the saved object after its save,
    so this passes on unpatched master as well — it guards that the
    shrink-window handling keeps truncate correct, it is not a RED repro.
    """
    import json
    from io import BytesIO
    from types import SimpleNamespace

    import api.routes as routes

    models = _prepare_session_dir(monkeypatch, tmp_path, "truncrace")
    session = _five_message_sidecar("truncrace")
    session.save()
    models.SESSIONS["truncrace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "truncrace")

    body = {"session_id": "truncrace", "keep_count": 3}
    body_bytes = json.dumps(body).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured.update(payload)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))

    assert captured.get("ok") is True
    on_disk = models.Session.load("truncrace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert disk_contents == ["q1", "a1", "partial"], (
        f"truncate did not persist: {disk_contents}"
    )
    after = models.get_session("truncrace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache after truncate: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_genuine_stale_cache_still_reloads_from_disk(monkeypatch, tmp_path):
    """Control group: the freshness check must keep doing its actual job.

    When the sidecar is legitimately AHEAD of the cache (another writer
    persisted new messages while the LRU still holds the older object),
    get_session() must reload from disk. The #9029 fix must not turn the
    shrink-window exemption into a blanket 'cache is always fresh'."""
    models = _prepare_session_dir(monkeypatch, tmp_path, "staleref")

    cached = _five_message_sidecar("staleref")
    cached.save()
    models.SESSIONS["staleref"] = cached

    # Another writer persists two NEW messages through a different object,
    # exactly like the active/reconnect path this check exists for.
    other = models.Session.load("staleref")
    assert other is not None
    other.messages = other.messages + [
        _msg("user", "q3", 5.0, "u3"),
        _msg("assistant", "A3", 6.0, "a3"),
    ]
    other.save()

    resolved = models.get_session("staleref")
    contents = [m["content"] for m in resolved.messages]
    assert "q3" in contents and "A3" in contents, (
        "a genuinely stale cache (disk ahead) must still be reloaded; "
        f"got {contents}"
    )
    assert len(contents) == 7


# ---------------------------------------------------------------------------
# The reversed order: the concurrent full read finishes AFTER the save.
#
# The tests above run the concurrent read to completion *inside* save(), i.e.
# BEFORE the truncated transcript is durable. The race below is the other
# order: the read already holds the OLD sidecar snapshot and publishes it
# only after retry/undo saved and republished the shortened object.
#
#   1. Retry/Undo shrinks the cached object in place under LOCK. The sidecar
#      still holds the OLD transcript.
#   2. The reader starts and captures that (now shortened) cached object.
#      The freshness check sees "cache behind disk" and demands a reload.
#   3. The reader's Session.load() reads the OLD file -- the removed reply is
#      still on disk at this instant -- and the reader pauses holding it.
#   4. Retry/Undo continues: detach, save the truncated transcript, republish
#      the shortened object into SESSIONS.
#   5. The reader resumes and publishes its OLD snapshot over the republished
#      object, so the removed reply is back in the cache. The next read shows
#      it and the next whole-session save writes it back to disk.
#
# The reader is started from the shrink/detach seam so the ordering is
# deterministic: the cache-hit reload path is only taken when the cached
# object is ALREADY shorter than the sidecar.
# ---------------------------------------------------------------------------


class _LateReaderBarrier:
    """Pause the reader right after it read the OLD sidecar, before it publishes.

    ``Session.load`` is wrapped so the reader thread -- identified by name --
    blocks AFTER the real load returned (it now holds the OLD snapshot) and
    BEFORE control returns to the resolver that publishes it into ``SESSIONS``.
    The cache-publication logic under test is NOT mocked: only the ordering is
    controlled.
    """

    def __init__(self, monkeypatch, thread_name: str):
        self.thread_name = thread_name
        self.loaded = threading.Event()
        self.resume = threading.Event()
        self.loads = 0
        import api.models as models
        from api.models import Session

        real_load = Session.load.__func__

        def _load(cls, sid):
            session = real_load(cls, sid)
            if threading.current_thread().name == self.thread_name:
                self.loads += 1
                if self.loads == 1:
                    self.loaded.set()
                    assert self.resume.wait(10), "test never resumed the late reader"
            return session

        monkeypatch.setattr(models.Session, "load", classmethod(_load))


def _seed_session(models, session_id: str):
    session = _five_message_sidecar(session_id)
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _run_mutation_with_a_late_reader(models, session_ops, session_id, barrier, mutate):
    """Drive *mutate* (retry/undo) while a full read finishes AFTER the save.

    The reader is started at the shrink/detach seam: by then the cached
    object is already shorter than the sidecar, so the reader takes the
    cache-hit reload path and reads the OLD file.
    """
    real_detach = session_ops._detach_session_for_intentional_shrink
    results: dict = {}

    def _read():
        try:
            results["session"] = models.get_session(session_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            results["error"] = exc

    def _detach_hook(sid):
        # The shrink already happened; the sidecar still holds the OLD
        # transcript. Start the reader here so it captures the shortened
        # cached object and takes the cache-hit reload path.
        reader = threading.Thread(target=_read, name=barrier.thread_name)
        reader.start()
        assert barrier.loaded.wait(10), "the reader never reached the sidecar read"
        return real_detach(sid)

    session_ops._detach_session_for_intentional_shrink = _detach_hook
    try:
        mutate(session_id)
    finally:
        session_ops._detach_session_for_intentional_shrink = real_detach
    return results


def _assert_removed_reply_stays_removed(models, session_id: str, label: str):
    """Three checks: the cache, the next full read, and disk after a re-save."""
    # 1) The cache must still hold the shortened session.
    with models.LOCK:
        cached = models.SESSIONS.get(session_id)
    assert cached is not None, f"{label}: the session vanished from the cache"
    cached_contents = [m["content"] for m in (cached.messages or [])]
    assert "OLD-A2" not in cached_contents, (
        f"{label}: the cache holds the removed reply again: {cached_contents}"
    )
    assert cached_contents == ["q1", "a1", "partial"], (
        f"{label}: the cache is not the shortened session: {cached_contents}"
    )

    # 2) The next full read must not resurrect the removed reply.
    after = models.get_session(session_id)
    after_contents = [m["content"] for m in (after.messages or [])]
    assert "OLD-A2" not in after_contents, (
        f"{label}: the next full read resurrected the removed reply: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"], (
        f"{label}: the next full read is not the shortened session: {after_contents}"
    )

    # 3) Saving that read result and re-reading from disk must not bring the
    #    reply back on disk either (the "next save writes it to disk again"
    #    half of the issue).
    after.save()
    reloaded = models.Session.load(session_id)
    assert reloaded is not None
    disk_contents = [m["content"] for m in (reloaded.messages or [])]
    assert "OLD-A2" not in disk_contents, (
        f"{label}: the removed reply came back on disk: {disk_contents}"
    )
    assert disk_contents == ["q1", "a1", "partial"], (
        f"{label}: the sidecar is not the shortened transcript: {disk_contents}"
    )


def test_retry_with_a_late_concurrent_read_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """#9029 second race: the concurrent full read finishes AFTER retry's save.

    The reader takes the cache-hit reload path (the cached object is shorter
    than the sidecar, so the freshness check demands a reload), reads the OLD
    file, and publishes it after retry already republished the shortened
    object. The removed reply must not come back.
    """
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "retrylate")
    _seed_session(models, "retrylate")

    barrier = _LateReaderBarrier(monkeypatch, "late-reader-retry")
    results = _run_mutation_with_a_late_reader(
        models, session_ops, "retrylate", barrier, session_ops.retry_last
    )

    # Let the reader publish its stale snapshot over the republished object.
    barrier.resume.set()
    assert barrier.resume.is_set()
    reader = [t for t in threading.enumerate() if t.name == barrier.thread_name]
    for thread in reader:
        thread.join(10)
        assert not thread.is_alive(), "the late reader never finished"
    assert "error" not in results, f"the late reader raised: {results.get('error')}"

    _assert_removed_reply_stays_removed(
        models, "retrylate", "retry with a late concurrent read"
    )


def test_undo_with_a_late_concurrent_read_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """Same reversed order for undo_last (#9029)."""
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "undolate")
    _seed_session(models, "undolate")

    barrier = _LateReaderBarrier(monkeypatch, "late-reader-undo")
    results = _run_mutation_with_a_late_reader(
        models, session_ops, "undolate", barrier, session_ops.undo_last
    )

    barrier.resume.set()
    reader = [t for t in threading.enumerate() if t.name == barrier.thread_name]
    for thread in reader:
        thread.join(10)
        assert not thread.is_alive(), "the late reader never finished"
    assert "error" not in results, f"the late reader raised: {results.get('error')}"

    _assert_removed_reply_stays_removed(
        models, "undolate", "undo with a late concurrent read"
    )


def test_a_read_that_finishes_before_the_save_still_sees_the_old_transcript(
    monkeypatch, tmp_path
):
    """Control group: the pre-save semantics must not change.

    A full read that lands BEFORE retry's save is durable legitimately sees
    the old transcript -- the removal is not persisted yet. The fix must not
    turn that into a stale-cache rejection or an exception.
    """
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "presave")

    _seed_session(models, "presave")
    seen = models.get_session("presave")
    contents = [m["content"] for m in (seen.messages or [])]
    assert "OLD-A2" in contents, (
        "a read before the save must still serve the on-disk transcript: "
        f"{contents}"
    )

    session_ops.retry_last("presave")
    after = models.get_session("presave")
    after_contents = [m["content"] for m in (after.messages or [])]
    assert "OLD-A2" not in after_contents, after_contents


def test_a_concurrent_non_transcript_write_does_not_serve_older_messages(
    monkeypatch, tmp_path
):
    """#9229 follow-up: a concurrent write that does NOT change the transcript
    must not make the reader fall back to older cached messages.

    A reasoning-effort pick rewrites the sidecar without touching the
    transcript, but it still bumps the sidecar write count. The read then
    knows its snapshot is stale, but the resident object is still BEHIND the
    sidecar (it holds fewer messages), so serving the resident would return
    older messages than the sidecar holds -- and saving that object could
    erase the newer ones.
    """
    import api.models as models

    models = _prepare_session_dir(monkeypatch, tmp_path, "effortrace")

    def _msg(role, content, ts, mid):
        return {"id": mid, "role": role, "content": content, "timestamp": ts}

    # The cache holds 5 messages.
    cached = models.Session(
        session_id="effortrace",
        messages=[
            _msg("user", "q1", 1.0, "u1"),
            _msg("assistant", "a1", 2.0, "a1"),
            _msg("assistant", "partial", 2.5, "p1"),
            _msg("user", "q2", 3.0, "u2"),
            _msg("assistant", "A2", 4.0, "a2"),
        ],
    )
    cached.save()
    models.SESSIONS["effortrace"] = cached

    # Another writer persists two MORE messages: the sidecar now holds 7 and
    # the cache still holds 5, so the cached object genuinely lags the disk.
    other = models.Session.load("effortrace")
    other.messages = other.messages + [
        _msg("user", "q3", 5.0, "u3"),
        _msg("assistant", "A3", 6.0, "a3"),
    ]
    other.save()
    models.SESSIONS["effortrace"] = cached

    # During the read, ANOTHER thread makes a non-transcript write: it bumps
    # the write count but leaves the messages alone.
    real_load = models.Session.load.__func__

    def _load(cls, sid):
        session = real_load(cls, sid)
        if sid == "effortrace" and threading.current_thread().name == "effort-reader":
            def _writer():
                models.set_session_reasoning_effort("effortrace", "high")

            thread = threading.Thread(target=_writer, name="effort-writer")
            thread.start()
            thread.join(10)
        return session

    monkeypatch.setattr(models.Session, "load", classmethod(_load))

    results: dict = {}

    def _read():
        try:
            results["session"] = models.get_session("effortrace")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
            results["error"] = exc

    reader = threading.Thread(target=_read, name="effort-reader")
    reader.start()
    reader.join(10)
    assert not reader.is_alive(), "the reader never finished"
    assert "error" not in results, f"the reader raised: {results.get('error')}"

    contents = [m["content"] for m in (results["session"].messages or [])]
    assert "q3" in contents and "A3" in contents, (
        "a concurrent write that did not change the transcript made the read "
        f"serve older messages than the sidecar holds: {contents}"
    )
    assert len(contents) == 7, contents

    # The next full read must still see all seven, and the cache must hold
    # them too -- a stale resident must not survive the read.
    with models.LOCK:
        cached_now = models.SESSIONS.get("effortrace")
    assert cached_now is not None
    cached_contents = [m["content"] for m in (cached_now.messages or [])]
    assert "q3" in cached_contents and "A3" in cached_contents, (
        f"the cache kept the stale transcript: {cached_contents}"
    )
