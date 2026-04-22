"""Tests for #631 — streaming race conditions in messages.js

Bug A: A trailing 'token'/'reasoning' event queued a requestAnimationFrame that
fired after 'done' had already called renderMessages(), causing the thinking card
to reappear below the final answer or the response to render twice.

Bug B: On SSE reconnect, the closure variables (assistantText, reasoningText)
were not reset. Server replays token events into the new EventSource, causing
text to accumulate again from the stale values — response doubled, stuck cursor.

Fixes:
- _streamFinalized flag + _pendingRafHandle stored for cancellation
- done/apperror/cancel: set _streamFinalized, cancel pending rAF, call finalizeThinkingCard
- _scheduleRender: guard on _streamFinalized
- _wireSSE: reset accumulators when (re)opening source, unless stream already finalized
- error handler: bail if _streamFinalized (same as _terminalStateReached)
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


class TestStreamFinalized:
    """_streamFinalized flag and rAF cancellation."""

    def test_stream_finalized_declared(self):
        src = read('static/messages.js')
        assert '_streamFinalized' in src, (
            "_streamFinalized must be declared in attachLiveStream"
        )

    def test_pending_raf_handle_declared(self):
        src = read('static/messages.js')
        assert '_pendingRafHandle' in src, (
            "_pendingRafHandle must be declared to enable rAF cancellation"
        )

    def test_schedule_render_guards_on_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r'function _scheduleRender\(\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_scheduleRender not found"
        fn = m.group(0)
        assert '_streamFinalized' in fn, (
            "_scheduleRender must return early when _streamFinalized is true"
        )

    def test_raf_handle_stored_in_schedule_render(self):
        src = read('static/messages.js')
        assert '_pendingRafHandle=requestAnimationFrame' in src or \
               '_pendingRafHandle = requestAnimationFrame' in src, (
            "rAF handle must be stored in _pendingRafHandle for cancellation"
        )

    def test_done_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('done'.*?\}\);", src, re.DOTALL)
        assert m, "'done' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'done' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn, (
            "'done' handler must cancel any pending rAF"
        )
        assert 'finalizeThinkingCard' in fn, (
            "'done' handler must call finalizeThinkingCard() to close thinking card"
        )

    def test_apperror_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('apperror'.*?\}\);", src, re.DOTALL)
        assert m, "'apperror' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'apperror' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn

    def test_cancel_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('cancel'.*?\}\);", src, re.DOTALL)
        assert m, "'cancel' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'cancel' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn


class TestReconnectAccumulatorReset:
    """Bug B: text accumulators must be reset on reconnect."""

    def test_wire_sse_resets_assistant_text(self):
        src = read('static/messages.js')
        m = re.search(r'function _wireSSE\(source\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_wireSSE not found"
        fn = m.group(0)
        # Must reset assistantText inside _wireSSE (not just at closure scope)
        assert "assistantText=''" in fn or 'assistantText = ""' in fn or \
               "assistantText=''" in fn, (
            "_wireSSE must reset assistantText='' on open to prevent doubled responses on reconnect"
        )
        assert "reasoningText=''" in fn or 'reasoningText = ""' in fn, (
            "_wireSSE must reset reasoningText='' on open"
        )

    def test_wire_sse_reset_guarded_by_stream_finalized(self):
        """Reset must only happen when the stream hasn't been finalized, to avoid
        wiping data on the post-done close path."""
        src = read('static/messages.js')
        m = re.search(r'function _wireSSE\(source\)\{.*?\n  \}', src, re.DOTALL)
        assert m
        fn = m.group(0)
        # The reset should be inside an if(!_streamFinalized) guard
        assert '_streamFinalized' in fn, (
            "_wireSSE reset must be guarded by !_streamFinalized"
        )

    def test_error_handler_guards_on_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('error'.*?\}\);", src, re.DOTALL)
        assert m, "'error' handler not found"
        fn = m.group(0)
        assert '_streamFinalized' in fn, (
            "'error' reconnect handler must bail if _streamFinalized is true"
        )
