"""Regression coverage for #3319: pinned chat should recover after DOM rebuilds."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


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


def test_scroll_to_bottom_retries_after_next_layout_frame():
    fn = _extract_fn(UI_JS, "_setMessageScrollToBottom")
    assert "el.scrollTop=el.scrollHeight;" in fn
    assert "requestAnimationFrame(()=>{" in fn
    assert fn.count("el.scrollTop=el.scrollHeight;") >= 2
    assert fn.count("_lastScrollTop=el.scrollTop;") >= 2


def test_scroll_if_pinned_recovers_when_far_from_bottom():
    fn = _extract_fn(UI_JS, "scrollIfPinned")
    assert "_messageBottomDistance()>500" in fn
    assert "_setMessageScrollToBottom();" in fn
    assert fn.index("_messageBottomDistance()>500") < fn.index("_settleMessageScrollToBottom(false)")

