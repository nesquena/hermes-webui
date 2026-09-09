from __future__ import annotations

import api.config as config
import api.gateway_chat as gateway_chat
import api.models as models
import pytest
from api.models import Session
from api.process_event_utils import build_active_turn_token


@pytest.fixture(autouse=True)
def _isolate_session_storage(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def _session(tmp_path, *, with_final: bool) -> Session:
    stream_id = "gateway-terminal-run"
    started_at = 100.0
    token = build_active_turn_token(stream_id, started_at)
    messages = [
        {
            "role": "user",
            "content": "current prompt",
            "timestamp": started_at,
            "_active_turn_token": token,
        }
    ]
    if with_final:
        messages.append(
            {"role": "assistant", "content": "durably saved gateway final"}
        )
    session = Session(
        session_id="gateway-terminal-session",
        title="Gateway terminal truth",
        workspace=str(tmp_path),
        messages=messages,
        context_messages=list(messages),
    )
    session.pending_user_message = "current prompt"
    session.pending_started_at = started_at
    session.pending_attachments = []
    session.pending_user_source = "webui"
    session.active_stream_id = stream_id
    return session


def test_gateway_activity_without_final_persists_incomplete_final(
    tmp_path, monkeypatch
):
    session = _session(tmp_path, with_final=False)
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setitem(config.STREAM_PARTIAL_TEXT, session.active_stream_id, "partial")

    payload = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        session.active_stream_id,
        session.workspace,
        session.model,
        session.model_provider,
        "Gateway returned no final response",
        terminal_state="incomplete_final",
    )

    assert payload["terminal_state"] == "incomplete_final"
    assert payload["type"] == "incomplete_final"
    assert session.messages[-1]["_error"] is True
    assert session.messages[-1]["_terminal_state"] == "incomplete_final"


def test_gateway_durable_final_wins_over_late_error(tmp_path, monkeypatch):
    session = _session(tmp_path, with_final=True)
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    original_messages = list(session.messages)

    payload = gateway_chat._settle_gateway_terminal_error(
        session.session_id,
        session.active_stream_id,
        session.workspace,
        session.model,
        session.model_provider,
        "late gateway transport failure",
    )

    assert payload["terminal_state"] == "completed"
    assert payload["type"] == "completed"
    assert session.messages == original_messages
