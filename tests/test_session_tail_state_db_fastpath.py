from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch
from urllib.parse import urlparse


class _MetadataSession:
    def __init__(self):
        self.session_id = "tail_state_db_fastpath"
        self.title = "State DB tail fastpath"
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
        self.parent_session_id: Optional[str] = None
        self.pre_compression_snapshot = False
        self.truncation_watermark = None
        self._metadata_message_count = 4
        self._metadata_tool_call_count: Optional[int] = 0
        self._loaded_metadata_only = True

    def compact(self, include_runtime=False, active_stream_ids=None):
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "model_provider": self.model_provider,
            "message_count": self._metadata_message_count,
            "context_length": self.context_length,
            "threshold_tokens": self.threshold_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "active_stream_id": self.active_stream_id,
            "pending_user_message": self.pending_user_message,
            "composer_draft": self.composer_draft,
        }


def test_session_tail_load_uses_state_db_without_full_sidecar_hydration():
    """A tail-window load should not hydrate a large sidecar when state.db is complete."""
    import api.routes as routes

    session = _MetadataSession()
    state_messages = [
        {"role": "user", "content": "old", "timestamp": 1},
        {"role": "assistant", "content": "old answer", "timestamp": 2},
        {"role": "user", "content": "latest", "timestamp": 3},
        {"role": "assistant", "content": "latest answer", "timestamp": 4},
    ]
    captured = {}
    get_session_calls = []

    def fake_get_session(sid, metadata_only=False):
        get_session_calls.append(metadata_only)
        if metadata_only is False:
            raise AssertionError("tail fastpath must not full-load the sidecar")
        return session

    def fake_j(_handler, data, status=200, extra_headers=None):
        captured["status"] = status
        captured["data"] = data
        return data

    parsed = urlparse(
        "/api/session?session_id=tail_state_db_fastpath&messages=1&resolve_model=0&msg_limit=2&expand_renderable=1"
    )
    with patch("api.routes.get_session", side_effect=fake_get_session), \
         patch("api.routes.get_state_db_session_messages", return_value=state_messages), \
         patch("api.routes._clear_stale_stream_state", return_value=False), \
         patch("api.routes._lookup_cli_session_metadata", return_value={}), \
         patch("api.routes._webui_sidecar_lineage_messages_for_display") as sidecar_messages, \
         patch("api.routes._merged_webui_lineage_messages_for_display") as lineage_merge, \
         patch("api.routes.redact_session_data", side_effect=lambda raw: raw), \
         patch("api.routes.j", side_effect=fake_j):
        sidecar_messages.side_effect = AssertionError("sidecar transcript should not be read")
        lineage_merge.side_effect = AssertionError("lineage merge should stay on the full-load path")
        routes.handle_get(SimpleNamespace(), parsed)

    assert get_session_calls == [True]
    payload = captured["data"]["session"]
    assert captured["status"] == 200
    assert payload["messages"] == state_messages[-2:]
    assert payload["message_count"] == 4
    assert payload["_messages_truncated"] is True
    assert payload["_messages_offset"] == 2


def test_session_tail_fastpath_falls_back_for_incomplete_non_lineage_sidecars():
    """Ordinary sessions still full-load when state.db does not cover the sidecar."""
    from api.routes import _state_db_tail_fastpath_eligible

    session = _MetadataSession()
    session._metadata_message_count = 100

    assert not _state_db_tail_fastpath_eligible(
        session,
        [{"role": "user", "content": "x"}],
        msg_limit=2,
        msg_before=None,
    )


def test_session_tail_fastpath_falls_back_for_lineage_children():
    """Lineage children need full hydration so parent history and counts stay reachable."""
    from api.routes import _state_db_tail_fastpath_eligible

    session = _MetadataSession()
    session.parent_session_id = "parent-tail-history"

    assert not _state_db_tail_fastpath_eligible(
        session,
        [{"role": "user", "content": str(i), "timestamp": i} for i in range(100)],
        msg_limit=30,
        msg_before=None,
    )


def test_session_tail_fastpath_requires_known_empty_legacy_tool_calls():
    """Metadata-only loads cannot preserve session-level JSON tool cards unless count proves none exist."""
    from api.routes import _state_db_tail_fastpath_eligible

    session = _MetadataSession()
    session._metadata_tool_call_count = None
    assert not _state_db_tail_fastpath_eligible(
        session,
        [{"role": "user", "content": str(i), "timestamp": i} for i in range(100)],
        msg_limit=30,
        msg_before=None,
    )

    session._metadata_tool_call_count = 1
    assert not _state_db_tail_fastpath_eligible(
        session,
        [{"role": "user", "content": str(i), "timestamp": i} for i in range(100)],
        msg_limit=30,
        msg_before=None,
    )
