"""Regression tests for preserving live streams across session switches."""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name}() not found"
    brace = src.find("){", start)
    assert brace != -1, f"{name}() body not found"
    brace += 1
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1 : i - 1]


def test_attach_live_stream_reuses_existing_same_stream_transport():
    """Returning to a running session must not tear down its same SSE stream.

    The server-side stream queue is not a replay log. If a sidebar switch back
    to the running session closes and reopens the same EventSource, there is a
    narrow window where stream events can be consumed by the old transport but
    no longer represented in the pane/cache. The same session/stream pair should
    therefore reuse the existing transport.
    """
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    assert reuse_pos != -1, "attachLiveStream() should check for an existing live stream"
    assert close_pos != -1, "attachLiveStream() should still close stale/different streams"
    assert reuse_pos < close_pos, "same-stream reuse must run before closeLiveStream(activeSid)"
    assert "existingLive.streamId===streamId" in body
    assert "existingLive.source.readyState!==EventSource.CLOSED" in body
    assert "return" in body[reuse_pos:close_pos]


def test_attach_live_stream_closes_other_session_streams_before_opening_new_one():
    """Only the selected conversation pane should hold an open chat SSE transport."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    helper = _function_body(MESSAGES_JS, "closeOtherLiveStreams")

    helper_compact = helper.replace(" ", "")
    assert "Object.keys(LIVE_STREAMS)" in helper
    assert "if(sid!==activeSid)closeLiveStream(sid)" in helper_compact

    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_other_pos = body.find("closeOtherLiveStreams(activeSid)")
    close_current_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert close_other_pos != -1, "attachLiveStream() should prune background chat EventSources"
    assert reuse_pos < close_other_pos < close_current_pos, (
        "same-stream reuse should happen before pruning, and pruning should happen "
        "before replacing the active session transport"
    )


def test_attach_live_stream_updates_uploads_before_same_stream_reuse():
    """Reusing transport must not skip per-session uploaded attachment state."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    upload_pos = body.find("if(uploaded.length) INFLIGHT[activeSid].uploaded=[...uploaded]")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert upload_pos != -1
    assert reuse_pos != -1
    assert close_pos != -1
    assert upload_pos < reuse_pos < close_pos


def test_attach_live_stream_different_stream_still_reopens_transport():
    """A new stream id for the same session must not reuse the old transport."""
    body = _function_body(MESSAGES_JS, "attachLiveStream")
    reuse_pos = body.find("const existingLive=LIVE_STREAMS[activeSid]")
    close_pos = body.find("\n  closeLiveStream(activeSid);\n")
    assert reuse_pos != -1
    assert close_pos != -1
    reuse_block = body[reuse_pos:close_pos]
    assert "existingLive.streamId===streamId" in reuse_block
    assert "existingLive.streamId!==streamId" not in reuse_block
    assert "return" in reuse_block
    assert reuse_pos < close_pos


def test_load_session_reattach_path_uses_attach_live_stream_for_running_sessions():
    """The session switch-back path should still route through attachLiveStream()."""
    body = _function_body(SESSIONS_JS, "loadSession")
    active_pos = body.find("const activeStreamId=S.session.active_stream_id||null")
    reattach_pos = body.find("attachLiveStream(sid, activeStreamId")
    assert active_pos != -1
    assert reattach_pos != -1
    assert active_pos < reattach_pos
    assert "{reconnecting:true}" in body[reattach_pos : reattach_pos + 200]


def test_close_live_stream_marks_inflight_for_reattach_on_return():
    """When closeLiveStream() tears down a still-active SSE transport (e.g. the
    user switched to another session), the corresponding INFLIGHT entry must be
    flagged so loadSession() reopens the SSE on return.

    Without this flag the in-memory INFLIGHT entry stays as it was (no
    `reattach:true`, which is only set on the storage-load path), so
    loadSession()'s reattach branch is skipped — the SSE is never reopened and
    the user sees no streamed tokens until the LLM finishes and a metadata
    refresh swaps in the final reply.
    """
    body = _function_body(MESSAGES_JS, "closeLiveStream")
    assert "INFLIGHT" in body, (
        "closeLiveStream() must touch INFLIGHT so loadSession() reattaches the "
        "SSE when the user switches back to a still-streaming session"
    )
    assert re.search(r"INFLIGHT\[\w+\]\s*&&\s*\(?INFLIGHT\[\w+\]\.reattach\s*=\s*true", body) \
           or re.search(r"if\s*\(\s*INFLIGHT\[\w+\]\s*\)\s*INFLIGHT\[\w+\]\.reattach\s*=\s*true", body), (
        "closeLiveStream() must set INFLIGHT[sessionId].reattach = true "
        "(guarded by an existence check) so loadSession()'s reattach branch fires"
    )


def test_close_other_live_streams_triggers_reattach_for_backgrounded_sessions():
    """closeOtherLiveStreams() during session switch must mark every closed
    background session for reattach. Otherwise switching back to a session whose
    stream was closed during the switch leaves the SSE permanently disconnected.
    """
    helper_body = _function_body(MESSAGES_JS, "closeOtherLiveStreams")
    close_body = _function_body(MESSAGES_JS, "closeLiveStream")
    # closeOtherLiveStreams delegates per-session teardown to closeLiveStream,
    # so the reattach flag must be set inside closeLiveStream itself for the
    # chain to work — this guards the indirection.
    assert "closeLiveStream(sid)" in helper_body.replace(" ", ""), (
        "closeOtherLiveStreams() must delegate teardown to closeLiveStream()"
    )
    assert "reattach" in close_body, (
        "closeLiveStream() must set the reattach flag so closeOtherLiveStreams() "
        "propagates the reattach intent to every backgrounded session"
    )


def test_load_session_reattaches_when_inflight_is_in_memory_and_marked_for_reattach():
    """The session-switch return path must hit attachLiveStream() even when
    INFLIGHT[sid] is already in memory (i.e. wasn't loaded from storage).

    Before the fix, only the storage-load path set `reattach:true` on INFLIGHT,
    so a switch-back through an in-memory INFLIGHT entry skipped the reattach
    branch. Once closeLiveStream() also sets reattach=true, the existing
    `INFLIGHT[sid].reattach && activeStreamId` gate is enough — this test
    pins the gate's shape so future refactors don't drop the flag check.
    """
    body = _function_body(SESSIONS_JS, "loadSession")
    inflight_idx = body.find("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0, "INFLIGHT branch not found in loadSession"
    inflight_block = body[inflight_idx : inflight_idx + 2400]
    assert "INFLIGHT[sid].reattach" in inflight_block, (
        "loadSession()'s INFLIGHT branch must gate the SSE reattach on the "
        "reattach flag so closeLiveStream()'s marking flows through"
    )
    reattach_gate = re.search(
        r"if\(INFLIGHT\[sid\]\.reattach\s*&&\s*activeStreamId.*?attachLiveStream\(sid, activeStreamId",
        inflight_block,
        re.DOTALL,
    )
    assert reattach_gate, (
        "loadSession() must reattach via attachLiveStream() when "
        "INFLIGHT[sid].reattach && activeStreamId"
    )
