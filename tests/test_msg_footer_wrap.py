"""Regression test for the #4724-followup message-footer wrap fix.

The bug: on a narrow screen (mobile / split pane) the assistant message footer
(model name · duration · token/cost stats · timestamp · action buttons) overflowed
its row, pushing the per-message action buttons (edit/copy/retry) off the right edge
where they were unreachable.

The rejected approach (#4724) made every usage span `flex: 0 1 auto` + ellipsis, which
shrank the stats into unreadable stubs (`D…`, `98…`). This fix instead lets `.msg-foot`
WRAP to a second line — every stat stays fully readable and the buttons stay on-screen.

These are source-structure assertions on static/style.css (CSS isn't executed in the
suite), pinning the wrap behavior so it can't silently regress.
"""
import pathlib

REPO = pathlib.Path(__file__).parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _rule_body(selector_literal: str) -> str:
    """Return the declaration block for the first rule whose head exactly matches."""
    idx = CSS.find(selector_literal + " {")
    if idx == -1:
        idx = CSS.find(selector_literal + "{")
    assert idx != -1, f"rule {selector_literal!r} not found in style.css"
    brace = CSS.index("{", idx)
    end = CSS.index("}", brace)
    return CSS[brace + 1:end]


def test_msg_foot_wraps_not_overflows():
    body = _rule_body(".msg-foot")
    assert "flex-wrap: wrap" in body, (
        ".msg-foot must flex-wrap so a long footer wraps to a second line on narrow "
        "screens instead of overflowing and pushing the action buttons off-screen"
    )
    # must still be a flex row
    assert "display: flex" in body


def test_msg_foot_controls_pinned_not_shrunk():
    # The actions + timestamp must NOT shrink/ellipsis — they stay full size and wrap
    # as whole units, so the buttons are always intact and reachable.
    actions = _rule_body(".msg-foot .msg-actions")
    time = _rule_body(".msg-foot .msg-time")
    assert "flex: 0 0 auto" in actions, ".msg-actions must be flex:0 0 auto (never shrink)"
    assert "flex: 0 0 auto" in time, ".msg-time must be flex:0 0 auto (never shrink)"


def test_usage_stats_not_ellipsis_shrunk():
    # Guard against regressing to the rejected ellipsis-everything approach: the usage
    # spans must NOT be flex:0 1 auto with text-overflow:ellipsis (that produced the
    # unreadable `D…` / `98…` stubs). They keep flex:0 0 auto and wrap as whole units.
    # The usage spans share a grouped rule (.msg-usage-inline, .msg-duration-inline, …),
    # so extract that group's block by its leading selector then the next `{`.
    head = CSS.index(".msg-usage-inline,")
    brace = CSS.index("{", head)
    body = CSS[brace + 1: CSS.index("}", brace)]
    assert "flex: 0 0 auto" in body, (
        "usage spans must stay flex:0 0 auto (full size, wrap as a unit) — not "
        "flex:0 1 auto+ellipsis, which shrinks them into unreadable stubs"
    )
    assert "text-overflow: ellipsis" not in body, (
        "usage spans must not ellipsis-truncate; the footer wraps instead"
    )
