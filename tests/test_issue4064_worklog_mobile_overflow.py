"""Regression guard for worklog summary overflow on narrow mobile viewports."""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    start = STYLE_CSS.find(selector)
    assert start >= 0, f"{selector} selector not found in style.css"
    open_brace = STYLE_CSS.find("{", start + len(selector) - 1)
    assert open_brace >= 0, f"{selector} rule did not open"
    depth = 0
    for idx in range(open_brace, len(STYLE_CSS)):
        ch = STYLE_CSS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[start:idx]
    raise AssertionError(f"{selector} rule did not close")


def test_worklog_summary_no_longer_uses_auto_width_inline_flex():
    """The summary button must be width-bounded inside the chat column."""
    rule = _rule(".tool-worklog-summary{")
    assert "width:auto" not in rule, (
        "The worklog summary must not force width:auto; that lets long one-line "
        "labels widen narrow mobile viewports."
    )
    assert "display:inline-flex" in rule, "The summary should keep its inline-flex layout."
    assert "max-width:100%" in rule, (
        "The worklog summary must cap itself to the available message width."
    )
    assert "min-width:0" in rule, (
        "The worklog summary must allow its inner label to shrink inside the chat column."
    )


def test_worklog_summary_label_can_shrink_with_ellipsis():
    """The long summary label must be the shrinkable flex child."""
    rule = _rule(".tool-worklog-summary .tool-worklog-label{")
    assert "white-space:nowrap" in rule, "The summary copy should stay single-line."
    assert "text-overflow:ellipsis" in rule, "The summary copy should still ellipsize."
    assert "flex:1 1 auto" in rule, (
        "The summary label must be a shrinkable flex child so it truncates before widening the viewport."
    )
    assert "min-width:0" in rule, (
        "The summary label must opt out of the default flex min-width:auto clamp."
    )
