from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    next_def = src.find("\n            def ", start + 1)
    assert next_def != -1, f"end of {name} not found"
    return src[start:next_def]


def test_tool_start_callback_emits_existing_tool_sse_event_with_tool_id():
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool_start")

    assert "put('tool'" in block, (
        "The dedicated Hermes Agent tool_start_callback must emit the existing "
        "tool SSE event; otherwise WebUI stays visually silent while tools run."
    )
    assert "'event_type': 'tool.started'" in block
    assert "'tid': tool_call_id" in block, (
        "Live frontend cards need the tool_call_id so tool_complete can update "
        "the running card in place."
    )
    assert "_live_tool_event_start_ids" in block, (
        "Tool start SSE emission should be idempotent per callback id."
    )
    assert "STREAM_LIVE_TOOL_CALLS" in block and "'done': False" in block


def test_tool_complete_callback_emits_existing_tool_complete_sse_event_with_tool_id():
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool_complete")

    assert "put('tool_complete'" in block, (
        "The dedicated Hermes Agent tool_complete_callback must emit the existing "
        "tool_complete SSE event so the frontend can settle the running tool card."
    )
    assert "'event_type': 'tool.completed'" in block
    assert "'tid': tool_call_id" in block
    assert "_live_tool_event_complete_ids" in block, (
        "Tool completion SSE emission should be idempotent per callback id."
    )
    assert "result_snippet = _tool_result_snippet(function_result)" in block
    assert "_checkpoint_activity[0] += 1" in block


def test_legacy_progress_events_are_suppressed_when_structured_callbacks_are_wired():
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool")

    # Verify the targeted-suppression contract.
    # Legacy tool.started and tool.completed events are only suppressed
    # when (a) the structured callback is wired AND (b) this specific tool
    # name was already handled by the structured path (tracked via the
    # _live_tool_event_start_names / _live_tool_event_complete_names sets).
    # Internal/framework-only tool calls not touched by the structured
    # callbacks still flow through the legacy path.
    assert "event_type in (None, 'tool.started') and 'tool_start_callback' in _agent_params" in block
    assert "name in _live_tool_event_start_names" in block
    assert "event_type == 'tool.completed' and 'tool_complete_callback' in _agent_params" in block
    assert "name in _live_tool_event_complete_names" in block
    assert "name and name in" in block, (
        "The name check must guard against None/empty names so "
        "unnamed tool-progress events still reach the legacy path."
    )
    # Suppression guard must come before the corresponding legacy emit
    assert block.index("'tool_start_callback' in _agent_params") < block.index("put('tool'")
    assert block.index("'tool_complete_callback' in _agent_params") < block.index("put('tool_complete'")


def test_tool_callback_events_keep_existing_frontend_event_contract():
    messages = _read("static/messages.js")
    ui = _read("static/ui.js")

    assert "source.addEventListener('tool',e=>{" in messages
    assert "source.addEventListener('tool_complete',e=>{" in messages
    assert "String(d&&d.tid" in messages or "explicitTid=String(d&&d.tid" in messages, (
        "frontend tool handlers must still consume explicit server tid when present"
    )
    assert "upsertLiveToolCall(d,'start')" in messages
    assert "upsertLiveToolCall(d,'complete')" in messages
    assert "data-live-tid" in ui
    assert "existing.replaceWith(replacement)" in ui
