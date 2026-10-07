"""rc3a + rc3b regression coverage.

rc3a: the reasoning SSE handler must defer its thinking-card/anchor DOM write to
the existing 15fps render scheduler (_scheduleRender/_doRender) instead of
writing the card DOM once per event — the main-thread starvation measured in
fase 6 (4258 reasoning events replayed on open of a tool-heavy active session).

rc3b: the tool/tool_complete hot path must use the existing trailing throttles
(_throttledSnapshotLiveTurn, _throttledPersist — WS2.2/WS2.3 contracts) instead
of a synchronous whole-turn outerHTML snapshot and a whole-map synchronous
localStorage persist per event. Terminal paths (done/cancel/stream_end) keep
their synchronous finalize/cancel semantics.

State layers mutated and invariant proven here: the reasoning accumulators
(reasoningText/liveReasoningText) and INFLIGHT are still advanced synchronously
per event, so no event content is dropped; only DOM writes and persistence
latency move onto the already-accepted throttle contracts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"


def _source(path: Path = MESSAGES_JS) -> str:
    return path.read_text(encoding="utf-8")


def _head_source() -> str:
    """Pre-rc3 baseline (messages.js is untouched by rc1/rc2, so HEAD == pre-rc3)."""
    return subprocess.run(
        ["git", "show", "HEAD:static/messages.js"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout


def _extract_block(src: str, start_marker: str) -> str:
    start = src.find(start_marker)
    assert start >= 0, f"marker not found: {start_marker!r}"
    depth = 0
    started = False
    i = start
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced block at {start_marker!r}")


def _event_listener(src: str, event: str) -> str:
    return _extract_block(src, "source.addEventListener('" + event + "',e=>{")


_PREAMBLE = r"""
let __now = 0;
let __frames = [];
let __timers = [];
const performance = { now: () => __now };
function requestAnimationFrame(cb){ __frames.push(cb); return __frames.length; }
function setTimeout(cb, ms){ __timers.push({cb: cb, at: __now + (ms || 0)}); return __timers.length; }
function clearTimeout(t){ if(t && t <= __timers.length) __timers[t - 1].cb = null; }
function __advance(ms){
  const end = __now + ms;
  while(__now < end){
    __now += 1;
    const frames = __frames.splice(0);
    for(const f of frames) f();
    for(const t of __timers) if(t.cb && __now >= t.at) { const cb = t.cb; t.cb = null; cb(); }
  }
}
function __drainFrames(){ const frames = __frames.splice(0); for(const f of frames) f(); }
"""

_RC3A_REGION_START = "let _lastRenderMs=0;"


def _rc3a_harness(src: str) -> str:
    schedule = _extract_block(src, "function _scheduleRender(")
    region_start = src.find(_RC3A_REGION_START)
    assert region_start >= 0
    helper_region = src[region_start:src.find("function _scheduleRender(")]
    return helper_region + "\n" + schedule


_RC3A_STUBS = r"""
const S = { session: { session_id: 's1' }, activeStreamId: 'r1' };
const activeSid = 's1', streamId = 'r1';
let _streamFinalized = false;
let _renderPending = false, _pendingRafHandle = null;
let reasoningText = '', liveReasoningText = '', assistantText = '';
let segmentStart = 0, assistantBody = null;
let _isActiveSession = () => true;
const window = { };
let cardWrites = 0, anchorUpserts = 0;
let __flushed = [];
function _peekFlushed(){ return __flushed[__flushed.length-1] || null; }
function _parseStreamState(){ return { displayText: '', thinkingText: liveReasoningText, inThinking: true }; }
function _renderLiveThinking(parsed){ }
function _liveThinkingText(){ return liveReasoningText; }
function _upsertAnchorReasoning(text){ __flushed.push(text); anchorUpserts++; return true; }
function _updateLiveThinkingCard(text){ cardWrites++; }
function scrollIfPinned(){ }
function _throttledSnapshotLiveTurn(){ }
function _upsertAnchorProcessProse(){ }
function _shouldUseLiveProseFade(){ return false; }
function _stripXmlToolCalls(s){ return s; }
"""

_RC3B_PERSIST_REGION = r"""
const events = { persist: 0, snapshot: 0, lastSaved: null };
const S = { todos: [], todoStateMeta: null };
const INFLIGHT = { s1: { messages: [{role:'assistant', content:'x', _live:true}], toolCalls: [{name:'terminal'}],
                         uploaded: [], lastAssistantText: 'a'.repeat(9000),
                         lastReasoningText: 'r'.repeat(177000) } };
const activeSid = 's1', streamId = 'r1', uploaded = [];
function saveInflightState(sid, state){ events.persist++; events.lastSaved = state; }
function snapshotLiveTurnHtmlForSession(sid){ events.snapshot++; }
"""

_RC3B_BODY = r"""
let coalesced = null;
for(let i = 0; i < 198; i++){
  INFLIGHT[activeSid].toolCalls.push({ name: 'terminal', done: true, tid: 't' + i });
  _throttledPersist();
  _throttledSnapshotLiveTurn();
  __advance(100);
}
coalesced = { persist: events.persist, snapshot: events.snapshot };
if(_persistTimer){ clearTimeout(_persistTimer); _persistTimer = null; }
_cancelThrottledSnapshotTimer();
const afterCancel = { persist: events.persist, snapshot: events.snapshot };
__advance(5000);
console.log(JSON.stringify({ coalesced: coalesced, afterCancel: afterCancel }));
"""


def _run_node(script: str, timeout: float = 30.0) -> dict:
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── rc3a static contract ─────────────────────────────────────────────────────

def test_rc3a_reasoning_handler_defers_dom_write_to_render_frame():
    src = _source()
    handler = _event_listener(src, "reasoning")
    assert "_updateLiveThinkingCard(" not in handler, "reasoning handler must not write the card DOM per event"
    assert "_upsertAnchorReasoning(" not in handler, "reasoning handler must not touch anchor rows per event"
    assert "_pendingReasoningDomFlush=true" in handler
    assert "_scheduleRender()" in handler
    assert "reasoningText += text" in handler
    assert "liveReasoningText += text" in handler
    assert "syncInflightAssistantMessage()" in handler


def test_rc3a_flush_helper_runs_inside_do_render_before_thinking():
    src = _source()
    helper = _extract_block(src, "function _flushPendingReasoningDom(")
    assert "_updateLiveThinkingCard(" in helper and "_upsertAnchorReasoning(" in helper
    assert "if(_streamFinalized) return" in helper
    do_render = _extract_block(src, "const _doRender=()=>{")
    flush_at = do_render.find("_flushPendingReasoningDom();")
    thinking_at = do_render.find("_renderLiveThinking(parsed);")
    assert 0 < flush_at < thinking_at, "flush must run before the thinking render inside the frame"


def test_rc3a_ordering_flushes_before_reasoning_accumulator_resets():
    src = _source()
    tool = _event_listener(src, "tool")
    assert 0 < tool.find("_flushPendingReasoningDom();") < tool.find("liveReasoningText='';")
    interim = _event_listener(src, "interim_assistant")
    assert 0 < interim.find("_flushPendingReasoningDom();") < interim.find("liveReasoningText='';")


# ── rc3b static contract ─────────────────────────────────────────────────────

def test_rc3b_tool_hot_path_uses_throttled_snapshot_and_persist():
    src = _source()
    upsert = _extract_block(src, "function upsertLiveToolCall(")
    assert "_throttledPersist();" in upsert
    assert "persistInflightState();" not in upsert
    for event in ("tool", "tool_complete"):
        listener = _event_listener(src, event)
        assert "_throttledSnapshotLiveTurn();" in listener, event
        assert "snapshotLiveTurn();" not in listener, event


def test_rc3b_terminal_paths_keep_synchronous_finalize_semantics():
    src = _source()
    for event in ("done", "cancel"):
        listener = _event_listener(src, event)
        assert "_cancelThrottledSnapshotTimer()" in listener, event
        assert "_throttledSnapshotLiveTurn()" not in listener, event
        assert "clearTimeout(_persistTimer)" in listener, event
    # stream_end finalizes through _finalizeStreamEndFallback, which cancels both
    # trailing timers synchronously before finalizing.
    stream_end = _extract_block(src, "source.addEventListener('stream_end',async e=>{")
    fallback = _extract_block(src, "function _finalizeStreamEndFallback(source){")
    assert "_finalizeStreamEndFallback(" in stream_end or "_cancelThrottledSnapshotTimer()" in stream_end
    assert "_cancelThrottledSnapshotTimer()" in fallback and "clearTimeout(_persistTimer)" in fallback
    assert "_throttledSnapshotLiveTurn()" not in fallback


# ── behavioral (virtual clock) ───────────────────────────────────────────────

def test_rc3a_burst_reasoning_renders_at_frame_rate_not_event_rate():
    """4258 reasoning events across a 40s virtual replay must produce
    ~frame-rate card writes (<= 700) instead of one write per event (4258)."""
    body = r"""
for(let i = 0; i < 4258; i++){
  const text = 'r' + i;
  reasoningText += text;
  liveReasoningText += text;
  _pendingReasoningDomFlush = true;
  _scheduleRender();
  __advance(9);
}
__drainFrames(); __advance(200); __drainFrames();
console.log(JSON.stringify({ cardWrites: cardWrites, anchorUpserts: anchorUpserts }));
"""
    after = _run_node(_PREAMBLE + _RC3A_STUBS + _rc3a_harness(_source()) + body)
    assert after["anchorUpserts"] <= 700, after
    assert after["anchorUpserts"] >= 1

    before_body = body.replace(
        "  _pendingReasoningDomFlush = true;\n  _scheduleRender();",
        "  _updateLiveThinkingCard(liveReasoningText);",
    )
    # The pre-rc3 source has no _pendingReasoningDomFlush; strip it from the harness.
    before_harness = _rc3a_harness(_head_source())
    before = _run_node(_PREAMBLE + _RC3A_STUBS + before_harness + before_body)
    assert before["cardWrites"] == 4258, before  # one full-card write per event: the starvation
    assert before["anchorUpserts"] == 0, before


def test_rc3a_no_reasoning_content_lost_and_tool_boundary_ordering_preserved():
    body = r"""
for(const chunk of ['alpha', 'beta', 'gamma']){
  liveReasoningText += chunk;
  _pendingReasoningDomFlush = true;
  _scheduleRender();
}
// frame still pending here — the tool boundary must flush synchronously:
_flushPendingReasoningDom();          // what the tool handler does first (sync)
const flushedBySyncFlush = _peekFlushed();
liveReasoningText = '';               // tool handler reset
_flushPendingReasoningDom();          // must be a no-op now (flag consumed)
__drainFrames(); __advance(100); __drainFrames();
console.log(JSON.stringify({ flushedBySyncFlush: flushedBySyncFlush, flushCount: __flushed.length }));
"""
    script = _PREAMBLE + _RC3A_STUBS + _rc3a_harness(_source()) + body
    payload = _run_node(script)
    assert payload["flushedBySyncFlush"] == "alphabetagamma", payload  # no content lost
    assert payload["flushCount"] == 1, payload                          # consumed exactly once


def test_rc3b_throttled_snapshot_and_persist_coalesce_with_virtual_clock():
    """198 tool events across 20s must coalesce into <= 11 persists and
    <= 30 snapshots; the terminal cancel stops any pending timer."""
    src = _source()
    region_start = src.find("  function persistInflightState(){")
    region_end = src.find("  function _closeSource(source){")
    assert 0 < region_start < region_end, "persist/snapshot throttle region must be contiguous"
    region = src[region_start:region_end]
    script = _PREAMBLE + _RC3B_PERSIST_REGION + region + _RC3B_BODY
    payload = _run_node(script)
    assert payload["coalesced"]["persist"] <= 11, payload
    assert payload["coalesced"]["snapshot"] <= 30, payload
    assert payload["afterCancel"]["persist"] == payload["coalesced"]["persist"], payload
    assert payload["afterCancel"]["snapshot"] == payload["coalesced"]["snapshot"], payload


def test_rc3b_trailing_persist_writes_latest_state_within_contract_window():
    src = _source()
    region_start = src.find("  function persistInflightState(){")
    region_end = src.find("  function _closeSource(source){")
    region = src[region_start:region_end]
    body = r"""
INFLIGHT[activeSid].lastAssistantText = 't1';
_throttledPersist();
INFLIGHT[activeSid].lastAssistantText = 't2';
_throttledPersist();
__advance(2500);
console.log(JSON.stringify({ savedLast: events.lastSaved ? events.lastSaved.lastAssistantText : null }));
"""
    payload = _run_node(_PREAMBLE + _RC3B_PERSIST_REGION + region + body)
    assert payload["savedLast"] == "t2", payload


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
