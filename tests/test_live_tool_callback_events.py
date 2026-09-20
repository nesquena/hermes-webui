from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    # Find the next def at the same indent level: either a
    # module-level def (0 spaces) or a closure-local def (12 spaces
    # inside a method inside a function — the depth where
    # on_tool_complete, on_tool_start, and on_tool live).
    next_def = -1
    for indent in ("\n            def ", "\ndef "):
        i = src.find(indent, start + 1)
        if i != -1 and (next_def == -1 or i < next_def):
            next_def = i
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

    # #7358: the on_tool_complete closure is now a thin wrapper that
    # delegates the mirror writes + SSE payload emission to the
    # module-level helper so the cancellation-vs-live agreement can
    # be tested directly. The closure still owns the
    # ``_live_tool_event_complete_ids`` idempotency guard, the
    # ``_checkpoint_activity`` counter, and the live/shared list
    # wiring that the helper needs.
    assert "_emit_tool_complete_to_mirrors_and_sse(" in block, (
        "on_tool_complete must delegate to the module-level helper "
        "so live_tc, shared_tc, and the SSE payload all agree on is_error"
    )
    assert "tool_call_id=tool_call_id" in block
    assert "_live_tool_event_complete_ids" in block, (
        "Tool completion SSE emission should be idempotent per callback id."
    )
    assert "_checkpoint_activity[0] += 1" in block
    assert "live_tool_calls_list=_live_tool_calls" in block
    assert "record_live_tool_complete=_record_live_tool_complete" in block


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
    assert _tool_result_is_error("terminal", {"is_error": True}) is True
    # Mixed with other fields still wins on is_error.
    assert _tool_result_is_error("terminal", {"is_error": True, "output": "ok"}) is True


def test_tool_result_is_error_matches_success_false():
    """Tools that follow the ``{success, error, output}`` shape —
    common in our own failure paths and in many third-party tools —
    must surface success:false as a failure."""
    from api.streaming import _tool_result_is_error
    assert _tool_result_is_error("any_tool", {"success": False, "error": "HTTP 433"}) is True
    assert _tool_result_is_error("any_tool", {"success": False}) is True


def test_tool_result_is_error_matches_terminal_nonzero_exit():
    """#7358 re-gate: a terminal result with ``exit_code != 0`` must
    surface as a failure. The Agent core classifies this in
    ``_detect_tool_failure`` and the structured callback path must
    agree so the WebUI card stays in sync with the CLI's ``[error]``
    tag."""
    from api.streaming import _tool_result_is_error
    # exit_code 0 is success.
    assert _tool_result_is_error("terminal", {"exit_code": 0, "output": "ok"}) is False
    assert _tool_result_is_error("terminal", {"exit_code": 0}) is False
    # exit_code missing on a terminal result is ambiguous → default False.
    assert _tool_result_is_error("terminal", {"output": "ok"}) is False
    # exit_code != 0 is the canonical failure.
    assert _tool_result_is_error("terminal", {"exit_code": 1, "error": "command not found"}) is True
    assert _tool_result_is_error("terminal", {"exit_code": 127, "output": ""}) is True
    assert _tool_result_is_error("terminal", {"exit_code": 2}) is True


def test_tool_result_is_error_matches_memory_store_full():
    """Memory tool: ``success: false`` only counts as a failure when
    the ``exceed the limit`` signal is present, matching the Agent's
    own guard at ``agent/tool_guardrails.py:218-225``. A bare
    success:false on a memory tool (e.g. duplicate) must NOT be
    classified as a failure."""
    from api.streaming import _tool_result_is_error
    # store-full is a failure.
    assert _tool_result_is_error("memory", {"success": False, "error": "Cannot store: would exceed the limit (1000 entries)"}) is True
    # bare success:false on memory is not (matches Agent's own guard).
    assert _tool_result_is_error("memory", {"success": False, "error": "duplicate entry"}) is False


def test_tool_result_is_error_matches_string_markers():
    """String results: the helper must recognize the same
    ``"error"`` / ``"failed"`` markers the Agent's own classifier
    recognizes at ``agent/display.py:925-929``."""
    from api.streaming import _tool_result_is_error
    assert _tool_result_is_error("any_tool", '{"error": "something broke"}') is True
    assert _tool_result_is_error("any_tool", '{"failed": true, "code": 500}') is True
    assert _tool_result_is_error("any_tool", "Error: connection refused") is True
    # Plain success string is not a failure.
    assert _tool_result_is_error("any_tool", '{"output": "ok", "data": [1, 2, 3]}') is False
    assert _tool_result_is_error("any_tool", "ok") is False


def test_tool_result_is_error_keeps_default_for_ambiguous_shapes():
    """Regression guard: the helper must not accidentally flip a
    success card to Failed. The default is False for any shape that
    is not one of the explicit signals above, including an
    informational ``error`` key or a custom ``status`` field. Native
    clients (Hermex) currently render ``is_error == true`` with a
    red icon, so a false positive is user-visible."""
    from api.streaming import _tool_result_is_error
    # Empty / non-dict inputs
    assert _tool_result_is_error("any_tool", None) is False
    assert _tool_result_is_error("any_tool", "") is False
    assert _tool_result_is_error("any_tool", "plain string result") is False
    assert _tool_result_is_error("any_tool", [1, 2, 3]) is False
    # Empty dict
    assert _tool_result_is_error("any_tool", {}) is False
    # Explicit success stays success
    assert _tool_result_is_error("any_tool", {"success": True}) is False
    assert _tool_result_is_error("any_tool", {"success": True, "error": "informational"}) is False
    # ``is_error: false`` is not failure
    assert _tool_result_is_error("any_tool", {"is_error": False}) is False
    # Informational ``error`` key with success not explicitly false
    # should NOT be classified as failure — only the explicit
    # signals are. This pins the conservative scope of the helper.
    assert _tool_result_is_error("any_tool", {"error": "rate-limited retry succeeded"}) is False
    # status is deliberately not classified.
    assert _tool_result_is_error("any_tool", {"status": "error"}) is False


def test_on_tool_complete_emits_is_error_from_payload():
    """The structured callback's ``tool_complete`` SSE event must
    carry an accurate ``is_error`` bit sourced from the result
    payload, not the legacy hardcoded False. Otherwise WebUI and
    native clients render the card as Completed even when the
    underlying tool call failed (#7358)."""
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool_complete")

    # The hardcoded ``is_error': False`` is gone; the value is
    # computed inside the module-level helper that on_tool_complete
    # now delegates to.
    assert "'is_error': False" not in block, (
        "the hardcoded False is the bug; is_error must be sourced "
        "from the structured payload via _tool_result_is_error()"
    )
    # on_tool_complete itself does not classify is_error directly —
    # it delegates to the helper. The helper does the classification.
    helper_block = _function_block(src, "_emit_tool_complete_to_mirrors_and_sse")
    assert "_tool_result_is_error(name, function_result)" in helper_block, (
        "the emission helper must derive is_error from the structured "
        "function_result via _tool_result_is_error()"
    )


def test_emission_helper_writes_is_error_to_both_mirrors():
    """#7358 re-gate: ``is_error`` must be written to all three
    projections of the structured callback path — the per-stream
    ``_live_tool_calls`` mirror, the cross-process
    ``STREAM_LIVE_TOOL_CALLS`` shared mirror, and the SSE payload.
    Without the mirror writes, a failed tool renders red live and
    then becomes a Completed card after cancel + reload, because
    ``_build_partial_message`` at ``api/streaming.py:14009-14017``
    persists the shared internal shape ``{name, args, done,
    duration, is_error}`` through ``_partial_tool_calls`` on
    cancellation."""
    src = _read("api/streaming.py")
    block = _function_block(src, "_emit_tool_complete_to_mirrors_and_sse")

    # The helper classifies is_error once (via the
    # is_error_override ternary) and writes the same value into
    # every projection.
    assert "_tool_result_is_error(name, function_result)" in block, (
        "the helper must classify is_error once via the classifier, "
        "then write the same value into all three projections"
    )
    assert block.count("live_tc['is_error'] = is_error") == 1, (
        "the per-stream _live_tool_calls mirror must receive "
        "is_error on the same code path as done/snippet"
    )
    assert block.count("shared_tc['is_error'] = is_error") == 1, (
        "the STREAM_LIVE_TOOL_CALLS shared mirror must receive "
        "is_error so cancellation persistence agrees with the live card"
    )
    # The SSE payload also carries the captured is_error.
    assert "'is_error': is_error" in block, (
        "the tool_complete SSE payload must use the captured "
        "is_error local, not call the helper a second time"
    )


def test_emission_helper_keeps_three_projections_in_sync():
    """Behavioral coverage requested by the #7358 re-gate: a single
    call to the emission helper must leave the live mirror, the
    shared mirror, and the SSE payload all carrying the same
    ``is_error`` value. The test drives the helper directly (not the
    closure) so it runs without standing up the full
    ``_run_agent_streaming`` generator.

    The two scenarios — a non-terminal tool with
    ``{success: false, error: ...}`` and a terminal tool with
    ``{exit_code: 1}`` — are the two real production failure shapes
    the Agent core classifies with ``_detect_tool_failure``. They
    must propagate to every projection so cancellation persistence
    at ``api/streaming.py:14009-14017`` agrees with the live card.
    """
    from api.streaming import (
        _emit_tool_complete_to_mirrors_and_sse,
        _build_partial_message,
    )

    sse_events = []

    def put(kind, payload):
        sse_events.append((kind, payload))

    def record_live_tool_complete(tool_call_id, name, function_result):
        # No-op for the test; production code wires this to
        # _record_live_tool_complete() which logs to the run journal.
        return None

    def args_snapshot(args):
        return dict(args) if isinstance(args, dict) else {"args": args}

    def _run_one(tool_name, function_result):
        sse_events.clear()
        live_tcs = [
            {"tid": "t-1", "name": tool_name, "done": False},
        ]
        shared_tcs = [
            {"tid": "t-1", "name": tool_name, "done": False},
        ]
        is_error = _emit_tool_complete_to_mirrors_and_sse(
            tool_call_id="t-1",
            name=tool_name,
            args={"input": "x"},
            function_result=function_result,
            live_tool_calls_list=live_tcs,
            shared_tool_calls_list=shared_tcs,
            put=put,
            record_live_tool_complete=record_live_tool_complete,
            args_snapshot_fn=args_snapshot,
        )
        # Build the cancellation partial the way
        # ``_run_agent_streaming`` builds it on cancel, so we can
        # verify the shared mirror's is_error is what gets persisted.
        partial = _build_partial_message(
            "",
            "",
            [shared_tcs[0]],
        )
        return is_error, live_tcs[0], shared_tcs[0], sse_events, partial

    # Case 1: non-terminal tool with success:false + error.
    is_error, live_tc, shared_tc, events, partial = _run_one(
        "fetch_url",
        {"success": False, "error": "HTTP 503", "output": None},
    )
    assert is_error is True
    assert live_tc["is_error"] is True, (
        "live_tc mirror must carry is_error=True after a success:false tool"
    )
    assert live_tc["done"] is True
    assert shared_tc["is_error"] is True, (
        "shared_tc mirror must carry is_error=True so cancellation "
        "persistence agrees with the live card"
    )
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "tool_complete"
    assert payload["is_error"] is True, (
        "SSE payload must carry is_error=True for native clients (Hermex)"
    )
    assert payload["tid"] == "t-1"
    assert partial is not None, (
        "_build_partial_message must produce a non-None partial when "
        "shared_tcs has at least one tool call"
    )
    # The partial persists the shared internal shape through
    # _partial_tool_calls; verify the cancelled partial agrees with
    # the live card.
    assert "_partial_tool_calls" in partial
    persisted = partial["_partial_tool_calls"][0]
    assert persisted.get("is_error") is True, (
        "cancelled _partial_tool_calls[0].is_error must match the live card"
    )

    # Case 2: terminal tool with non-zero exit_code.
    is_error, live_tc, shared_tc, events, partial = _run_one(
        "terminal",
        {"exit_code": 1, "error": "command not found", "output": ""},
    )
    assert is_error is True, (
        "terminal exit_code != 0 must classify as a failure"
    )
    assert live_tc["is_error"] is True
    assert shared_tc["is_error"] is True
    assert events[0][1]["is_error"] is True
    assert partial is not None
    assert partial["_partial_tool_calls"][0].get("is_error") is True, (
        "cancelled _partial_tool_calls[0].is_error must match the live "
        "card after a non-zero terminal exit"
    )

    # Case 3: success shape must NOT flip any projection to error.
    is_error, live_tc, shared_tc, events, partial = _run_one(
        "terminal",
        {"exit_code": 0, "output": "ok"},
    )
    assert is_error is False
    assert live_tc["is_error"] is False
    assert shared_tc["is_error"] is False
    assert events[0][1]["is_error"] is False
    assert partial is not None
    assert partial["_partial_tool_calls"][0].get("is_error") is False, (
        "cancelled _partial_tool_calls[0].is_error must match the live "
        "card for a successful tool (regression guard against the "
        "false-positive that motivated the re-gate)"
    )
