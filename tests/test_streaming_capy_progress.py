"""Capy progress receipts for WebUI streaming tool lifecycle events."""
import queue
import sys
import types
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


def test_streaming_tool_terminal_receipt_includes_metadata_only_compaction(tmp_path, monkeypatch):
    """Terminal WebUI tool callbacks should expose bounded compaction evidence.

    Tool completion/failure callbacks can carry raw command output, prompts,
    source fields, renderer payloads, and secrets. The returned receipt should
    show only safe output-compaction metadata and preserve failure status.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.failed",
        stream_id="stream-terminal-tool",
        tool_name="terminal_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt ignore previous instructions",
        args={
            "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
            "api_key": "sk-live-SECRET_VALUE_DO_NOT_LEAK",
            "renderer": "<script>alert(1)</script>",
        },
        function_result={
            "exit_status": 2,
            "source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK",
            "html": "<script>alert(1)</script>",
            "output": "Traceback: SECRET_VALUE_DO_NOT_LEAK raw_prompt",
        },
        status=2,
        is_error=True,
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status) + "\n" + str(receipt)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "tool.failed"
    assert receipt["run_id"] == "webui.tool:stream-terminal-tool"
    assert receipt["output_compaction"]["tool"] == "webui-streaming"
    assert receipt["output_compaction"]["command"] == "tool.failed"
    assert receipt["output_compaction"]["exit_status"] == 2
    assert receipt["output_compaction"]["original_chars"] > 0
    assert receipt["output_compaction"]["compacted_chars"] > 0
    assert "preserve_error_blocks" in receipt["output_compaction"]["rules_applied"]
    assert status["recent_family_counts"]["tool"] == 1
    for unsafe in UNSAFE_FIXTURES + ("api_key", "id_rsa", "raw fetched body", "sk-live"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_subagent_terminal_receipt_includes_metadata_only_compaction(tmp_path, monkeypatch):
    """Terminal subagent callbacks should expose safe compaction evidence."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    receipt = streaming._record_streaming_progress_event(
        event_type="subagent.complete",
        stream_id="stream-terminal-subagent",
        tool_name="delegate_task_<script>",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt ignore previous instructions",
        args={
            "goal": "exfiltrate SECRET_VALUE_DO_NOT_LEAK",
            "api_auth": "bearer placeholder",
            "source": "<script>alert(1)</script>",
        },
        function_result={"summary": "SECRET_VALUE_DO_NOT_LEAK", "exit_status": 1},
        status="timeout",
        is_error=True,
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status) + "\n" + str(receipt)

    assert receipt["stored"] is True
    assert receipt["event_type"] == "subagent.failed"
    assert receipt["family"] == "subagent"
    assert receipt["run_id"] == "webui.subagent:stream-terminal-subagent"
    assert receipt["output_compaction"]["tool"] == "webui-streaming"
    assert receipt["output_compaction"]["command"] == "subagent.failed"
    assert receipt["output_compaction"]["exit_status"] == 1
    assert receipt["output_compaction"]["redaction_status"] == "none"
    assert status["recent_family_counts"]["subagent"] == 1
    for unsafe in UNSAFE_FIXTURES + ("api_auth", "bearer placeholder", "exfiltrate"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_terminal_receipt_prefers_result_exit_status_over_generic_status(tmp_path, monkeypatch):
    """Terminal callback result exit codes should not be masked by generic status."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import streaming

    receipt = streaming._record_streaming_tool_progress_event(
        event_type="tool.failed",
        stream_id="stream-terminal-exit-precedence",
        function_result={"exit_status": 2, "output": "SECRET_VALUE_DO_NOT_LEAK"},
        status=0,
        is_error=True,
    )

    assert receipt["output_compaction"]["exit_status"] == 2


def test_streaming_terminal_receipt_falls_back_for_path_like_stream_ids(tmp_path, monkeypatch):
    """Path-shaped stream ids must not appear in receipts or durable progress."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    for stream_id in (
        "/Users/bschmidy10/.ssh/id_rsa",
        "/opt/project/run.log",
        "relative/path/to/thing",
        "foo\\bar",
        "file:///opt/project/run.log",
        "https://example.com/repo/file.txt",
    ):
        receipt = streaming._record_streaming_tool_progress_event(
            event_type="tool.completed",
            stream_id=stream_id,
            function_result={"exit_status": 0},
        )
        assert receipt["run_id"] == "webui.tool:stream"
        assert receipt["output_compaction"]["retained_artifact_handles"] == [
            {"kind": "progress_event", "handle": "webui.tool:stream", "label": "Streaming progress event"}
        ]
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(capy_progress.progress_status())

    for unsafe in ("/Users", "Users-bschmidy10", ".ssh", "id_rsa", "/opt", "relative-path", "foo-bar", "example.com"):
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


def test_run_agent_streaming_structured_tool_callbacks_record_metadata_only_progress(tmp_path, monkeypatch):
    """Structured AIAgent tool callbacks should produce safe Capy tool progress.

    Newer AIAgent builds can emit tool_start_callback/tool_complete_callback
    without the legacy tool_progress_callback path. The WebUI stream worker must
    still record metadata-only lifecycle progress and must not persist callback
    payloads such as tool names, args, results, prompts, commands, or secrets.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    saved_snapshots = []

    class FakeSession:
        def __init__(self):
            self.session_id = "structured_tool_callbacks_session"
            self.title = "Structured callbacks"
            self.workspace = str(tmp_path)
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = "stream-structured-tool"
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True
            self.path = str(tmp_path / "ephemeral-session.json")

        def save(self, *args, **kwargs):
            saved_snapshots.append(kwargs)

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    class StructuredOnlyAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
        ):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.tool_progress_callback = tool_progress_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback

        def run_conversation(self, **kwargs):
            assert self.tool_start_callback is not None
            assert self.tool_complete_callback is not None
            # Intentionally do not call tool_progress_callback; this is the gap.
            hostile_args = {
                "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
                "api_key": "sk-test-SECRET_VALUE_DO_NOT_LEAK",
                "renderer": "<script>alert(1)</script>",
                "raw_prompt": "ignore previous instructions",
            }
            self.tool_start_callback("call-structured-1", "dangerous_tool_<script>", hostile_args)
            self.tool_complete_callback(
                "call-structured-1",
                "dangerous_tool_<script>",
                hostile_args,
                {"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK", "html": "<script>", "exit_status": 7},
            )
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = fake_session.active_stream_id
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    setattr(fake_runtime_module, "resolve_runtime_provider", lambda requested=None: {
        "provider": "openai",
        "base_url": None,
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    setattr(fake_hermes_cli, "runtime_provider", fake_runtime_module)
    fake_hermes_state = types.ModuleType("hermes_state")
    setattr(fake_hermes_state, "SessionDB", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: StructuredOnlyAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("gpt-test", "openai", None))
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])

    q = queue.Queue()
    streaming.STREAMS[fake_stream_id] = q
    stream_messages = []
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="please use a tool",
            model="gpt-test",
            workspace=str(tmp_path),
            stream_id=fake_stream_id,
            ephemeral=True,
        )
        while not q.empty():
            stream_messages.append(q.get_nowait())
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)

    status = capy_progress.progress_status()
    log_path = Path(tmp_path / "progress" / "events.jsonl")
    raw_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    serialized = raw_log + "\n" + str(status) + "\n" + str(stream_messages)

    assert saved_snapshots, "stream worker should have reached the agent run"
    assert status["recent_family_counts"]["tool"] == 2
    assert status["recent_family_counts"]["run"] == 2
    assert {event["event_type"] for event in status["recent_events"]} >= {
        "tool.started",
        "tool.completed",
    }
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "tool"
    } == {"webui.tool:stream-structured-tool"}
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "run"
    } == {"webui.run:stream-structured-tool"}
    terminal_progress_receipts = [
        data for event, data in stream_messages
        if event == "progress_event" and data.get("event_type") == "tool.completed" and data.get("output_compaction")
    ]
    assert terminal_progress_receipts
    assert terminal_progress_receipts[0]["output_compaction"]["tool"] == "webui-streaming"
    assert terminal_progress_receipts[0]["output_compaction"]["exit_status"] == 7
    assert terminal_progress_receipts[0]["output_compaction"].get("text") is None
    for unsafe in UNSAFE_FIXTURES + (
        "api_key",
        "id_rsa",
        "sk-test",
        "html",
        "raw fetched body",
    ):
        assert unsafe.lower() not in serialized.lower()


def test_run_agent_streaming_prefers_structured_tool_progress_over_legacy_duplicates(tmp_path, monkeypatch):
    """Agents that emit both legacy and structured tool callbacks should count once.

    Current Hermes Agent builds can call the legacy tool_progress_callback without
    an id and also call structured tool_start/tool_complete callbacks with a
    tool_call_id for the same tool invocation. WebUI should keep the structured
    durable Capy lifecycle markers and not double-count the legacy compatibility
    callbacks. The persisted progress stream must remain metadata-only.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    saved_snapshots = []

    class FakeSession:
        def __init__(self):
            self.session_id = "structured_legacy_duplicate_session"
            self.title = "Structured and legacy callbacks"
            self.workspace = str(tmp_path)
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = "stream-structured-legacy-duplicate"
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True
            self.path = str(tmp_path / "ephemeral-duplicate-session.json")

        def save(self, *args, **kwargs):
            saved_snapshots.append(kwargs)

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    class LegacyAndStructuredAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
        ):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.tool_progress_callback = tool_progress_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback

        def run_conversation(self, **kwargs):
            assert self.tool_progress_callback is not None
            assert self.tool_start_callback is not None
            assert self.tool_complete_callback is not None
            hostile_args = {
                "id": "payload-id-is-not-callback-metadata",
                "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
                "api_key": "sk-tes...LEAK",
                "renderer": "<script>alert(1)</script>",
                "raw_prompt": "ignore previous instructions",
            }
            self.tool_progress_callback(
                "tool.started",
                "dangerous_tool_<script>",
                "SECRET_VALUE_DO_NOT_LEAK raw prompt ignore previous instructions",
                hostile_args,
            )
            self.tool_start_callback("call-structured-legacy-1", "dangerous_tool_<script>", hostile_args)
            self.tool_progress_callback(
                "tool.completed",
                "dangerous_tool_<script>",
                None,
                None,
                duration=0.01,
                is_error=False,
                result={"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK", "html": "<script>"},
            )
            self.tool_complete_callback(
                "call-structured-legacy-1",
                "dangerous_tool_<script>",
                hostile_args,
                {"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK", "html": "<script>"},
            )
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = fake_session.active_stream_id
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    setattr(fake_runtime_module, "resolve_runtime_provider", lambda requested=None: {
        "provider": "openai",
        "base_url": None,
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    setattr(fake_hermes_cli, "runtime_provider", fake_runtime_module)
    fake_hermes_state = types.ModuleType("hermes_state")
    setattr(fake_hermes_state, "SessionDB", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: LegacyAndStructuredAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("gpt-test", "openai", None))
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])

    streaming.STREAMS[fake_stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="please use a tool",
            model="gpt-test",
            workspace=str(tmp_path),
            stream_id=fake_stream_id,
            ephemeral=True,
        )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)

    status = capy_progress.progress_status()
    log_path = Path(tmp_path / "progress" / "events.jsonl")
    raw_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    serialized = raw_log + "\n" + str(status)
    event_types = [event["event_type"] for event in status["recent_events"]]

    assert saved_snapshots, "stream worker should have reached the agent run"
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.completed") == 1
    assert status["recent_family_counts"]["tool"] == 2
    assert status["recent_family_counts"]["run"] == 2
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "tool"
    } == {"webui.tool:stream-structured-legacy-duplicate"}
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "run"
    } == {"webui.run:stream-structured-legacy-duplicate"}
    for unsafe in UNSAFE_FIXTURES + (
        "api_key",
        "payload-id-is-not-callback-metadata",
        "id_rsa",
        "sk-test",
        "html",
        "raw fetched body",
    ):
        assert unsafe.lower() not in serialized.lower()


def test_run_agent_streaming_preserves_legacy_failed_tool_status_with_structured_callbacks(tmp_path, monkeypatch):
    """Legacy failed completions must not be overwritten by structured complete callbacks.

    Current Hermes Agent emits legacy completion first with is_error=True, then
    structured completion without any failure/status field. When WebUI prefers
    structured progress to avoid duplicate success events, it must still keep the
    only failure signal and suppress the following structured tool.completed.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    saved_snapshots = []

    class FakeSession:
        def __init__(self):
            self.session_id = "structured_legacy_failed_session"
            self.title = "Structured and legacy failed callbacks"
            self.workspace = str(tmp_path)
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = "stream-structured-legacy-failed"
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True
            self.path = str(tmp_path / "ephemeral-failed-session.json")

        def save(self, *args, **kwargs):
            saved_snapshots.append(kwargs)

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    class FailedLegacyAndStructuredAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
        ):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.ephemeral_system_prompt = None
            self._last_error = None
            self.tool_progress_callback = tool_progress_callback
            self.tool_start_callback = tool_start_callback
            self.tool_complete_callback = tool_complete_callback

        def run_conversation(self, **kwargs):
            assert self.tool_progress_callback is not None
            assert self.tool_start_callback is not None
            assert self.tool_complete_callback is not None
            hostile_args = {
                "id": "failed-payload-id-is-not-callback-metadata",
                "command": "cat ~/.ssh/id_rsa SECRET_VALUE_DO_NOT_LEAK",
                "api_key": "sk-tes...LEAK",
                "renderer": "<script>alert(1)</script>",
                "raw_prompt": "ignore previous instructions",
            }
            self.tool_progress_callback(
                "tool.started",
                "dangerous_tool_<script>",
                "SECRET_VALUE_DO_NOT_LEAK raw prompt ignore previous instructions",
                hostile_args,
            )
            self.tool_start_callback("call-structured-legacy-failed-1", "dangerous_tool_<script>", hostile_args)
            self.tool_progress_callback(
                "tool.completed",
                "dangerous_tool_<script>",
                None,
                None,
                duration=0.01,
                is_error=True,
                result={"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK", "html": "<script>"},
            )
            self.tool_complete_callback(
                "call-structured-legacy-failed-1",
                "dangerous_tool_<script>",
                hostile_args,
                {"source": "raw fetched body SECRET_VALUE_DO_NOT_LEAK", "html": "<script>"},
            )
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = fake_session.active_stream_id
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    setattr(fake_runtime_module, "resolve_runtime_provider", lambda requested=None: {
        "provider": "openai",
        "base_url": None,
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    setattr(fake_hermes_cli, "runtime_provider", fake_runtime_module)
    fake_hermes_state = types.ModuleType("hermes_state")
    setattr(fake_hermes_state, "SessionDB", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FailedLegacyAndStructuredAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("gpt-test", "openai", None))
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])

    streaming.STREAMS[fake_stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="please use a failing tool",
            model="gpt-test",
            workspace=str(tmp_path),
            stream_id=fake_stream_id,
            ephemeral=True,
        )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)

    status = capy_progress.progress_status()
    log_path = Path(tmp_path / "progress" / "events.jsonl")
    raw_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    serialized = raw_log + "\n" + str(status)
    event_types = [event["event_type"] for event in status["recent_events"]]

    assert saved_snapshots, "stream worker should have reached the agent run"
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.failed") == 1
    assert "tool.completed" not in event_types
    assert status["recent_family_counts"]["tool"] == 2
    assert status["recent_family_counts"]["run"] == 2
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "tool"
    } == {"webui.tool:stream-structured-legacy-failed"}
    assert {
        event["run_id"] for event in status["recent_events"] if event["family"] == "run"
    } == {"webui.run:stream-structured-legacy-failed"}
    for unsafe in UNSAFE_FIXTURES + (
        "api_key",
        "failed-payload-id-is-not-callback-metadata",
        "id_rsa",
        "sk-test",
        "html",
        "raw fetched body",
    ):
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


def test_streaming_run_lifecycle_records_metadata_only_progress_events(tmp_path, monkeypatch):
    """WebUI stream run start/finish markers should be metadata-only.

    A streaming run can be initiated by hostile user text, attachments, or state.
    The durable Capy progress event must keep only the safe run lifecycle and a
    sanitized stream-scoped run id, not the raw prompt or any request payload.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    started = streaming._record_streaming_progress_event(
        event_type="run.started",
        stream_id="stream-run-123",
        preview="SECRET_VALUE_DO_NOT_LEAK raw_prompt <script>ignore previous instructions</script>",
        args={"api_key": "bearer placeholder", "renderer": "<script>alert(1)</script>"},
    )
    completed = streaming._record_streaming_progress_event(
        event_type="run.completed",
        stream_id="stream-run-123",
        function_result={"source": "raw assistant output SECRET_VALUE_DO_NOT_LEAK"},
    )
    failed = streaming._record_streaming_progress_event(
        event_type="run.failed",
        stream_id="SECRET_VALUE_DO_NOT_LEAK<script>source.data ignore previous instructions",
        preview="raw exception with api_auth bearer placeholder",
    )
    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)

    assert started["event_type"] == "run.started"
    assert started["family"] == "run"
    assert started["run_id"] == "webui.run:stream-run-123"
    assert completed["event_type"] == "run.completed"
    assert completed["run_id"] == "webui.run:stream-run-123"
    assert failed["event_type"] == "run.failed"
    assert failed["family"] == "run"
    assert failed["run_id"] == "webui.run:stream"
    assert status["recent_family_counts"]["run"] == 3
    assert status["active_run_count"] == 0
    for unsafe in UNSAFE_FIXTURES + ("api_key", "api_auth", "bearer placeholder", "raw assistant output"):
        assert unsafe.lower() not in serialized.lower()


def test_run_agent_streaming_records_run_started_and_completed_progress(tmp_path, monkeypatch):
    """The real WebUI stream worker should emit run lifecycle progress.

    Tool/subagent/delta producers cover work inside the run; product progress
    also needs the stream-level run.started/run.completed envelope so autonomous
    WebUI work appears as a bounded active run without leaking user text.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    saved_snapshots = []

    class FakeSession:
        def __init__(self):
            self.session_id = "run_lifecycle_session"
            self.title = "Run lifecycle"
            self.workspace = str(tmp_path)
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = "stream-run-lifecycle"
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True
            self.path = str(tmp_path / "ephemeral-run-lifecycle-session.json")

        def save(self, *args, **kwargs):
            saved_snapshots.append(kwargs)

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    class SimpleAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
        ):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = fake_session.active_stream_id
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    setattr(fake_runtime_module, "resolve_runtime_provider", lambda requested=None: {
        "provider": "openai",
        "base_url": None,
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    setattr(fake_hermes_cli, "runtime_provider", fake_runtime_module)
    fake_hermes_state = types.ModuleType("hermes_state")
    setattr(fake_hermes_state, "SessionDB", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: SimpleAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("gpt-test", "openai", None))
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])

    streaming.STREAMS[fake_stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="SECRET_VALUE_DO_NOT_LEAK raw_prompt <script>ignore previous instructions</script>",
            model="gpt-test",
            workspace=str(tmp_path),
            stream_id=fake_stream_id,
            ephemeral=True,
        )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)

    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)
    event_types = [event["event_type"] for event in status["recent_events"]]

    assert saved_snapshots, "stream worker should have reached the agent run"
    assert event_types.count("run.started") == 1
    assert event_types.count("run.completed") == 1
    assert "run.failed" not in event_types
    assert status["recent_family_counts"] == {"run": 2}
    assert status["active_run_count"] == 0
    assert {event["run_id"] for event in status["recent_events"]} == {"webui.run:stream-run-lifecycle"}
    for unsafe in UNSAFE_FIXTURES + ("sk-test", "raw_prompt"):
        assert unsafe.lower() not in serialized.lower()


def test_run_agent_streaming_records_failed_progress_for_stale_writeback(tmp_path, monkeypatch):
    """Stale stream workers should close their run progress before returning.

    A canceled/replaced browser stream can become stale after the agent returns.
    The worker must not leave a durable run.started without a terminal event,
    because the product progress card counts unterminated runs as active.
    """
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    class FakeSession:
        def __init__(self):
            self.session_id = "stale_writeback_session"
            self.title = "Stale writeback"
            self.workspace = str(tmp_path)
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = []
            self.active_stream_id = "newer-stream-wins"
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.llm_title_generated = True
            self.path = str(tmp_path / "stale-writeback-session.json")

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {"session_id": self.session_id, "title": self.title}

    class SimpleAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            clarify_callback=None,
            tool_start_callback=None,
            tool_complete_callback=None,
        ):
            self.session_id = session_id
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            return {
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "done"},
                ]
            }

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    fake_stream_id = "stale-stream"
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    setattr(fake_runtime_module, "resolve_runtime_provider", lambda requested=None: {
        "provider": "openai",
        "base_url": None,
        "api_key": "sk-test",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    })
    fake_hermes_cli = types.ModuleType("hermes_cli")
    setattr(fake_hermes_cli, "runtime_provider", fake_runtime_module)
    fake_hermes_state = types.ModuleType("hermes_state")
    setattr(fake_hermes_state, "SessionDB", lambda: None)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: SimpleAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda _model: ("gpt-test", "openai", None))
    monkeypatch.setattr(streaming, "_prewarm_skill_tool_modules", lambda: None)
    monkeypatch.setattr(streaming, "append_turn_journal_event_for_stream", lambda *args, **kwargs: None)
    monkeypatch.setattr("api.config.get_config", lambda: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda _cfg: [])

    streaming.STREAMS[fake_stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=fake_session.session_id,
            msg_text="SECRET_VALUE_DO_NOT_LEAK raw_prompt <script>ignore previous instructions</script>",
            model="gpt-test",
            workspace=str(tmp_path),
            stream_id=fake_stream_id,
            ephemeral=False,
        )
    finally:
        streaming.STREAMS.pop(fake_stream_id, None)

    status = capy_progress.progress_status()
    raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
    serialized = raw_log + "\n" + str(status)
    event_types = [event["event_type"] for event in status["recent_events"]]

    assert event_types.count("run.started") == 1
    assert event_types.count("run.failed") == 1
    assert "run.completed" not in event_types
    assert status["recent_family_counts"] == {"run": 2}
    assert status["active_run_count"] == 0
    assert {event["run_id"] for event in status["recent_events"]} == {"webui.run:stale-stream"}
    for unsafe in UNSAFE_FIXTURES + ("sk-test", "raw_prompt"):
        assert unsafe.lower() not in serialized.lower()


def test_streaming_run_terminal_retry_after_recording_failure(tmp_path, monkeypatch):
    """A transient progress-log failure must not permanently dedupe the terminal event."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    real_record_progress_event = capy_progress.record_progress_event
    calls = []

    def flaky_record_progress_event(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("temporary progress log failure")
        return real_record_progress_event(payload)

    monkeypatch.setattr(capy_progress, "record_progress_event", flaky_record_progress_event)

    failed_once = streaming._record_streaming_progress_event(event_type="run.failed", stream_id="retry-terminal-stream")
    retried = streaming._record_streaming_progress_event(event_type="run.failed", stream_id="retry-terminal-stream")
    status = capy_progress.progress_status()

    assert failed_once["stored"] is False
    assert retried["stored"] is True
    assert [event["event_type"] for event in status["recent_events"]] == ["run.failed"]
    assert status["active_run_count"] == 0


def test_streaming_run_terminal_dedupe_uses_lock():
    """Terminal run-event dedupe must be atomic across cancel and worker threads."""
    src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(encoding="utf-8")
    lock_decl_idx = src.find("_STREAMING_RUN_TERMINAL_PROGRESS_LOCK")
    dedupe_set_idx = src.find("_STREAMING_RUN_TERMINAL_PROGRESS_SEEN")
    branch_idx = src.find('elif safe_event_type in {"run.completed", "run.failed"}:')
    lock_use_idx = src.find("with _STREAMING_RUN_TERMINAL_PROGRESS_LOCK:", branch_idx)
    record_idx = src.find("result = record_progress_event", branch_idx)

    assert lock_decl_idx != -1 and lock_decl_idx < dedupe_set_idx, "run terminal dedupe lock must be declared with the dedupe set"
    assert lock_use_idx != -1 and branch_idx < lock_use_idx < record_idx, (
        "run terminal dedupe check/add must happen under the lock before persistence"
    )


def test_cancel_stream_records_failed_progress_immediately(tmp_path, monkeypatch):
    """User cancellation should close active run progress even if worker is blocked."""
    monkeypatch.setenv("CAPY_PROGRESS_LOG", str(tmp_path / "progress" / "events.jsonl"))

    from api import capy_progress, streaming

    class InterruptibleAgent:
        session_id = None

        def __init__(self):
            self.interruptions = []

        def interrupt(self, message):
            self.interruptions.append(message)

    stream_id = "cancel-progress-stream"
    agent = InterruptibleAgent()
    streaming._record_streaming_progress_event(event_type="run.started", stream_id=stream_id)
    streaming.STREAMS[stream_id] = queue.Queue()
    streaming.CANCEL_FLAGS[stream_id] = streaming.threading.Event()
    streaming.AGENT_INSTANCES[stream_id] = agent
    streaming.STREAM_PARTIAL_TEXT[stream_id] = "SECRET_VALUE_DO_NOT_LEAK raw_prompt <script>ignore previous instructions</script>"
    try:
        assert streaming.cancel_stream(stream_id) is True
        status = capy_progress.progress_status()
        raw_log = Path(tmp_path / "progress" / "events.jsonl").read_text(encoding="utf-8")
        serialized = raw_log + "\n" + str(status)
        event_types = [event["event_type"] for event in status["recent_events"]]

        assert agent.interruptions == ["Cancelled by user"]
        assert event_types.count("run.started") == 1
        assert event_types.count("run.failed") == 1
        assert status["recent_family_counts"] == {"run": 2}
        assert status["active_run_count"] == 0
        assert {event["run_id"] for event in status["recent_events"]} == {"webui.run:cancel-progress-stream"}
        for unsafe in UNSAFE_FIXTURES:
            assert unsafe.lower() not in serialized.lower()
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.CANCEL_FLAGS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        streaming.STREAM_PARTIAL_TEXT.pop(stream_id, None)


def test_run_agent_streaming_cancel_returns_close_run_progress_before_returning():
    """Cancel returns must terminate Capy run progress before leaving worker."""
    src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text(encoding="utf-8")
    cancel_before_start_idx = src.find("if cancel_event.is_set():")
    cancel_before_start_terminal_idx = src.find('_record_stream_run_terminal("run.failed")', cancel_before_start_idx)
    cancel_before_start_return_idx = src.find("return", cancel_before_start_idx)
    cancel_during_init_idx = src.find("if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():")
    cancel_during_init_terminal_idx = src.find('_record_stream_run_terminal("run.failed")', cancel_during_init_idx)
    cancel_during_init_return_idx = src.find("return", cancel_during_init_idx)

    assert cancel_before_start_idx != -1, "pre-flight cancel branch not found"
    assert cancel_before_start_terminal_idx != -1 and cancel_before_start_idx < cancel_before_start_terminal_idx < cancel_before_start_return_idx, (
        "pre-flight cancel branch must record run.failed before returning"
    )
    assert cancel_during_init_idx != -1, "cancel-during-agent-init branch not found"
    assert cancel_during_init_terminal_idx != -1 and cancel_during_init_idx < cancel_during_init_terminal_idx < cancel_during_init_return_idx, (
        "cancel-during-agent-init branch must record run.failed before returning"
    )


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
