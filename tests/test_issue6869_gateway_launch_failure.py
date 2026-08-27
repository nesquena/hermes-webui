"""Regression tests for #6869: failed Gateway worker launch cleanup."""

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

    def fake_prepare(session, *, stream_id, **kwargs):
        session.active_stream_id = stream_id
        session.pending_user_message = kwargs["msg"]
        session.pending_started_at = 1.0
        _register_failed_stream(session.session_id, stream_id)
        canonical_by_id[session.session_id] = session

    monkeypatch.setattr(routes.threading, "Thread", FailingThread)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
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

    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    assert config.STREAMS == {}


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

    routes._cleanup_chat_start_launch_failure(stale, "old-stream")

    assert config.session_writeback_owner(stale.session_id) == "new-stream"
    assert canonical.active_stream_id == "new-stream"
    assert canonical.pending_user_message == "new prompt"
    assert "old-stream" not in config.STREAMS


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

    routes._cleanup_chat_start_launch_failure(stale, "old-stream")

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

    routes._cleanup_chat_start_launch_failure(stale, "old-stream")

    assert config.session_writeback_owner(stale.session_id) is None
    assert "old-stream" not in config.STREAM_SESSION_OWNERS
    assert "old-stream" not in config.STREAMS
    assert stale.active_stream_id == "old-stream"
    assert stale.pending_user_message == "pending prompt"
    assert saved == [], "cleanup must not save a deleted session"