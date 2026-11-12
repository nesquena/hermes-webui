"""Regression coverage for the two maintainer fixes folded into PR #6757.

1. redact_session_data must NOT credential-mask operational session fields such
   as the workspace path — a valid path can legitimately contain a
   credential-shaped component, and masking it corrupts the authoritative value
   the client echoes back on the next send (which then fails validation).
2. Runner-backed SSE payloads must be projected before relay so an internal
   `api_content` replay sidecar never reaches the browser.
"""
from __future__ import annotations


def test_redact_session_data_preserves_credential_shaped_workspace_path():
    from api.helpers import redact_session_data

    # A real workspace path whose leaf looks like a secret token.
    workdir = "/home/user/keys/sk-abcdefghijklmnopqrstuvwxyz012345/project"
    session = {
        "id": "sess-1",
        "workspace": workdir,
        "workdir": workdir,
        "cwd": workdir,
        "messages": [{"role": "user", "content": "hello"}],
    }
    out = redact_session_data(session)
    # Operational path fields survive verbatim (not masked).
    assert out["workspace"] == workdir
    assert out["workdir"] == workdir
    assert out["cwd"] == workdir
    # And the internal replay/id aliases are still stripped from messages.
    assert "api_content" not in out["messages"][0]


def test_redact_session_data_still_strips_api_content_from_messages():
    from api.helpers import redact_session_data

    session = {
        "id": "sess-2",
        "workspace": "/home/user/project",
        "messages": [
            {
                "role": "assistant",
                "content": "clean transcript text",
                "api_content": "PROVIDER REPLAY BYTES — must never leak",
                "_state_db_row_id": 42,
                "_db_row_id": 42,
                "state_db_row_id": 42,
            }
        ],
        "context_messages": [
            {"role": "user", "content": "ctx", "api_content": "ctx replay bytes"}
        ],
    }
    out = redact_session_data(session)
    msg = out["messages"][0]
    assert "api_content" not in msg
    assert "_state_db_row_id" not in msg
    assert "_db_row_id" not in msg
    assert "state_db_row_id" not in msg
    assert msg["content"] == "clean transcript text"
    # context_messages sidecars are stripped too.
    assert "api_content" not in out["context_messages"][0]


def test_project_runner_event_payload_strips_api_content_from_session():
    from api.routes import _project_runner_event_payload

    payload = {
        "status": "done",
        "session": {
            "id": "sess-3",
            "messages": [
                {
                    "role": "assistant",
                    "content": "visible",
                    "api_content": "REPLAY BYTES — must not reach browser",
                }
            ],
        },
    }
    out = _project_runner_event_payload(payload)
    assert "api_content" not in out["session"]["messages"][0]
    assert out["session"]["messages"][0]["content"] == "visible"
    # Non-transcript fields on the envelope are preserved.
    assert out["status"] == "done"


def test_project_runner_event_payload_strips_api_content_from_message_shaped():
    from api.routes import _project_runner_event_payload

    payload = {
        "id": "sess-4",
        "messages": [
            {"role": "assistant", "content": "hi", "api_content": "REPLAY — leak?"}
        ],
    }
    out = _project_runner_event_payload(payload)
    assert "api_content" not in out["messages"][0]


def test_project_runner_event_payload_passes_non_session_through():
    from api.routes import _project_runner_event_payload

    # A token/delta event with no transcript must pass through untouched.
    payload = {"type": "token", "delta": "hello", "seq": 7}
    assert _project_runner_event_payload(payload) == payload
    # Non-dict payloads pass through unchanged.
    assert _project_runner_event_payload("raw") == "raw"
    assert _project_runner_event_payload(None) is None
