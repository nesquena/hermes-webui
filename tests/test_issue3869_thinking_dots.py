from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text()


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not closed")


def test_finalize_removes_dot_only_legacy_thinking_placeholder():
    """#3869: an empty legacy thinking row is only a spinner, not durable content."""
    body = _function_body(UI_JS, "finalizeThinkingCard")

    assert "const hasContent=!!row.querySelector('.thinking-card');" in body
    assert "row.classList.contains('thinking-card-row')" not in body
    assert "if(!hasContent && row.getAttribute('data-thinking-active')==='1'){" in body
    assert "row.remove();" in body


def test_live_worklog_thinking_cards_are_preserved_on_finalize():
    """#3869 fix must not remove real Worklog Thinking Cards that contain text."""
    body = _function_body(UI_JS, "finalizeThinkingCard")

    assert "turn.querySelectorAll('.agent-activity-thinking[data-thinking-active=\"1\"]')" in body
    assert "active.removeAttribute('data-thinking-active');" in body
    assert "active.removeAttribute('data-live-thinking');" in body
    assert "active.remove()" not in body
