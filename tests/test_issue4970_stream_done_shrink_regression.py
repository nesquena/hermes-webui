"""Regression locks for #4970 round 6: stream-end worklog collapse shrink jump."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"function {name} body not found")


def test_pinned_follow_keeps_settled_worklog_open_to_avoid_stream_done_shrink():
    """A pinned reader must not see a huge upward clamp when live worklog settles.

    The live worklog can be hundreds of px tall. If the settled worklog is forced
    collapsed at STREAM_DONE, scrollHeight shrinks and the browser clamps
    scrollTop, which looks like a large backward jump even though pinned state is
    correct. Pinned followers keep the settled worklog open for this turn; users
    who scrolled away still get compact/collapsed settled worklogs.
    """
    assert "function _shouldKeepSettledWorklogOpenForPinnedFollow" in UI_JS
    helper = _function_body(UI_JS, "_shouldKeepSettledWorklogOpenForPinnedFollow")
    assert "_scrollPinned" in helper
    assert "!_messageUserUnpinned" in helper
    assert "bottom distance can transiently exceed" in helper

    render_fn = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    assert "const keepSettledWorklogOpen=_shouldKeepSettledWorklogOpenForPinnedFollow();" in render_fn
    assert "collapsed:!keepSettledWorklogOpen" in render_fn

    group_fn = _function_body(UI_JS, "_anchorSceneWorklogGroup")
    assert "opts&&opts.collapsed!==undefined" in group_fn
    assert "collapsed:(opts&&opts.collapsed!==undefined)?opts.collapsed:!live" in group_fn


def test_unpinned_reader_still_gets_compact_settled_worklog():
    helper = _function_body(UI_JS, "_shouldKeepSettledWorklogOpenForPinnedFollow")
    assert "!_messageUserUnpinned" in helper, (
        "The keep-open exception must be limited to pinned followers; unpinned "
        "readers should not get their viewport or settled worklog state changed."
    )
