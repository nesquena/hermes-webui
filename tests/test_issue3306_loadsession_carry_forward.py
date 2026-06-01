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


def _load_session_clear_block() -> str:
    """The if(currentSid!==sid||forceReload){...} block in loadSession()."""
    start = SESSIONS_JS.index("async function loadSession(sid)")
    return SESSIONS_JS[start: start + 4000]


def _ensure_messages_loaded_body() -> str:
    start = SESSIONS_JS.index("async function _ensureMessagesLoaded")
    return SESSIONS_JS[start: start + 2500]


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


def test_carry_forward_call_still_present():
    """If the #3018 carry-forward is ever removed, this fix becomes moot —
    flag that explicitly so reviewers reconsider the snapshot machinery."""
    body = _ensure_messages_loaded_body().replace(" ", "")
    assert "_carryForwardEphemeralTurnFields" in body, (
        "the #3018 carry-forward is gone — re-evaluate whether "
        "_pendingCarryForwardSnapshot is still needed (#3306)"
    )
