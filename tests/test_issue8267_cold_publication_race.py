"""Regression coverage for #8267.

A cold ``get_session`` reads the sidecar, then publishes that object into
``SESSIONS``. A writer that saves the session in between (for example the
rename handler's lock-held ``Session.load`` fallback after an eviction) used to
be overwritten in the cache by the pre-write object. ``_cached_session_lags_disk``
compares message counts and scenes only, so a metadata-only write such as a
title change stayed hidden, and the next whole-session save (archive here)
wrote the old title back to disk.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session

from tests.test_issue7738_stale_session_overwrite import (
    _FakeHandler,
    _capture_post,
    _isolate_session_store,
    _load_disk_session,
)


@pytest.fixture(autouse=True)
def _focused_resolver(monkeypatch):
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda _s: False)
    monkeypatch.setattr(models, "_repair_stale_pending", lambda _s: False)
    monkeypatch.setattr(models, "_session_has_pending_journal_retry", lambda _s: False)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda _s: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_worktree_retained_payload", lambda _s: {})
    yield
    SESSIONS.clear()


class _ColdLoaderBarrier:
    """Pause one named thread after ``Session.load`` returns, before publish."""

    def __init__(self, monkeypatch, thread_name):
        self.thread_name = thread_name
        self.loaded = threading.Event()
        self.resume = threading.Event()
        self.loads = 0
        real_load = Session.load.__func__

        def _load(cls, sid):
            session = real_load(cls, sid)
            if threading.current_thread().name == self.thread_name:
                self.loads += 1
                if self.loads == 1:
                    self.loaded.set()
                    assert self.resume.wait(5), "test never resumed the cold loader"
            return session

        monkeypatch.setattr(models.Session, "load", classmethod(_load))


class _EvictThenStartLoaderLock:
    """Agent-lock proxy: on acquire, evict the cache entry (cap pressure) and
    start a cold ``get_session`` that pauses after its sidecar read."""

    def __init__(self, inner, sid, barrier, results):
        self._inner = inner
        self._sid = sid
        self._barrier = barrier
        self._results = results

    def acquire(self, *args, **kwargs):
        if "thread" not in self._results:
            SESSIONS.pop(self._sid, None)

            def _cold_get():
                self._results["session"] = models.get_session(self._sid)

            thread = threading.Thread(target=_cold_get, name=self._barrier.thread_name)
            self._results["thread"] = thread
            thread.start()
            assert self._barrier.loaded.wait(5), "cold loader never read the sidecar"
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def _seed(session_dir, sid):
    session = Session(
        session_id=sid,
        title="old title",
        workspace=str(session_dir.parent),
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save()
    SESSIONS.clear()


def _rename_during_cold_load(monkeypatch, session_dir, sid):
    barrier = _ColdLoaderBarrier(monkeypatch, "cold-loader-8267")
    results = {}
    real_lock_factory = routes._get_session_agent_lock
    monkeypatch.setattr(
        routes,
        "_get_session_agent_lock",
        lambda value: _EvictThenStartLoaderLock(real_lock_factory(value), value, barrier, results),
    )
    captured = _capture_post(monkeypatch, {"session_id": sid, "title": "new title"})
    assert routes.handle_post(
        _FakeHandler(b"{}"), SimpleNamespace(path="/api/session/rename")
    ) is True
    assert captured["status"] == 200, captured
    assert _load_disk_session(session_dir, sid)["title"] == "new title"
    monkeypatch.setattr(routes, "_get_session_agent_lock", real_lock_factory)

    barrier.resume.set()
    results["thread"].join(5)
    assert not results["thread"].is_alive()
    return barrier, results


def test_cold_load_does_not_publish_a_session_read_before_a_rename(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-publish-rename"
    _seed(session_dir, sid)

    _barrier, results = _rename_during_cold_load(monkeypatch, session_dir, sid)

    assert results["session"].title == "new title"
    assert models.get_session(sid).title == "new title"
    with models.LOCK:
        assert SESSIONS[sid].title == "new title"


def test_archive_after_cold_publication_race_keeps_the_rename(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-publish-archive"
    _seed(session_dir, sid)

    _rename_during_cold_load(monkeypatch, session_dir, sid)

    captured = _capture_post(monkeypatch, {"session_id": sid, "archived": True})
    assert routes.handle_post(
        _FakeHandler(b"{}"), SimpleNamespace(path="/api/session/archive")
    ) is True
    assert captured["status"] == 200, captured

    on_disk = _load_disk_session(session_dir, sid)
    assert on_disk["archived"] is True
    assert on_disk["title"] == "new title"
    assert Session.load(sid).title == "new title"


def test_cold_load_republishes_after_an_unpublished_locked_write(tmp_path, monkeypatch):
    """A lock-held writer that saves a fresh ``Session.load`` object without
    touching ``SESSIONS`` (the #8251 import repair shape) must not be hidden by
    a cold load that read the sidecar first."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-publish-unpublished-writer"
    _seed(session_dir, sid)
    barrier = _ColdLoaderBarrier(monkeypatch, "cold-loader-8267b")
    results = {}

    def _cold_get():
        results["session"] = models.get_session(sid)

    thread = threading.Thread(target=_cold_get, name=barrier.thread_name)
    thread.start()
    assert barrier.loaded.wait(5)

    from api.config import _get_session_agent_lock

    with _get_session_agent_lock(sid):
        writer = Session.load(sid)
        writer.model_provider = "xai-oauth"
        writer.save(touch_updated_at=False)

    barrier.resume.set()
    thread.join(5)
    assert not thread.is_alive()

    assert results["session"].model_provider == "xai-oauth"
    assert models.get_session(sid).model_provider == "xai-oauth"
    # One extra read is the cost of the detected write; an unchanged sidecar
    # still costs exactly one (see test_full_session_resolve_gate.py).
    assert barrier.loads == 2


def test_cold_load_raises_keyerror_when_sidecar_deleted_before_publish(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-publish-deleted"
    _seed(session_dir, sid)
    barrier = _ColdLoaderBarrier(monkeypatch, "cold-loader-8267c")
    results = {}

    def _cold_get():
        try:
            results["session"] = models.get_session(sid)
        except KeyError as exc:
            results["error"] = exc

    thread = threading.Thread(target=_cold_get, name=barrier.thread_name)
    thread.start()
    assert barrier.loaded.wait(5)
    (session_dir / f"{sid}.json").unlink()
    barrier.resume.set()
    thread.join(5)
    assert not thread.is_alive()

    assert "session" not in results
    assert isinstance(results.get("error"), KeyError)
    with models.LOCK:
        assert sid not in SESSIONS


def test_raw_sidecar_restores_count_as_writes(tmp_path, monkeypatch):
    """Writers that bypass ``Session.save`` must still bump the generation a
    cold loader compares, or they reopen the race."""
    from api import session_recovery

    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-publish-raw-restore"
    _seed(session_dir, sid)
    sidecar = session_dir / f"{sid}.json"
    payload = sidecar.read_bytes()

    before, _ = models._sidecar_write_generation(sid)
    routes._restore_chat_start_entry_sidecar(
        (sidecar, payload, sidecar.with_suffix(".json.bak"), False, None, False)
    )
    after_restore, _ = models._sidecar_write_generation(sid)
    assert after_restore == before + 2
    assert sidecar.read_bytes() == payload

    session_recovery._note_recovered_sidecar_write(sidecar)
    assert models._sidecar_write_generation(sid)[0] == after_restore + 1
