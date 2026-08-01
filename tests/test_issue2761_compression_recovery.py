"""Regression tests for #2761 — mid-conversation recovery after auto-compression.

The bug: when auto-compression rotates the backend session id to a
continuation session, the WebUI's live->settled transition could be skipped:

- Gate A (done handler): the settle block is gated on
  ``_isSessionCurrentPane(activeSid)``; if the pane already advanced to the
  rotated continuation id before ``done`` arrives, the block is skipped and
  the final answer is never rendered (blue compression card frozen).
- Gate B (``_restoreSettledSession``): the recovery always polled the
  original ``activeSid``, which post-rotation is an archived pre-compression
  session that may keep a stale ``active_stream_id`` (exhausting the retry
  loop) or simply lack the final answer.

These are source-level regression tests in the same style as
``test_auto_compression_card.py``: they read ``static/messages.js`` and pin
the recovery guards so a future refactor cannot silently reintroduce the
skipped settle.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def _done_listener_block() -> str:
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('done'")
    assert start != -1, "done SSE listener not found"
    end = src.find("source.addEventListener('stream_end'", start)
    assert end != -1, "stream_end listener after done SSE listener not found"
    return src[start:end]


def _compressed_listener_block() -> str:
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('compressed'")
    assert start != -1, "compressed SSE listener not found"
    end = src.find("source.addEventListener('metering'", start)
    assert end != -1, "metering listener after compressed SSE listener not found"
    return src[start:end]


def _restore_settled_session_block() -> str:
    src = _read("static/messages.js")
    start = src.find("async function _restoreSettledSession(")
    assert start != -1, "_restoreSettledSession not found"
    end = src.find("function _handleStreamError(", start)
    assert end != -1, "_handleStreamError after _restoreSettledSession not found"
    return src[start:end]


def test_done_settle_survives_continuation_session_rotation():
    """Gate A: the done settle must run when the completed session id equals
    the currently displayed session, even if the turn's original activeSid was
    rotated by auto-compression."""
    block = _done_listener_block()

    # The fallback must be present: treat done as active-session when the pane
    # is showing the completed session (continuation id).
    assert "S.session.session_id===completedSid" in block
    # completedSid must be computed BEFORE isActiveSession uses it.
    completed_idx = block.find("const completedSid=")
    active_idx = block.find("const isActiveSession=")
    assert completed_idx != -1 and active_idx != -1
    assert completed_idx < active_idx, (
        "completedSid must be resolved before isActiveSession so the rotation "
        "fallback can reference it"
    )
    # The settle itself (transcript replace + render) must still be gated on
    # isActiveSession (now rotation-aware), not removed or bypassed.
    assert "S.session=d.session" in block
    assert "renderMessages({preserveScroll:true})" in block


def test_compressed_listener_captures_continuation_sid():
    """Gate B: the compressed SSE listener must record the rotated continuation
    id so recovery paths can poll the session carrying the final answer."""
    block = _compressed_listener_block()

    assert "_streamCompressionContinuationSid=continuationSid" in block
    assert "continuationSid!==activeSid" in block


def test_restore_settled_session_polls_rotated_continuation_id():
    """Gate B: _restoreSettledSession must poll the rotated continuation id
    (when known) instead of always polling the archived pre-compression
    activeSid, and the settle must accept the rotated session as the pane."""
    block = _restore_settled_session_block()

    # Recovery resolves a restore target sid that prefers the continuation id.
    assert "_restoreSid=" in block
    assert "_streamCompressionContinuationSid!==activeSid" in block
    # The /api/session poll uses the resolved sid, not hardcoded activeSid.
    assert "session_id=${encodeURIComponent(_restoreSid)}" in block
    # The settle pane check accepts the rotated session.
    assert "_isSessionCurrentPane(_restoreSid)" in block
    # The queue drain follows the rotated session when the pane shows it.
    assert "_queueDrainSid=_restoreSid" in block


def test_continuation_sid_is_reset_per_turn():
    """The captured continuation id must be scoped to one stream/turn so a
    previous turn's rotation cannot leak into the next turn's recovery."""
    src = _read("static/messages.js")
    decl = src.find("let _streamCompressionContinuationSid='';")
    assert decl != -1, "per-stream continuation sid declaration not found"
    # Skip past the declaration itself (it also ends with `='';`).
    reset = src.find("_streamCompressionContinuationSid='';", decl + len("let _streamCompressionContinuationSid='';"))
    assert reset != -1 and reset > decl, (
        "continuation sid must be reset after stream id assignment"
    )
    stream_id_assign = src.find("S.activeStreamId = streamId;", decl)
    assert stream_id_assign != -1 and stream_id_assign < reset, (
        "continuation sid reset must happen at stream start"
    )
