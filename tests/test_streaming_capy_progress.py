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


def test_streaming_tool_callbacks_invoke_progress_recorder_for_start_and_completion():
    """The real streaming callback must feed start/completion events into the recorder."""
    src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(encoding="utf-8")
    started_idx = src.find("if event_type in (None, 'tool.started'):")
    started_recorder_idx = src.find("_record_streaming_tool_progress_event", started_idx)
    started_return_idx = src.find("return", started_idx)
    completed_idx = src.find("if event_type == 'tool.completed':")
    completed_recorder_idx = src.find("_record_streaming_tool_progress_event", completed_idx)
    completed_return_idx = src.find("return", completed_idx)

    assert started_idx != -1, "streaming tool.started callback block not found"
    assert started_recorder_idx != -1 and started_idx < started_recorder_idx < started_return_idx, (
        "tool.started callback must record a metadata-only Capy progress event before returning"
    )
    assert completed_idx != -1, "streaming tool.completed callback block not found"
    assert completed_recorder_idx != -1 and completed_idx < completed_recorder_idx < completed_return_idx, (
        "tool.completed callback must record a metadata-only Capy progress event before returning"
    )
