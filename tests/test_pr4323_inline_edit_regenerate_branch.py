"""Regression tests for PR #4323 — inline edit/regenerate preserve source session.

Inline edit and assistant regenerate used to call POST /api/session/truncate
in place, destructively rewinding the active transcript. They now branch the
conversation from the message point, load the new branch, and send from there,
leaving the original session untouched.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Slice the body of ``async function <name>`` (or ``function <name>``)."""
    needle_async = f"async function {name}"
    needle_sync = f"function {name}"
    if needle_async in src:
        start = src.index(needle_async)
    elif needle_sync in src:
        start = src.index(needle_sync)
    else:
        raise AssertionError(f"function {name!r} not found")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not closed")


# ---------------------------------------------------------------------------
# Edit and regenerate must not destructively truncate
# ---------------------------------------------------------------------------

def test_submitEdit_does_not_call_truncate():
    """submitEdit must not call /api/session/truncate."""
    body = _function_body(UI_JS, "submitEdit")
    assert "/api/session/truncate" not in body, (
        "submitEdit should not call /api/session/truncate; it must branch "
        "instead. See PR #4323."
    )
    assert "S.messages = S.messages.slice" not in body, (
        "submitEdit must not mutate S.messages manually with slice()."
    )


def test_regenerateResponse_does_not_call_truncate():
    """regenerateResponse must not call /api/session/truncate."""
    body = _function_body(UI_JS, "regenerateResponse")
    assert "/api/session/truncate" not in body, (
        "regenerateResponse should not call /api/session/truncate; it must "
        "branch instead. See PR #4323."
    )
    assert "S.messages = S.messages.slice" not in body, (
        "regenerateResponse must not mutate S.messages manually with slice()."
    )


# ---------------------------------------------------------------------------
# Both paths branch instead of truncating
# ---------------------------------------------------------------------------

def test_submitEdit_uses_branch_path():
    """submitEdit must delegate to the branch-based resend helper."""
    body = _function_body(UI_JS, "submitEdit")
    assert "_branchFromMessageForResend(" in body, (
        "submitEdit must use _branchFromMessageForResend to preserve the "
        "source session. See PR #4323."
    )


def test_regenerateResponse_uses_branch_path():
    """regenerateResponse must delegate to the branch-based resend helper."""
    body = _function_body(UI_JS, "regenerateResponse")
    assert "_branchFromMessageForResend(" in body, (
        "regenerateResponse must use _branchFromMessageForResend to preserve "
        "the source session. See PR #4323."
    )


def test_branch_helper_calls_branch_endpoint():
    """The shared helper must call /api/session/branch."""
    body = _function_body(UI_JS, "_branchFromMessageForResend")
    assert "'/api/session/branch'" in body, (
        "_branchFromMessageForResend must call the /api/session/branch "
        "endpoint. See PR #4323."
    )
    assert re.search(r"keep_count\s*:\s*absoluteKeepCount", body), (
        "_branchFromMessageForResend must send keep_count:absoluteKeepCount."
    )


# ---------------------------------------------------------------------------
# Coordinate safety: absolute keep_count captured before any await
# ---------------------------------------------------------------------------

def test_branch_helper_captures_absolute_count_before_await():
    """The absolute keep_count must be captured before _ensureAllMessagesLoaded."""
    body = _function_body(UI_JS, "_branchFromMessageForResend")
    capture_match = re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*localIdx", body)
    assert capture_match, (
        "_branchFromMessageForResend must compute absoluteKeepCount as "
        "_oldestIdx + localIdx. See PR #4323."
    )
    capture_idx = capture_match.start()

    await_match = re.search(r"\bawait\b", body)
    assert await_match, "Helper should contain at least one await"
    assert capture_idx < await_match.start(), (
        "absoluteKeepCount must be captured BEFORE the first await, because "
        "_ensureAllMessagesLoaded resets _oldestIdx. See PR #4323."
    )


def test_submitEdit_passes_msgIdx_to_branch_helper():
    """submitEdit must forward its raw msgIdx so the helper can compute absolute count."""
    body = _function_body(UI_JS, "submitEdit")
    assert re.search(r"_branchFromMessageForResend\(\s*msgIdx", body), (
        "submitEdit must pass msgIdx to _branchFromMessageForResend so the "
        "absolute keep_count includes _oldestIdx. See PR #4323."
    )


def test_regenerateResponse_passes_assistantIdx_to_branch_helper():
    """regenerateResponse must forward the assistant raw index to the helper."""
    body = _function_body(UI_JS, "regenerateResponse")
    assert re.search(r"_branchFromMessageForResend\(\s*assistantIdx", body), (
        "regenerateResponse must pass assistantIdx to "
        "_branchFromMessageForResend so the absolute keep_count includes "
        "_oldestIdx. See PR #4323."
    )


# ---------------------------------------------------------------------------
# New branch is loaded before sending
# ---------------------------------------------------------------------------

def test_branch_helper_loads_new_session_before_send():
    """The helper must switch to the returned branch before calling send()."""
    body = _function_body(UI_JS, "_branchFromMessageForResend")
    target_idx = body.index("const targetSid = data.session_id")
    load_idx = body.index("loadSession(targetSid)")
    guard_idx = body.index("S.session.session_id !== targetSid")
    send_idx = body.index("await send()")
    assert target_idx < load_idx < guard_idx < send_idx, (
        "_branchFromMessageForResend must load the new branch, verify the "
        "active session is still the branch target, then call send(). See PR #4323."
    )


def test_branch_helper_refreshes_session_list():
    """The helper should refresh the session list after branching."""
    body = _function_body(UI_JS, "_branchFromMessageForResend")
    assert "renderSessionList" in body, (
        "_branchFromMessageForResend should refresh the session list after "
        "creating a branch. See PR #4323."
    )
