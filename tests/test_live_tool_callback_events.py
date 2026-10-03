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


# ── #7358 round-3: real Agent payloads must not be substring-classified ──

# Captured from the Agent's own terminal tool
# (tools/terminal_tool.py) via registry.dispatch(), then handed to the
# structured ``tool_complete_callback(tc.id, name, args, function_result)``
# exactly as run_agent.py:6816 invokes it. ``function_result`` is a JSON
# *string*, not a dict — the shape the round-2 helper substring-scanned.
# Capture procedure: see tests/fixtures/capture_tool_payloads.py.
REAL_TERMINAL_SUCCESS = '{"output": "hello-world", "exit_code": 0, "error": null}'
REAL_TERMINAL_FAILURE = '{"output": "", "exit_code": 3, "error": null}'
REAL_TERMINAL_GREP_NO_MATCH = (
    '{"output": "", "exit_code": 1, "error": null, '
    '"exit_code_meaning": "No matches found (not an error)"}'
)


def test_tool_result_is_error_decodes_real_agent_json_string_success():
    """#7358 round-3 Finding 1 (SILENT): the real Agent emits a JSON
    *string* — ``{"output": "hello-world", "exit_code": 0, "error":
    null}`` — as the structured callback's ``function_result``. The
    round-2 helper substring-matched on ``"error"`` and returned True,
    so **every successful terminal command rendered as a red/failed
    card**. A false positive on the happy path is worse than the
    original false negative: the user cannot trust a red card at all.

    The fix must structurally decode the JSON string and classify from
    the decoded fields, never from a substring scan.
    """
    from api.streaming import _tool_result_is_error

    assert _tool_result_is_error("terminal", REAL_TERMINAL_SUCCESS) is False, (
        "a captured real Agent success payload (exit_code 0, error null) "
        "must NOT be classified as a failure — this is the reviewer's "
        "Finding 1 false positive on the happy path"
    )


def test_tool_result_is_error_decodes_real_agent_json_string_failure():
    """The same decode path must still catch a real failure: a captured
    ``exit_code: 3`` payload is a genuine failure, so the structural
    decode cannot be so conservative that it swallows it."""
    from api.streaming import _tool_result_is_error

    assert _tool_result_is_error("terminal", REAL_TERMINAL_FAILURE) is True, (
        "a captured real Agent failure payload (exit_code 3) must be "
        "classified as a failure so the fix does not over-correct"
    )


def test_tool_result_is_error_honours_explicit_is_error_in_json_string():
    """A JSON string carrying ``{"is_error": true}`` with no other marker
    must be a failure. The round-2 helper only inspected dict payloads
    for ``is_error`` and substring-scanned strings, so this shape fell
    through to the default False — a missed failure."""
    from api.streaming import _tool_result_is_error

    assert _tool_result_is_error("any_tool", '{"is_error": true}') is True
    assert _tool_result_is_error("any_tool", '{"is_error": true, "output": "ok"}') is True
    assert _tool_result_is_error("any_tool", '{"is_error": false}') is False


def test_tool_result_is_error_rejects_malformed_json_like_input():
    """Malformed JSON-like strings must not crash and must not be
    classified from a substring scan. ``{"exit_code": 0,`` (truncated)
    contains the literal ``error`` key in some variants; the classifier
    must fail safe rather than substring-match."""
    from api.streaming import _tool_result_is_error

    # Truncated JSON is not decodable -> conservative default, no crash.
    assert _tool_result_is_error("any_tool", '{"exit_code": 0, "erro') is False
    # Trailing garbage after otherwise-valid JSON is still rejected.
    assert _tool_result_is_error("any_tool", '{"a": 1} not json') is False


def test_tool_result_is_error_uses_agent_canonical_failure_classifier():
    """The classifier must stay aligned with the Agent core's
    ``_detect_tool_failure`` (agent/display.py:710-744). For non-terminal
    tools that helper still returns a failure on the generic
    ``"error"``/``"failed"`` markers, so a JSON-string payload whose
    decoded form carries an ``error`` value must be a failure while an
    ``error: null`` payload must not be."""
    from api.streaming import _tool_result_is_error

    # error: null with exit_code 0 is the canonical success shape.
    assert _tool_result_is_error("any_tool", '{"error": null}') is False
    # A real error string in the decoded payload is a failure.
    assert _tool_result_is_error("any_tool", '{"error": "connection refused"}') is True
    assert _tool_result_is_error("any_tool", '{"failed": true, "code": 500}') is True


def test_tool_result_is_error_treats_nested_result_errors_as_success():
    """Real tools wrap per-item errors under a nested container:
    ``web_extract``/``web_crawl`` return
    ``{"results": [{"url": ..., "error": "Blocked: ..."}]}`` where one
    blocked URL coexists with successfully extracted siblings. The tool
    call as a whole succeeded, so the top-level classification must not
    substring-scan the nested ``error`` text. Only the TOP-LEVEL
    ``error``/``failed`` markers classify.

    This is the other half of Finding 1: a naive "scan the raw text"
    fix for the JSON-string shape reintroduces the false positive on
    this real shape.
    """
    from api.streaming import _tool_result_is_error

    nested = (
        '{"results": [{"url": "https://a.example", "title": "A", '
        '"content": "ok", "error": null}, '
        '{"url": "https://b.example", "title": "", "content": "", '
        '"error": "Blocked: URL targets a private or internal network address"}]}'
    )
    assert _tool_result_is_error("web_crawl", nested) is False, (
        "a nested per-URL error under a successful top-level payload "
        "must NOT flip the tool card to Failed"
    )


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
    # status is deliberately not classified.
    assert _tool_result_is_error("any_tool", {"status": "error"}) is False
    # ``error`` explicitly null is the Agent's own success shape
    # (tools/terminal_tool.py emits ``{"error": null}`` on a clean run).
    assert _tool_result_is_error("any_tool", {"error": None}) is False
    assert _tool_result_is_error("any_tool", {"error": ""}) is False


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


# ── #7358 round-3: normal settlement must not lose the live is_error ──


def test_extract_tool_calls_copies_live_is_error_into_summaries():
    """#7358 round-3 Finding 2 (SILENT): a card shown red live still
    becomes "Completed" after a NORMAL settlement and reload.

    The chain: ``_emit_tool_complete_to_mirrors_and_sse`` writes
    ``is_error`` into the per-stream ``_live_tool_calls`` mirror, but
    ``_extract_tool_calls_from_messages`` builds the settled
    ``s.tool_calls`` summaries from the final messages and drops the
    live flag entirely. The persisted summary therefore has no
    ``is_error``, hydration creates a successful transcript-owned row,
    and ``merge_duplicate_tool_row`` never merges an error status that
    does not exist.

    Round 2 only fixed the *cancellation* path
    (``_build_partial_message`` -> ``_partial_tool_calls``); normal
    settlement is the common case.
    """
    from api.streaming import _extract_tool_calls_from_messages

    # A settled turn: one assistant tool_use + its matching tool result.
    messages = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t-1", "name": "terminal", "input": {"command": "exit 3"}},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "t-1",
            "content": '{"output": "", "exit_code": 3, "error": null}',
        },
    ]
    # The live mirror carries what the emission helper wrote.
    live_tool_calls = [
        {"name": "terminal", "tid": "t-1", "args": {"command": "exit 3"}, "done": True, "is_error": True},
    ]

    tool_calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live_tool_calls)

    assert tool_calls, "the settled turn must produce a tool-call summary"
    settled = tool_calls[0]
    assert settled.get("tid") == "t-1"
    assert settled.get("is_error") is True, (
        "normal settlement must copy the live is_error into the "
        "persisted summary by tid — otherwise a card shown red live "
        "becomes Completed after settle + reload (Finding 2)"
    )


def test_extract_tool_calls_copies_live_is_error_into_fallback_summaries():
    """The live-fallback branch of ``_extract_tool_calls_from_messages``
    (unresolved tool messages matched positionally against the live
    mirror) must also carry the live ``is_error``. A tool whose start
    event the final history omits still has to settle red."""
    from api.streaming import _extract_tool_calls_from_messages

    # Tool message with no resolvable assistant tool_use id -> the
    # positional live-fallback branch builds the summary.
    messages = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": "let me check"},
        {"role": "tool", "content": '{"output": "", "exit_code": 3, "error": null}'},
    ]
    live_tool_calls = [
        {"name": "terminal", "args": {"command": "exit 3"}, "done": True, "is_error": True},
    ]

    tool_calls = _extract_tool_calls_from_messages(messages, live_tool_calls=live_tool_calls)

    assert tool_calls, "the unresolved tool message must still produce a summary"
    assert tool_calls[0].get("is_error") is True, (
        "the live-fallback summary must carry the live is_error so an "
        "unresolved tool message does not settle green (Finding 2)"
    )


def test_hydration_merges_authoritative_is_error_and_status():
    """Finding 2, second half: even once the settled summary carries
    ``is_error``, hydration must merge the authoritative flag and its
    corresponding ``status`` into the row.

    ``merge_duplicate_tool_row`` currently merges snippet/preview/args
    but never ``is_error`` nor ``status``, so an incoming authoritative
    error row can be absorbed by a completed non-error row.
    """
    from api import routes
    import inspect

    src = inspect.getsource(routes)
    start = src.index("def merge_duplicate_tool_row(")
    # Bound the block by the next module-level def so the whole body is
    # covered regardless of size (a fixed char window truncates it).
    next_def = src.find("\ndef ", start + 1)
    merge_block = src[start:next_def if next_def != -1 else start + 12000]

    assert "is_error" in merge_block, (
        "merge_duplicate_tool_row must merge the authoritative is_error "
        "from the incoming row so a persisted error status survives "
        "hydration (Finding 2)"
    )
    assert '"status"' in merge_block or "'status'" in merge_block, (
        "merge_duplicate_tool_row must merge the corresponding status "
        "so the hydrated row renders Failed, not Completed (Finding 2)"
    )


def test_hydration_merges_error_status_from_settled_summary():
    """Behavioural half of the hydration merge: drive the real
    ``_complete_hydrated_anchor_scene`` with a session whose settled
    tool_calls summary carries ``is_error: True`` and assert the
    resulting scene row renders as an error, not completed."""
    import inspect

    from api import routes

    src = inspect.getsource(routes._complete_hydrated_anchor_scene)
    # The settled summaries loop must be able to project is_error into
    # the row — either by building the row from a summary that already
    # carries it, or by merging the flag explicitly.
    assert "is_error" in src, (
        "_complete_hydrated_anchor_scene must project the settled "
        "summary's is_error into the scene row (Finding 2)"
    )


def test_completion_only_replay_copies_is_error():
    """#7358 round-3 Finding 3 (SILENT): a ``tool_complete`` event
    carrying ``is_error: true`` with NO matching start event replays as
    a completed, non-error row.

    ``update_completed_tool`` only copies ``is_error`` onto a call it
    found in the running-calls list; when it falls through to the
    synthesized-call branch it never copies the payload boolean.
    """
    from api import routes
    import inspect

    src = inspect.getsource(routes)
    start = src.index("def update_completed_tool(")
    block = src[start:start + 3000]
    # The synthesized-call branch is the tail of the function after the
    # ``for call in reversed(tool_calls)`` loop.
    synth_idx = block.index("call = {")
    synth_block = block[synth_idx:]

    assert "is_error" in synth_block, (
        "the completion-only replay branch must copy the completion "
        "payload's boolean into the synthesized call (Finding 3)"
    )


def test_completion_only_replay_keeps_is_error_behaviourally():
    """#7358 round-3 Finding 3 (SILENT), behavioural half.

    Drives the REAL replay path: a run journal that contains ONLY a
    ``tool_complete`` event (the start event was lost) with
    ``is_error: true`` in the payload. ``update_completed_tool`` finds
    no running call to update and falls through to its synthesized-call
    branch, which previously dropped the payload boolean — so the
    replayed row rendered as a completed, non-error row.
    """
    from api import models, routes
    from api.run_journal import RunJournalWriter

    session_dir = models.SESSION_DIR
    session_id = "completiononly1"
    stream_id = "stream-completion-only-1"

    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    writer.append_sse_event(
        "tool_complete",
        {
            "name": "terminal",
            "tid": "call-co-1",
            "preview": '{"output": "", "exit_code": 3, "error": null}',
            "args": {"command": "exit 3"},
            "is_error": True,
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None, "the journal must replay into a live snapshot"
    scene = snapshot.get("anchor_activity_scene")
    assert isinstance(scene, dict), "the snapshot must carry an anchor scene"
    tool_rows = [r for r in scene.get("activity_rows") or [] if r.get("role") == "tool"]
    assert tool_rows, "the completion-only event must synthesize a tool row"
    row = tool_rows[0]
    tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
    assert row.get("tool_call_id") == "call-co-1" or tool.get("id") == "call-co-1"
    assert tool.get("is_error") is True, (
        "a completion-only replay must carry the payload's is_error into "
        "the synthesized call (Finding 3)"
    )
    assert row.get("status") == "error", (
        "the completion-only replayed row must render as an error, not "
        "completed (Finding 3)"
    )


def test_normal_settlement_keeps_live_is_error_end_to_end():
    """#7358 round-3 Finding 2 (SILENT), end-to-end.

    Exercises the whole normal-settlement chain the reviewer traced:
    the emission helper classifies a real failed terminal payload,
    writes it into the live mirror, and the settled summary built from
    the final messages must still carry it. This is the path that made
    a red live card become "Completed" after settle + reload.
    """
    from api.streaming import (
        _emit_tool_complete_to_mirrors_and_sse,
        _extract_tool_calls_from_messages,
    )

    # Real captured Agent payload for a failed terminal command.
    real_failure = '{"output": "", "exit_code": 3, "error": null}'

    live_tcs = [{"tid": "t-settle", "name": "terminal", "args": {"command": "exit 3"}, "done": False}]
    shared_tcs = [{"tid": "t-settle", "name": "terminal", "args": {"command": "exit 3"}, "done": False}]
    emitted = []

    is_error = _emit_tool_complete_to_mirrors_and_sse(
        tool_call_id="t-settle",
        name="terminal",
        args={"command": "exit 3"},
        function_result=real_failure,
        live_tool_calls_list=live_tcs,
        shared_tool_calls_list=shared_tcs,
        put=lambda kind, payload: emitted.append((kind, payload)),
        record_live_tool_complete=lambda *a, **k: None,
        args_snapshot_fn=lambda args: dict(args) if isinstance(args, dict) else {},
    )
    assert is_error is True
    assert emitted[0][1]["is_error"] is True

    # Normal settlement: the final message history does not carry the
    # error bit, only the live mirror does.
    messages = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t-settle", "name": "terminal", "input": {"command": "exit 3"}},
            ],
        },
        {"role": "tool", "tool_call_id": "t-settle", "content": real_failure},
        {"role": "assistant", "content": "The command failed with exit code 3."},
    ]

    settled = _extract_tool_calls_from_messages(messages, live_tool_calls=live_tcs)
    assert settled, "settlement must produce a summary"
    summary = next(tc for tc in settled if tc.get("tid") == "t-settle")
    assert summary.get("is_error") is True, (
        "normal settlement must preserve the live is_error into the "
        "persisted summary — this is the Finding 2 regression (red live "
        "card becoming Completed after settle + reload)"
    )

    # The settled summary must project into a hydrated scene row that
    # renders as an error, via _anchor_scene_tool_row.
    from api.routes import _anchor_scene_tool_row

    row = _anchor_scene_tool_row(summary, 0, 1, "stream-settle")
    assert isinstance(row, dict)
    assert row["tool"]["is_error"] is True, (
        "the hydrated row must read is_error from the settled summary"
    )
    assert row["status"] == "error", (
        "the hydrated row must render as error so the card stays red"
    )


def test_captured_payload_fixture_matches_the_real_agent_payloads():
    """The classifier's real-payload constants must stay byte-identical
    to the payloads captured from the Agent's own terminal tool.

    This guards against the exact failure mode the reviewer called out:
    a fixture that "looks right" but does not match what the Agent
    emits. Re-run ``tests/fixtures/capture_tool_payloads.py`` against a
    current hermes-agent checkout to refresh the fixture, then update the
    constants here deliberately.
    """
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "captured_tool_payloads.json").read_text()
    )
    by_label = {p["label"]: p for p in fixture["payloads"]}

    assert REAL_TERMINAL_SUCCESS == by_label["success"]["function_result"], (
        "the success constant must match the captured Agent payload verbatim"
    )
    assert REAL_TERMINAL_FAILURE == by_label["failure"]["function_result"], (
        "the failure constant must match the captured Agent payload verbatim"
    )
    assert REAL_TERMINAL_GREP_NO_MATCH == by_label["grep_no_match"]["function_result"]

    # And the classifier must agree with the captured expectations.
    from api.streaming import _tool_result_is_error

    for payload in fixture["payloads"]:
        got = _tool_result_is_error(payload["tool_name"], payload["function_result"])
        assert got is payload["expected_is_error"], (
            f"captured payload {payload['label']!r} classified as {got}, "
            f"expected {payload['expected_is_error']}"
        )
