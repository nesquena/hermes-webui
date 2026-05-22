"""Regression tests for stale session model hydration in the WebUI.

Old sessions can persist provider-shaped model IDs such as ``openai/gpt-5.4-mini``
after the active runtime moved to OpenAI Codex ``gpt-5.5``.  The first
``loadSession()`` metadata request must ask the backend for the resolved model so
that the composer state cannot briefly use the stale raw value for display or the
next chat-start payload.
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


def test_load_session_initial_metadata_request_resolves_model_before_state_assignment():
    body = _extract_function(SESSIONS_JS, "async function loadSession(sid")
    metadata_fetch = "messages=0&resolve_model=1"
    stale_metadata_fetch = "messages=0&resolve_model=0"
    assignment = "S.session=data.session"

    assert metadata_fetch in body, (
        "loadSession() must resolve model metadata on the initial fetch so stale "
        "persisted models like openai/gpt-5.4-mini cannot become active composer state"
    )
    assert stale_metadata_fetch not in body[: body.index(assignment)], (
        "loadSession() must not assign S.session from unresolved metadata before the "
        "backend has normalized stale model/provider combinations"
    )
