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


def _function_body(name: str) -> str:
    marker = f"function {name}("
    start = MESSAGES_JS.find(marker)
    assert start >= 0, f"function not found: {name}"
    signature_end = MESSAGES_JS.find(")", start)
    assert signature_end >= 0, f"function signature not found: {name}"
    brace = MESSAGES_JS.find("{", signature_end)
    return _body_from_brace(MESSAGES_JS, brace, name)


def test_busy_dips_no_longer_force_stop_approval_polling():
    body = _function_body("_startApprovalFallbackPoll")

    assert "_approvalPollingSessionMissingOrMismatched(sid)" in body
    assert "!S.busy || !S.session || S.session.session_id !== sid" not in body


def test_empty_pending_clears_card_and_only_stops_after_confirmed_idle():
    fallback_body = _function_body("_startApprovalFallbackPoll")

    assert "else if (!_approvalPollingSessionMissingOrMismatched(sid)) {" in fallback_body
    assert "_clearApprovalPendingForSession(sid);" in fallback_body
    assert "_hideApprovalCardIfOwner(sid);" in fallback_body
    assert "if (!S.busy) {" in fallback_body
    assert "stopApprovalPollingForSession(sid);" in fallback_body
    assert "else if (!_approvalPollingSessionMissingOrMismatched(sid) && !S.busy)" not in fallback_body
    assert "stopApprovalPolling(); _hideApprovalCardIfOwner(sid, true); return;" in fallback_body
