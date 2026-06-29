"""Source-lock for the SSE-error multi-probe reconnect window in attachLiveStream.

Symptom this guards against: after the chat EventSource emits `error`, the live
turn's whole message could blank out for a moment and then reappear (from the
sidecar / run-journal replay) or only come back on refresh.

Root cause: the `EventSource.onerror` handler made a SINGLE 1.5s reconnect probe
and, if `/api/chat/stream/status` did not yet report `active`/`replay_available`,
fell straight through to `_handleStreamError(source)`. `_handleStreamError`
clears the owner INFLIGHT state, nulls `S.activeStreamId`, pushes a
"Connection interrupted" message and re-renders — wiping the live DOM even
though the backend was frequently still producing tokens (or the replay file was
a beat away from becoming visible). The settled response then reappeared later,
producing the disappear-then-restore flicker.

Fix: replace the single 1.5s probe with a short staged retry window
(`_retryDelays=[1500,3000,5000,8000]` ms). Each stage re-queries the stream
status and reconnects/replays if the backend is reachable; the live DOM and
`S.activeStreamId`/INFLIGHT state are kept INTACT across the whole window, and
`_handleStreamError` is only reached after every stage has failed. This file
asserts that structure so the single-probe regression cannot silently return.

These are static source assertions (no Node harness needed): they read
static/messages.js, strip whitespace, and assert the staged-retry shape exists
and that the terminal `_handleStreamError` call sits AFTER the
"schedule the next probe" guard inside the reconnect block.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def _reconnect_block(compact: str) -> str:
    """Return the body of the `if(!_reconnectAttempted&&streamId){...}` block by
    brace-matching from its opening brace to the matching close, rather than a
    fixed character window (which could silently truncate — and so under-assert —
    if future guard lines grow the block). greptile P2 on #5122.
    """
    marker = "if(!_reconnectAttempted&&streamId){"
    start = compact.find(marker)
    assert start != -1, "expected the reconnect block guarded by _reconnectAttempted"
    i = start + len(marker) - 1  # position of the opening brace
    depth = 0
    for j in range(i, len(compact)):
        c = compact[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return compact[start: j + 1]
    raise AssertionError("unbalanced braces: reconnect block never closed")


def test_staged_retry_delays_present():
    # Four escalating backoff stages replace the old single 1500ms probe.
    assert "_retryDelays=[1500,3000,5000,8000]" in _compact(MESSAGES_JS)


def test_probe_reconnect_helper_defined():
    # The staged retry is driven by a recursive _probeReconnect(attempt) closure.
    compact = _compact(MESSAGES_JS)
    assert "const_probeReconnect=async(attempt=0)=>{" in compact


def test_first_probe_scheduled_from_retry_delays_head():
    # The initial probe must be scheduled off the head of _retryDelays, not a
    # hard-coded 1500 — i.e. the old `setTimeout(async()=>{...},1500)` single
    # probe is gone.
    compact = _compact(MESSAGES_JS)
    assert "setTimeout(()=>{void_probeReconnect(0);},_retryDelays[0]);" in compact


def test_stage_counter_starts_at_one():
    # The very first status shown must read (1/N), not a bare "Reconnecting…"
    # that then jumps to (2/4) — otherwise the counter appears to start at 2,
    # which looks like a glitch. _retryDelays must be declared before the first
    # setComposerStatus so (1/${_retryDelays.length}) is in scope. (greptile P2)
    compact = _compact(MESSAGES_JS)
    assert "setComposerStatus(`Reconnecting…(1/${_retryDelays.length})`);" in compact
    # And the declaration precedes that first status call.
    decl = compact.find("const_retryDelays=[1500,3000,5000,8000];")
    first_status = compact.find("setComposerStatus(`Reconnecting…(1/${_retryDelays.length})`)")
    assert decl != -1 and first_status != -1
    assert decl < first_status


def test_each_stage_requeries_stream_status():
    # Every probe attempt re-queries the server stream status so a backend that
    # is still alive (or whose replay just became available) is detected within
    # the window rather than after a single shot.
    compact = _compact(MESSAGES_JS)
    assert "api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`)" in compact


def test_next_stage_is_scheduled_before_giving_up():
    # When a stage fails but more stages remain, the handler must schedule the
    # NEXT probe and return — never fall through to the terminal error path yet.
    compact = _compact(MESSAGES_JS)
    assert "constnextDelay=_retryDelays[attempt+1];" in compact
    assert "if(nextDelay){" in compact
    assert "setTimeout(()=>{void_probeReconnect(attempt+1);},nextDelay);" in compact


def test_handle_stream_error_only_after_retry_window_exhausted():
    # Inside the reconnect block, the terminal _handleStreamError(source) call
    # must come AFTER the "schedule next stage" guard, so live state is held
    # across the whole window and the error is surfaced only once every stage
    # has failed. This is the exact ordering that prevents the premature
    # state-wipe / blank-then-restore flicker.
    block = _reconnect_block(_compact(MESSAGES_JS))

    guard_idx = block.find("if(nextDelay){")
    assert guard_idx != -1, "expected the next-stage scheduling guard in the reconnect block"

    hse_idx = block.find("_handleStreamError(source);", guard_idx)
    assert hse_idx != -1, "expected a terminal _handleStreamError(source) in the reconnect block"
    # The terminal error call sits after the next-stage guard (retry window first).
    assert guard_idx < hse_idx


def test_live_state_not_cleared_mid_window():
    # Guard against a re-introduced early state-wipe: the reconnect block must not
    # null the active stream id or clear inflight state before the retry window
    # is exhausted. We assert neither _clearOwnerInflightState() nor an
    # S.activeStreamId=null assignment appears inside the reconnect block body
    # before its terminal _handleStreamError (those belong only in
    # _handleStreamError, reached after the window).
    block = _reconnect_block(_compact(MESSAGES_JS))
    end = block.find("_handleStreamError(source);")
    assert end != -1
    body = block[:end]
    assert "_clearOwnerInflightState()" not in body, (
        "reconnect window must not clear inflight state before all probes fail"
    )
    assert "S.activeStreamId=null" not in body, (
        "reconnect window must not null activeStreamId before all probes fail"
    )
