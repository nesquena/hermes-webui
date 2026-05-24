"""Regression checks for per-conversation action menu click stability."""
from pathlib import Path

SESSIONS_JS = (Path(__file__).resolve().parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def test_session_list_refresh_does_not_close_open_conversation_actions():
    """Sidebar refreshes must not eat the three-dot menu before users can click it."""
    body = _function_block(SESSIONS_JS, "renderSessionListFromCache")

    assert "if(_renamingSid) return;" in body
    assert "if(_sessionActionMenu) return;" in body
    assert body.index("if(_sessionActionMenu) return;") < body.index("closeSessionActionMenu();")
