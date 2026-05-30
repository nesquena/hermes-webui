"""Regression coverage for mobile reload recovery after compression session rotation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"


def _function_block(source: str, marker: str) -> str:
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 1
    i = brace + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start:i]


def test_load_session_follows_backend_continuation_hint():
    """Reloading a stale pre-compression URL should update URL/localStorage to the continuation."""
    src = SESSIONS_JS.read_text(encoding="utf-8")
    load_session = _function_block(src, "async function loadSession")

    assert "continuation_session_id" in load_session
    assert "loadSession(continuationSid" in load_session
    assert "skipContinuationResolve" in load_session
    assert "localStorage.setItem('hermes-webui-session',continuationSid)" in load_session
    assert "_setActiveSessionUrl(continuationSid)" in load_session
