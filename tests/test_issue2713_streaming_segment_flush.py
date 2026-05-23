"""Regression tests for #2713 — flush pending render before segment reset.

During live streaming with tool calls, the rAF-throttled render callback could
be orphaned when _resetAssistantSegment() cleared assistantBody before the
pending callback fired. The fix introduces _flushPendingSegmentRender() which
synchronously writes any pending segment text to the DOM before the segment is
sealed.

These tests use static analysis (same pattern as test_streaming_race_fix.py)
to pin the structural invariants so a future refactor cannot silently re-break
the flush guarantee.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


class TestFlushHelperExists:
    """_flushPendingSegmentRender must exist and have the right shape."""

    def test_flush_helper_declared(self):
        src = read("static/messages.js")
        assert "function _flushPendingSegmentRender()" in src, (
            "_flushPendingSegmentRender helper must be declared in messages.js"
        )

    def test_flush_helper_guards_on_assistant_body(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m, "_flushPendingSegmentRender not found"
        fn = m.group(0)
        assert "assistantBody" in fn, (
            "_flushPendingSegmentRender must guard on assistantBody"
        )

    def test_flush_helper_guards_on_render_pending(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "_renderPending" in fn, (
            "_flushPendingSegmentRender must guard on _renderPending"
        )

    def test_flush_helper_cancels_pending_raf(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "_cancelAnimationFramePendingStreamRender()" in fn, (
            "_flushPendingSegmentRender must cancel the pending rAF"
        )

    def test_flush_helper_uses_smd_write(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "_smdWrite(" in fn, (
            "_flushPendingSegmentRender must write via _smdWrite for smd path"
        )

    def test_flush_helper_has_render_md_fallback(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "renderMd" in fn, (
            "_flushPendingSegmentRender must have renderMd fallback"
        )

    def test_flush_helper_has_esc_fallback(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(\)\{.*?\n  \}",
            src,
            re.DOTALL,
        )
        assert m
        fn = m.group(0)
        assert "esc(" in fn, (
            "_flushPendingSegmentRender must have esc() fallback"
        )


def _extract_handler(src, event_name):
    """Extract a full SSE handler body by matching balanced indentation.

    Finds `source.addEventListener('<event_name>'` and captures through the
    matching `    });` closing (4-space indent, matching the addEventListener
    call site inside _wireSSE).
    """
    start_pattern = f"source.addEventListener('{event_name}'"
    start = src.index(start_pattern)
    # Find the closing `    });` that ends this handler at 6-space indent level
    # (the handler bodies are indented 6 spaces inside _wireSSE)
    end_marker = "\n    });"
    pos = start
    while True:
        idx = src.index(end_marker, pos + 1)
        # Confirm the next line after `});` starts a new addEventListener or
        # is at the same or lower indent. Accept first match after the handler
        # body has at least some content.
        if idx > start + len(start_pattern) + 20:
            return src[start : idx + len(end_marker)]
        pos = idx


class TestToolHandlerFlush:
    """The tool SSE handler must call _flushPendingSegmentRender before reset."""

    def test_tool_handler_calls_flush(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "tool")
        assert "_flushPendingSegmentRender()" in fn, (
            "tool handler must call _flushPendingSegmentRender() before "
            "_resetAssistantSegment()"
        )

    def test_tool_handler_flush_before_reset(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "tool")
        flush_pos = fn.index("_flushPendingSegmentRender()")
        reset_pos = fn.index("_resetAssistantSegment()")
        assert flush_pos < reset_pos, (
            "_flushPendingSegmentRender must be called BEFORE "
            "_resetAssistantSegment in the tool handler"
        )


class TestInterimAssistantHandlerFlush:
    """The interim_assistant handler must call _flushPendingSegmentRender."""

    def test_interim_handler_calls_flush(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        assert "_flushPendingSegmentRender()" in fn, (
            "interim_assistant handler must call _flushPendingSegmentRender() "
            "before _resetAssistantSegment()"
        )

    def test_interim_handler_flush_before_last_reset(self):
        """The flush must precede the final _resetAssistantSegment that seals
        the segment for new content (not the early alreadyStreamed branch)."""
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        flush_pos = fn.index("_flushPendingSegmentRender()")
        # Find the _resetAssistantSegment call that comes AFTER the flush
        reset_pos = fn.index("_resetAssistantSegment()", flush_pos)
        assert flush_pos < reset_pos, (
            "_flushPendingSegmentRender must be called BEFORE the final "
            "_resetAssistantSegment in the interim_assistant handler"
        )
