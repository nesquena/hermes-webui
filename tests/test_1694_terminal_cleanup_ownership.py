"""Regression tests for #1694 terminal stream cleanup ownership.

Terminal SSE events for one session must not mutate another currently viewed
active pane. The owning session's persisted/runtime stream marker can be cleared,
but global pane state such as ``clearInflight()``, approval/clarify polling, and
``setBusy(false)`` must be gated to the session that owns the active pane/card.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _body_from_brace(src: str, brace: int, label: str) -> str:
    assert brace >= 0, f"body opening brace not found for: {label}"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"body did not close for: {label}"
    return src[brace + 1 : i - 1]


def _brace_body_after(src: str, marker: str) -> str:
    start = src.find(marker)
    assert start >= 0, f"marker not found: {marker}"
    brace = src.find("{", start)
    return _body_from_brace(src, brace, marker)


def _event_body(event_name: str) -> str:
    return _brace_body_after(MESSAGES_JS, f"source.addEventListener('{event_name}'")


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = MESSAGES_JS.find("){", start)
    assert signature_end >= 0, f"function body not found: {name}"
    return _body_from_brace(MESSAGES_JS, signature_end + 1, name)


def test_terminal_handlers_use_session_owned_cleanup_helpers():
    """Patch #1694 should centralize terminal cleanup behind owner-aware helpers."""
    attach_body = _function_body("attachLiveStream")
    assert "function _clearOwnerInflightState()" in attach_body
    owner_helper = _function_body("_clearOwnerInflightState")
    assert "delete INFLIGHT[activeSid]" in owner_helper
    assert "clearInflightState(activeSid)" in owner_helper
    assert "_clearActivePaneInflightIfOwner();" in owner_helper
    assert "function _clearActivePaneInflightIfOwner()" in attach_body
    assert "function _clearApprovalForOwner()" in attach_body
    assert "function _clearClarifyForOwner(" in attach_body
    assert "function _setActivePaneIdleIfOwner(" in attach_body


def test_done_event_does_not_clear_active_pane_for_background_session():
    """A background done event may clear its owner marker, not the active pane."""
    body = _event_body("done")
    assert "_clearOwnerInflightState();" in body
    assert "clearInflight();clearInflightState(activeSid)" not in body
    assert "delete INFLIGHT[activeSid];\n      clearInflight();" not in body
    assert "renderSessionList();setBusy(false)" not in body
    assert "_setActivePaneIdleIfOwner" in body


def test_error_and_cancel_events_do_not_blanket_stop_active_pane_polling():
    """Background app errors/cancels must not stop another pane's prompt polling."""
    for event_name in ("apperror", "cancel"):
        body = _event_body(event_name)
        assert "_clearOwnerInflightState();" in body, event_name
        assert "_clearApprovalForOwner" in body, event_name
        assert "_clearClarifyForOwner" in body, event_name
        assert "stopApprovalPolling();stopClarifyPolling();" not in body, event_name
        assert "clearInflight();clearInflightState(activeSid)" not in body, event_name


def test_reconnect_settled_and_error_paths_keep_cleanup_session_scoped():
    """Reconnect terminal cleanup paths should follow the same owner model."""
    restore_body = _function_body("_restoreSettledSession")
    error_body = _function_body("_handleStreamError")
    combined = restore_body + "\n" + error_body
    assert combined.count("_clearOwnerInflightState();") >= 2
    assert "delete INFLIGHT[activeSid];clearInflight();clearInflightState(activeSid)" not in combined
    assert "stopApprovalPolling();stopClarifyPolling();" not in combined
    assert "renderSessionList();setBusy(false)" not in combined
    assert "_setActivePaneIdleIfOwner" in combined
