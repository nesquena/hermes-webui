"""Regression tests for #1784: sidebar scroll remains independent while streaming."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def test_sidebar_wheel_intent_is_recorded_passively():
    """A sidebar wheel gesture must not be swallowed or ignored during streaming."""
    assert "_recordNonMessageScrollIntent" in UI_JS
    assert "document.addEventListener('wheel',_recordNonMessageScrollIntent" in UI_JS
    assert "{capture:true,passive:true}" in UI_JS
    assert "!el.contains(target)" in UI_JS
    assert "_lastNonMessageScrollIntentMs=performance.now()" in UI_JS


def test_scroll_if_pinned_skips_during_recent_non_message_scroll():
    """Token rendering must not force-scroll #messages while the sidebar is being scrolled."""
    fn = _extract_fn(UI_JS, "scrollIfPinned")
    assert "_recentNonMessageScrollIntent()" in fn
    guard_index = fn.find("_recentNonMessageScrollIntent()")
    write_index = fn.find("scrollTop=el.scrollHeight")
    assert guard_index >= 0 and write_index >= 0 and guard_index < write_index


def test_session_list_has_its_own_scroll_boundary():
    """The session list is its own scroll surface, not chained to the chat/body scroller."""
    assert ".session-list{flex:1;overflow-y:auto;padding:0 8px 8px;min-height:0;overscroll-behavior-y:contain;touch-action:pan-y;}" in STYLE_CSS
