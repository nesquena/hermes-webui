"""Regression coverage for replying to cron-origin sessions (#3975)."""

import io
import json
from types import SimpleNamespace

import api.routes as routes


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        self.response_headers.append(("__end__", ""))


def _json_body(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def test_chat_start_materializes_cron_session_before_reply(monkeypatch, tmp_path):
    """Cron sessions shown from state.db must be replyable, not treated as stale."""
    sid = "cron_job123_20260615_101544"
    materialized = SimpleNamespace(
        session_id=sid,
        title="Daily cron output",
        workspace=str(tmp_path),
        model="gpt-5.4",
        model_provider=None,
        profile="default",
        messages=[{"role": "user", "content": "cron prompt"}],
        context_messages=[],
        pending_user_message=None,
    )
    captured = {}

    def fail_get_session(_sid):
        raise KeyError(_sid)

    def materialize(_sid, **_kwargs):
        captured["materialize_sid"] = _sid
        return materialized

    def start_run(session, **kwargs):
        captured["session"] = session
        captured["kwargs"] = kwargs
        return {"stream_id": "stream-3975", "session_id": session.session_id}

    monkeypatch.setattr(routes, "get_session", fail_get_session)
    monkeypatch.setattr(routes, "_get_or_materialize_session", materialize)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _provider: (None, None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_args, **_kwargs: ("gpt-5.4", None, "gpt-5.4"),
    )
    monkeypatch.setattr(routes, "_start_run", start_run)

    handler = _FakeHandler()
    routes._handle_chat_start(
        handler,
        {
            "session_id": sid,
            "message": "follow up on the cron output",
            "workspace": str(tmp_path),
            "profile": "default",
        },
    )

    assert handler.status == 200
    assert _json_body(handler)["stream_id"] == "stream-3975"
    assert captured["materialize_sid"] == sid
    assert captured["session"] is materialized
    assert captured["kwargs"]["route"] == "/api/chat/start"
