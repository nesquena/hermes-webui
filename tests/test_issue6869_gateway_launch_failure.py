"""Regression tests for #6869: failed Gateway worker launch cleanup."""

import copy
from types import SimpleNamespace

import api.config as config
import api.routes as routes
from api import turn_journal


def _make_session(session_id, *, active_stream_id=None, pending_user_message=None, save=None):
    return SimpleNamespace(
        session_id=session_id,
        title="Existing",
        active_stream_id=active_stream_id,
        pending_user_message=pending_user_message,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        messages=[{"role": "user", "content": "old"}],
        workspace="/tmp",
        model="old-model",
        model_provider=None,
        worktree_path=None,
        profile=None,
        save=save or (lambda: None),
    )


def _register_failed_stream(session_id, stream_id):
    """Mirror the registry state _prepare_chat_start_session_for_stream installs."""
    config.register_session_writeback_owner(session_id, stream_id)
    config.register_stream_owner(stream_id, session_id)
    config.STREAMS[stream_id] = object()


def test_gateway_thread_start_failure_releases_writeback_owner_and_stream_state(monkeypatch):
    """A thread-start exception must not leave one owner per failed Gateway launch."""
    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread launch failed")

    canonical_by_id = {}
    registered_stream_ids = []

    def fake_prepare(session, *, stream_id, **kwargs):
        session.active_stream_id = stream_id
        session.pending_user_message = kwargs["msg"]
        session.pending_started_at = 1.0
        _register_failed_stream(session.session_id, stream_id)
        canonical_by_id[session.session_id] = session
        registered_stream_ids.append(stream_id)

    monkeypatch.setattr(routes.threading, "Thread", FailingThread)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace, **_kw: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: canonical_by_id[sid],
    )
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: {},
    )

    for index in range(3):
        session = _make_session(f"session-launch-failure-{index}")
        try:
            routes._start_chat_stream_for_session(
                session,
                msg="start gateway turn",
                attachments=[],
                workspace="/tmp",
                model="test-model",
                external_runtime_owned=True,
            )
        except RuntimeError as exc:
            assert str(exc) == "thread launch failed"
        else:
            raise AssertionError("thread-start failure must propagate")
        assert session.active_stream_id is None
        assert session.pending_user_message is None
        assert session.pending_started_at is None

    # Scope these to the streams this test registered. scripts/test.sh runs the
    # whole selection in one process, so asserting the global registries are
    # empty makes this test fail on unrelated files that leave entries behind.
    assert registered_stream_ids, "the launch path must register state before failing"
    for stream_id in registered_stream_ids:
        assert stream_id not in config.STREAM_SESSION_OWNERS
        assert stream_id not in config.STREAMS
    for session_id in canonical_by_id:
        assert config.session_writeback_owner(session_id) is None


def test_gateway_launch_failure_cleanup_does_not_clear_successor_owner(monkeypatch):
    """A successor fully installed before cleanup keeps its registry ownership."""
    canonical = _make_session(
        "session-launch-successor",
        active_stream_id="new-stream",
        pending_user_message="new prompt",
        save=lambda: None,
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: canonical,
    )
    stale = _make_session("session-launch-successor")
    _register_failed_stream(stale.session_id, "old-stream")
    config.register_session_writeback_owner(stale.session_id, "new-stream")

    routes._cleanup_chat_start_launch_failure(stale, "old-stream", copy.deepcopy(stale.__dict__))

    assert config.session_writeback_owner(stale.session_id) == "new-stream"
    assert canonical.active_stream_id == "new-stream"
    assert canonical.pending_user_message == "new prompt"
    assert "old-stream" not in config.STREAMS


def test_gateway_launch_failure_cleanup_restores_snapshot_before_saving(monkeypatch):
    saved = []
    canonical = _make_session(
        "session-launch-restore",
        save=lambda *args, **kwargs: saved.append(kwargs),
    )
    snapshot = copy.deepcopy(canonical.__dict__)
    canonical.active_stream_id = "old-stream"
    canonical.pending_user_message = "failed prompt"
    canonical.pending_attachments = [{"name": "failed.txt"}]
    canonical.pending_started_at = 9.0
    canonical.pending_user_source = "webui"
    canonical.messages.append({"role": "user", "content": "failed prompt"})
    _register_failed_stream(canonical.session_id, "old-stream")
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: canonical,
    )

    routes._cleanup_chat_start_launch_failure(canonical, "old-stream", snapshot)

    restored = copy.deepcopy(canonical.__dict__)
    assert restored.pop("intentional_shrink_generation")
    expected = copy.deepcopy(snapshot)
    expected.pop("intentional_shrink_generation", None)
    assert restored == expected
    assert saved == [{"touch_updated_at": False}]


def test_gateway_launch_failure_cleanup_does_not_wipe_successor_admitted_during_cleanup(monkeypatch):
    """A successor admitted mid-cleanup survives: the guard runs on the re-resolved
    canonical session, not on the stale passed-in object that still shows the
    failed stream."""
    saved = []

    canonical = _make_session(
        "session-launch-race",
        active_stream_id="new-stream",
        pending_user_message="successor prompt",
        save=lambda: saved.append("canonical"),
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: canonical,
    )
    stale = _make_session(
        "session-launch-race",
        active_stream_id="old-stream",
        pending_user_message="failed-stream prompt",
        save=lambda: saved.append("stale"),
    )
    _register_failed_stream(stale.session_id, "old-stream")
    config.register_session_writeback_owner(stale.session_id, "new-stream")

    routes._cleanup_chat_start_launch_failure(stale, "old-stream", copy.deepcopy(stale.__dict__))

    assert config.session_writeback_owner(stale.session_id) == "new-stream"
    assert canonical.active_stream_id == "new-stream"
    assert canonical.pending_user_message == "successor prompt"
    assert "old-stream" not in config.STREAMS
    assert saved == [], "cleanup must not save a session it early-returns on"


def test_gateway_launch_failure_cleanup_does_not_resurrect_deleted_session(monkeypatch):
    """A session deleted while the launch was failing must not be recreated:
    the resolver raises KeyError and cleanup returns without saving anything."""
    saved = []

    def deleted_resolver(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", deleted_resolver)
    stale = _make_session(
        "session-launch-deleted",
        active_stream_id="old-stream",
        pending_user_message="pending prompt",
        save=lambda: saved.append("stale"),
    )
    _register_failed_stream(stale.session_id, "old-stream")

    routes._cleanup_chat_start_launch_failure(stale, "old-stream", copy.deepcopy(stale.__dict__))

    assert config.session_writeback_owner(stale.session_id) is None
    assert "old-stream" not in config.STREAM_SESSION_OWNERS
    assert "old-stream" not in config.STREAMS
    assert stale.active_stream_id == "old-stream"
    assert stale.pending_user_message == "pending prompt"
    assert saved == [], "cleanup must not save a deleted session"


def test_gateway_launch_failure_cleanup_never_masks_the_launch_error(monkeypatch):
    """A resolution failure during cleanup must not replace the launch error.

    Cleanup runs inside the launch-failure handler, so an error it fails to
    contain is reported instead of the real launch failure. Resolution can fail
    with something other than KeyError when the session store is unreadable or
    cannot be deserialized.
    """
    saved = []

    def failing_resolver(sid, metadata_only=False):
        raise OSError("session store unreadable")

    monkeypatch.setattr(routes, "get_session", failing_resolver)
    stale = _make_session(
        "session-launch-resolution-error",
        active_stream_id="old-stream",
        pending_user_message="pending prompt",
        save=lambda: saved.append("stale"),
    )
    _register_failed_stream(stale.session_id, "old-stream")

    # Must not raise: the caller is already handling the launch failure.
    routes._cleanup_chat_start_launch_failure(stale, "old-stream", copy.deepcopy(stale.__dict__))

    # The registry half still completed, and nothing was saved.
    assert config.session_writeback_owner(stale.session_id) is None
    assert "old-stream" not in config.STREAM_SESSION_OWNERS
    assert "old-stream" not in config.STREAMS
    assert saved == []
