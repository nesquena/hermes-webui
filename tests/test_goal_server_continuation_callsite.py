"""Call-surface regressions for server-owned goal continuation delivery."""

from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_both_streaming_backends_persist_before_goal_continue_event():
    for relative in ("api/streaming.py", "api/gateway_chat.py"):
        source = (ROOT / relative).read_text()
        schedule = source.find("schedule_goal_continuation(")
        event = source.find('"goal_continue"', schedule)
        if event < 0:
            event = source.find("'goal_continue'", schedule)
        assert schedule >= 0, f"{relative} must persist the continuation intent"
        assert event > schedule, f"{relative} must persist before emitting goal_continue"


def test_server_turn_marks_goal_continuation_explicitly_goal_related():
    routes = (ROOT / "api/routes.py").read_text()
    start = routes.index("def start_session_turn(")
    body = routes[start : routes.index("def _handle_bg_task_complete_ack", start)]
    assert 'turn_source == "goal_continuation"' in body
    assert "goal_related=" in body
    assert "bind_goal_continuation_stream" in routes
    assert "adopt_legacy_browser_goal_stream" in routes


def test_browser_observes_goal_continue_but_never_dispatches_it():
    messages = (ROOT / "static/messages.js").read_text()
    handler = messages.index("source.addEventListener('goal_continue'")
    body = messages[handler : messages.index("source.addEventListener('done'", handler)]
    assert "queueSessionMessage" not in body
    assert "_pendingGoalContinuation" not in messages


def test_server_starts_and_stops_durable_goal_worker():
    server = (ROOT / "server.py").read_text()
    background = (ROOT / "api" / "background_process.py").read_text()
    assert "start_drain_thread" in server
    assert "stop_drain_thread" in server
    assert "start_goal_continuation_worker" in server
    assert "stop_goal_continuation_worker" in background
    shutdown = server.index("finally:\n        httpd.server_close()")
    stop = server.index("stop_drain_thread()", shutdown)
    lifecycle_drain = server.index("drain_all_on_shutdown()", shutdown)
    assert stop < lifecycle_drain, "new goal turns must be disabled before shutdown drains"
    bind = server.index("httpd = QuietHTTPServer")
    start = server.index("start_goal_continuation_worker()")
    assert bind < start, "the durable worker must not start before the server owns its port"
    drain_stop = background.index("def stop_drain_thread(")
    assert background.index("stop_goal_continuation_worker", drain_stop) > drain_stop


def test_empty_response_retry_is_limited_to_server_goal_continuations():
    streaming = (ROOT / "api/streaming.py").read_text()
    gateway = (ROOT / "api/gateway_chat.py").read_text()
    assert "requeue_goal_continuation_after_no_response" in streaming
    assert "requeue_goal_continuation_after_no_response" in gateway
    assert "had_activity=" in streaming
    assert "had_activity=" in gateway


def test_goal_state_session_id_follows_compression_child():
    from api.streaming import _goal_state_session_id

    assert _goal_state_session_id(
        "session-parent",
        SimpleNamespace(session_id="session-child"),
    ) == "session-child"
    assert _goal_state_session_id("session-parent", SimpleNamespace(session_id=None)) == "session-parent"


def test_goal_hook_uses_child_for_state_and_next_intent_after_compression():
    streaming = (ROOT / "api/streaming.py").read_text()
    start = streaming.index("# /goal parity: after a successful assistant turn")
    end = streaming.index("with _stream_writeback_stage(_writeback_timings, \"done_payload\")", start)
    hook = streaming[start:end]

    assert "_goal_session_id = _goal_state_session_id(session_id, s)" in hook
    assert "has_active_goal(_goal_session_id" in hook
    assert "evaluate_goal_after_turn(\n                        _goal_session_id," in hook
    assert "goal_state_snapshot(_goal_session_id" in hook
    assert "schedule_goal_continuation(\n                            _goal_session_id," in hook
    assert "predecessor_session_id=session_id" in hook
    assert "producer_kind=goal_producer_kind" in hook
    assert "if _continuation_record.get('status') == 'pending':" in hook
    assert "PENDING_GOAL_CONTINUATION.add(_goal_session_id)" in hook


def test_route_passes_explicit_goal_producer_kind_to_stream_worker():
    routes = (ROOT / "api" / "routes.py").read_text()
    assert 'goal_producer_kind = "continuation"' in routes
    assert 'goal_producer_kind = "initial_goal"' in routes
    assert '"goal_producer_kind": goal_producer_kind' in routes


def test_background_title_keeps_parent_relay_after_child_rotation(monkeypatch):
    from api import streaming

    source = (ROOT / "api" / "streaming.py").read_text()
    caller = source[source.index("target=_run_background_title_update") - 200:source.index("target=_run_background_title_update") + 600]
    assert '"relay_session_id": session_id' in caller

    requested = []
    events = []
    session = SimpleNamespace(
        session_id="session-child",
        title="Stable title",
        llm_title_generated=True,
    )

    def get_session(session_id):
        requested.append(session_id)
        return session

    monkeypatch.setattr(streaming, "get_session", get_session)
    streaming._run_background_title_update(
        "session-child",
        "user text",
        "assistant text",
        "Stable title",
        lambda event, payload: events.append((event, payload)),
        relay_session_id="session-parent",
    )

    assert requested == ["session-child"]
    assert events
    assert events[-1] == ("stream_end", {"session_id": "session-parent"})
    assert all(payload.get("session_id") == "session-parent" for _event, payload in events)
