from types import SimpleNamespace
from unittest.mock import patch
import json

from api import config, gateway_chat, routes
from api.models import Session


def test_save_clears_queue_correlation_when_no_pending_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path)
    monkeypatch.setattr("api.models.SESSION_DIR", tmp_path)
    session = Session(
        session_id="safe-correlation",
        pending_queue_item_id="stale-item",
        pending_queue_client_id="stale-client",
    )

    session.save(skip_index=True)

    persisted = json.loads((tmp_path / "safe-correlation.json").read_text(encoding="utf-8"))
    assert persisted["pending_queue_item_id"] is None
    assert persisted["pending_queue_client_id"] is None


def test_worker_thread_start_failure_clears_durable_pending_owner(monkeypatch):
    recorded = {}
    released_owners = []
    session = SimpleNamespace(
        session_id="sess-queue-start-fail",
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        title="title",
        profile=None,
        process_wakeup_pause={},
        save=lambda **_kwargs: None,
    )

    class _NoopLock:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    class _BoomThread:
        def __init__(self, *, target=None, args=None, kwargs=None, daemon=None):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    def fake_prepare(session_obj, *, stream_id, **_kwargs):
        recorded["stream_id"] = stream_id
        session_obj.active_stream_id = stream_id
        session_obj.pending_user_message = "queued prompt"
        session_obj.pending_started_at = 123.0
        session_obj.pending_user_source = "queued_followup"

    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda *_args, **_kwargs: _NoopLock())
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", fake_prepare)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: SimpleNamespace())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "unregister_stream_owner", lambda stream_id: released_owners.append(stream_id))
    monkeypatch.setattr(config, "clear_session_writeback_owner_if_owned", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "threading", SimpleNamespace(Thread=_BoomThread))

    with patch("api.turn_journal.append_turn_journal_event", return_value={}):
        response = routes._start_chat_stream_for_session(
            session,
            msg="queued prompt",
            attachments=[],
            workspace="/tmp",
            model="test-model",
            source="queued_followup",
            external_runtime_owned=True,
        )

    assert response == {"error": "failed to start agent worker", "_status": 500}
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_started_at is None
    assert released_owners == [recorded["stream_id"]]
    assert recorded["stream_id"] not in routes.STREAMS
    assert gateway_chat.gateway_run_id_pending(recorded["stream_id"]) is False
