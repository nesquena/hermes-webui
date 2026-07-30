"""Tests for #4479: gateway approval URL contract and conversation history threading."""
from __future__ import annotations

import types


# ---------------------------------------------------------------------------
# Bug 1: respond_approval URL and payload shape
# ---------------------------------------------------------------------------

def test_respond_approval_url_matches_gateway_contract():
    from api.runner_client import HttpRunnerClient

    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval("run-abc", "appr-xyz", "approve")

    assert captured["url"] == "/v1/runs/run-abc/approval"
    assert "/approvals/" not in captured["url"]
    assert "/respond" not in captured["url"]


def test_respond_approval_body_includes_approval_id():
    from api.runner_client import HttpRunnerClient

    client = HttpRunnerClient(base_url="http://gw:8642")
    captured = {}

    def fake_post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {}

    client._post = fake_post
    client.respond_approval("run-1", "appr-2", "deny")

    assert captured["body"] == {"choice": "deny", "approval_id": "appr-2"}


# ---------------------------------------------------------------------------
# Bug 2: conversation history threading via session parameter
# ---------------------------------------------------------------------------

def _build_conversation_history(context_messages, prefill_messages):
    """Exercise the same logic as _run_gateway_runs_api_streaming's conversation builder."""
    session = types.SimpleNamespace(context_messages=context_messages)
    instructions_parts = []
    conversation_history = []
    for entry in getattr(session, "context_messages", None) or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        if content is not None:
            conversation_history.append({"role": role, "content": content})
    for entry in prefill_messages or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        content = entry.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content)
            elif content is not None:
                instructions_parts.append(str(content))
            continue
        if role not in {"user", "assistant"}:
            continue
        conversation_history.append({"role": role, "content": content})
    return conversation_history, instructions_parts


def test_runs_api_threads_session_context_messages():
    context = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    prefill = [{"role": "system", "content": "You are helpful."}]

    history, instructions = _build_conversation_history(context, prefill)

    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "hello"}
    assert history[1] == {"role": "assistant", "content": "hi there"}
    assert instructions == ["You are helpful."]


def test_runs_api_filters_system_from_context_messages():
    context = [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "injected system"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "tool output"},
    ]
    prefill = []

    history, _ = _build_conversation_history(context, prefill)

    roles = [m["role"] for m in history]
    assert "system" not in roles
    assert "tool" not in roles
    assert roles == ["user", "assistant"]


def test_runs_api_empty_context_messages():
    history, _ = _build_conversation_history(None, [{"role": "system", "content": "sys"}])
    assert history == []

    history2, _ = _build_conversation_history([], [{"role": "user", "content": "q"}])
    assert history2 == [{"role": "user", "content": "q"}]
