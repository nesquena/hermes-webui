import importlib
import queue


def test_runtime_adapter_interface_and_legacy_journal_methods_exist():
    runtime = importlib.import_module("api.runtime_adapter")

    required = (
        "start_run",
        "observe_run",
        "get_run",
        "cancel_run",
        "respond_approval",
        "respond_clarify",
    )
    for name in required:
        assert hasattr(runtime.RuntimeAdapter, name)
        assert hasattr(runtime.LegacyJournalRuntimeAdapter, name)

    assert runtime.runtime_adapter_mode({}) == "legacy-direct"
    assert runtime.runtime_adapter_enabled({}) is False
    assert runtime.runtime_adapter_mode({"HERMES_WEBUI_RUNTIME_ADAPTER": "legacy-journal"}) == "legacy-journal"
    assert runtime.runtime_adapter_enabled({"HERMES_WEBUI_RUNTIME_ADAPTER": "legacy-journal"}) is True
    assert runtime.runtime_adapter_mode({"HERMES_WEBUI_RUNTIME_ADAPTER": "sidecar"}) == "legacy-direct"


def test_legacy_journal_adapter_start_run_delegates_without_owning_runtime_state():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []

    def start_delegate(request):
        calls.append(request)
        return {
            "stream_id": "stream-123",
            "session_id": request.session_id,
            "status": "started",
            "active_controls": ["cancel"],
        }

    adapter = runtime.LegacyJournalRuntimeAdapter(start_run_delegate=start_delegate)
    request = runtime.StartRunRequest(
        session_id="s1",
        message="hello",
        attachments=[{"name": "a.txt"}],
        workspace="/tmp/work",
        profile="default",
        provider="openai-codex",
        model="gpt-5.5",
        toolsets=["terminal"],
        source="webui",
        metadata={"k": "v"},
    )

    result = adapter.start_run(request)

    assert calls == [request]
    assert result.session_id == "s1"
    assert result.stream_id == "stream-123"
    assert result.run_id == "stream-123"
    assert result.status == "started"
    assert result.active_controls == ["cancel"]


def test_legacy_journal_adapter_observe_and_get_run_use_journal_and_live_state(tmp_path):
    runtime = importlib.import_module("api.runtime_adapter")
    run_journal = importlib.import_module("api.run_journal")

    run_journal.append_run_event("s1", "r1", "token", {"text": "a"}, session_dir=tmp_path)
    run_journal.append_run_event("s1", "r1", "done", {"ok": True}, session_dir=tmp_path)

    adapter = runtime.LegacyJournalRuntimeAdapter(
        session_dir=tmp_path,
        live_stream_lookup=lambda run_id: run_id == "live-run",
    )

    replay = adapter.observe_run("r1", cursor="0")
    assert [event["type"] for event in replay.events] == ["token", "done"]
    assert replay.last_event_id == "r1:2"

    completed = adapter.get_run("r1")
    assert completed.run_id == "r1"
    assert completed.session_id == "s1"
    assert completed.status == "completed"
    assert completed.terminal_state == "completed"
    assert completed.last_event_id == "r1:2"

    live = adapter.get_run("live-run")
    assert live.run_id == "live-run"
    assert live.status == "running"
    assert live.active_controls == ["cancel"]


def test_legacy_journal_adapter_controls_delegate_to_existing_handlers():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []
    adapter = runtime.LegacyJournalRuntimeAdapter(
        cancel_delegate=lambda run_id: calls.append(("cancel", run_id)) or True,
        approval_delegate=lambda run_id, approval_id, choice: calls.append(("approval", run_id, approval_id, choice)) or True,
        clarify_delegate=lambda run_id, clarify_id, response: calls.append(("clarify", run_id, clarify_id, response)) or True,
    )

    assert adapter.cancel_run("r1").accepted is True
    assert adapter.respond_approval("r1", "a1", "once").accepted is True
    assert adapter.respond_clarify("r1", "c1", "answer").accepted is True
    assert calls == [
        ("cancel", "r1"),
        ("approval", "r1", "a1", "once"),
        ("clarify", "r1", "c1", "answer"),
    ]


def test_chat_start_route_selects_adapter_only_when_flag_enabled():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    start_idx = src.index("def _handle_chat_start")
    start_body = src[start_idx:src.index("def _resolve_chat_workspace_with_recovery", start_idx)]

    assert "runtime_adapter_enabled()" in start_body
    assert "LegacyJournalRuntimeAdapter" in start_body
    assert "_start_chat_stream_for_session(" in start_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in start_body, "route should use runtime_adapter_enabled(), not inline env checks"


def test_chat_start_adapter_path_preserves_legacy_response_shape():
    """The RuntimeAdapter seam must be invisible to /api/chat/start callers.

    The adapter can use run_id/status/controls internally, but the flagged
    route must not add fields that the legacy-direct response does not expose.
    """
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    branch_start = src.index("if runtime_adapter_enabled():")
    branch_end = src.index("else:", branch_start)
    adapter_branch = src[branch_start:branch_end]

    assert 'response.setdefault("stream_id", result.stream_id)' in adapter_branch
    assert 'response.setdefault("session_id", result.session_id)' in adapter_branch
    assert 'response.setdefault("run_id", result.run_id)' not in adapter_branch
    assert 'response.setdefault("status", result.status)' not in adapter_branch
    assert 'response.setdefault("active_controls", result.active_controls)' not in adapter_branch
