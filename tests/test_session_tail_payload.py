from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


class _FakeSession:
    def __init__(self, messages):
        self.session_id = "tail_payload_001"
        self.title = "Tail payload"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = messages
        self.tool_calls = [
            {"name": "old-tool", "snippet": "historical snippet", "assistant_msg_idx": 0}
        ]
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = 0
        self.context_length = 1
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.composer_draft = {}

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "model_provider": self.model_provider,
            "message_count": len(self.messages),
            "context_length": self.context_length,
            "threshold_tokens": self.threshold_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "active_stream_id": self.active_stream_id,
            "pending_user_message": self.pending_user_message,
            "composer_draft": self.composer_draft,
        }


def _invoke(session):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    parsed = urlparse("/api/session?session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=1")
    with patch("api.routes.get_session", return_value=session), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured["data"]["session"]


def test_tail_window_omits_historical_tool_calls_when_messages_have_tool_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {
            "role": "assistant",
            "content": "visible",
            "tool_calls": [{"id": "call_1", "function": {"name": "tool", "arguments": "{}"}}],
        },
    ])

    payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["tool_calls"] == []
    assert payload["_messages_truncated"] is True


def test_tail_window_keeps_session_tool_calls_for_legacy_messages_without_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible legacy message"},
    ])

    payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["tool_calls"] == session.tool_calls
