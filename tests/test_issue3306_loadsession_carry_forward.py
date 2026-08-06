"""Regression test pinning the #3306 fix.

#3306: `loadSession()` in static/sessions.js cleared `S.messages = []` BEFORE
issuing the API fetch and BEFORE `_ensureMessagesLoaded()` invoked the #3018
ephemeral-field carry-forward (`_carryForwardEphemeralTurnFields(S.messages||[],
msgs)`). Because the clear happened first, the carry-forward saw an empty
array and ephemeral turn fields (_turnUsage, _turnDuration, _turnTps,
_gatewayRouting, _statusCard) were dropped on every force-reload. The visible
symptom: the token-usage badge vanished ~10s after each assistant turn finished
when an external poll triggered loadSession(..., forceReload).

Fix: snapshot S.messages BEFORE the clear (only when force-reloading the
currently-active session) into a module-level `_pendingCarryForwardSnapshot`,
then consume it in `_ensureMessagesLoaded()` ahead of the live S.messages
(which is now []).

This file is the targeted source-text pin in the same style as
tests/test_issue3162_ensure_messages_loaded.py.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(start_marker: str, end_marker: str) -> str:
    start = SESSIONS_JS.index(start_marker)
    end = SESSIONS_JS.index(end_marker, start)
    return SESSIONS_JS[start:end]


def _load_session_clear_block() -> str:
    """The if(currentSid!==sid||forceReload){...} block in loadSession()."""
    start = SESSIONS_JS.index("async function loadSession(sid)")
    clear_start = SESSIONS_JS.index("if (currentSid !== sid || forceReload) {", start)
    clear_end = SESSIONS_JS.index("// Phase 1: Load metadata only", clear_start)
    return SESSIONS_JS[clear_start:clear_end]


def _ensure_messages_loaded_body() -> str:
    return _function_body(
        "async function _ensureMessagesLoaded(sid",
        "function _messageComparableText",
    )


def test_pending_carry_forward_snapshot_declared_at_module_scope():
    assert "let _pendingCarryForwardSnapshot" in SESSIONS_JS, (
        "module-level _pendingCarryForwardSnapshot is the bridge between "
        "loadSession()'s pre-clear snapshot and _ensureMessagesLoaded()'s "
        "carry-forward call — required by #3306 fix"
    )


def test_loadsession_snapshots_messages_before_clearing():
    body = _load_session_clear_block()
    # The assignment to _pendingCarryForwardSnapshot must appear before the
    # `S.messages = [];` clear inside the if-block.
    assign_idx = body.find("_pendingCarryForwardSnapshot =")
    clear_idx = body.find("S.messages = [];")
    assert assign_idx != -1, (
        "#3306: loadSession() must snapshot S.messages into "
        "_pendingCarryForwardSnapshot before clearing — snapshot assignment missing"
    )
    assert clear_idx != -1, "S.messages = [] clear not found in loadSession()"
    assert assign_idx < clear_idx, (
        "#3306: _pendingCarryForwardSnapshot must be assigned BEFORE "
        "`S.messages = []`, otherwise the snapshot captures an already-empty array"
    )


def test_loadsession_snapshot_gated_on_force_reload_of_active_session():
    body = _load_session_clear_block()
    # The snapshot should only happen when reloading the currently-active session.
    assert "currentSid === sid && forceReload" in body, (
        "#3306: snapshot must be gated on `currentSid === sid && forceReload` "
        "so switching to a different session still gets a clean carry-forward "
        "(prior messages would belong to a different conversation)"
    )


def test_ensure_messages_loaded_consumes_snapshot_then_clears_it():
    body = _ensure_messages_loaded_body()
    assert "_pendingCarryForwardSnapshot" in body, (
        "#3306: _ensureMessagesLoaded() must consult _pendingCarryForwardSnapshot "
        "when calling _carryForwardEphemeralTurnFields"
    )
    # And must reset it afterwards so subsequent non-force loads don't reuse
    # a stale snapshot.
    assert "_pendingCarryForwardSnapshot = null" in body, (
        "#3306: _ensureMessagesLoaded() must reset _pendingCarryForwardSnapshot "
        "to null after consuming it, to avoid leaking stale ephemeral fields "
        "into a later unrelated load"
    )


def _load_older_messages_body() -> str:
    return _function_body(
        "async function _loadOlderMessages() {",
        "async function _ensureAllMessagesLoaded",
    )


def _start_gateway_sse_body() -> str:
    return _function_body(
        "function startGatewaySSE(){",
        "function stopGatewaySSE",
    )


def test_load_older_messages_tail_match_carries_forward():
    """#3306 follow-up: _loadOlderMessages does a wholesale `S.messages = nextMessages`
    on the tail-match path. Without carry-forward this drops ephemeral turn fields
    (the intermittent "badge appears then disappears" symptom flagged in review)."""
    body = _load_older_messages_body()
    cf_idx = body.find("_carryForwardEphemeralTurnFields(S.messages || [], nextMessages)")
    assign_idx = body.find("S.messages = nextMessages;")
    assert cf_idx != -1, (
        "#3306: _loadOlderMessages must carry forward ephemeral turn fields "
        "into nextMessages before the wholesale S.messages assignment"
    )
    assert assign_idx != -1, "S.messages = nextMessages assignment not found"
    assert cf_idx < assign_idx, (
        "#3306: carry-forward call must precede `S.messages = nextMessages` "
        "in _loadOlderMessages, otherwise the fields are already gone"
    )


def test_start_gateway_sse_import_cli_carries_forward():
    """#3306 follow-up: the gateway SSE → import_cli refresh path also does a
    wholesale `S.messages = next` for CLI sessions; it must carry forward
    ephemeral turn fields too."""
    body = _start_gateway_sse_body()
    cf_idx = body.find("_carryForwardEphemeralTurnFields(S.messages || [], next)")
    assign_idx = body.find("S.messages = _nextToAssign;")
    assert cf_idx != -1, (
        "#3306: startGatewaySSE import_cli branch must carry forward "
        "ephemeral turn fields before assigning S.messages"
    )
    assert assign_idx != -1, (
        "expected `S.messages = _nextToAssign;` after carry-forward in "
        "startGatewaySSE import_cli branch"
    )
    assert cf_idx < assign_idx, (
        "#3306: carry-forward call must precede the S.messages assignment "
        "in the startGatewaySSE import_cli branch"
    )


def test_carry_forward_call_still_present():
    """If the #3018 carry-forward is ever removed, this fix becomes moot —
    flag that explicitly so reviewers reconsider the snapshot machinery."""
    body = _ensure_messages_loaded_body().replace(" ", "")
    assert "_carryForwardEphemeralTurnFields" in body, (
        "the #3018 carry-forward is gone — re-evaluate whether "
        "_pendingCarryForwardSnapshot is still needed (#3306)"
    )
