from api.run_graph import build_run_graph_from_events


def _evt(seq, event, payload=None, **extra):
    return {
        "version": 1,
        "event_id": f"run_1:{seq}",
        "seq": seq,
        "run_id": "run_1",
        "session_id": "session_1",
        "event": event,
        "type": event,
        "created_at": float(seq),
        "terminal": event in {"done", "cancel", "apperror", "error", "stream_end"},
        "payload": payload or {},
        **extra,
    }


def _node(graph, kind_or_id):
    for node in graph["nodes"]:
        if node["id"] == kind_or_id or node["kind"] == kind_or_id:
            return node
    raise AssertionError(f"node not found: {kind_or_id}")


def test_build_run_graph_projects_reasoning_tool_output_and_terminal_success():
    graph = build_run_graph_from_events(
        "session_1",
        "run_1",
        [
            _evt(1, "reasoning", {"text": "thinking"}),
            _evt(2, "tool", {"tool_call_id": "call_1", "name": "read_file", "args": {"path": "x"}}),
            _evt(3, "tool_complete", {"tool_call_id": "call_1", "name": "read_file", "ok": True, "result": "done"}),
            _evt(4, "token", {"text": "answer"}),
            _evt(5, "done", {"session": {"session_id": "session_1"}}, terminal_state="completed"),
        ],
    )

    assert graph["version"] == 1
    assert graph["status"] == "succeeded"
    assert graph["event_count"] == 5

    root = _node(graph, "run:run_1")
    assert root["kind"] == "run"
    assert root["status"] == "succeeded"
    assert root["event_count"] == 5

    reasoning = _node(graph, "model_reasoning")
    assert reasoning["label"] == "Reasoning"
    assert reasoning["latest"]["text_chars"] == len("thinking")

    tool = _node(graph, "tool_call")
    assert tool["label"] == "read_file"
    assert tool["status"] == "succeeded"
    assert tool["event_count"] == 2
    assert tool["latest"]["result_chars"] == len("done")

    assistant = _node(graph, "assistant_output")
    assert assistant["latest"]["text_chars"] == len("answer")

    assert {edge["source"] for edge in graph["edges"]} == {"run:run_1"}


def test_build_run_graph_marks_tool_failure_and_run_failure():
    graph = build_run_graph_from_events(
        "session_1",
        "run_1",
        [
            _evt(1, "tool", {"tool_call_id": "call_1", "name": "terminal"}),
            _evt(2, "tool_complete", {"tool_call_id": "call_1", "name": "terminal", "ok": False, "result": "boom"}),
            _evt(3, "apperror", {"type": "provider", "message": "model failed"}, terminal_state="errored"),
        ],
    )

    assert graph["status"] == "failed"
    assert _node(graph, "tool_call")["status"] == "failed"
    terminal = _node(graph, "terminal")
    assert terminal["status"] == "failed"
    assert terminal["latest"]["message"] == "model failed"


def test_build_run_graph_marks_cancel_as_interrupted():
    graph = build_run_graph_from_events(
        "session_1",
        "run_1",
        [
            _evt(1, "token", {"text": "partial"}),
            _evt(2, "cancel", {"message": "cancelled by user"}, terminal_state="interrupted-by-user"),
        ],
    )

    assert graph["status"] == "interrupted"
    assert _node(graph, "terminal")["status"] == "interrupted"


def test_build_run_graph_aggregates_compression_and_metering_without_raw_payload_dump():
    graph = build_run_graph_from_events(
        "session_1",
        "run_1",
        [
            _evt(1, "compressing", {"message": "Auto-compressing"}),
            _evt(2, "compressed", {"message": "Compressed"}),
            _evt(3, "metering", {"usage": {"input_tokens": 10, "output_tokens": 3, "private": "drop-me"}}),
        ],
    )

    compression = _node(graph, "compression")
    assert compression["status"] == "succeeded"
    assert compression["event_count"] == 2

    metering = _node(graph, "metering")
    assert metering["latest"]["usage"] == {"input_tokens": 10, "output_tokens": 3}


def test_build_run_graph_unknown_events_become_event_nodes():
    graph = build_run_graph_from_events(
        "session_1",
        "run_1",
        [_evt(1, "custom_status", {"phase": "weird"})],
    )

    custom = _node(graph, "event")
    assert custom["label"] == "custom_status"
    assert custom["status"] == "succeeded"
    assert custom["latest"]["phase"] == "weird"
