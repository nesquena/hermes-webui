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
        assert "function _flushPendingSegmentRender(options={})" in src, (
            "_flushPendingSegmentRender helper must be declared in messages.js"
        )

    def test_flush_helper_guards_on_assistant_body(self):
        src = read("static/messages.js")
        m = re.search(
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
            r"function _flushPendingSegmentRender\(options=\{\}\)\{.*?\n  \}",
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
        assert "_flushPendingSegmentRender({force:true})" in fn, (
            "tool handler must force _flushPendingSegmentRender() before "
            "_resetAssistantSegment()"
        )

    def test_tool_handler_flush_before_reset(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "tool")
        flush_pos = fn.index("_flushPendingSegmentRender({force:true})")
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
        assert "_flushPendingSegmentRender({force:true})" in fn, (
            "interim_assistant handler must call _flushPendingSegmentRender() "
            "before _resetAssistantSegment()"
        )

    def test_interim_handler_flush_before_last_reset(self):
        """The flush must precede the final _resetAssistantSegment that seals
        the segment for new content (not the early alreadyStreamed branch)."""
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        flush_pos = fn.index("_flushPendingSegmentRender({force:true})")
        # Find the _resetAssistantSegment call that comes AFTER the flush
        reset_pos = fn.index("_resetAssistantSegment()", flush_pos)
        assert flush_pos < reset_pos, (
            "_flushPendingSegmentRender must be called BEFORE the final "
            "_resetAssistantSegment in the interim_assistant handler"
        )

    def test_already_streamed_interim_handler_flushes_before_reset(self):
        """already_streamed interim events are still visible-progress boundaries.

        The visible text already arrived through token events, so the client
        must not append it again. It must still flush any pending token render
        before resetting the segment; otherwise a fast tool boundary can orphan
        the text until a later render or session switch.
        """
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        branch_start = fn.index("if(alreadyStreamed)")
        branch = fn[branch_start : fn.index("assistantText +=", branch_start)]
        assert "ensureAssistantRow(true)" in branch, (
            "already_streamed interim boundaries must materialize the current "
            "token segment before reset"
        )
        assert "_flushPendingSegmentRender({force:true})" in branch, (
            "already_streamed interim boundaries must flush pending token DOM "
            "before reset"
        )
        inactive_guard = "if(!S.session||S.session.session_id!==activeSid){"
        if inactive_guard in branch:
            active_branch_start = branch.index(inactive_guard) + branch[branch.index(inactive_guard):].index("}") + 1
        else:
            active_branch_start = 0
        flush_pos = branch.index("_flushPendingSegmentRender({force:true})", active_branch_start)
        reset_pos = branch.index("_resetAssistantSegment()", active_branch_start)
        assert flush_pos < reset_pos, (
            "already_streamed interim flush must happen before segment reset"
        )

    def test_interim_handler_creates_visible_segment_before_forced_flush(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        ensure_pos = fn.index("ensureAssistantRow(true)")
        flush_pos = fn.index("_flushPendingSegmentRender({force:true})")
        reset_pos = fn.index("_resetAssistantSegment()", flush_pos)
        assert ensure_pos < flush_pos < reset_pos, (
            "visible interim assistant progress must create a live assistant "
            "segment, synchronously flush it, then reset for the next segment"
        )

    def test_interim_handler_closes_activity_after_visible_progress_boundary(self):
        src = read("static/messages.js")
        fn = _extract_handler(src, "interim_assistant")
        flush_pos = fn.index("_flushPendingSegmentRender({force:true})")
        close_pos = fn.index("closeCurrentLiveActivityGroup()", flush_pos)
        reset_pos = fn.index("_resetAssistantSegment()", close_pos)
        assert flush_pos < close_pos < reset_pos, (
            "visible interim assistant progress is timeline content; it must "
            "close the current live Activity burst before later tools append"
        )
