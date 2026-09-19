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

    assert "event_type in (None, 'tool.started') and 'tool_start_callback' in _agent_params" in block
    assert "event_type == 'tool.completed' and 'tool_complete_callback' in _agent_params" in block
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


# ── #7358: structured tool_complete must source is_error from the payload ──


def test_tool_result_is_error_helper_is_defined():
    """The structured ``tool_complete_callback`` signature is
    ``(tool_call_id, name, args, function_result)`` and does not
    receive the already-classified ``is_error`` bit the sibling
    tool_progress_callback carries. The fix is a local helper
    re-deriving a conservative failure flag from the structured
    payload."""
    src = _read("api/streaming.py")
    assert "def _tool_result_is_error(" in src, (
        "must add a module-level helper that classifies a structured "
        "tool result, mirroring the Agent's own _detect_tool_failure() "
        "shape on the four-arg structured callback path (#7358)"
    )


def test_tool_result_is_error_matches_is_error_true():
    """The most explicit failure signal: ``is_error: true`` must
    surface as a failure so clients that mirror agent-core's own
    ``is_error`` shape correctly render Failed."""
    from api.streaming import _tool_result_is_error
    assert _tool_result_is_error({"is_error": True}) is True
    # Mixed with other fields still wins on is_error.
    assert _tool_result_is_error({"is_error": True, "output": "ok"}) is True


def test_tool_result_is_error_matches_success_false():
    """Tools that follow the ``{success, error, output}`` shape —
    common in our own failure paths and in many third-party tools —
    must surface success:false as a failure."""
    from api.streaming import _tool_result_is_error
    assert _tool_result_is_error({"success": False, "error": "HTTP 433"}) is True
    assert _tool_result_is_error({"success": False}) is True


def test_tool_result_is_error_keeps_default_for_ambiguous_shapes():
    """Regression guard: the helper must not accidentally flip a
    success card to Failed. The default is False for any shape that
    is not one of the two explicit signals above, including an
    informational ``error`` key or a custom ``status`` field. Native
    clients (Hermex) currently render ``is_error == true`` with a
    red icon, so a false positive is user-visible."""
    from api.streaming import _tool_result_is_error
    # Empty / non-dict inputs
    assert _tool_result_is_error(None) is False
    assert _tool_result_is_error("") is False
    assert _tool_result_is_error("plain string result") is False
    assert _tool_result_is_error([1, 2, 3]) is False
    # Empty dict
    assert _tool_result_is_error({}) is False
    # Explicit success stays success
    assert _tool_result_is_error({"success": True}) is False
    assert _tool_result_is_error({"success": True, "error": "informational"}) is False
    # ``is_error: false`` is not failure
    assert _tool_result_is_error({"is_error": False}) is False
    # Informational ``error`` key with success not explicitly false
    # should NOT be classified as failure — only the two explicit
    # signals are. This pins the conservative scope of the helper.
    assert _tool_result_is_error({"error": "rate-limited retry succeeded"}) is False
    # status is deliberately not classified.
    assert _tool_result_is_error({"status": "error"}) is False


def test_on_tool_complete_emits_is_error_from_payload():
    """The structured callback's ``tool_complete`` SSE event must
    carry an accurate ``is_error`` bit sourced from the result
    payload, not the legacy hardcoded False. Otherwise WebUI and
    native clients render the card as Completed even when the
    underlying tool call failed (#7358)."""
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool_complete")

    # The hardcoded ``is_error': False`` is gone; the value comes
    # from the new helper.
    assert "'is_error': False" not in block, (
        "the hardcoded False is the bug; is_error must be sourced "
        "from the structured payload via _tool_result_is_error()"
    )
    assert "_tool_result_is_error(function_result)" in block, (
        "on_tool_complete must derive is_error from the structured "
        "function_result via the new helper, not the hardcoded False"
    )
