import importlib
import io
import queue

from tests.conftest import requires_agent_modules


def test_runtime_adapter_interface_and_legacy_journal_methods_exist():
    runtime = importlib.import_module("api.runtime_adapter")

    required = (
        "start_run",
        "observe_run",
        "get_run",
        "cancel_run",
        "respond_approval",
        "respond_clarify",
        "queue_message",
        "update_goal",
    )
    for name in required:
        assert hasattr(runtime.RuntimeAdapter, name)
        assert hasattr(runtime.LegacyJournalRuntimeAdapter, name)
        assert hasattr(runtime.RunnerRuntimeAdapter, name)

    assert runtime.runtime_adapter_mode({}) == "legacy-direct"
    assert runtime.runtime_adapter_enabled({}) is False
    assert runtime.runtime_adapter_mode({"HERMES_WEBUI_RUNTIME_ADAPTER": "legacy-journal"}) == "legacy-journal"
    assert runtime.runtime_adapter_enabled({"HERMES_WEBUI_RUNTIME_ADAPTER": "legacy-journal"}) is True
    assert runtime.runtime_adapter_mode({"HERMES_WEBUI_RUNTIME_ADAPTER": "runner-local"}) == "runner-local"
    assert runtime.runtime_adapter_runner_enabled({"HERMES_WEBUI_RUNTIME_ADAPTER": "runner-local"}) is True
    assert runtime.runtime_adapter_mode({"HERMES_WEBUI_RUNTIME_ADAPTER": "sidecar"}) == "legacy-direct"


def test_runtime_adapter_factory_selects_only_explicit_default_off_modes():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []

    class FakeRunnerClient:
        pass

    def legacy_factory():
        calls.append("legacy")
        return runtime.LegacyJournalRuntimeAdapter(start_run_delegate=lambda request: {"stream_id": "s"})

    def runner_factory():
        calls.append("runner")
        return FakeRunnerClient()

    assert runtime.build_runtime_adapter(environ={}) is None

    legacy = runtime.build_runtime_adapter(
        environ={"HERMES_WEBUI_RUNTIME_ADAPTER": "legacy-journal"},
        legacy_adapter_factory=legacy_factory,
        runner_client_factory=runner_factory,
    )
    assert isinstance(legacy, runtime.LegacyJournalRuntimeAdapter)

    runner = runtime.build_runtime_adapter(
        environ={"HERMES_WEBUI_RUNTIME_ADAPTER": "runner-local"},
        legacy_adapter_factory=legacy_factory,
        runner_client_factory=runner_factory,
    )
    assert isinstance(runner, runtime.RunnerRuntimeAdapter)
    assert calls == ["legacy", "runner"]


def test_runner_local_factory_requires_injected_client_and_does_not_fallback_to_legacy():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []

    def legacy_factory():
        calls.append("legacy")
        return runtime.LegacyJournalRuntimeAdapter(start_run_delegate=lambda request: {"stream_id": "s"})

    try:
        runtime.build_runtime_adapter(
            environ={"HERMES_WEBUI_RUNTIME_ADAPTER": "runner-local"},
            legacy_adapter_factory=legacy_factory,
        )
    except NotImplementedError as exc:
        assert "runner client factory" in str(exc)
    else:
        raise AssertionError("runner-local must require an injected runner client factory")

    assert calls == []


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


def test_legacy_journal_adapter_queue_and_goal_delegate_without_owning_runtime_state():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []
    adapter = runtime.LegacyJournalRuntimeAdapter(
        queue_delegate=lambda run_id, message, mode: calls.append(("queue", run_id, message, mode)) or True,
        goal_delegate=lambda session_id, action, text: calls.append(("goal", session_id, action, text)) or {
            "ok": True,
            "action": action,
            "message": "Goal updated.",
        },
    )

    queued = adapter.queue_message("r1", "follow up", mode="queue")
    goal = adapter.update_goal("s1", "set", "finish the task")

    assert queued.accepted is True
    assert goal.accepted is True
    assert goal.payload["action"] == "set"
    assert calls == [
        ("queue", "r1", "follow up", "queue"),
        ("goal", "s1", "set", "finish the task"),
    ]


def test_legacy_journal_adapter_cancel_returns_bounded_not_active_status():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []
    adapter = runtime.LegacyJournalRuntimeAdapter(
        cancel_delegate=lambda run_id: calls.append(run_id) or False,
    )

    result = adapter.cancel_run("already-finished-run")

    assert calls == ["already-finished-run"]
    assert result.accepted is False
    assert result.status == "not-active"
    assert result.safe_message == "Legacy control did not accept the request."


def test_legacy_journal_adapter_approval_and_clarify_return_bounded_not_active_status():
    runtime = importlib.import_module("api.runtime_adapter")
    calls = []
    adapter = runtime.LegacyJournalRuntimeAdapter(
        approval_delegate=lambda run_id, approval_id, choice: calls.append(("approval", run_id, approval_id, choice)) or False,
        clarify_delegate=lambda run_id, clarify_id, response: calls.append(("clarify", run_id, clarify_id, response)) or False,
    )

    approval = adapter.respond_approval("already-finished-run", "stale-approval", "deny")
    clarify = adapter.respond_clarify("already-finished-run", "stale-clarify", "answer")

    assert calls == [
        ("approval", "already-finished-run", "stale-approval", "deny"),
        ("clarify", "already-finished-run", "stale-clarify", "answer"),
    ]
    assert approval.accepted is False
    assert approval.status == "not-active"
    assert clarify.accepted is False
    assert clarify.status == "not-active"


def test_legacy_journal_adapter_queue_and_goal_return_bounded_statuses():
    runtime = importlib.import_module("api.runtime_adapter")
    adapter = runtime.LegacyJournalRuntimeAdapter(
        queue_delegate=lambda run_id, message, mode: False,
        goal_delegate=lambda session_id, action, text: {
            "ok": False,
            "action": action,
            "error": "agent_running",
            "message": "Agent is running.",
        },
    )

    queued = adapter.queue_message("already-finished-run", "follow up")
    goal = adapter.update_goal("s1", "set", "new goal")

    assert queued.accepted is False
    assert queued.status == "not-active"
    assert goal.accepted is False
    assert goal.status == "set"
    assert goal.safe_message == "Agent is running."
    assert goal.payload["error"] == "agent_running"


def test_chat_cancel_route_uses_adapter_only_when_flag_enabled():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    cancel_idx = src.index('if parsed.path == "/api/chat/cancel":')
    cancel_body = src[cancel_idx:src.index('if parsed.path == "/api/chat/stream":', cancel_idx)]

    assert "runtime_adapter_enabled()" in cancel_body
    assert "LegacyJournalRuntimeAdapter(cancel_delegate=cancel_stream)" in cancel_body
    assert "adapter.cancel_run(stream_id).accepted" in cancel_body
    assert "else:\n            cancelled = cancel_stream(stream_id)" in cancel_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in cancel_body, "route should use runtime_adapter_enabled(), not inline env checks"


def test_approval_and_clarify_routes_use_adapter_only_when_flag_enabled():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")

    approval_idx = src.index("def _handle_approval_respond")
    approval_body = src[approval_idx:src.index("def _resolve_clarify_legacy", approval_idx)]
    clarify_idx = src.index("def _handle_clarify_respond")
    clarify_body = src[clarify_idx:src.index("class _ManualCompressionMemoryHandler", clarify_idx)]

    assert "runtime_adapter_enabled()" in approval_body
    assert "LegacyJournalRuntimeAdapter(approval_delegate=_resolve_approval_legacy)" in approval_body
    assert "adapter.respond_approval(sid, approval_id, choice).accepted" in approval_body
    assert "else:\n        ok = _resolve_approval_legacy(sid, approval_id, choice)" in approval_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in approval_body

    assert "runtime_adapter_enabled()" in clarify_body
    assert "LegacyJournalRuntimeAdapter(clarify_delegate=_resolve_clarify_legacy)" in clarify_body
    assert "adapter.respond_clarify(sid, clarify_id, response).accepted" in clarify_body
    assert "else:\n        ok = _resolve_clarify_legacy(sid, clarify_id, response)" in clarify_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in clarify_body


def test_goal_route_uses_adapter_only_when_flag_enabled():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    goal_idx = src.index("def _handle_goal_command")
    goal_body = src[goal_idx:src.index("def _handle_chat_start", goal_idx)]

    assert "runtime_adapter_enabled()" in goal_body
    assert "LegacyJournalRuntimeAdapter(goal_delegate=_legacy_goal_update)" in goal_body
    assert "goal_adapter_action = _runtime_adapter_goal_action(goal_args)" in goal_body
    assert "adapter.update_goal(" in goal_body
    assert "goal_adapter_action," in goal_body
    assert "payload = dict(control_result.payload)" in goal_body
    assert "else:\n        payload = _legacy_goal_update" in goal_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in goal_body


def test_goal_adapter_action_is_bounded_to_slice3c_actions():
    routes = importlib.import_module("api.routes")

    assert routes._runtime_adapter_goal_action("") == "status"
    assert routes._runtime_adapter_goal_action("status") == "status"
    assert routes._runtime_adapter_goal_action("pause") == "pause"
    assert routes._runtime_adapter_goal_action("resume") == "resume"
    assert routes._runtime_adapter_goal_action("clear") == "clear"
    assert routes._runtime_adapter_goal_action("stop") == "clear"
    assert routes._runtime_adapter_goal_action("done") == "clear"
    assert routes._runtime_adapter_goal_action("ship #1925") == "set"


def test_approval_respond_does_not_fallback_to_oldest_when_explicit_id_is_stale():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    helper_idx = src.index("def _resolve_approval_legacy")
    helper_body = src[helper_idx:src.index("def _handle_approval_respond", helper_idx)]

    assert "A stale explicit id must not accidentally approve" in helper_body
    assert "if found_target or not approval_id:" in helper_body
    stale_branch = helper_body[helper_body.index("else:", helper_body.index("for i, entry")):helper_body.index("else:\n                pending = queue.pop(0)")]
    assert "pending = None" in stale_branch
    assert "queue.pop(0)" not in stale_branch


def test_approval_respond_peeks_gateway_queues_when_pending_empty() -> None:
    """When _pending has no matching entry but _gateway_queues does, the
    helper should extract pattern_keys from the gateway queue and call
    approve_session even though pending is None.
    """
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    helper_idx = src.index("def _resolve_approval_legacy")
    helper_body = src[helper_idx:src.index("def _handle_approval_respond", helper_idx)]

    assert "_gateway_queues" in helper_body, (
        "_resolve_approval_legacy must reference _gateway_queues "
        "to read pattern_keys when _pending is empty"
    )
    assert "gateway_keys" in helper_body, (
        "Must extract pattern_keys from _gateway_queues into a gateway_keys variable"
    )
    assert "approve_session" in helper_body[helper_body.index("all_keys"):], (
        "Must call approve_session for keys extracted from _gateway_queues"
    )


@requires_agent_modules
def test_approval_respond_approves_from_gateway_queues_when_pending_empty() -> None:
    """Verify _resolve_approval_legacy peeks into _gateway_queues for
    pattern_keys when _pending has no matching entry, and calls
    approve_session() even though pending is None (the real streaming case).
    """
    import threading
    from api.routes import _resolve_approval_legacy

    routes = importlib.import_module("api.routes")
    approval_mod = importlib.import_module("tools.approval")

    test_sid = "__test_gateway_approval_sid__"
    test_key = "__test_pattern_key__"

    # 1. Ensure _pending is empty for this sid
    with approval_mod._lock:
        approval_mod._pending.pop(test_sid, None)

    # 2. Populate _gateway_queues with a real entry
    entry = approval_mod._ApprovalEntry({
        "command": "test_cmd",
        "pattern_key": test_key,
        "pattern_keys": [test_key],
        "description": "test dangerous cmd",
    })
    with approval_mod._lock:
        approval_mod._gateway_queues.setdefault(test_sid, []).append(entry)

    try:
        # 3. Run the helper with empty _pending but populated _gateway_queues
        result = _resolve_approval_legacy(test_sid, "", "session")

        # 4. Verify approve_session was called (is_approved must return True)
        assert approval_mod.is_approved(test_sid, test_key), (
            "approve_session should have been called for the pattern_key "
            "extracted from _gateway_queues"
        )
        assert result is True, (
            "_resolve_approval_legacy should return True when it finds "
            "and resolves the gateway entry"
        )
    finally:
        # 5. Cleanup
        with approval_mod._lock:
            approval_mod._gateway_queues.pop(test_sid, None)
            approval_mod._session_approved.pop(test_sid, None)
            approval_mod._pending.pop(test_sid, None)


def test_chat_start_route_selects_adapter_only_when_flag_enabled():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    # NOTE: T-2979-fix factored the adapter-selection block out of
    # _handle_chat_start into the shared `_start_run` helper (used by both
    # /api/chat/start and start_session_turn — Q-2979-A2 / Copilot
    # r3305864087/r3305864173). Scan the helper body for the contract; the
    # route body only needs to delegate to it.
    helper_idx = src.index("def _start_run(")
    helper_body = src[helper_idx:src.index("def start_session_turn(", helper_idx)]
    start_idx = src.index("def _handle_chat_start")
    start_body = src[start_idx:src.index("def _resolve_chat_workspace_with_recovery", start_idx)]

    # Contract enforced in the shared helper:
    assert "runtime_adapter_enabled()" in helper_body
    assert "runtime_adapter_runner_enabled()" in helper_body
    assert "build_runtime_adapter(" in helper_body
    assert "legacy_adapter_factory=_legacy_adapter_factory" in helper_body
    assert "runner_client_factory=_runtime_runner_client_factory" in helper_body
    assert "LegacyJournalRuntimeAdapter" in helper_body
    assert "_start_chat_stream_for_session(" in helper_body
    # Route delegates to the helper instead of inlining env checks:
    assert "_start_run(" in start_body
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in start_body, "route should use runtime_adapter_enabled() via _start_run, not inline env checks"
    assert "HERMES_WEBUI_RUNTIME_ADAPTER" not in helper_body, "helper should use runtime_adapter_enabled(), not inline env checks"


def test_runner_local_chat_start_selection_does_not_fallback_to_legacy():
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    # See note in test_chat_start_route_selects_adapter_only_when_flag_enabled
    # — adapter selection moved into the shared `_start_run` helper.
    helper_idx = src.index("def _start_run(")
    helper_body = src[helper_idx:src.index("def start_session_turn(", helper_idx)]
    start_idx = src.index("def _handle_chat_start")
    start_body = src[start_idx:src.index("def _resolve_chat_workspace_with_recovery", start_idx)]

    flag_branch = "if runtime_adapter_enabled() or runtime_adapter_runner_enabled():"
    assert flag_branch in helper_body
    assert "except NotImplementedError as exc:" in helper_body
    # The helper returns {"error": str(exc), "_status": 501}; the route then
    # maps that onto the legacy j(handler, {...}, status=501) response shape
    # to keep the public contract identical to pre-refactor behavior.
    assert 'return {"error": str(exc), "_status": 501}' in helper_body
    assert 'return j(handler, {"error": response["error"]}, status=501)' in start_body
    assert "runner-local chat backend is not configured" in src
    # The adapter branch inside the helper still calls _start_chat_stream_for_session
    # through the _legacy_start_run delegate before the trailing legacy-direct
    # fallthrough (the function returns the legacy direct call when the flag
    # is off — no `else:` keyword anymore since each branch returns).
    adapter_branch_start = helper_body.index(flag_branch)
    # Slice up to the final (post-flag) return _start_chat_stream_for_session
    # — there are two occurrences: one inside _legacy_start_run, one at the
    # fallthrough; we want both inside the branch slice.
    fallthrough = helper_body.rindex("return _start_chat_stream_for_session(")
    adapter_branch = helper_body[adapter_branch_start:fallthrough]
    assert "_start_chat_stream_for_session(" in adapter_branch, "legacy-journal delegate should still call the legacy path"
    assert "runtime_adapter_runner_enabled()" in adapter_branch or "runtime_adapter_runner_enabled()" in helper_body


def test_chat_start_adapter_path_preserves_legacy_response_shape():
    """The RuntimeAdapter seam must be invisible to /api/chat/start callers.

    The adapter can use run_id/status/controls internally, but the flagged
    route must not add fields that the legacy-direct response does not expose.
    """
    routes = importlib.import_module("api.routes")
    src = (routes.Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
    helper_idx = src.index("def _chat_start_response_from_run_start")
    helper_body = src[helper_idx:src.index("def _runtime_adapter_goal_action", helper_idx)]

    assert '"stream_id",' in helper_body
    assert '"session_id",' in helper_body
    assert 'response.setdefault("stream_id", result.stream_id)' in helper_body
    assert 'response.setdefault("session_id", result.session_id)' in helper_body
    assert '"run_id",' not in helper_body
    assert '"status",' not in helper_body
    assert '"active_controls",' not in helper_body


def test_chat_start_response_from_run_start_filters_adapter_internal_fields():
    routes = importlib.import_module("api.routes")
    runtime = importlib.import_module("api.runtime_adapter")

    response = routes._chat_start_response_from_run_start(
        runtime.RunStartResult(
            run_id="runner-internal-1",
            session_id="s1",
            stream_id="runner-stream-1",
            status="running",
            active_controls=["cancel"],
            payload={
                "stream_id": "runner-stream-1",
                "session_id": "s1",
                "pending_started_at": 123.0,
                "turn_id": "turn-1",
                "title": "Demo",
                "effective_model": "gpt-5.5",
                "effective_model_provider": "openai-codex",
                "run_id": "runner-internal-1",
                "status": "running",
                "active_controls": ["cancel"],
            },
        )
    )

    assert response == {
        "stream_id": "runner-stream-1",
        "session_id": "s1",
        "pending_started_at": 123.0,
        "turn_id": "turn-1",
        "title": "Demo",
        "effective_model": "gpt-5.5",
        "effective_model_provider": "openai-codex",
    }


def test_rfc_distinguishes_goal_routing_from_queue_route_staging():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#2544 shipped the first Slice 3c implementation" in rfc
    assert "#2560 shipped the queue-staging clarification" in rfc
    assert "route now uses `RuntimeAdapter.update_goal(...)`" in rfc
    assert "`queue_message(...)` as a staged protocol method only" in rfc
    assert "no new server-side queue endpoint" in rfc
    assert "no server-side queue endpoint or queue\n  scheduler should be added merely for adapter symmetry" in rfc


def test_rfc_defines_slice4_runner_contract_before_runner_code():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4a: Runner contract gate" in rfc
    assert "docs/test contract PR before any\nrunner code lands" in rfc
    assert "feature-flagged, default-off" in rfc
    assert "The runner, not the main WebUI request process, owns" in rfc
    assert "restart only\n   `hermes-webui.service`" in rfc
    assert "profile,\n   workspace, attachments, model/provider, toolset, and source metadata" in rfc
    assert "no removal of the legacy in-process backend" in rfc
    assert "no default-on runner mode" in rfc
    assert "#### Slice 4b: Runner adapter client facade" in rfc
    assert "Status as of 2026-05-20: shipped in v0.51.94 via #2599" in rfc
    assert "delegates to an injected runner client" in rfc
    assert "without relying on process-local `STREAMS`" in rfc


def test_rfc_defines_slice4c_runner_backend_harness_gate():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4c: Feature-flagged runner backend and restart/reattach harness" in rfc
    assert "Status as of 2026-05-21: shipped in v0.51.105 via #2696" in rfc
    assert "`HERMES_WEBUI_RUNTIME_ADAPTER=runner-local`" in rfc
    assert "`legacy-direct` remains the default" in rfc
    assert "No route-shape drift" in rfc
    assert "Restart/reattach harness" in rfc
    assert "discard the first WebUI adapter instance" in rfc
    assert "No runtime-surrogate globals" in rfc
    assert "no live chat route switch to the runner backend before the restart/reattach" in rfc


def test_rfc_defines_slice4d_supervised_runner_route_gate():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4d: Supervised runner backend route gate" in rfc
    assert "Status as of 2026-05-23: shipped in v0.51.108 via #2744" in rfc
    assert "After `runner-local` selection exists" in rfc
    assert "route-selection harness before live\nbrowser chat can use it" in rfc
    assert "Route remains default-off" in rfc
    assert "Restart/reattach harness proves ownership moved" in rfc
    assert "No public response-shape drift" in rfc
    assert "No runtime-surrogate globals" in rfc
    assert "Explicit context payloads" in rfc
    assert "active-run discovery, session-to-run lookup, command capability\n  metadata, artifact events, and provider/tool routing" in rfc
    assert "WebUI remains the rich workbench while\n  only execution ownership moves" in rfc


def test_rfc_defines_slice4e_runner_chat_start_route_selection_harness():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4e: Default-off runner chat-start route-selection harness" in rfc
    assert "Status as of 2026-05-24: shipped in v0.51.129 via #2794" in rfc
    assert "route `/api/chat/start` through `build_runtime_adapter(...)`" in rfc
    assert "`legacy-direct` stays default" in rfc
    assert "`legacy-journal`\ncontinues to delegate to the legacy in-process stream path" in rfc
    assert "`runner-local`\ndoes not silently fall back to legacy" in rfc
    assert "return a bounded not-configured error for `runner-local`" in rfc
    assert "`run_id`, `status`, and\n   `active_controls` remain internal" in rfc
    assert "no supervised runner process yet" in rfc


def test_rfc_defines_slice4f_supervised_local_runner_client_gate():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4f: Supervised local runner client backend gate" in rfc
    assert "Status as of 2026-05-31: shipped in v0.51.188 via #3073 / #3274" in rfc
    assert "The client\ntransport is now implemented behind `HERMES_WEBUI_RUNNER_BASE_URL`" in rfc
    assert "`HttpRunnerClient` rejects non-`http(s)` base URL schemes" in rfc
    assert "uses an opener that\ndoes not follow redirects" in rfc
    assert "the configured\nrunner must emit events that are already compatible with the browser SSE event\nnames/payloads" in rfc
    assert "a later runner-owned normalization layer must translate\nHermes runtime families such as `token.delta`, `tool.started`, and `done`" in rfc
    assert "After the configured runner-client boundary ships" in rfc
    assert "configured external endpoint or fake-runner fixture" in rfc
    assert "cancel as the first required live control" in rfc
    assert "501 path replaced only when configured" in rfc
    assert "Restart/reattach proves ownership moved" in rfc
    assert "No runtime-surrogate globals" in rfc
    assert "Successful chat-start responses remain limited\n   to the legacy-compatible field whitelist" in rfc
    assert "Unsupported runner controls return safe\n   `unsupported`, `not-active`, or `conflict` results" in rfc
    assert "no permanent WebUI-owned active-run discovery cache" in rfc


def test_rfc_defines_slice4g_supervised_local_runner_process_gate():
    routes = importlib.import_module("api.routes")
    rfc = (routes.Path(__file__).parent.parent / "docs" / "rfcs" / "hermes-run-adapter-contract.md").read_text(encoding="utf-8")

    assert "#### Slice 4g: Supervised local runner process harness gate" in rfc
    assert "After #3073 / #3274, WebUI has an explicit configured-runner HTTP client" in rfc
    assert "still does not ship the supervised runner process itself" in rfc
    assert "own\n`AIAgent` execution outside the main WebUI request process" in rfc
    assert "keep WebUI as a client of `HERMES_WEBUI_RUNNER_BASE_URL`" in rfc
    assert "without WebUI process-global\n  environment mutation" in rfc
    assert "Process ownership moved" in rfc
    assert "Restart/reattach with a real runner" in rfc
    assert "No runtime-surrogate globals in WebUI" in rfc
    assert "Default-off and reversible" in rfc
    assert "Runner health and failure are observable" in rfc
    assert "no claim that this is the canonical Hermes Agent Runtime API" in rfc

def test_runtime_runner_client_factory_stays_bounded_until_endpoint_configured(monkeypatch):
    routes = importlib.import_module("api.routes")

    monkeypatch.delenv("HERMES_WEBUI_RUNNER_BASE_URL", raising=False)
    try:
        routes._runtime_runner_client_factory()
    except NotImplementedError as exc:
        assert "runner-local chat backend is not configured" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("runner-local should remain bounded without an endpoint")

    monkeypatch.setenv("HERMES_WEBUI_RUNNER_BASE_URL", "http://runner.local/")
    client = routes._runtime_runner_client_factory()

    assert client.base_url == "http://runner.local"


def test_configured_runner_sse_stream_observes_runner_without_process_maps(monkeypatch):
    routes = importlib.import_module("api.routes")
    calls = []

    class FakeRunnerClient:
        def observe_run(self, run_id, *, cursor=None):
            calls.append((run_id, cursor))
            return {
                "run_id": run_id,
                "cursor": "7",
                "events": [
                    {"event": "message", "payload": {"content": "hello"}, "event_id": "run-1:6"},
                    {"event": "stream_end", "payload": {"ok": True}, "event_id": "run-1:7"},
                ],
            }

    class FakeHandler:
        def __init__(self):
            self.status = None
            self.headers = []
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            self.headers.append(("__end__", ""))

    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner-local")
    monkeypatch.setattr(routes, "_runtime_runner_client_factory", lambda: FakeRunnerClient())
    handler = FakeHandler()

    assert routes._stream_runner_run_events(handler, "run-1", "5") is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert handler.status == 200
    assert calls == [("run-1", "5")]
    assert "event: message" in body
    assert "id: run-1:6" in body
    assert 'data: {"content": "hello"}' in body
    assert "event: stream_end" in body
    assert "STREAMS" not in routes._stream_runner_run_events.__code__.co_names



def test_runner_runtime_adapter_passes_explicit_start_payload_without_env_mutation(monkeypatch):
    runtime = importlib.import_module("api.runtime_adapter")
    captured = []

    class FakeRunnerClient:
        def start_run(self, request):
            captured.append(request)
            return {
                "run_id": "runner-1",
                "session_id": request.session_id,
                "stream_id": "runner-1",
                "status": "running",
                "active_controls": ["cancel", "approval", "clarify", "goal"],
            }

    before_terminal_cwd = "existing-cwd"
    monkeypatch.setenv("TERMINAL_CWD", before_terminal_cwd)
    adapter = runtime.RunnerRuntimeAdapter(client=FakeRunnerClient())
    request = runtime.StartRunRequest(
        session_id="s-runner",
        message="hello runner",
        attachments=[{"path": "/tmp/a.png", "mime": "image/png"}],
        workspace="/workspace/project",
        profile="research",
        provider="openai-codex",
        model="gpt-5.5",
        toolsets=["terminal", "file"],
        source="webui",
        metadata={"route": "/api/chat/start", "csrf_checked": True},
    )

    result = adapter.start_run(request)

    assert captured == [request]
    assert captured[0].workspace == "/workspace/project"
    assert captured[0].profile == "research"
    assert captured[0].attachments == [{"path": "/tmp/a.png", "mime": "image/png"}]
    assert captured[0].provider == "openai-codex"
    assert captured[0].model == "gpt-5.5"
    assert captured[0].toolsets == ["terminal", "file"]
    assert result.run_id == "runner-1"
    assert result.active_controls == ["cancel", "approval", "clarify", "goal"]
    assert runtime.os.environ["TERMINAL_CWD"] == before_terminal_cwd


def test_runner_runtime_adapter_observe_and_get_survive_adapter_recreation():
    runtime = importlib.import_module("api.runtime_adapter")

    class FakeRunnerClient:
        def __init__(self):
            self.events = []
            self.status = "unknown"

        def start_run(self, request):
            self.status = "running"
            self.events.append({"event_id": "runner-1:1", "seq": 1, "type": "token", "data": {"text": "hi"}})
            self.events.append({"event_id": "runner-1:2", "seq": 2, "type": "done", "data": {"ok": True}})
            self.status = "completed"
            return {"run_id": "runner-1", "session_id": request.session_id, "stream_id": "runner-1", "status": "running"}

        def observe_run(self, run_id, *, cursor=None):
            after = int(cursor or 0)
            return {"run_id": run_id, "events": [e for e in self.events if e["seq"] > after]}

        def get_run(self, run_id):
            return {
                "run_id": run_id,
                "session_id": "s-runner",
                "status": self.status,
                "terminal_state": "completed",
                "last_event_id": self.events[-1]["event_id"],
                "active_controls": [],
            }

    shared_runner = FakeRunnerClient()
    first_webui_process = runtime.RunnerRuntimeAdapter(client=shared_runner)
    first_webui_process.start_run(runtime.StartRunRequest(session_id="s-runner", message="hello"))

    restarted_webui_process = runtime.RunnerRuntimeAdapter(client=shared_runner)
    replay = restarted_webui_process.observe_run("runner-1", cursor="1")
    status = restarted_webui_process.get_run("runner-1")

    assert [event["type"] for event in replay.events] == ["done"]
    assert replay.cursor == "2"
    assert replay.last_event_id == "runner-1:2"
    assert status.status == "completed"
    assert status.terminal_state == "completed"
    assert status.last_event_id == "runner-1:2"


def test_runner_runtime_adapter_controls_are_bounded_and_do_not_use_legacy_state():
    runtime = importlib.import_module("api.runtime_adapter")

    class FakeRunnerClient:
        def cancel_run(self, run_id):
            return {"ok": False, "status": "not-active", "message": "Run is not active."}

    adapter = runtime.RunnerRuntimeAdapter(client=FakeRunnerClient())

    cancel = adapter.cancel_run("finished-run")
    approval = adapter.respond_approval("finished-run", "approval-1", "once")
    clarify = adapter.respond_clarify("finished-run", "clarify-1", "answer")
    queued = adapter.queue_message("finished-run", "next")
    goal = adapter.update_goal("s-runner", "status")

    assert cancel.accepted is False
    assert cancel.status == "not-active"
    assert cancel.safe_message == "Run is not active."
    for result in (approval, clarify, queued, goal):
        assert result.accepted is False
        assert result.status == "unsupported"
        assert "not supported by this runner backend" in (result.safe_message or "")
