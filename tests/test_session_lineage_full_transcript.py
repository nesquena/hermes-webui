"""Regression coverage for stitched full-transcript loading across session segments."""

from __future__ import annotations

import api.routes as routes



def test_session_endpoint_merges_sidecar_and_lineage_messages_for_cli_sessions(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [{"role": "assistant", "content": "sidecar tail", "timestamp": 10.0}]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {"role": "assistant", "content": "tip assistant", "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    assert [m["content"] for m in session["messages"]] == [
        "root user",
        "tip assistant",
        "sidecar tail",
    ]


def test_session_endpoint_preserves_distinct_messages_with_different_ids(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [
                {
                    "id": "sidecar-retry",
                    "role": "user",
                    "content": "retry the same request",
                    "timestamp": 2.0,
                }
            ]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {
                "id": "cli-retry",
                "role": "user",
                "content": "retry the same request",
                "timestamp": 2.0,
            },
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    retry_messages = [m for m in session["messages"] if m.get("content") == "retry the same request"]
    assert [m.get("id") for m in retry_messages] == ["cli-retry", "sidecar-retry"]
