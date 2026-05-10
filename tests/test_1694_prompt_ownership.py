"""Regression tests for #1694 approval/clarify prompt ownership.

Prompt state belongs to the session that owns the running stream. A background
session's approval/clarify event must not render over or hide the currently
active pane's card, but the pending prompt should remain available when the user
switches back to that session.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


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


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = src.find(")", start)
    assert signature_end >= 0, f"function signature not found: {name}"
    brace = src.find("{", signature_end)
    return _body_from_brace(src, brace, name)


def _event_body(event_name: str) -> str:
    return _brace_body_after(MESSAGES_JS, f"source.addEventListener('{event_name}'")


def test_stream_prompt_events_use_session_owned_show_helpers():
    """Background stream prompts should be cached by owner before pane render."""
    approval_body = _event_body("approval")
    clarify_body = _event_body("clarify")
    assert "showApprovalForSession(activeSid" in approval_body
    assert "showApprovalCard(d, 1)" not in approval_body
    assert "showClarifyForSession(activeSid" in clarify_body
    assert "showClarifyCard(d)" not in clarify_body


def test_approval_card_render_is_gated_to_active_session_and_cached():
    body = _function_body(MESSAGES_JS, "showApprovalCard")
    assert "_rememberApprovalPending(" in body
    assert "_approvalPromptBelongsToActiveSession(sid)" in body
    assert "return;" in body
    assert "let _approvalPendingBySession" in MESSAGES_JS
    assert "function _renderPendingPromptsForActiveSession()" in MESSAGES_JS


def test_clarify_card_render_is_gated_to_active_session_and_cached():
    body = _function_body(MESSAGES_JS, "showClarifyCard")
    assert "_rememberClarifyPending(" in body
    assert "_clarifyPromptBelongsToActiveSession(sid)" in body
    assert "return;" in body
    assert "let _clarifyPendingBySession" in MESSAGES_JS
    assert "function _renderPendingPromptsForActiveSession()" in MESSAGES_JS


def test_polling_empty_state_clears_only_the_owner_prompt():
    approval_poll = _function_body(MESSAGES_JS, "startApprovalPolling")
    approval_fallback = _function_body(MESSAGES_JS, "_startApprovalFallbackPoll")
    clarify_poll = _function_body(MESSAGES_JS, "startClarifyPolling")
    clarify_fallback = _function_body(MESSAGES_JS, "_startClarifyFallbackPoll")
    combined = "\n".join(
        [approval_poll, approval_fallback, clarify_poll, clarify_fallback]
    )
    assert "_clearApprovalPendingForSession(sid)" in combined
    assert "_hideApprovalCardIfOwner(sid" in combined
    assert "_clearClarifyPendingForSession(sid)" in combined
    assert "_hideClarifyCardIfOwner(sid" in combined
    assert "else { hideApprovalCard(); }" not in combined
    assert "else { hideClarifyCard(false, 'expired'); }" not in combined
    assert "stopApprovalPolling(); hideApprovalCard(true); return;" not in combined
    assert (
        "stopClarifyPolling(); hideClarifyCard(true, 'session'); return;"
        not in combined
    )


def test_load_session_rerenders_cached_prompt_for_new_active_session():
    body = _function_body(SESSIONS_JS, "loadSession")
    assert "_renderPendingPromptsForActiveSession();" in body


def test_prompt_rerender_hides_previous_session_cards_without_clearing_cache():
    approval_body = _function_body(
        MESSAGES_JS, "_renderPendingApprovalForActiveSession"
    )
    clarify_body = _function_body(MESSAGES_JS, "_renderPendingClarifyForActiveSession")
    assert "_approvalSessionId && _approvalSessionId !== sid" in approval_body
    assert "hideApprovalCard(true)" in approval_body
    assert "_clarifySessionId && _clarifySessionId !== sid" in clarify_body
    assert "hideClarifyCard(true, 'session')" in clarify_body
