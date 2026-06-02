"""Regression tests for manual sidebar title regeneration.

The session three-dot menu now exposes a start-of-conversation title refresh that
uses the configured auxiliary title-generation route. These tests pin the backend
contract without making a real provider call.
"""

import io
import json
from contextlib import nullcontext
from urllib.parse import urlparse


class _FakeHandler:
    def __init__(self, body_dict):
        raw = json.dumps(body_dict).encode("utf-8")
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(raw)
        self.headers = {"Content-Length": str(len(raw))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


class _FakeSession:
    def __init__(self, messages):
        self.session_id = "sid-title-regen"
        self.title = "Old title"
        self.messages = messages
        self.read_only = False
        self.llm_title_generated = False
        self.saved_touch_values = []

    def save(self, touch_updated_at=True, **_kwargs):
        self.saved_touch_values.append(touch_updated_at)

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "message_count": len(self.messages),
            "llm_title_generated": self.llm_title_generated,
        }


def _post_regenerate_title(monkeypatch, session, fake_title: str | None = "Session title regeneration menu"):
    import api.routes as routes

    calls = []
    events = []

    def fake_generate(user_text, assistant_text, workspace_context='', **kwargs):
        calls.append({
            "user_text": user_text,
            "assistant_text": assistant_text,
            "workspace_context": workspace_context,
            **kwargs,
        })
        return fake_title, "llm_aux", ""

    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: nullcontext())
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda reason: events.append(reason))
    monkeypatch.setattr(routes, "_generate_llm_session_title_via_aux", fake_generate)

    handler = _FakeHandler({"session_id": session.session_id})
    routes.handle_post(handler, urlparse("http://example.com/api/session/regenerate_title"))
    return handler, calls, events


def test_regenerate_title_uses_opening_context_and_configured_aux(monkeypatch):
    session = _FakeSession([
        {"role": "user", "content": "How can I add title regeneration to sessions?"},
        {"role": "assistant", "content": "Use the existing session action menu."},
        {"role": "user", "content": "Opening follow-up detail"},
        {"role": "assistant", "content": "Second opening answer should be included."},
    ])

    handler, calls, events = _post_regenerate_title(monkeypatch, session)

    assert handler.status == 200
    payload = handler.json_body()
    assert payload["ok"] is True
    assert payload["title"] == "Session title regeneration menu"

    assert len(calls) == 1
    assert calls[0]["user_text"] == (
        "User 1: How can I add title regeneration to sessions?\n"
        "User 2: Opening follow-up detail"
    )
    assert calls[0]["assistant_text"] == (
        "Assistant 1: Use the existing session action menu.\n"
        "Assistant 2: Second opening answer should be included."
    )
    assert calls[0]["workspace_context"] == ""

    assert session.title == "Session title regeneration menu"
    assert session.llm_title_generated is True
    assert session.saved_touch_values == [False]
    assert events == ["session_title_regenerated"]


def test_regenerate_title_requires_user_message(monkeypatch):
    import api.routes as routes

    session = _FakeSession([{"role": "assistant", "content": "No user turn yet."}])
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)

    handler = _FakeHandler({"session_id": session.session_id})
    routes.handle_post(handler, urlparse("http://example.com/api/session/regenerate_title"))

    assert handler.status == 400
    assert "Need at least one user message" in handler.json_body()["error"]
    assert session.title == "Old title"
    assert session.saved_touch_values == []


def test_regenerate_title_rejects_fragmentary_kimi_title_and_uses_fallback(monkeypatch):
    session = _FakeSession([
        {"role": "user", "content": "how can i add a drop down to sessions to regenerate the title for a session?"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "I’ll trace the existing session action menu."},
    ])

    handler, calls, events = _post_regenerate_title(monkeypatch, session, fake_title="how can add drop")

    assert handler.status == 200
    payload = handler.json_body()
    assert payload["title"] == "Session title regeneration dropdown"
    assert payload["status"] == "fallback:weak:llm_aux"
    assert len(calls) == 1
    assert session.title == "Session title regeneration dropdown"
    assert session.llm_title_generated is True
    assert session.saved_touch_values == [False]
    assert events == ["session_title_regenerated"]


def test_regenerate_title_uses_sanitized_aux_title(monkeypatch):
    session = _FakeSession([
        {"role": "user", "content": "hey, how can we make a more clear unread tag for a session?"},
        {"role": "assistant", "content": "I’ll implement server-side read state and sidebar unread UI."},
    ])

    handler, calls, events = _post_regenerate_title(
        monkeypatch,
        session,
        fake_title="Session Read State Sync Implementation",
    )

    assert handler.status == 200
    payload = handler.json_body()
    assert payload["title"] == "Session Read State Sync Implementation"
    assert payload["status"] == "llm_aux"
    assert len(calls) == 1
    assert session.title == "Session Read State Sync Implementation"
    assert session.llm_title_generated is True
    assert session.saved_touch_values == [False]
    assert events == ["session_title_regenerated"]


def test_regenerate_title_rejects_weak_fallback_title(monkeypatch):
    session = _FakeSession([
        {
            "role": "user",
            "content": "hey, how can we make a more clear unread tag for a session and sync read state?",
        },
        {"role": "assistant", "content": "I’ll inspect the sidebar read-state path."},
    ])

    handler, calls, events = _post_regenerate_title(monkeypatch, session, fake_title=None)

    assert handler.status == 500
    payload = handler.json_body()
    assert "Could not generate a useful title" in payload["error"]
    assert "weak_fallback" in payload["error"]
    assert len(calls) == 1
    assert session.title == "Old title"
    assert session.llm_title_generated is False
    assert session.saved_touch_values == []
    assert events == []
