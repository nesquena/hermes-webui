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
    import api.verification_sanitizer as verification_sanitizer

    installed = []

    def _fake_install():
        installed.append(True)
        return True

    # require_ai_agent_class does a lazy from-import inside the function body,
    # so pointing api.verification_sanitizer's symbol at the fake is sufficient
    # (the installer moved out of api.streaming into the cycle-safe module so
    # the cold-start path at streaming.py:631 can reach it — #6481 re-gate).
    monkeypatch.setattr(
        verification_sanitizer,
        "_install_agent_verification_evidence_sanitizer",
        _fake_install,
    )

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


# --------------------------------------------------------------------------- #
# Gap 4: _correlate_tool_call_name fails CLOSED on ambiguous identity (#6481)
# --------------------------------------------------------------------------- #


def _assistant_with_tool_call(call_id, name, *, position=0):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": "{}"}}],
    }


def test_correlate_tool_call_name_returns_preceding_terminal_name():
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]
    assert streaming._correlate_tool_call_name(messages, messages[1], tool_index=1) == "terminal"


def test_correlate_tool_call_name_fails_closed_on_future_match():
    """An id whose only assistant match is AFTER the tool row must not correlate.

    A recovered/duplicate id that appears later in the transcript cannot prove
    this row is the result of a preceding call — fail closed to avoid stripping
    legitimate data from a non-terminal row.
    """
    messages = [
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        _assistant_with_tool_call("call-1", "terminal"),
    ]
    assert streaming._correlate_tool_call_name(messages, messages[0], tool_index=0) == ""


def test_correlate_tool_call_name_fails_closed_when_id_reused_in_future():
    """Match in BOTH a preceding AND a future assistant call → ambiguous → ""."""
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        _assistant_with_tool_call("call-1", "read_file"),
    ]
    assert streaming._correlate_tool_call_name(messages, messages[1], tool_index=1) == ""


def test_correlate_tool_call_name_fails_closed_on_duplicate_preceding_ids():
    """Same id in two PRECEDING assistant calls → ambiguous → ""."""
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        _assistant_with_tool_call("call-1", "terminal"),
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]
    assert streaming._correlate_tool_call_name(messages, messages[2], tool_index=2) == ""


def test_correlate_tool_call_name_fails_closed_on_conflicting_names():
    """Same id correlated to DIFFERENT function names → ambiguous → ""."""
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        _assistant_with_tool_call("call-1", "read_file"),
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]
    assert streaming._correlate_tool_call_name(messages, messages[2], tool_index=2) == ""


def test_correlate_tool_call_name_fails_closed_on_empty_id():
    assert streaming._correlate_tool_call_name([{"role": "tool", "content": "x"}], {"role": "tool", "content": "x"}) == ""
    assert streaming._correlate_tool_call_name([], {"role": "tool", "tool_call_id": ""}) == ""


def test_correlate_tool_call_name_fails_closed_on_missing_match():
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        {"role": "tool", "tool_call_id": "call-9", "content": "ok"},
    ]
    assert streaming._correlate_tool_call_name(messages, messages[1], tool_index=1) == ""


def test_correlate_tool_call_name_bounds_by_explicit_tool_index():
    """Passing tool_index bounds the scan: a same-position match is a future match."""
    messages = [
        _assistant_with_tool_call("call-1", "terminal"),
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
    ]
    # tool_index=0 means the "tool row" is BEFORE the assistant call — the only
    # match is at idx 1 >= 0, so it is a future match and must fail closed.
    assert streaming._correlate_tool_call_name(messages, messages[1], tool_index=0) == ""


# --------------------------------------------------------------------------- #
# Production gap: state.db missing-sidecar recovery sanitizes full list (#6481)
# --------------------------------------------------------------------------- #


def _recovery_state_db(path, *, sid="recovered_6481"):
    """Build a temp state.db with a contaminated terminal row + identity columns."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, "
        "started_at REAL, message_count INTEGER, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, timestamp REAL, tool_call_id TEXT, tool_name TEXT, tool_calls TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Recovered", "openai/gpt-5", 2000.0, 3, None),
    )
    contaminated = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    rows = [
        # Preceding assistant terminal tool_call (identity preserved by reader).
        (sid, "assistant", "", 2000.0, None, None, json.dumps(
            [{"id": "call-rec-1", "function": {"name": "terminal", "arguments": "{}"}}]
        )),
        # NAMELESS legacy terminal row — must be correlated + stripped.
        (sid, "tool", contaminated, 2001.0, "call-rec-1", None, None),
        # Named non-terminal row with a top-level evidence-like key — must NOT strip.
        (sid, "tool", json.dumps({"output": "custom", "verification_evidence": {"status": "passed"}}),
         2002.0, "call-rec-2", "read_file", None),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_name, tool_calls) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return sid


def test_recover_missing_sidecars_from_state_db_strips_verification_evidence(tmp_path):
    """Recovered sidecars never persist the phantom field; nameless legacy terminal
    rows are correlated through tool_call_id and stripped, non-terminal rows kept."""
    from api.session_recovery import recover_missing_sidecars_from_state_db

    sid = _recovery_state_db(tmp_path / "state.db")
    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
    assert result["materialized"] == 1

    sidecar = tmp_path / f"{sid}.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    tool_msgs = [m for m in data["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    nameless = [m for m in tool_msgs if m.get("name") != "read_file" and not m.get("tool_name")]
    named_non_terminal = [m for m in tool_msgs if m.get("name") == "read_file"]
    assert len(nameless) == 1
    assert len(named_non_terminal) == 1
    # The nameless legacy terminal row was stripped of the phantom field.
    assert "verification_evidence" not in nameless[0]["content"]
    # The named non-terminal row keeps its payload untouched.
    assert "verification_evidence" in named_non_terminal[0]["content"]


def test_state_db_row_to_sidecar_sanitizes_full_list(tmp_path):
    """Direct unit: _state_db_row_to_sidecar strips the phantom field from the
    full recovered message list before the sidecar payload is built."""
    from api.session_recovery import _state_db_row_to_sidecar

    contaminated = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    row = {
        "id": "direct-6481",
        "source": "webui",
        "title": "Direct",
        "started_at": 3000.0,
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-d-1", "function": {"name": "terminal", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-d-1", "content": contaminated},
        ],
    }
    payload = _state_db_row_to_sidecar(row)
    for msg in payload["messages"]:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))


# --------------------------------------------------------------------------- #
# Production gap: display projection strips verification_evidence (#6481)
# --------------------------------------------------------------------------- #


def _limited_display_fixture(contaminated_tail=True):
    import types

    tool_payload = json.dumps({
        "output": "12 passed",
        "exit_code": 0,
        "verification_evidence": {"status": "passed"},
    })
    sidecar_messages = [
        {"role": "user", "content": "old question", "timestamp": 100.0},
        {"role": "assistant", "content": "old answer", "timestamp": 101.0},
    ]
    state_db_messages = [
        {"role": "user", "content": "new request", "timestamp": 102.0},
        {"role": "assistant", "content": "visible text", "timestamp": 103.0},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "terminal",
            "content": tool_payload,
            "timestamp": 104.0,
        },
        {"role": "assistant", "content": "latest live progress", "timestamp": 105.0},
    ]
    if not contaminated_tail:
        state_db_messages[-1]["content"] = "clean"
    session = types.SimpleNamespace(
        session_id="limited-6481",
        session_source="webui",
        messages=list(sidecar_messages),
        parent_session_id=None,
        truncation_watermark=None,
        truncation_boundary=None,
    )
    return session, sidecar_messages, state_db_messages


def test_limited_webui_messages_for_display_strips_verification_evidence():
    from api.routes import _limited_webui_messages_for_display_with_sidecar

    session, sidecar_messages, state_db_messages = _limited_display_fixture()
    displayed = _limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar_messages,
        state_db_messages,
    )
    for msg in displayed:
        if msg.get("role") == "tool":
            assert "verification_evidence" not in str(msg.get("content", ""))


def test_limited_webui_messages_for_display_does_not_mutate_session_messages():
    """The display projection is a read path: sanitizing the output must not
    mutate the session's in-memory transcript or the caller's state.db dicts."""
    from api.routes import _limited_webui_messages_for_display_with_sidecar

    session, sidecar_messages, state_db_messages = _limited_display_fixture()
    displayed = _limited_webui_messages_for_display_with_sidecar(
        session,
        sidecar_messages,
        state_db_messages,
    )
    # The output is clean…
    assert all("verification_evidence" not in str(m.get("content", "")) for m in displayed)
    # …but the caller's dicts are untouched (shallow-copied projection).
    contaminated_input = [m for m in state_db_messages if m.get("role") == "tool"]
    assert contaminated_input
    assert "verification_evidence" in str(contaminated_input[0].get("content", ""))
    assert all("verification_evidence" not in str(m.get("content", "")) for m in session.messages)
    assert all(id(m) not in {id(s) for s in displayed} for m in session.messages)


# --------------------------------------------------------------------------- #
# Production gap: cold-start subprocess installs the sanitizer (#6481 re-gate)
# --------------------------------------------------------------------------- #


def test_cold_start_subprocess_installs_verification_sanitizer(tmp_path):
    """A REAL cold import of api.streaming must install the producer-boundary
    wrapper on BOTH the helper and the executor alias.

    This is the exact re-gate failure: on master the installer lived in
    api.streaming, and streaming.py:631 calls get_ai_agent_class() while the
    module is still partially initialized, so the installer was unreachable and
    _webui_verification_sanitized stayed False on a normal server boot. The
    cycle-safe api.verification_sanitizer module makes the cold-start install
    work; this test proves it in a fresh subprocess.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "import api.streaming\n"
        "import agent.tool_dispatch_helpers as tdh\n"
        "import agent.tool_executor as te\n"
        "ok = (\n"
        "    getattr(tdh.make_tool_result_message, '_webui_verification_sanitized', False)\n"
        "    and getattr(te.make_tool_result_message, '_webui_verification_sanitized', False)\n"
        "    and te.make_tool_result_message is tdh.make_tool_result_message\n"
        ")\n"
        "print('SANITIZER_INSTALLED' if ok else 'SANITIZER_MISSING')\n"
        "sys.exit(0 if ok else 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, f"cold-start install failed:\n{result.stdout}\n{result.stderr}"
    assert "SANITIZER_INSTALLED" in result.stdout


def test_real_executor_alias_flushes_clean_terminal_message(tmp_path):
    """The REAL agent.tool_executor alias must produce a message free of the
    phantom field and flush clean rows into a real temp SessionDB.

    The executor calls its module-level make_tool_result_message reference
    (imported via ``from agent.tool_dispatch_helpers import ...``), so patching
    only the helper is not enough — the alias must be wrapped too. This test
    installs the sanitizer, builds a fresh contaminated terminal result through
    the executor's own reference, and persists it to a real SQLite SessionDB.
    """
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import agent.tool_executor as te
    from api.verification_sanitizer import _install_agent_verification_evidence_sanitizer

    installed = _install_agent_verification_evidence_sanitizer()
    assert installed, "sanitizer must install against the real agent modules"
    assert getattr(te.make_tool_result_message, "_webui_verification_sanitized", False)

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
    tool_message = te.make_tool_result_message("terminal", dict(fresh_result), "call-real-1")
    assert "verification_evidence" not in str(tool_message.get("content", ""))

    # Flush into a REAL temp SessionDB the way the executor's incremental
    # persistence does (append_message on the canonical store).
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("real-exec-6481", "webui")
    content = tool_message["content"]
    db.append_message(
        session_id="real-exec-6481",
        role="tool",
        content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        tool_name=tool_message.get("tool_name"),
        tool_call_id=tool_message.get("tool_call_id"),
    )
    rows = db.get_messages("real-exec-6481")
    assert len(rows) == 1
    assert "verification_evidence" not in str(rows[0]["content"])
    assert "12 passed, 0 failed" in str(rows[0]["content"])


# --------------------------------------------------------------------------- #
# Re-gate: executor-owned suffix (valid JSON object + guidance) stays intact
# --------------------------------------------------------------------------- #


def _terminal_json_with_evidence():
    return json.dumps({
        "output": "12 passed, 0 failed",
        "exit_code": 0,
        "error": None,
        "verification_evidence": {
            "status": "passed",
            "kind": "test",
            "scope": "full",
            "canonical_command": "pytest",
        },
    }, ensure_ascii=False)


_GUARDRAIL_SUFFIX = (
    "\n\n[Tool loop warning: tool_loop; count=2; terminal failed twice; "
    "diagnose before retrying]"
)

_SUBDIR_SUFFIX = "\n\n[Subdirectory context: project/AGENTS.md project conventions]"


def test_strips_verification_evidence_from_json_with_appended_guardrail_suffix():
    """A valid JSON result with tool-guard guidance appended is sanitized in place.

    The installed Agent's executor appends ``_append_guardrail_observation``
    output to the serialized terminal result BEFORE the wrapped
    ``make_tool_result_message`` is called, so the content is
    ``valid JSON object + suffix`` — not whole-string JSON. The sanitizer must
    remove the field from the decoded prefix and leave the exact suffix
    untouched.
    """
    content = _terminal_json_with_evidence() + _GUARDRAIL_SUFFIX
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    assert "verification_evidence" not in stripped
    assert stripped.endswith(_GUARDRAIL_SUFFIX), "executor-owned suffix must survive verbatim"
    prefix = stripped[: -len(_GUARDRAIL_SUFFIX)]
    parsed = json.loads(prefix)
    assert parsed["output"] == "12 passed, 0 failed"
    assert parsed["exit_code"] == 0


def test_strips_verification_evidence_from_json_with_appended_subdir_suffix():
    """A valid JSON result with subdirectory hints appended is sanitized in place.

    ``agent.tool_executor`` can also append ``_subdirectory_hints`` output to
    the result string; same prefix-plus-suffix handling must apply.
    """
    content = _terminal_json_with_evidence() + _SUBDIR_SUFFIX
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    assert "verification_evidence" not in stripped
    assert stripped.endswith(_SUBDIR_SUFFIX), "subdirectory hint suffix must survive verbatim"
    parsed = json.loads(stripped[: -len(_SUBDIR_SUFFIX)])
    assert parsed["output"] == "12 passed, 0 failed"


def test_preserves_json_with_suffix_but_no_evidence():
    """A JSON+suffix result WITHOUT the evidence field is returned unchanged."""
    content = json.dumps({"output": "ok", "exit_code": 0}, ensure_ascii=False) + _GUARDRAIL_SUFFIX
    stripped = streaming._strip_verification_evidence_from_tool_content(content)
    assert stripped == content


def test_preserves_non_dict_json_with_suffix():
    """A JSON array + suffix is not a terminal dict — left fully untouched."""
    content = json.dumps(["a", "b", "verification_evidence"]) + _GUARDRAIL_SUFFIX
    assert streaming._strip_verification_evidence_from_tool_content(content) == content


def test_strips_evidence_from_json_with_suffix_inside_display_projection():
    """End-to-end: _strip_verification_from_messages cleans JSON+suffix rows."""
    content = _terminal_json_with_evidence() + _GUARDRAIL_SUFFIX
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call-guard-1",
            "name": "terminal",
            "content": content,
        },
    ]
    streaming._strip_verification_from_messages(messages)
    cleaned = messages[0]["content"]
    assert "verification_evidence" not in cleaned
    assert cleaned.endswith(_GUARDRAIL_SUFFIX)


def test_conflicting_explicit_identity_fails_closed():
    """name/tool_name disagreement must preserve the row (fail closed).

    ``{name: \"terminal\", tool_name: \"read_file\"}`` must NOT be stripped —
    the old ``name or tool_name`` selection stripped it while preserving the
    inverse conflict. Any present explicit identity naming a non-terminal tool
    wins: we cannot prove the row is a terminal result.
    """
    payload = json.dumps({
        "output": "custom",
        "verification_evidence": {"status": "passed"},
    })
    for name, tool_name in (
        ("terminal", "read_file"),
        ("read_file", "terminal"),
        ("read_file", "read_file"),
    ):
        messages = [{
            "role": "tool",
            "tool_call_id": "call-conflict-1",
            "name": name,
            "tool_name": tool_name,
            "content": payload,
        }]
        streaming._strip_verification_from_messages(messages)
        assert "verification_evidence" in messages[0]["content"], (
            f"conflicting identity {{{name!r}, {tool_name!r}}} must be preserved"
        )


def test_agreeing_explicit_identity_strips():
    """Both explicit identity fields agreeing on terminal → strip."""
    messages = [{
        "role": "tool",
        "tool_call_id": "call-agree-1",
        "name": "terminal",
        "tool_name": "terminal",
        "content": _terminal_json_with_evidence(),
    }]
    streaming._strip_verification_from_messages(messages)
    assert "verification_evidence" not in messages[0]["content"]


def _run_real_sequential_executor_with_guardrail_suffix(tmp_path, monkeypatch):
    """Drive the REAL agent.tool_executor sequential loop with a fake agent.

    Returns (messages, db, session_id, tool_message) after the executor's
    incremental SessionDB flush ran. The terminal dispatch is stubbed to return
    the actual serialized result (exactly what ``tools/terminal_tool.py``
    produces), and the fake agent's ``_append_guardrail_observation`` forces a
    tool-guard WARN decision so the executor appends real guidance to the
    result string — the production composition the re-gate flagged.
    """
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import agent.tool_executor as te
    from agent.tool_guardrails import ToolGuardrailDecision, append_toolguard_guidance
    from api.verification_sanitizer import _install_agent_verification_evidence_sanitizer

    # Install the real producer-boundary wrapper on BOTH the helper and the
    # executor alias — the executor calls its module-level reference.
    installed = _install_agent_verification_evidence_sanitizer()
    assert installed, "sanitizer must install against the real agent modules"
    assert getattr(te.make_tool_result_message, "_webui_verification_sanitized", False)

    serialized_result = _terminal_json_with_evidence()

    def _stub_middleware(agent, *, function_name, function_args, effective_task_id,
                         tool_call_id, execute, scope_block=None, display_index=None,
                         middleware_trace=None, begin_execution=None,
                         authorization_gate=None, **kwargs):
        # Stubbed terminal dispatch: return the actual serialized terminal
        # result without running the real command pipeline.
        return te._ManagedToolResult(
            result=serialized_result,
            args=function_args,
            middleware_trace=list(middleware_trace or []),
            blocked=False,
        )

    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", _stub_middleware)

    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "seq-exec-6481"
    db.create_session(session_id, "webui")

    class _FakeAgent:
        _incremental_persistence_failed = False
        _interrupt_requested = False
        verbose_logging = False
        quiet_mode = False
        log_prefix = "[fake] "
        log_prefix_chars = 200
        session_id = ""
        tool_progress_callback = None
        tool_complete_callback = None
        _current_tool = None
        _tool_guardrails = None
        _context_engine_tool_names = None
        _memory_manager = None
        # Executor reads agent._subdirectory_hints.check_tool_call(...).
        _subdirectory_hints = SimpleNamespace(check_tool_call=lambda name, args: None)

        def _vprint(self, msg, force=False):
            pass

        def _should_emit_quiet_tool_messages(self):
            return False

        def _touch_activity(self, msg):
            pass

        def _record_file_mutation_result(self, *args, **kwargs):
            pass

        def _append_guardrail_observation(self, tool_name, function_args,
                                          function_result, *, failed):
            # Force a WARN decision through the REAL guidance appender so the
            # executor-owned suffix matches production byte-for-byte.
            decision = ToolGuardrailDecision(
                action="warn",
                code="tool_loop",
                message="terminal failed twice; diagnose before retrying",
                tool_name=tool_name,
                count=2,
            )
            return append_toolguard_guidance(function_result, decision)

        def _tool_result_content_for_active_model(self, name, result):
            # String results pass through unchanged (matches run_agent).
            return result

        def _apply_pending_steer_to_tool_results(self, messages, num_tools):
            pass

        def _flush_messages_to_session_db(self, messages):
            # Mirror the executor's incremental persistence: append the
            # in-memory tool rows to the REAL temp SessionDB.
            for msg in messages:
                if not isinstance(msg, dict) or msg.get("role") != "tool":
                    continue
                content = msg.get("content")
                db.append_message(
                    session_id=session_id,
                    role="tool",
                    content=content if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                    tool_name=msg.get("tool_name"),
                    tool_call_id=msg.get("tool_call_id"),
                )
            return True

    agent = _FakeAgent()
    agent.session_id = session_id

    tool_call = SimpleNamespace(
        id="call-seq-guard-1",
        function=SimpleNamespace(
            name="terminal",
            arguments=json.dumps({"command": "pytest tests/ -q"}),
        ),
    )
    assistant_message = SimpleNamespace(tool_calls=[tool_call])
    messages = [
        {"role": "user", "content": "Run the tests."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-seq-guard-1", "function": {"name": "terminal", "arguments": "{}"}}
        ]},
    ]

    te.execute_tool_calls_sequential(
        agent, assistant_message, messages, effective_task_id="task-6481"
    )
    return messages, db, session_id


def test_real_sequential_executor_guardrail_suffix_stays_sanitized(tmp_path, monkeypatch):
    """The REAL sequential executor never leaks verification_evidence.

    Production composition from the re-gate: the executor runs the terminal
    dispatch (stubbed to return the actual serialized result), appends
    tool-guard guidance to the string (``_append_guardrail_observation``),
    builds the tool message through the sanitizer-wrapped
    ``make_tool_result_message``, appends it to the live conversation, and
    flushes it to SessionDB — all before WebUI regains control. The evidence
    field must be absent from the in-memory tool message, the second same-turn
    provider payload, and the persisted row, while the guidance suffix remains
    intact.
    """
    messages, db, session_id = _run_real_sequential_executor_with_guardrail_suffix(
        tmp_path, monkeypatch
    )

    # 1. In-memory tool message — evidence gone, suffix intact.
    tool_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert isinstance(content, str)
    assert "verification_evidence" not in content
    assert content.endswith(_GUARDRAIL_SUFFIX), "executor-owned guidance must survive"
    parsed = json.loads(content[: -len(_GUARDRAIL_SUFFIX)])
    assert parsed["output"] == "12 passed, 0 failed"
    assert parsed["exit_code"] == 0

    # 2. Second same-turn provider payload — what the model would receive on
    # the next request in the same turn (raw serialized messages). The suffix
    # is embedded as escaped ``\n\n`` inside the JSON-encoded string.
    provider_payload = json.dumps(messages, ensure_ascii=False)
    assert "verification_evidence" not in provider_payload
    assert "[Tool loop warning: tool_loop; count=2;" in provider_payload

    # 2b. The WebUI model-boundary sanitizer is also clean on the live list.
    sanitized = streaming._sanitize_messages_for_api(messages)
    assert "verification_evidence" not in json.dumps(sanitized, ensure_ascii=False)

    # 3. Persisted SessionDB row — evidence gone, suffix intact.
    rows = db.get_messages(session_id)
    assert len(rows) == 1
    row_content = str(rows[0]["content"])
    assert "verification_evidence" not in row_content
    assert "12 passed, 0 failed" in row_content
    assert _GUARDRAIL_SUFFIX in row_content
