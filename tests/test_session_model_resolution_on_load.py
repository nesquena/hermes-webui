"""Regression tests for session switch model hydration in the WebUI.

Old sessions can persist provider-shaped model IDs such as ``openai/gpt-5.4-mini``
after the active runtime moved to OpenAI Codex ``gpt-5.5``.  The UI still needs
to repair those stale values, but session switching first paint must not pay the
model catalog cost synchronously.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _extract_function(src: str, signature: str) -> str:
    start = src.find(signature)
    assert start >= 0, f"missing function signature: {signature}"
    brace = src.find("{", start)
    assert brace >= 0, f"missing function body for: {signature}"
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"unterminated function body for: {signature}")


def test_load_session_initial_metadata_request_defers_model_resolution_until_after_state_assignment():
    body = _extract_function(SESSIONS_JS, "async function loadSession(sid")
    fast_metadata_fetch = "messages=0&resolve_model=0"
    deferred_metadata_fetch = "messages=0&resolve_model=1"
    assignment = "S.session=data.session"

    assert fast_metadata_fetch in body[: body.index(assignment)], (
        "loadSession() first paint must use the metadata fast path so session "
        "switching cannot block on cold model catalog hydration"
    )
    assert deferred_metadata_fetch not in body[: body.index(assignment)], (
        "loadSession() must not resolve model metadata before assigning S.session; "
        "stale model/provider correction belongs to the deferred path"
    )
    assert "_resolveSessionModelForDisplaySoon(sid)" in body[body.index(assignment):], (
        "stale persisted model/provider correction must still happen after first paint"
    )
    assert body.count("_resolveSessionModelForDisplaySoon(sid)") == 1, (
        "deferred model repair should run once per session switch, not once after "
        "metadata and again after message hydration"
    )
