"""Regression tests for #6869: failed Gateway worker launch cleanup."""

from types import SimpleNamespace

import api.config as config
import api.routes as routes
from api import turn_journal


def test_gateway_thread_start_failure_releases_writeback_owner_and_stream_state(monkeypatch):
    """A thread-start exception must not leave one owner per failed Gateway launch."""
    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread launch failed")

    def fake_prepare(session, *, stream_id, **kwargs):
        session.active_stream_id = stream_id
        session.pending_user_message = kwargs["msg"]
        session.pending_started_at = 1.0
        config.register_session_writeback_owner(session.session_id, stream_id)

    monkeypatch.setattr(routes.threading, "Thread", FailingThread)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: {},
    )

    for index in range(3):
        session = SimpleNamespace(
            session_id=f"session-launch-failure-{index}",
            title="Existing",
            active_stream_id=None,
            pending_user_message=None,
            pending_attachments=[],
            pending_started_at=None,
            pending_user_source=None,
            messages=[{"role": "user", "content": "old"}],
            workspace="/tmp",
            model="old-model",
            model_provider=None,
            worktree_path=None,
            profile=None,
            save=lambda: None,
        )

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

    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    assert config.STREAMS == {}


def test_gateway_launch_failure_cleanup_does_not_clear_successor_owner():
    """Cleanup must be compare-and-clear when a successor already took over."""
    session = SimpleNamespace(
        session_id="session-launch-successor",
        active_stream_id="new-stream",
        pending_user_message="new prompt",
        pending_attachments=["new.txt"],
        pending_started_at=2.0,
        pending_user_source="webui",
        save=lambda: None,
    )
    config.register_session_writeback_owner(session.session_id, "old-stream")
    config.register_session_writeback_owner(session.session_id, "new-stream")
    config.register_stream_owner("old-stream", session.session_id)
    config.STREAMS["old-stream"] = object()

    routes._cleanup_chat_start_launch_failure(session, "old-stream")

    assert config.session_writeback_owner(session.session_id) == "new-stream"
    assert session.active_stream_id == "new-stream"
    assert session.pending_user_message == "new prompt"
    assert "old-stream" not in config.STREAMS
