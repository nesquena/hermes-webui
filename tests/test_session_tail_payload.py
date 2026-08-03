from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


def test_tail_projection_rebases_session_tool_calls_after_filtered_row():
    from api.models import Session
    from api.transcript_mutations import project_transcript, record_dismissal, admit_generated_provider_error

    session = Session(
        session_id="tail-projection-6610",
        source_tag="webui",
        raw_source="webui",
        session_source="webui",
        messages=[],
        tool_calls=[{"name": "tail", "assistant_msg_idx": 2}],
    )
    error = {"role": "assistant", "content": "dismissed", "_error": True}
    admit_generated_provider_error(error, session)
    rows = [{"role": "user", "content": "first"}, error, {"role": "assistant", "content": "tail"}]
    record_dismissal(session, session.session_id, error)
    projection = project_transcript(session, rows)
    assert [row["content"] for row in projection.messages] == ["first", "tail"]
    assert projection.tool_calls[0]["assistant_msg_idx"] == 1


def test_lineage_merge_maps_tool_calls_by_message_key_before_projection():
    from api.models import Session
    from api.routes import _session_message_merge_key
    from api.transcript_mutations import admit_generated_provider_error, project_transcript, record_dismissal

    session = Session(
        session_id="lineage-projection-6610",
        source_tag="webui",
        raw_source="webui",
        session_source="webui",
        messages=[],
        tool_calls=[{"name": "later", "assistant_msg_idx": 2}],
    )
    error = {"id": "dismissed", "role": "assistant", "content": "dismissed", "_error": True}
    admit_generated_provider_error(error, session)
    source_messages = [
        {"id": "first", "role": "user", "content": "first"},
        error,
        {"id": "later", "role": "assistant", "content": "later"},
    ]
    merged_messages = [
        {"id": "parent-only", "role": "assistant", "content": "compressed parent"},
        *source_messages,
    ]
    record_dismissal(session, session.session_id, error)
    projection = project_transcript(
        session,
        merged_messages,
        source_messages=source_messages,
        merge_key=_session_message_merge_key,
    )
    assert [row["id"] for row in projection.messages] == ["parent-only", "first", "later"]
    assert projection.tool_calls[0]["assistant_msg_idx"] == 2


class _FakeSession:
    def __init__(self, messages):
        self.session_id = "tail_payload_001"
        self.title = "Tail payload"
        self.workspace = "/tmp"
        self.model = "gpt-test"
        self.model_provider = None
        self.messages = messages
        self.tool_calls = [
            {"name": "old-tool", "snippet": "historical snippet", "assistant_msg_idx": 0},
            {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 1},
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


def _invoke(session, query=None):
    import api.routes as routes

    captured = {}

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return data

    if query is None:
        query = "session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=1"
    parsed = urlparse(f"/api/session?{query}")
    with patch("api.routes.get_session", return_value=session), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes.get_state_db_session_messages", return_value=[]), \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        routes.handle_get(SimpleNamespace(), parsed)
    return captured["data"]["session"]


def test_tail_window_includes_windowed_session_tool_calls_even_when_messages_have_tool_metadata():
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
    # PR #3665: always return session-level tool_calls (windowed to the
    # message window) so the browser can merge them with per-message ones.
    assert payload["tool_calls"] == [
        {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 0}
    ]
    assert payload["_messages_truncated"] is True


def test_tail_window_keeps_only_visible_session_tool_calls_for_legacy_messages_without_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible legacy message"},
    ])

    payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["tool_calls"] == [
        {"name": "visible-tool", "snippet": "visible snippet", "assistant_msg_idx": 0}
    ]
    assert session.tool_calls[-1]["assistant_msg_idx"] == 1


def test_full_load_keeps_all_session_tool_calls_for_legacy_messages_without_metadata():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible legacy message"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0",
    )

    assert payload["messages"] == session.messages
    assert payload["tool_calls"] == session.tool_calls


def test_msg_before_window_keeps_only_that_page_session_tool_calls():
    session = _FakeSession([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second legacy message"},
        {"role": "assistant", "content": "third legacy message"},
        {"role": "assistant", "content": "fourth legacy message"},
    ])
    session.tool_calls = [
        {"name": "first-page-tool", "snippet": "kept", "assistant_msg_idx": 1},
        {"name": "second-page-tool", "snippet": "also kept", "assistant_msg_idx": 2},
        {"name": "tail-tool", "snippet": "not in page", "assistant_msg_idx": 3},
        {"name": "unindexed-tool", "snippet": "cannot place"},
    ]

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_before=3&msg_limit=2",
    )

    assert payload["messages"] == session.messages[1:3]
    assert payload["tool_calls"] == [
        {"name": "first-page-tool", "snippet": "kept", "assistant_msg_idx": 0},
        {"name": "second-page-tool", "snippet": "also kept", "assistant_msg_idx": 1},
    ]
    assert session.tool_calls[0]["assistant_msg_idx"] == 1
    assert session.tool_calls[1]["assistant_msg_idx"] == 2
    assert payload["_messages_offset"] == 1


def test_msg_limit_tail_does_not_run_heavy_webui_lineage_merge():
    session = _FakeSession([
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "visible"},
    ])
    session.parent_session_id = "parent"
    session.session_source = "webui"

    with patch(
        "api.routes._merged_webui_lineage_messages_for_display",
        side_effect=AssertionError("limited loads must not merge parent sidecars"),
    ), patch(
        "api.routes.Session.load",
        return_value=None,
    ):
        payload = _invoke(session)

    assert payload["messages"] == [session.messages[-1]]
    assert payload["message_count"] == 2
    assert payload["_messages_truncated"] is True


def test_msg_limit_tail_keeps_pre_compression_snapshot_parent_reachable():
    parent = _FakeSession([
        {"role": "user", "content": "archived question", "timestamp": 1.0},
        {"role": "assistant", "content": "archived answer", "timestamp": 2.0},
    ])
    parent.session_id = "snapshot_parent"
    parent.pre_compression_snapshot = True

    child = _FakeSession([
        {"role": "user", "content": "continuation question", "timestamp": 3.0},
        {"role": "assistant", "content": "continuation answer", "timestamp": 4.0},
    ])
    child.parent_session_id = "snapshot_parent"
    child.pre_compression_snapshot = False
    child.session_source = "webui"

    with patch("api.routes.Session.load", return_value=parent):
        payload = _invoke(
            child,
            query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=30",
        )

    assert [m["content"] for m in payload["messages"]] == [
        "archived question",
        "archived answer",
        "continuation question",
        "continuation answer",
    ]
    assert payload["_messages_offset"] == 0
    assert payload["_messages_truncated"] is False


def test_msg_limit_tail_truncates_large_hidden_tool_results():
    huge_tool_output = "x" * 20_000
    session = _FakeSession([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "vision", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": huge_tool_output,
        },
        {"role": "assistant", "content": "done"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2",
    )

    tool_msg = payload["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["_content_truncated"] is True
    assert tool_msg["_content_original_chars"] == len(huge_tool_output)
    assert len(tool_msg["content"]) < len(huge_tool_output)
    assert "Tool output truncated" in tool_msg["content"]


def test_msg_limit_tail_does_not_signal_truncated_for_trailing_hidden_tool_rows():
    session = _FakeSession([
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ] + [
        {"role": "tool", "content": f"hidden tool row {idx}"}
        for idx in range(40)
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=30",
    )

    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert payload["_messages_offset"] == 0
    assert payload["_messages_truncated"] is False


def test_msg_limit_tail_preserves_list_tool_content_type_when_truncated():
    large_list_content = [{"type": "text", "text": "x" * 20_000}]
    session = _FakeSession([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "vision", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": large_list_content,
        },
        {"role": "assistant", "content": "done"},
    ])

    payload = _invoke(
        session,
        query="session_id=tail_payload_001&messages=1&resolve_model=0&msg_limit=2",
    )

    tool_msg = payload["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["_content_truncated"] is True
    assert isinstance(tool_msg["content"], list)
    assert not isinstance(tool_msg["content"], str)
    assert tool_msg["content"][0]["type"] == "text"
    assert "Tool output truncated" in tool_msg["content"][0]["text"]
