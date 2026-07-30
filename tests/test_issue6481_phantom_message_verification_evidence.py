"""Regression tests for #6481 — Phantom assistant message from verification_evidence.

The Hermes Agent stamps terminal tool results that match a known verification
command (pytest, lint, etc.) with a ``verification_evidence`` field in the JSON
result dict. This field is an internal audit artifact for the agent's
verify-on-stop loop and must never be persisted in the WebUI's stored transcript
or fed back to the model on subsequent turns.

The fix adds:
- ``_strip_verification_evidence_from_tool_content`` which strips the
  ``verification_evidence`` key from terminal tool result JSON strings (and
  dict content)
- ``_strip_verification_from_messages`` which applies this to terminal
  tool-role messages in a message list, correlating nameless legacy rows
  through ``tool_call_id``
- ``_install_agent_verification_evidence_sanitizer`` which wraps the Agent's
  ``make_tool_result_message`` at the producer/executor boundary so fresh
  same-turn evidence never reaches the model request or state.db
- Wiring into ``_sanitize_messages_for_api`` (before the model/persistence
  boundary) so old evidence never reaches the model on replay
- Wiring into ``_merge_display_messages_after_agent_result`` as a safety net
  at the persistence boundary
- Owner-level recovery sanitization in ``api.models`` core/state-db sync
"""

import json
import time

import pytest

from api import streaming


# --------------------------------------------------------------------------- #
# _strip_verification_evidence_from_tool_content
# --------------------------------------------------------------------------- #


def test_strips_verification_evidence_from_terminal_tool_result():
    """The ``verification_evidence`` key is removed from a terminal tool result JSON string."""
    content = json.dumps({
        "output": "12 passed, 0 failed",
        "exit_code": 0,
        "error": None,
        "verification_evidence": {
            "status": "passed",
            "kind": "test",
            "scope": "full",
            "canonical_command": "pytest",
        },
    })
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    parsed = json.loads(stripped)
    assert "verification_evidence" not in parsed
    assert parsed["output"] == "12 passed, 0 failed"
    assert parsed["exit_code"] == 0


def test_preserves_content_without_verification_evidence():
    """A JSON string without ``verification_evidence`` is returned unchanged."""
    content = json.dumps({
        "output": "Build succeeded",
        "exit_code": 0,
        "error": None,
    })
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    assert stripped == content


def test_preserves_non_json_content():
    """Non-JSON content (plain text, error messages) passes through unchanged."""
    assert streaming._strip_verification_evidence_from_tool_content("plain text") == "plain text"
    assert streaming._strip_verification_evidence_from_tool_content("") == ""


def test_handles_malformed_json_gracefully():
    """Malformed JSON content is returned as-is without crashing."""
    bad = '{"output": "truncated, no closing brace'
    assert streaming._strip_verification_evidence_from_tool_content(bad) == bad


def test_handles_empty_dict():
    """An empty JSON object is returned as-is."""
    assert streaming._strip_verification_evidence_from_tool_content("{}") == "{}"


def test_missing_verification_evidence_key_in_dict():
    """A non-empty dict without the key is not treated as having evidence."""
    content = json.dumps({"output": "ok", "exit_code": 0})
    assert streaming._strip_verification_evidence_from_tool_content(content) == content


def test_other_keys_preserved_after_removal():
    """All keys besides ``verification_evidence`` survive the strip."""
    content = json.dumps({
        "output": "All tests passed",
        "exit_code": 0,
        "error": None,
        "approval": "Approved by gate",
        "verification_evidence": {"status": "passed"},
    })
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    parsed = json.loads(stripped)
    assert "verification_evidence" not in parsed
    assert parsed["output"] == "All tests passed"
    assert parsed["exit_code"] == 0
    assert parsed["approval"] == "Approved by gate"
    assert parsed["error"] is None


def test_only_strips_verification_evidence_once():
    """Double-calling is safe — no-op on the second pass."""
    content = json.dumps({
        "output": "ok",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    first = streaming._strip_verification_evidence_from_tool_content(content)
    second = streaming._strip_verification_evidence_from_tool_content(first)
    assert first == second


def test_preserves_nested_json_precision():
    """The output JSON is structurally equivalent (keys in same order, same values)."""
    content = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "error": None,
        "verification_evidence": {"status": "passed"},
    })
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    parsed = json.loads(stripped)
    assert list(parsed.keys()) == ["output", "exit_code", "error"]
    assert parsed["output"] == "12 passed"


# --------------------------------------------------------------------------- #
# _strip_verification_from_messages
# --------------------------------------------------------------------------- #


def test_strips_verification_evidence_from_terminal_tool_messages():
    """``_strip_verification_from_messages`` strips verification_evidence from terminal tool-role messages."""
    messages = [
        {"role": "user", "content": "Run tests."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "terminal",
            "content": json.dumps({
                "output": "12 passed",
                "exit_code": 0,
                "verification_evidence": {"status": "passed"},
            }),
        },
        {"role": "assistant", "content": "Tests passed."},
    ]
    streaming._strip_verification_from_messages(messages)
    for msg in messages:
        if msg.get("role") == "tool":
            parsed = json.loads(msg["content"])
            assert "verification_evidence" not in parsed
        else:
            assert "verification_evidence" not in str(msg.get("content", ""))


def test_ignores_non_terminal_tool_messages():
    """Non-terminal tool messages are left completely unchanged."""
    payload = json.dumps({
        "output": "ok",
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": payload,
        },
    ]
    original_content = messages[0]["content"]
    streaming._strip_verification_from_messages(messages)
    assert messages[0]["content"] == original_content


def test_ignores_tool_without_name_field():
    """Tool messages missing both ``name`` and ``tool_name`` are stripped only when ``tool_call_id`` correlates to terminal."""
    payload = json.dumps({
        "output": "ok",
        "verification_evidence": {"status": "passed"},
    })
    # No assistant tool_call with this id anywhere in the list → no correlation,
    # so the row is preserved conservatively (cannot confirm it is terminal).
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": payload,
        },
    ]
    original_content = messages[0]["content"]
    streaming._strip_verification_from_messages(messages)
    assert messages[0]["content"] == original_content


def test_correlates_nameless_terminal_tool_message_via_tool_call_id():
    """A nameless tool row whose ``tool_call_id`` matches a preceding assistant ``terminal`` call is stripped."""
    payload = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {"role": "user", "content": "Run tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": payload,
        },
    ]
    streaming._strip_verification_from_messages(messages)
    for msg in messages:
        if msg.get("role") == "tool":
            parsed = json.loads(msg["content"])
            assert "verification_evidence" not in parsed
            assert parsed["output"] == "12 passed"


def test_preserves_nameless_non_terminal_tool_message_via_tool_call_id():
    """A nameless tool row correlated to a NON-terminal call keeps its payload intact."""
    payload = json.dumps({
        "output": "custom",
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {"role": "user", "content": "Run tool."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": payload,
        },
    ]
    original_content = messages[2]["content"]
    streaming._strip_verification_from_messages(messages)
    assert messages[2]["content"] == original_content


def test_strips_dict_content_from_terminal_tool_message():
    """Fresh in-process terminal results carry dict content — stripped in place."""
    content = {
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    }
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": content},
    ]
    streaming._strip_verification_from_messages(messages)
    assert "verification_evidence" not in messages[0]["content"]
    assert messages[0]["content"]["output"] == "12 passed"
    assert messages[0]["content"]["exit_code"] == 0


def test_strip_content_helper_handles_dict_and_string():
    """The content-level helper accepts both dict and JSON-string forms."""
    d = {"output": "ok", "verification_evidence": {"status": "passed"}}
    cleaned = streaming._strip_verification_evidence_from_tool_content(d)
    assert isinstance(cleaned, dict)
    assert "verification_evidence" not in cleaned
    assert cleaned["output"] == "ok"

    s = json.dumps({"output": "ok", "verification_evidence": {"status": "passed"}})
    cleaned_s = streaming._strip_verification_evidence_from_tool_content(s)
    assert isinstance(cleaned_s, str)
    assert "verification_evidence" not in cleaned_s


def test_correlate_tool_call_name_matches_openai_and_anthropic_shapes():
    """``_correlate_tool_call_name`` resolves both OpenAI ``id`` and Anthropic ``call_id`` forms."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "openai-1", "function": {"name": "terminal", "arguments": "{}"}},
                {"call_id": "anthropic-2", "name": "read_file", "input": {}},
            ],
        },
    ]
    assert streaming._correlate_tool_call_name(messages, {"tool_call_id": "openai-1"}) == "terminal"
    assert streaming._correlate_tool_call_name(messages, {"tool_call_id": "anthropic-2"}) == "read_file"
    assert streaming._correlate_tool_call_name(messages, {"tool_call_id": "missing"}) == ""
    assert streaming._correlate_tool_call_name(messages, {}) == ""


def test_ignores_non_tool_messages():
    """Non-tool messages are left completely unchanged."""
    messages = [
        {"role": "user", "content": "Hello."},
        {"role": "assistant", "content": "Hi there."},
    ]
    original = list(messages)
    streaming._strip_verification_from_messages(messages)
    assert messages == original


def test_handles_empty_message_list():
    """An empty message list is a no-op."""
    messages: list = []
    streaming._strip_verification_from_messages(messages)
    assert messages == []


def test_handles_tool_message_without_verification_evidence():
    """A terminal tool message without ``verification_evidence`` is left unchanged."""
    content = json.dumps({"output": "Build OK", "exit_code": 0})
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": content},
    ]
    streaming._strip_verification_from_messages(messages)
    assert messages[0]["content"] == content


def test_strips_from_all_terminal_tool_messages_in_list():
    """All terminal tool-role messages in the list are processed."""
    payload = json.dumps({
        "output": "ok",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": payload},
        {"role": "tool", "tool_call_id": "call-2", "name": "terminal", "content": payload},
    ]
    streaming._strip_verification_from_messages(messages)
    for msg in messages:
        parsed = json.loads(msg["content"])
        assert "verification_evidence" not in parsed


# --------------------------------------------------------------------------- #
# _sanitize_messages_for_api integration (before model boundary)
# --------------------------------------------------------------------------- #


def test_sanitize_strips_verification_from_terminal_tool_messages():
    """``_sanitize_messages_for_api`` strips ``verification_evidence`` from terminal tool results."""
    tool_payload = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {"role": "user", "content": "Run tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": tool_payload},
    ]
    sanitized = streaming._sanitize_messages_for_api(messages)
    for msg in sanitized:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))


def test_sanitize_preserves_verification_in_non_terminal_tools():
    """``_sanitize_messages_for_api`` preserves ``verification_evidence`` in non-terminal tools."""
    tool_payload = json.dumps({
        "output": "custom result",
        "verification_evidence": {"status": "passed"},
    })
    messages = [
        {"role": "user", "content": "Run custom tool."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "custom_tool", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "custom_tool", "content": tool_payload},
    ]
    sanitized = streaming._sanitize_messages_for_api(messages)
    evidence_found = False
    for msg in sanitized:
        if msg.get("role") == "tool" and "verification_evidence" in str(msg.get("content", "")):
            evidence_found = True
    assert evidence_found, "Non-terminal tool verification_evidence was incorrectly stripped"


# --------------------------------------------------------------------------- #
# End-to-end: merge integration (persistence boundary safety net)
# --------------------------------------------------------------------------- #


def test_merge_strips_verification_from_tool_results():
    """The merge function strips ``verification_evidence`` from terminal tool results in all inputs."""
    tool_payload = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    previous_display = [{"role": "user", "content": "Run tests."}]
    previous_context = [{"role": "user", "content": "Run tests."}]
    result_messages = [
        {"role": "user", "content": "Run tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": tool_payload},
        {"role": "assistant", "content": "Tests passed."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Run tests.",
    )

    for msg in merged:
        if msg.get("role") == "tool":
            parsed = json.loads(msg["content"])
            assert "verification_evidence" not in parsed, f"Found in tool message: {msg}"


# --------------------------------------------------------------------------- #
# Production boundary: Agent tool-result message builder wrapper (#6481)
# --------------------------------------------------------------------------- #


def _agent_tdh():
    """Import the Agent's tool-message builder source module, if available."""
    pytest.importorskip("agent.tool_dispatch_helpers")
    import agent.tool_dispatch_helpers as tdh

    return tdh


def test_install_wrapper_sanitizes_fresh_terminal_tool_message(monkeypatch):
    """The installed wrapper strips fresh ``verification_evidence`` at the Agent boundary.

    This is the same-turn path the re-gate flagged: the Agent's tool executor
    calls ``make_tool_result_message`` with the fresh terminal result dict and
    appends it to the live conversation + flushes state.db before WebUI regains
    control. The wrapper must remove the field from the dict BEFORE the message
    is built, so neither the same-turn provider payload nor the SessionDB row
    ever contains it.
    """
    tdh = _agent_tdh()
    original = tdh.make_tool_result_message
    streaming._AGENT_VERIFICATION_SANITIZER_INSTALLED = False
    try:
        streaming._install_agent_verification_evidence_sanitizer()

        fresh_result = {
            "output": "12 passed, 0 failed",
            "exit_code": 0,
            "error": None,
            "verification_evidence": {
                "status": "passed",
                "kind": "test",
                "scope": "full",
                "canonical_command": "pytest",
            },
        }
        tool_message = tdh.make_tool_result_message(
            "terminal",
            dict(fresh_result),
            "call-boundary-1",
        )
        assert "verification_evidence" not in tool_message["content"]
        assert tool_message["content"]["output"] == "12 passed, 0 failed"
        assert tool_message["content"]["exit_code"] == 0

        # Non-terminal tools keep any legitimate top-level key untouched.
        custom = {"output": "custom", "verification_evidence": {"status": "passed"}}
        non_terminal = tdh.make_tool_result_message("read_file", dict(custom), "call-boundary-2")
        assert non_terminal["content"]["verification_evidence"]["status"] == "passed"
    finally:
        tdh.make_tool_result_message = original
        streaming._AGENT_VERIFICATION_SANITIZER_INSTALLED = False


def test_install_wrapper_is_idempotent(monkeypatch):
    """Re-installing the wrapper does not double-wrap or raise."""
    tdh = _agent_tdh()
    original = tdh.make_tool_result_message
    streaming._AGENT_VERIFICATION_SANITIZER_INSTALLED = False
    try:
        streaming._install_agent_verification_evidence_sanitizer()
        first = tdh.make_tool_result_message
        streaming._install_agent_verification_evidence_sanitizer()
        assert tdh.make_tool_result_message is first
        # The wrapped callable is tagged so re-entry detects it even if the
        # module flag was reset.
        assert getattr(tdh.make_tool_result_message, "_webui_verification_sanitized", False)
    finally:
        tdh.make_tool_result_message = original
        streaming._AGENT_VERIFICATION_SANITIZER_INSTALLED = False


def test_require_ai_agent_class_installs_sanitizer(monkeypatch):
    """The Agent entry chokepoint wires the boundary wrapper (streaming + sync)."""
    import api.agent_runtime as agent_runtime

    installed = []

    def _fake_install():
        installed.append(True)

    # require_ai_agent_class does a lazy from-import inside the function body,
    # so pointing api.streaming's symbol at the fake is sufficient.
    monkeypatch.setattr(streaming, "_install_agent_verification_evidence_sanitizer", _fake_install)

    # Avoid actually importing the real AIAgent — stub `run_agent` in
    # sys.modules so the import inside require_ai_agent_class resolves to a
    # fake class without touching the installed Agent's heavy dependency tree.
    import sys

    class _FakeAIAgent:
        pass

    class _FakeRunAgentModule:
        AIAgent = _FakeAIAgent

    monkeypatch.setitem(sys.modules, "run_agent", _FakeRunAgentModule())
    cls = agent_runtime.require_ai_agent_class()
    assert cls is _FakeAIAgent
    assert installed, "sanitizer installer must be invoked at the Agent entry chokepoint"


# --------------------------------------------------------------------------- #
# Owner-level recovery: core/state-db rows sanitized before re-persist (#6481)
# --------------------------------------------------------------------------- #


def test_apply_core_sync_or_error_marker_sanitizes_core_rows(tmp_path, monkeypatch):
    """Recovery projecting the Agent's core transcript strips phantom evidence."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    core_path = tmp_path / "core.json"
    contaminated = [
        {"role": "user", "content": "Run tests."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "terminal",
            "content": json.dumps({
                "output": "12 passed",
                "exit_code": 0,
                "verification_evidence": {"status": "passed"},
            }),
        },
        {"role": "assistant", "content": "Tests passed."},
    ]
    core_path.write_text(json.dumps({"messages": contaminated, "tool_calls": []}), encoding="utf-8")

    session = models.Session(
        session_id="core6481",
        title="Core recovery",
        messages=[],
        context_messages=[],
        pending_user_message="Run tests.",
        pending_started_at=time.time() - 120,
        active_stream_id="stream-core-6481",
    )
    # The repair path writes the sidecar; use a spy on save to assert the rows
    # that would be persisted are clean.
    saved_payloads = []

    def _spy_save(*args, **kwargs):
        saved_payloads.append([dict(m) for m in session.messages])
        return True

    monkeypatch.setattr(session, "save", _spy_save)
    applied = models._apply_core_sync_or_error_marker(
        session,
        core_path,
        require_stream_dead=False,
    )
    assert applied is True
    for batch in saved_payloads:
        for msg in batch:
            if msg.get("role") == "tool":
                assert "verification_evidence" not in str(msg.get("content", ""))
    if session.messages:
        for msg in session.messages:
            if msg.get("role") == "tool":
                assert "verification_evidence" not in str(msg.get("content", ""))
    models.SESSIONS.clear()


def test_sync_sidecar_from_state_db_sanitizes_merged_rows(monkeypatch, tmp_path):
    """Read-side self-heal from state.db strips phantom evidence before persisting."""
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    sid = "state6481"
    stream_id = "stream-state-6481"
    s = models.Session(
        session_id=sid,
        title="State newer",
        messages=[
            {"role": "user", "content": "old question", "timestamp": 100.0},
            {"role": "assistant", "content": "old answer", "timestamp": 101.0},
        ],
        context_messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ],
        active_stream_id=stream_id,
        pending_user_message="new request",
        pending_started_at=time.time() - 120,
    )
    s.save()
    models.SESSIONS.pop(sid, None)

    contaminated_tool = {
        "role": "tool",
        "tool_call_id": "call-9",
        "name": "terminal",
        "content": json.dumps({
            "output": "12 passed",
            "exit_code": 0,
            "verification_evidence": {"status": "passed"},
        }),
        "timestamp": 104.0,
    }
    state_messages = [
        {"role": "user", "content": "old question", "timestamp": 100.0},
        {"role": "assistant", "content": "old answer", "timestamp": 101.0},
        {"role": "user", "content": "new request", "timestamp": 102.0},
        {"role": "assistant", "content": "visible text", "timestamp": 103.0},
        contaminated_tool,
        {"role": "assistant", "content": "latest live progress", "timestamp": 105.0},
    ]
    monkeypatch.setattr(
        models,
        "get_state_db_session_summary",
        lambda sid_arg, profile=None: {"message_count": len(state_messages), "last_message_at": 105.0},
    )
    monkeypatch.setattr(
        models,
        "get_state_db_session_messages",
        lambda sid_arg, **kwargs: [dict(m) for m in state_messages],
    )

    # _active_stream_ids is consulted while holding the per-session lock; the
    # stream must appear dead for the heal to proceed.
    monkeypatch.setattr(models, "_active_stream_ids", lambda: set())
    # The under-lock reload must return a fresh Session carrying the same state.
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda cls, sid_arg: None if sid_arg != sid else s),
    )

    loaded = models.get_session(sid)
    for msg in loaded.messages:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))
    for msg in loaded.context_messages or []:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))

    reloaded = models.Session.load(sid)
    assert reloaded is not None
    for msg in reloaded.messages:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))
    models.SESSIONS.clear()
