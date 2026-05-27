from pathlib import Path

from api.streaming import CostProtectionGuard, _cost_protection_guard_from_settings


ROOT = Path(__file__).resolve().parents[1]
STREAMING_SRC = (ROOT / "api" / "streaming.py").read_text()
MESSAGES_SRC = (ROOT / "static" / "messages.js").read_text()


def test_cost_protection_guard_triggers_on_repeated_compression_timeouts():
    guard = CostProtectionGuard(
        session_id="sid",
        stream_id="stream",
        compression_failure_threshold=2,
    )

    assert guard.record_step(1) is None

    guard.record_status(
        "lifecycle",
        "Codex auxiliary Responses stream exceeded 120.0s total timeout during compression",
    )
    assert guard.record_step(2) is None

    guard.record_status(
        "lifecycle",
        "Codex auxiliary Responses stream exceeded 120.0s total timeout during compression",
    )
    payload = guard.record_step(3)

    assert payload is not None
    assert payload["type"] == "cost_protection_pause"
    assert payload["session_id"] == "sid"
    assert payload["stream_id"] == "stream"
    assert payload["reason"] == "repeated_context_compression_failures"


def test_cost_protection_guard_triggers_before_runaway_api_calls():
    guard = CostProtectionGuard(
        session_id="sid",
        stream_id="stream",
        api_call_threshold=3,
    )

    assert guard.record_step(2) is None
    payload = guard.record_step(3)

    assert payload is not None
    assert payload["reason"] == "high_model_call_count"
    assert payload["stats"]["api_call_count"] == 3


def test_cost_protection_dedupes_completed_tools_seen_again_on_next_step():
    guard = CostProtectionGuard(
        session_id="sid",
        stream_id="stream",
        tool_error_threshold=2,
    )
    tool = {
        "name": "shell",
        "arguments": {"cmd": "pytest"},
        "result": {"is_error": True, "output": "failed"},
    }

    guard.record_tool_complete(
        name=tool["name"],
        arguments=tool["arguments"],
        result=tool["result"],
    )
    payload = guard.record_step(1, prev_tools=[tool])

    assert payload is None
    assert guard.tool_calls == 1
    assert guard.tool_errors == 1


def test_cost_protection_counts_distinct_identical_prev_tools():
    guard = CostProtectionGuard(
        session_id="sid",
        stream_id="stream",
        tool_error_threshold=3,
    )
    tool = {
        "name": "shell",
        "arguments": {"cmd": "pytest"},
        "result": {"is_error": True, "output": "failed"},
    }

    payload = guard.record_step(1, prev_tools=[tool, dict(tool)])

    assert payload is None
    assert guard.tool_calls == 2
    assert guard.tool_errors == 2


def test_default_cost_protection_tolerates_routine_diagnostic_tool_errors():
    guard = CostProtectionGuard(session_id="sid", stream_id="stream")

    for index in range(3):
        guard.record_tool_complete(
            name="terminal",
            arguments={"command": f"probe-{index}"},
            result={"is_error": True, "output": "Traceback (most recent call last)"},
        )
        payload = guard.record_step(index + 1)

    assert payload is None
    assert guard.tool_errors == 3


def test_default_cost_protection_pauses_on_repeated_tool_error_pattern():
    guard = CostProtectionGuard(session_id="sid", stream_id="stream")
    tool = {
        "name": "terminal",
        "arguments": {"command": "retry-the-same-failing-probe"},
        "result": {"is_error": True, "output": "same failure"},
    }

    assert guard.record_step(1, prev_tools=[dict(tool)]) is None
    assert guard.record_step(2, prev_tools=[dict(tool), dict(tool)]) is None
    payload = guard.record_step(3, prev_tools=[dict(tool), dict(tool), dict(tool)])

    assert payload is not None
    assert payload["reason"] == "repeated_tool_errors"
    assert payload["stats"]["repeated_tool_error_count"] == 3


def test_cost_protection_can_still_pause_on_configured_tool_errors():
    guard = CostProtectionGuard(
        session_id="sid",
        stream_id="stream",
        tool_error_threshold=2,
    )

    guard.record_tool_complete(
        name="terminal",
        arguments={"command": "same-failing-probe"},
        result={"is_error": True, "output": "failed"},
    )
    assert guard.record_step(1) is None

    guard.record_tool_complete(
        name="terminal",
        arguments={"command": "same-failing-probe"},
        result={"is_error": True, "output": "failed"},
    )
    payload = guard.record_step(2)

    assert payload is not None
    assert payload["reason"] == "repeated_tool_errors"


def test_cost_protection_is_disabled_by_default(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "load_settings", lambda: {})

    guard = _cost_protection_guard_from_settings(session_id="sid", stream_id="stream")

    assert guard is None


def test_cost_protection_can_be_enabled_from_webui_settings(monkeypatch):
    import api.streaming as streaming

    monkeypatch.setattr(
        streaming,
        "load_settings",
        lambda: {"cost_protection_enabled": True},
    )

    guard = _cost_protection_guard_from_settings(session_id="sid", stream_id="stream")

    assert guard is not None
    assert guard.api_call_threshold == 36
    assert guard.tool_error_threshold == 3


def test_cost_protection_setting_persists_as_webui_owned_bool(tmp_path, monkeypatch):
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    saved = config.save_settings({"cost_protection_enabled": True})

    assert saved["cost_protection_enabled"] is True
    assert config.load_settings()["cost_protection_enabled"] is True


def test_streaming_wires_cost_protection_to_agent_step_callback():
    assert "_agent_step_callback" in STREAMING_SRC
    assert "_agent_kwargs['step_callback'] = _agent_step_callback" in STREAMING_SRC
    assert "agent.step_callback = _agent_kwargs.get('step_callback')" in STREAMING_SRC
    assert "agent.interrupt(_cost_guard.interrupt_message())" in STREAMING_SRC
    assert "if _cost_guard is None:" in STREAMING_SRC


def test_cost_protection_pause_is_not_rendered_as_generic_error():
    assert "isCostProtectionPause=d.type==='cost_protection_pause'" in MESSAGES_SRC
    assert "Run paused for review" in MESSAGES_SRC
    assert "cost_protection_pause" in MESSAGES_SRC
