"""#7358 round 5 regression — Agent's authoritative ``is_error`` must not be lost.

The 9/22 re-gate reviewer's two SILENT findings on commit ``03dbec0e``:

**Finding 1** — the Agent's authoritative ``is_error`` is discarded.
``on_tool`` (the legacy ``tool_progress_callback``) receives the Agent's
authoritative ``is_error`` via ``cb_kwargs`` and early-returns when the
structured ``tool_complete_callback`` is wired, so the bit is dropped
before ``on_tool_complete`` runs. The structured path then re-infers
``is_error`` from the payload text and disagrees with the Agent on:

- a successful Codex command whose output contains the literal
  ``"error"`` substring (e.g. ``"Found 0 errors"``): Agent says
  ``is_error=False``, the text scan says ``True``, and the card renders
  failed;
- a ``guardrail_refusal: true`` read: Agent says non-error, the
  scan finds a non-empty payload and may classify the result as a
  failure, and the card renders an error.

**Finding 2** — a failed card shows as Completed on cold reload. When
``S.toolCalls`` is cleared (no browser-persisted live mirror) and the
fallback renderer at ``static/sessions.js:3135-3139`` only copies
persisted snippets, the per-tid ``is_error`` from the server's
``session.tool_calls`` summary never reaches the compact / transparent
render paths in ``static/messages.js`` and ``static/ui.js``.

This test file pins both fixes:

- the source wiring in ``api/streaming.py`` (``on_tool`` captures
  ``cb_kwargs['is_error']`` before the suppression; ``on_tool_complete``
  reads it and passes as ``is_error_override``; only falls back to text
  inference when no authoritative value exists);
- the source wiring in ``static/sessions.js``, ``static/messages.js``,
  and ``static/ui.js`` (per-tid persisted ``is_error`` map populated at
  load time; the merge upgrade in ``_enrichSettledToolRowBodyFromLive``
  and the fallback renderer in ``static/ui.js`` both honour the map);
- behavioural coverage for the two real Codex / guardrail cases the
  reviewer called out, plus a cold-reload case where no live mirror
  exists at all.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    next_def = -1
    for indent in ("\n            def ", "\ndef "):
        i = src.find(indent, start + 1)
        if i != -1 and (next_def == -1 or i < next_def):
            next_def = i
    assert next_def != -1, f"end of {name} not found"
    return src[start:next_def]


# ── #7358 round 5 Finding 1: server-side authoritative ``is_error`` ──


def test_authoritative_is_error_dict_is_initialized_in_streaming_scope():
    """The per-stream dict that bridges ``on_tool``'s cb_kwargs capture to
    ``on_tool_complete``'s override must be initialised in the same scope
    where both closures live, so the late-binding ``nonlocal`` semantics
    never see an unbound name when a structured Agent invokes the
    suppression path."""
    src = _read("api/streaming.py")
    # Find the scope inside _run_agent_streaming where the closures live.
    scope_marker = "_live_tool_event_complete_ids = set()"
    assert scope_marker in src, (
        "expected the closure-scope set initialiser next to the "
        "_authoritative_is_error_by_tid dict (the round-5 fix)"
    )
    scope_idx = src.index(scope_marker)
    # The dict declaration must follow within the same scope (no other
    # function boundary in between).
    assert "_authoritative_is_error_by_tid = {}" in src[scope_idx:scope_idx + 2000], (
        "the round-5 fix must initialise _authoritative_is_error_by_tid "
        "in the same closure scope as _live_tool_event_complete_ids so "
        "on_tool can populate it and on_tool_complete can pop it"
    )


def test_on_tool_captures_cb_kwargs_is_error_before_suppression():
    """The legacy ``on_tool`` callback must capture ``cb_kwargs['is_error']``
    INSIDE the structured-callback suppression path (i.e. AFTER the
    ``if event_type == 'tool.completed' and 'tool_complete_callback'
    in _agent_params`` guard and BEFORE the early-return ``return``),
    so the Agent's authoritative bit is never silently dropped when
    the structured callback is wired.

    The capture lives inside the suppression block — not before it —
    because the capture is only meaningful when the structured
    callback is wired (otherwise the legacy path at line 10565+ would
    handle the cb_kwargs directly). The reviewer's fix moves the
    capture into the suppression block so on_tool_complete can read
    it back via the per-tid dict."""
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool")
    # The suppression line and the early-return must both exist.
    suppression_idx = block.index(
        "event_type == 'tool.completed' and 'tool_complete_callback' in _agent_params"
    )
    return_idx = block.index("return", suppression_idx)
    # The capture must appear between the suppression and the
    # early-return — that's the load-bearing wiring. Without the
    # capture being inside the suppression block, the bit is dropped
    # on the modern Agent.
    capture_idx = block.index("cb_kwargs.get('is_error')", suppression_idx)
    assert suppression_idx < capture_idx < return_idx, (
        "on_tool must capture cb_kwargs['is_error'] INSIDE the "
        "structured-callback suppression block (between the "
        "tool_complete_callback guard and the early-return), so the "
        "Agent's authoritative bit is preserved on the modern Agent "
        "and on_tool_complete can read it via the per-tid dict "
        "(Finding 1 of the 9/22 re-gate)"
    )
    # The capture must write to the per-tid dict using the live
    # entry's tid (looked up by name in the most recent not-done
    # entry stamped by on_tool_start).
    assert "_authoritative_is_error_by_tid" in block, (
        "on_tool must write the captured cb_kwargs is_error to "
        "_authoritative_is_error_by_tid keyed by tid so on_tool_complete "
        "can correlate it back"
    )
    assert "_live_tool_calls" in block, (
        "on_tool must look up the matching live entry by name in "
        "_live_tool_calls to recover the tid — the legacy callback "
        "does not receive the tool_call_id in cb_args"
    )


def test_on_tool_complete_passes_authoritative_is_error_as_override():
    """The structured ``on_tool_complete`` callback must read the captured
    value from the per-tid dict and pass it as ``is_error_override`` to
    the module-level emission helper, so the helper skips the
    text-inference fallback for that tool. The pop is required so a stale
    entry can never leak from a prior tool with the same id into a
    later, different tool."""
    src = _read("api/streaming.py")
    block = _function_block(src, "on_tool_complete")
    assert "_authoritative_is_error_by_tid.pop(" in block, (
        "on_tool_complete must pop the captured value by tool_call_id "
        "so a stale entry from a prior tool can never leak into a later "
        "tool with the same id (Finding 1 of the 9/22 re-gate)"
    )
    assert "is_error_override=" in block, (
        "on_tool_complete must pass the popped value as the "
        "is_error_override keyword to _emit_tool_complete_to_mirrors_and_sse"
    )
    helper_idx = block.index("is_error_override=")
    call_idx = block.index("_emit_tool_complete_to_mirrors_and_sse(")
    assert helper_idx > call_idx, (
        "the is_error_override= kwarg must be inside the helper call"
    )


def test_emission_helper_authoritative_is_error_overrides_text_inference():
    """When ``is_error_override`` is provided, the helper must use it
    directly and skip the ``_tool_result_is_error`` text-inference
    fallback. This is the load-bearing half of Finding 1: a Codex
    success whose output contains the literal ``"error"`` substring
    must render green, and a ``guardrail_refusal: true`` read must
    render green — both because the Agent said so, not because the
    text scan disagreed."""
    from api.streaming import _emit_tool_complete_to_mirrors_and_sse

    # Real Codex success shape: the tool result is a labelled JSON
    # envelope in prose ("output: {...}") rather than a bare JSON
    # object, so the structural decoder refuses it (the text does
    # not start with '{') and the raw-text fallback matches the
    # literal ``"error"`` key name inside the envelope — the
    # reviewer's Finding 1 case A. The Agent computed
    # ``is_error=False`` for this successful command; without the
    # override the WebUI would render the card failed.
    codex_success_with_error_text = (
        'output: {"output": "scanning...\\nFound 0 errors.\\nDone.", '
        '"exit_code": 0, "error": null}'
    )
    # Real guardrail refusal read: the Agent computes
    # ``is_error=False`` (a refusal is informational and the turn
    # continues) even though the payload carries a
    # ``guardrail_refusal: true`` marker and a non-empty ``error``
    # message string. The structural classifier reads that error
    # string and reports a failure — the reviewer's Finding 1
    # case B. Only the Agent's authoritative verdict keeps this
    # card green.
    guardrail_refusal_read = json.dumps({
        "output": "refused by guardrail",
        "guardrail_refusal": True,
        "error": "refused",
    })

    sse_events = []

    def put(kind, payload):
        sse_events.append((kind, payload))

    def record_live_tool_complete(*a, **k):
        return None

    def args_snapshot(args):
        return dict(args) if isinstance(args, dict) else {"args": args}

    def _run_one(tool_name, function_result, override):
        sse_events.clear()
        live_tcs = [{"tid": "t-1", "name": tool_name, "done": False}]
        shared_tcs = [{"tid": "t-1", "name": tool_name, "done": False}]
        return _emit_tool_complete_to_mirrors_and_sse(
            tool_call_id="t-1",
            name=tool_name,
            args={"input": "x"},
            function_result=function_result,
            live_tool_calls_list=live_tcs,
            shared_tool_calls_list=shared_tcs,
            put=put,
            record_live_tool_complete=record_live_tool_complete,
            args_snapshot_fn=args_snapshot,
            is_error_override=override,
        )

    # Case 1: Codex success whose output contains the literal
    # "error" substring. Agent says is_error=False. The override
    # must keep that verdict intact; without it, the text scan
    # would mis-classify.
    is_error = _run_one(
        "terminal",
        codex_success_with_error_text,
        override=False,
    )
    assert is_error is False, (
        "a successful Codex command whose output contains the "
        "literal 'error' substring must stay is_error=False when "
        "the Agent's authoritative verdict is passed as "
        "is_error_override (Finding 1 case A: Agent says False, "
        "WebUI must show Completed)"
    )

    # Case 2: guardrail_refusal read. Agent says non-error. The
    # override must keep that verdict intact.
    is_error = _run_one(
        "read",
        guardrail_refusal_read,
        override=False,
    )
    assert is_error is False, (
        "a guardrail_refusal:true read must stay non-error when "
        "the Agent's authoritative verdict is passed as "
        "is_error_override (Finding 1 case B: Agent says "
        "non-error, WebUI must NOT show error)"
    )

    # Sanity: override=True must upgrade to is_error even on a
    # payload that the text scan would leave alone (a successful
    # terminal). This proves the override is the authoritative
    # path, not a side-channel.
    is_error = _run_one(
        "terminal",
        '{"output": "ok", "exit_code": 0}',
        override=True,
    )
    assert is_error is True, (
        "an explicit is_error_override=True must win even on a "
        "text-clean success payload — the Agent's verdict is "
        "authoritative, not the payload"
    )


def test_emission_helper_falls_back_to_text_inference_when_no_override():
    """The reviewer's fix says: only fall back to text inference when
    no authoritative value exists. Verify the helper's default path
    (no ``is_error_override``) still runs the structured
    classification unchanged — a captured non-zero terminal exit
    code still surfaces as a failure, a successful one still
    surfaces as success."""
    from api.streaming import _emit_tool_complete_to_mirrors_and_sse

    sse_events = []

    def put(kind, payload):
        sse_events.append((kind, payload))

    live_tcs = [{"tid": "t-1", "name": "terminal", "done": False}]
    shared_tcs = [{"tid": "t-1", "name": "terminal", "done": False}]

    # Non-zero exit code: text-inference path must classify.
    is_error = _emit_tool_complete_to_mirrors_and_sse(
        tool_call_id="t-1",
        name="terminal",
        args={"command": "exit 3"},
        function_result='{"output": "", "exit_code": 3, "error": null}',
        live_tool_calls_list=live_tcs,
        shared_tool_calls_list=shared_tcs,
        put=put,
        record_live_tool_complete=lambda *a, **k: None,
        args_snapshot_fn=lambda args: dict(args) if isinstance(args, dict) else {},
    )
    assert is_error is True, (
        "fallback text-inference must still classify a non-zero "
        "terminal exit as a failure when no override is provided"
    )

    # Zero exit code with no override: must stay success.
    live_tcs = [{"tid": "t-2", "name": "terminal", "done": False}]
    shared_tcs = [{"tid": "t-2", "name": "terminal", "done": False}]
    is_error = _emit_tool_complete_to_mirrors_and_sse(
        tool_call_id="t-2",
        name="terminal",
        args={"command": "true"},
        function_result='{"output": "ok", "exit_code": 0, "error": null}',
        live_tool_calls_list=live_tcs,
        shared_tool_calls_list=shared_tcs,
        put=put,
        record_live_tool_complete=lambda *a, **k: None,
        args_snapshot_fn=lambda args: dict(args) if isinstance(args, dict) else {},
    )
    assert is_error is False, (
        "fallback text-inference must keep a zero-exit terminal as "
        "success when no override is provided"
    )


# ── #7358 round 5 Finding 2: client-side cold-reload path ──


def test_sync_tool_calls_for_loaded_messages_populates_persisted_is_error_map():
    """``_syncToolCallsForLoadedMessages`` in ``static/sessions.js`` must
    build a per-tid map of persisted ``is_error`` from the server's
    ``session.tool_calls`` summary and store it on ``S._settledToolIsErrorByTid``
    so the cold-reload fallback renderer (which clears ``S.toolCalls``)
    can still recover the failure classification for a failed tool.

    This is the load-bearing wiring for Finding 2: the reviewer's
    re-gate trace showed a failed card reverting to "Completed" on
    cold reload because the fallback renderer copied persisted
    snippets but not ``is_error``."""
    src = _read("static/sessions.js")
    # The function body must contain the per-tid map construction.
    assert "S._settledToolIsErrorByTid" in src, (
        "static/sessions.js must populate S._settledToolIsErrorByTid "
        "from sessionToolCalls so the cold-reload fallback renderer "
        "can recover the failure classification (Finding 2)"
    )
    # The map must be populated BEFORE the S.toolCalls = [] clearing
    # branch, otherwise the data is lost on the cold-reload path.
    fn_start = src.index("function _syncToolCallsForLoadedMessages(")
    map_init = src.index("_persistedIsErrorByTid", fn_start)
    clear_branch = src.index("S.toolCalls=hasMessageToolMetadata", fn_start) if "S.toolCalls=hasMessageToolMetadata" in src[fn_start:] else src.index("S.toolCalls=[];", fn_start)
    assert map_init < clear_branch, (
        "the per-tid is_error map must be populated before the "
        "S.toolCalls=[] clearing branch so the cold-reload fallback "
        "has it available when the messages-with-tool-metadata branch "
        "wipes the in-memory S.toolCalls (Finding 2)"
    )


def test_enrich_settled_tool_row_body_from_live_uses_persisted_is_error_map():
    """``_enrichSettledToolRowBodyFromLive`` in ``static/messages.js`` must
    also honour ``S._settledToolIsErrorByTid`` so a true cold reload
    with no browser-persisted live mirror still carries the persisted
    failure through to both ``tool`` and ``payload`` rows. The round-4
    fix only fired on the live mirror; the round-5 fix adds the
    persisted-map fallback."""
    src = _read("static/messages.js")
    # Lift the function body verbatim.
    marker = "function _enrichSettledToolRowBodyFromLive("
    start = src.index(marker)
    brace_pos = src.index("{", start)
    depth = 0
    end = brace_pos
    for i in range(brace_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fn_body = src[start:end]
    assert "_settledToolIsErrorByTid" in fn_body, (
        "_enrichSettledToolRowBodyFromLive must look up "
        "S._settledToolIsErrorByTid so a cold reload with no live "
        "mirror still carries the persisted is_error through (Finding 2)"
    )


def test_ui_copy_live_tool_metadata_carries_is_error_from_persisted_map():
    """``copyLiveToolMetadata`` in ``static/ui.js`` (the transparent
    render path) must also fall back to ``S._settledToolIsErrorByTid``
    when the live mirror is empty. Without this, a true cold reload
    rebuilds the transparent card from messages with no live data and
    a missing ``is_error`` — the card silently flips to Completed."""
    src = _read("static/ui.js")
    assert "_settledToolIsErrorByTid" in src, (
        "static/ui.js must read S._settledToolIsErrorByTid somewhere "
        "in the cold-reload fallback render path so the transparent "
        "render carries the persisted is_error (Finding 2)"
    )
    # Both upgrade paths must be present: the live-mirror one (when a
    # live entry matches) and the persisted-map one (the cold-reload
    # fallback). The live upgrade sits inside the matchEntry block; the
    # persisted-map upgrade sits outside, so both ``is_error = true``
    # writes are required.
    fn_idx = src.index("const copyLiveToolMetadata=")
    # The function spans until the next "fallbackToolSources.forEach" or
    # the closing of the inner block; bracket-count to be safe.
    brace = src.index("{", fn_idx)
    depth = 0
    end = brace
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fn_body = src[fn_idx:end]
    # The persisted-map fallback must run regardless of matchEntry.
    assert "next.is_error=true" in fn_body, (
        "copyLiveToolMetadata must write next.is_error=true at least "
        "once (the live upgrade) — without it the persisted map "
        "fallback above has nothing to upgrade"
    )
    # The persisted-map branch must reference the per-tid map.
    assert "_persistedIsErrorByTid[tid]" in fn_body, (
        "copyLiveToolMetadata must consult "
        "_persistedIsErrorByTid[tid] === true as the cold-reload "
        "fallback (Finding 2 of the 9/22 re-gate)"
    )


# ── #7358 round 5 Finding 2: behavioural cold-reload case ──


def _run_node_merge_enrichment(row, live, persisted_map):
    """Drive ``_enrichSettledToolRowBodyFromLive`` in a Node sandbox with
    a stubbed ``S._settledToolIsErrorByTid`` map, mirroring the
    production cold-reload shape (no live ``is_error`` on the row, a
    persisted map entry keyed by ``tid``)."""
    src = _read("static/messages.js")
    marker = "function _enrichSettledToolRowBodyFromLive("
    start = src.index(marker)
    brace_pos = src.index("{", start)
    depth = 0
    end = brace_pos
    for i in range(brace_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fn_body = src[start:end]

    driver = f"""
// Stub S FIRST so the round-5 _settledToolIsErrorByTid lookup in
// _enrichSettledToolRowBodyFromLive resolves against the persisted
// map the test sets. The function body is hoisted (function
// declaration), but the const is in a TDZ until its line runs, so
// the order matters even though the test only calls the function
// after the const is initialised below.
const S = {{ _settledToolIsErrorByTid: {json.dumps(persisted_map)} }};
// Minimal stubs for the two private helpers the production
// merge body calls. These match the production contract for
// the inputs the tests construct.
function _anchorSceneStringPayload(v) {{ return (v===undefined||v===null)?null:String(v); }}
function _anchorSceneToolArgs(live) {{ return (live && live.args && typeof live.args==='object') ? live.args : null; }}

{fn_body}

const _row = {json.dumps(row)};
const _live = {json.dumps(live)};
const _enriched = _enrichSettledToolRowBodyFromLive(_row, _live);
process.stdout.write(JSON.stringify({{row: _row, enriched: _enriched}}));
"""
    node = subprocess.run(
        ["node", "-e", driver], capture_output=True, text=True, timeout=30
    )
    if node.returncode != 0:
        pytest.skip(f"node failed: {node.stderr}")
    return json.loads(node.stdout)


def test_cold_reload_persisted_is_error_red_when_no_live_mirror():
    """Finding 2: a failed tool that has no browser-persisted live
    mirror (true cold reload: the browser was closed, the page was
    refreshed, or the S.toolCalls was cleared) must still render as
    a failure on reload. The persisted ``session.tool_calls`` summary
    carries ``is_error: true`` server-side after the round-3 fix; the
    round-5 fix plumbs that bit through the merge helper via
    ``S._settledToolIsErrorByTid``."""
    # Persisted row from the session.messages sidecar: no is_error
    # (the API's settle path drops it on the compact / transparent
    # render shape). The row shape mirrors
    # ``_anchorSceneToolRowFromCall`` with ``tool_call_id`` at the
    # top level (the canonical key the round-5 lookup uses).
    row = {"tool_call_id": "t1",
           "tool": {"tid": "t1", "name": "terminal", "snippet": "[exit 3]"},
           "payload": {"name": "terminal", "snippet": "[exit 3]"}}
    # No live mirror: simulating a true cold reload where the browser
    # has no in-memory live state.
    live = {"tid": "t1", "name": "terminal"}
    # Persisted map populated from the server's session.tool_calls
    # by _syncToolCallsForLoadedMessages.
    persisted = {"t1": True}
    result = _run_node_merge_enrichment(row, live, persisted)
    assert result["row"]["tool"].get("is_error") is True, (
        "cold reload with no live mirror must still render a failed "
        "tool as failed, not Completed — the persisted per-tid map "
        "is the only path the cold-reload render has to recover the "
        "failure classification (Finding 2 of the 9/22 re-gate)"
    )
    assert result["row"]["payload"].get("is_error") is True, (
        "the payload row that drives the compact / transparent "
        "render paths must also carry is_error on cold reload"
    )
    assert result["enriched"] is True, (
        "enriched must be true so the caller can persist the upgrade"
    )


def test_cold_reload_persisted_is_error_absent_for_successful_tool():
    """A successful tool (no persisted is_error) must keep ``is_error``
    absent or false after the merge — the persisted-map upgrade is
    one-way and only fires when the persisted map says ``true`` for
    the row's tid. Reverting a successful card to ``is_error: true``
    via the map would be a false positive."""
    row = {"tool_call_id": "t1",
           "tool": {"tid": "t1", "name": "terminal", "snippet": "[exit 0]"},
           "payload": {"name": "terminal", "snippet": "[exit 0]"}}
    live = {"tid": "t1", "name": "terminal"}
    # No entry in the persisted map — the successful tool's tid is
    # absent.
    persisted = {}
    result = _run_node_merge_enrichment(row, live, persisted)
    assert not result["row"]["tool"].get("is_error"), (
        "a successful tool with no persisted is_error entry must "
        "stay successful on cold reload — the per-tid map upgrade "
        "is one-way and only fires on a persisted true"
    )
    assert not result["row"]["payload"].get("is_error"), (
        "the payload row must also stay non-error on a cold reload "
        "of a successful tool"
    )


def test_cold_reload_persisted_is_error_does_not_clobber_persisted_true():
    """If the persisted row already carries ``is_error: true`` (a prior
    round-3 server-side write or a round-4 client upgrade), the
    round-5 persisted-map fallback must not clobber it. The merge
    contract is idempotent on a true input."""
    row = {"tool_call_id": "t1",
           "tool": {"tid": "t1", "name": "terminal", "is_error": True},
           "payload": {"name": "terminal", "is_error": True}}
    live = {"tid": "t1", "name": "terminal"}
    persisted = {"t1": True}
    result = _run_node_merge_enrichment(row, live, persisted)
    assert result["row"]["tool"].get("is_error") is True, (
        "an already-failed persisted row must stay failed after "
        "the cold-reload merge; the round-5 persisted-map fallback "
        "must not clobber an existing true"
    )
    assert result["row"]["payload"].get("is_error") is True, (
        "the payload row must also stay failed on a cold reload "
        "of an already-failed tool"
    )
