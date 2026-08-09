"""Call-surface regressions for server-owned goal continuation delivery."""

from pathlib import Path


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
    assert "start_goal_continuation_worker" in server
    assert "stop_goal_continuation_worker" in server
    shutdown = server.index("finally:\n        httpd.server_close()")
    stop = server.index("stop_goal_continuation_worker()", shutdown)
    lifecycle_drain = server.index("drain_all_on_shutdown()", shutdown)
    assert stop < lifecycle_drain, "new goal turns must be disabled before shutdown drains"
    bind = server.index("httpd = QuietHTTPServer")
    start = server.index("start_goal_continuation_worker()")
    assert bind < start, "the durable worker must not start before the server owns its port"


def test_empty_response_retry_is_limited_to_server_goal_continuations():
    streaming = (ROOT / "api/streaming.py").read_text()
    gateway = (ROOT / "api/gateway_chat.py").read_text()
    assert "requeue_goal_continuation_after_no_response" in streaming
    assert "requeue_goal_continuation_after_no_response" in gateway
    assert "had_activity=" in streaming
    assert "had_activity=" in gateway
