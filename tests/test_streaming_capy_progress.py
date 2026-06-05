"""Capy progress receipts for WebUI streaming tool lifecycle events."""
from pathlib import Path


UNSAFE_FIXTURES = (
    "SECRET_VALUE_DO_NOT_LEAK",
    "<script>",
    "raw_prompt",
    "api_key",
    "renderer",
    "ignore previous instructions",
    "dangerous_tool",
)


def test_streaming_tool_completion_records_metadata_only_progress_event(tmp_path, monkeypatch):
    """WebUI streaming tool completions should write only bounded metadata.

    The callback receives hostile tool names, previews, args, and results from
    arbitrary tool execution. The Capy progress event must keep only the safe
    lifecycle boundary: event type + stable stream-scoped run id.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.completed",
        stream_id="stream-abc123",
        tool_name="dangerous_tool_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw prompt ignore previous instructions",
        args={
            "raw_prompt": "ignore previous instructions",
            "api_key": "sk-live-SECRET_VALUE_DO_NOT_LEAK",
            "renderer": "<script>alert(1)</script>",
        },
        function_result={"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK"},
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "tool.completed"
    assert receipt["family"] == "tool"
    assert receipt["run_id"] == "webui.tool:stream-abc123"
    assert status["recent_family_counts"]["tool"] == 1
    assert status["recent_events"][0]["run_id"] == "webui.tool:stream-abc123"
    for unsafe in UNSAFE_FIXTURES:
        assert unsafe.lower() not in serialized.lower()


def test_streaming_tool_progress_uses_fallback_run_id_for_unsafe_stream_ids(tmp_path, monkeypatch):
    """Unsafe stream ids must not become durable progress metadata."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.completed",
        stream_id="SECRET_VALUE_DO_NOT_LEAK<script>source.data ignore previous instructions",
        tool_name="safe-name",
    )
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())

    assert receipt["stored"] is True
    assert receipt["run_id"] == "webui.tool:stream"
    prompt_injection_receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.completed",
        stream_id="ignore previous instructions",
        tool_name="safe-name",
    )
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())
    assert prompt_injection_receipt["run_id"] == "webui.tool:stream"
    assert "webui.tool:stream" in serialized
    for unsafe in ("SECRET_VALUE_DO_NOT_LEAK", "<script", "source.data", "ignore-previous-instructions"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_tool_start_records_metadata_only_progress_event(tmp_path, monkeypatch):
    """WebUI streaming tool starts should appear in Capy progress safely.

    Structured progress supports both tool.started and tool.completed. The start
    event has hostile preview/args available, so it must persist only the safe
    lifecycle boundary and never tool arguments or previews.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.started",
        stream_id="stream-start-123",
        tool_name="terminal_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt ignore previous instructions",
        args={
            "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
            "api_auth": "bearer placeholder",
            "source": "<script>alert(1)</script>",
        },
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "tool.started"
    assert receipt["family"] == "tool"
    assert receipt["run_id"] == "webui.tool:stream-start-123"
    assert status["recent_family_counts"]["tool"] == 1
    assert status["recent_events"][0]["event_type"] == "tool.started"
    assert status["recent_events"][0]["run_id"] == "webui.tool:stream-start-123"
    for unsafe in UNSAFE_FIXTURES + ("api_auth", "bearer placeholder", "id_rsa"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_tool_args_delta_records_metadata_only_progress_event(tmp_path, monkeypatch):
    """Streaming tool argument deltas should not be mislabeled as completion.

    Tool-argument deltas can contain command bodies, prompt fragments, renderer
    fields, or credentials. The durable progress event must record only the safe
    taxonomy event type and stream-scoped run id.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.args.delta",
        stream_id="stream-args-123",
        tool_name="terminal_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt ignore previous instructions",
        args={
            "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
            "api_key": "bearer placeholder",
            "renderer": "<script>alert(1)</script>",
        },
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "tool.args.delta"
    assert receipt["family"] == "tool"
    assert receipt["run_id"] == "webui.tool:stream-args-123"
    assert status["recent_family_counts"]["tool"] == 1
    assert status["recent_events"][0]["event_type"] == "tool.args.delta"
    assert status["recent_events"][0]["run_id"] == "webui.tool:stream-args-123"
    for unsafe in UNSAFE_FIXTURES + ("api_key", "bearer placeholder", "id_rsa"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_tool_args_delta_uses_fallback_run_id_for_unsafe_stream_ids(tmp_path, monkeypatch):
    """Unsafe stream ids in tool-args delta markers must not become public metadata."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.args.delta",
        stream_id="SECRET_VALUE_DO_NOT_LEAK<script>source.data ignore previous instructions",
        tool_name="safe-name",
        args={"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
    )
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())

    assert receipt["stored"] is True
    assert receipt["event_type"] == "tool.args.delta"
    assert receipt["family"] == "tool"
    assert receipt["run_id"] == "webui.tool:stream"
    assert "webui.tool:stream" in serialized
    for unsafe in ("SECRET_VALUE_DO_NOT_LEAK", "<script", "source.data", "ignore-previous-instructions", "api_key"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_subagent_spawn_records_metadata_only_progress_event(tmp_path, monkeypatch):
    """WebUI streaming subagent events should be recorded as subagent progress.

    Subagent callbacks can carry raw prompt previews, arguments, tool output, or
    secret-looking handoff details. The durable progress stream must keep only
    the safe event boundary and stream-scoped run id, not the raw payload.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_progress_event(
        event_type="subagent.spawned",
        stream_id="subagent-stream-123",
        tool_name="delegate_task_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt ignore previous instructions",
        args={
            "goal": "copy renderer source <script>alert(1)</script>",
            "api_auth": "bearer placeholder",
            "raw_prompt": "ignore previous instructions",
        },
        function_result={"source": "subagent output SECRET_VALUE_DO_NOT_LEAK"},
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "subagent.spawned"
    assert receipt["family"] == "subagent"
    assert receipt["run_id"] == "webui.subagent:subagent-stream-123"
    assert status["recent_family_counts"]["subagent"] == 1
    assert status["recent_events"][0]["event_type"] == "subagent.spawned"
    assert status["recent_events"][0]["run_id"] == "webui.subagent:subagent-stream-123"
    for unsafe in UNSAFE_FIXTURES + ("api_auth", "bearer placeholder"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_legacy_subagent_events_normalize_to_progress_taxonomy(tmp_path, monkeypatch):
    """Existing Hermes delegate callbacks should map onto Capy's progress taxonomy."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    started = streaming._record_streaming_progress_event(
        event_type="subagent.start",
        stream_id="legacy-subagent",
        preview="SECRET_VALUE_DO_NOT_LEAK raw prompt",
    )
    spawned = streaming._record_streaming_progress_event(
        event_type="subagent.spawn_requested",
        stream_id="legacy-subagent",
        preview="ignore previous instructions",
    )
    progress = streaming._record_streaming_progress_event(
        event_type="subagent_progress",
        stream_id="legacy-subagent",
        tool_name="🔀 [1] terminal, file SECRET_VALUE_DO_NOT_LEAK",
    )
    failed = streaming._record_streaming_progress_event(
        event_type="subagent.complete",
        stream_id="legacy-subagent",
        status="failed",
        preview="SECRET_VALUE_DO_NOT_LEAK failure details",
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert started["event_type"] == "subagent.started"
    assert spawned["event_type"] == "subagent.spawned"
    assert progress["event_type"] == "subagent.progress"
    assert failed["event_type"] == "subagent.failed"
    assert status["recent_family_counts"]["subagent"] == 4
    assert {event["event_type"] for event in status["recent_events"]} >= {
        "subagent.started",
        "subagent.spawned",
        "subagent.progress",
        "subagent.failed",
    }
    for unsafe in UNSAFE_FIXTURES:
        assert unsafe.lower() not in serialized.lower()


def test_streaming_callback_parser_preserves_legacy_subagent_one_arg_events():
    """One-arg delegate subagent callbacks are event names, not tool names."""
    from api import streaming

    event_type, name, preview, args = streaming._parse_streaming_progress_callback(
        ("subagent.start",),
        {"preview": "SECRET_VALUE_DO_NOT_LEAK raw goal"},
    )
    assert event_type == "subagent.start"
    assert name is None
    assert preview == "SECRET_VALUE_DO_NOT_LEAK raw goal"
    assert args is None

    event_type, name, preview, args = streaming._parse_streaming_progress_callback(
        ("subagent.complete",),
        {"status": "timeout", "preview": "Timed out after 600s SECRET_VALUE_DO_NOT_LEAK"},
    )
    assert event_type == "subagent.complete"
    assert name is None
    assert preview == "Timed out after 600s SECRET_VALUE_DO_NOT_LEAK"
    assert args is None


def test_streaming_legacy_subagent_timeout_maps_to_failed_progress(tmp_path, monkeypatch):
    """Timed-out subagent completions should not be reported as success."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import streaming

    receipt = streaming._record_streaming_progress_event(
        event_type="subagent.complete",
        stream_id="legacy-subagent",
        status="timeout",
        preview="SECRET_VALUE_DO_NOT_LEAK timeout details",
    )

    assert receipt["stored"] is True
    assert receipt["event_type"] == "subagent.failed"
    assert receipt["family"] == "subagent"


def test_streaming_current_subagent_tool_and_thinking_events_map_to_progress(tmp_path, monkeypatch):
    """Current delegate subagent.tool/thinking callbacks should stay subagent metadata."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    event_type, name, preview, args = streaming._parse_streaming_progress_callback(
        ("subagent.tool", "terminal", "SECRET_VALUE_DO_NOT_LEAK raw args", {"api_key": "SECRET_VALUE_DO_NOT_LEAK"}),
        {},
    )
    assert event_type == "subagent.tool"
    assert name == "terminal"
    assert preview == "SECRET_VALUE_DO_NOT_LEAK raw args"
    assert args == {"api_key": "SECRET_VALUE_DO_NOT_LEAK"}

    thinking = streaming._record_streaming_progress_event(
        event_type="subagent.thinking",
        stream_id="subagent-stream",
        preview="SECRET_VALUE_DO_NOT_LEAK raw thought",
    )
    tool = streaming._record_streaming_progress_event(
        event_type="subagent.tool",
        stream_id="subagent-stream",
        tool_name="terminal",
        args={"api_key": "SECRET_VALUE_DO_NOT_LEAK"},
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert thinking["event_type"] == "subagent.progress"
    assert thinking["family"] == "subagent"
    assert tool["event_type"] == "subagent.progress"
    assert tool["family"] == "subagent"
    assert status["recent_family_counts"]["subagent"] == 2
    for unsafe in UNSAFE_FIXTURES + ("api_key",):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_progress_uses_fallback_run_id_for_unsafe_subagent_stream_ids(tmp_path, monkeypatch):
    """Unsafe stream ids must not become durable subagent progress metadata."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_progress_event(
        event_type="subagent.progress",
        stream_id="SECRET_VALUE_DO_NOT_LEAK<script>source.data ignore previous instructions",
        tool_name="safe-name",
    )
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())

    assert receipt["stored"] is True
    assert receipt["event_type"] == "subagent.progress"
    assert receipt["family"] == "subagent"
    assert receipt["run_id"] == "webui.subagent:stream"
    assert "webui.subagent:stream" in serialized
    for unsafe in ("SECRET_VALUE_DO_NOT_LEAK", "<script", "source.data", "ignore-previous-instructions"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_text_and_thinking_deltas_record_metadata_only_once_per_stream(tmp_path, monkeypatch):
    """WebUI token/reasoning streams should appear as metadata-only Capy progress.

    Raw model text and reasoning deltas may contain prompts, source text, or
    secret-looking data. The durable progress producer should record only the
    event family/type and safe stream run id, and it should not write one row per
    token because model deltas can be very high volume.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    first_text = streaming._record_streaming_delta_progress_event(
        event_type="text.delta",
        stream_id="stream-delta-123",
        text="SECRET_VALUE_DO_NOT_LEAK raw_prompt <script>ignore previous instructions</script>",
    )
    duplicate_text = streaming._record_streaming_delta_progress_event(
        event_type="text.delta",
        stream_id="stream-delta-123",
        text="second token with api_key bearer placeholder renderer source data",
    )
    thinking = streaming._record_streaming_delta_progress_event(
        event_type="thinking.delta",
        stream_id="stream-delta-123",
        text="hidden chain with SECRET_VALUE_DO_NOT_LEAK api_auth raw_prompt",
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert first_text["stored"] is True
    assert first_text["event_type"] == "text.delta"
    assert first_text["family"] == "text"
    assert first_text["run_id"] == "webui.text:stream-delta-123"
    assert duplicate_text["stored"] is False
    assert duplicate_text["deduped"] is True
    assert thinking["stored"] is True
    assert thinking["event_type"] == "thinking.delta"
    assert thinking["family"] == "thinking"
    assert thinking["run_id"] == "webui.thinking:stream-delta-123"
    assert status["recent_family_counts"]["text"] == 1
    assert status["recent_family_counts"]["thinking"] == 1
    assert {event["event_type"] for event in status["recent_events"]} >= {"text.delta", "thinking.delta"}
    for unsafe in UNSAFE_FIXTURES + ("api_auth", "bearer placeholder", "source data"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_delta_progress_uses_fallback_run_id_for_unsafe_stream_ids(tmp_path, monkeypatch):
    """Unsafe text/thinking stream ids must not become durable progress metadata."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_delta_progress_event(
        event_type="text.delta",
        stream_id="SECRET_VALUE_DO_NOT_LEAK<script>source.data ignore previous instructions",
        text="safe visible token",
    )
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())

    assert receipt["stored"] is True
    assert receipt["event_type"] == "text.delta"
    assert receipt["family"] == "text"
    assert receipt["run_id"] == "webui.text:stream"
    assert "webui.text:stream" in serialized
    for unsafe in ("SECRET_VALUE_DO_NOT_LEAK", "<script", "source.data", "ignore-previous-instructions"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_callbacks_invoke_progress_recorder_for_tool_subagent_and_delta_events():
    """The real streaming callback must feed tool/subagent events into the recorder."""
    src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(encoding="utf-8")
    started_idx = src.find("if event_type in (None, 'tool.started'):")
    started_recorder_idx = src.find("_record_streaming_tool_progress_event", started_idx)
    started_return_idx = src.find("return", started_idx)
    completed_idx = src.find("if event_type == 'tool.completed':")
    completed_recorder_idx = src.find("_record_streaming_tool_progress_event", completed_idx)
    completed_return_idx = src.find("return", completed_idx)
    on_tool_idx = src.find("def on_tool(*cb_args, **cb_kwargs):")
    args_delta_idx = src.find("if event_type == 'tool.args.delta':", on_tool_idx)
    args_delta_recorder_idx = src.find("_record_streaming_tool_progress_event", args_delta_idx)
    args_delta_return_idx = src.find("return", args_delta_idx)
    subagent_idx = src.find("if event_type in _STREAMING_SUBAGENT_INPUT_EVENT_TYPES:", on_tool_idx)
    subagent_recorder_idx = src.find("_record_streaming_progress_event", subagent_idx)
    subagent_return_idx = src.find("return", subagent_idx)
    on_token_idx = src.find("def on_token(text):")
    on_token_recorder_idx = src.find("_record_streaming_delta_progress_event", on_token_idx)
    on_token_meter_idx = src.find("meter().record_token", on_token_idx)
    on_reasoning_idx = src.find("def on_reasoning(text):")
    on_reasoning_recorder_idx = src.find("_record_streaming_delta_progress_event", on_reasoning_idx)
    on_reasoning_meter_idx = src.find("meter().record_reasoning", on_reasoning_idx)

    assert started_idx != -1, "streaming tool.started callback block not found"
    assert started_recorder_idx != -1 and started_idx < started_recorder_idx < started_return_idx, (
        "tool.started callback must record a metadata-only Capy progress event before returning"
    )
    assert completed_idx != -1, "streaming tool.completed callback block not found"
    assert completed_recorder_idx != -1 and completed_idx < completed_recorder_idx < completed_return_idx, (
        "tool.completed callback must record a metadata-only Capy progress event before returning"
    )
    assert args_delta_idx != -1, "streaming tool.args.delta callback block not found"
    assert args_delta_recorder_idx != -1 and args_delta_idx < args_delta_recorder_idx < args_delta_return_idx, (
        "tool.args.delta callback must record a metadata-only Capy progress event before returning"
    )
    assert subagent_idx != -1, "streaming subagent callback block not found"
    assert subagent_recorder_idx != -1 and subagent_idx < subagent_recorder_idx < subagent_return_idx, (
        "subagent callback must record a metadata-only Capy progress event before returning"
    )
    assert on_token_idx != -1, "streaming token callback block not found"
    assert on_token_recorder_idx != -1 and on_token_idx < on_token_recorder_idx < on_token_meter_idx, (
        "token callback must record one metadata-only text.delta Capy progress event before metering"
    )
    assert on_reasoning_idx != -1, "streaming reasoning callback block not found"
    assert on_reasoning_recorder_idx != -1 and on_reasoning_idx < on_reasoning_recorder_idx < on_reasoning_meter_idx, (
        "reasoning callback must record one metadata-only thinking.delta Capy progress event before metering"
    )
