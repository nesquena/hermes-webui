from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


class _FakeSession:
    def __init__(self, *, is_cli_session=False, session_source=None, source_tag=None):
        self.session_id = "native_webui_001"
        self.title = "Native WebUI"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = []
        self.tool_calls = []
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
        self.is_cli_session = is_cli_session
        self.session_source = session_source
        self.source_tag = source_tag
        self.raw_source = source_tag
        self.source_label = source_tag

    def compact(self):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "model_provider": self.model_provider,
            "message_count": 0,
            "context_length": self.context_length,
            "threshold_tokens": self.threshold_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "active_stream_id": self.active_stream_id,
            "pending_user_message": self.pending_user_message,
            "composer_draft": self.composer_draft,
            "is_cli_session": self.is_cli_session,
            "session_source": self.session_source,
            "source_tag": self.source_tag,
            "raw_source": self.raw_source,
            "source_label": self.source_label,
        }


def _invoke_api_session(session_obj, *, lookup_cli):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    parsed = urlparse("/api/session?session_id=native_webui_001&messages=0&resolve_model=0")
    with patch("api.routes.get_session", return_value=session_obj), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", side_effect=lookup_cli) as lookup, \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured, lookup


def test_api_session_metadata_skips_cli_lookup_for_native_webui_session():
    """Native WebUI sessions should not scan Agent state.db on every metadata load."""
    session = _FakeSession()

    def fail_lookup(_sid):
        raise AssertionError("native WebUI metadata should not query CLI sessions")

    captured, lookup = _invoke_api_session(session, lookup_cli=fail_lookup)

    assert captured["status"] == 200
    assert captured["data"]["session"]["session_id"] == "native_webui_001"
    lookup.assert_not_called()


def test_api_session_metadata_keeps_cli_lookup_for_imported_cli_session():
    """Imported CLI/messaging sessions still need Agent metadata for overlap handling."""
    session = _FakeSession(is_cli_session=True, session_source="messaging", source_tag="telegram")

    captured, lookup = _invoke_api_session(
        session,
        lookup_cli=lambda sid: {
            "session_id": sid,
            "session_source": "messaging",
            "source_tag": "telegram",
            "raw_source": "telegram",
            "source_label": "Telegram",
        },
    )

    assert captured["status"] == 200
    assert captured["data"]["session"]["source_tag"] == "telegram"
    lookup.assert_called_once_with("native_webui_001")
