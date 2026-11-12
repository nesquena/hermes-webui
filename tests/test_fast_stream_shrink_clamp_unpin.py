"""Regression test: fast-stream shrink-clamp must not falsely unpin live-follow.

At high token throughput (200+ tok/s), content ABOVE the transcript tail can
re-render SHORTER mid-stream (live thinking block replaced by a shorter final
block, tool output collapsing into a compact card, provisional markdown
re-parse). When scrollHeight shrinks, the browser clamps scrollTop down and
fires a scroll event that looks like an upward user scroll. Before this fix,
the movedUp branch sticky-unpinned (`_messageUserUnpinned=true`) and
auto-follow silently died, stranding the viewport mid-transcript.

The guard (`shrankNoIntent`) suppresses the movedUp reading ONLY when
scrollHeight shrank since the last scroll event AND no user scroll input of any
kind is recent (wheel, keyboard, touch, scrollbar drag). A real user scroll
stamps one of the intent trackers, so genuine scroll-ups keep the 2px trigger.

Executed Node VM tests exercise the production listener body behavior.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

NODE_BIN = shutil.which("node") or str(pathlib.Path.home() / ".local/bin/node")
_node_available = pathlib.Path(NODE_BIN).exists()
_node_tests = pytest.mark.skipif(not _node_available, reason="node not available")


# ── Executed behavioral tests ────────────────────────────────────────────────

def _extract_listener_body() -> str:
    """Extract the scroll listener rAF callback body (between `const top=el.scrollTop;`
    and the end of the older-prefetch block)."""
    start = UI_JS.index("      const top=el.scrollTop;")
    end = UI_JS.index("        else _loadOlderMessages();", start)
    end = UI_JS.index("}", UI_JS.index("\n", end)) + 1
    return UI_JS[start:end]


def _run_scenario(samples, intents=None, start_unpinned=False):
    """Run scroll-event samples through the real listener body in node.

    samples: list of {scrollTop, scrollHeight, clientHeight}
    intents: dict of helper-name -> bool (recent intent), default all False
    """
    intents = intents or {}
    body = _extract_listener_body()
    payload = json.dumps({"body": body, "samples": samples, "intents": intents, "startUnpinned": bool(start_unpinned)})
    script = """
const payload = %s;
let _lastScrollTop = null;
let _lastMessageClientHeight = null;
let _lastMessageScrollHeight = null;
let _messageScrollInputTailHeight = null;
let _messageScrollInputTailGeneration = 0;
let _messageScrollInputTailConsumedGeneration = 0;
let _nearBottomCount = 0;
let _scrollPinned = !payload.startUnpinned;
let _messageUserUnpinned = !!payload.startUnpinned;
let _newMessageCueVisible = false;
let _messagesTruncated = false;
const window = { _autoScrollFollow: true };
const console2 = console;
const noop = () => {};
const _cancelBottomSettle = noop;
const _clearNewMessageScrollCue = noop;
const _syncScrollToBottomCue = noop;
const _isSessionEndlessScrollEnabled = () => false;
const _setMessageScrollToBottom = noop;
const i = (name) => !!payload.intents[name];
const _recentMessageRenderArtifactWindow = () => false;
const _recentMessageTouchScrollIntent = () => i('touch');
const _recentNonMessageScrollIntent = () => i('nonMessage');
const _recentMessageWheelIntent = () => i('wheel');
const _recentMessageKeyScrollIntent = () => i('key');
const step = new Function(
  'el','window','console',
  '_lastScrollTop','_lastMessageClientHeight','_lastMessageScrollHeight',
  '_messageScrollInputTailHeight','_messageScrollInputTailGeneration','_messageScrollInputTailConsumedGeneration',
  '_nearBottomCount','_scrollPinned','_messageUserUnpinned','_newMessageCueVisible',
  '_cancelBottomSettle','_clearNewMessageScrollCue','_syncScrollToBottomCue',
  '_isSessionEndlessScrollEnabled','_messagesTruncated','_setMessageScrollToBottom',
  '_recentMessageRenderArtifactWindow','_recentMessageTouchScrollIntent',
  '_recentNonMessageScrollIntent','_recentMessageWheelIntent','_recentMessageKeyScrollIntent',
  payload.body + `
return {_lastScrollTop,_lastMessageClientHeight,_lastMessageScrollHeight,_messageScrollInputTailHeight,_messageScrollInputTailGeneration,_messageScrollInputTailConsumedGeneration,_nearBottomCount,_scrollPinned,_messageUserUnpinned};
`);
let st = {_lastScrollTop, _lastMessageClientHeight, _lastMessageScrollHeight, _messageScrollInputTailHeight, _messageScrollInputTailGeneration, _messageScrollInputTailConsumedGeneration, _nearBottomCount, _scrollPinned, _messageUserUnpinned};
for (const s of payload.samples) {
  if (Object.prototype.hasOwnProperty.call(s, 'inputTailHeight')) {
    st._messageScrollInputTailGeneration += 1;
    st._messageScrollInputTailHeight = s.inputTailHeight;
  }
  st = step(
    s, window, console2,
    st._lastScrollTop, st._lastMessageClientHeight, st._lastMessageScrollHeight,
    st._messageScrollInputTailHeight, st._messageScrollInputTailGeneration, st._messageScrollInputTailConsumedGeneration,
    st._nearBottomCount, st._scrollPinned, st._messageUserUnpinned, false,
    noop, noop, noop, () => false, false, noop,
    _recentMessageRenderArtifactWindow, _recentMessageTouchScrollIntent,
    _recentNonMessageScrollIntent, _recentMessageWheelIntent, _recentMessageKeyScrollIntent
  );
}
console.log(JSON.stringify(st));
""" % payload
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@_node_tests
def test_shrink_clamp_with_no_intent_keeps_pin():
    """scrollHeight shrinks mid-stream, scrollTop clamps down, no user input:
    the reader must STAY pinned (was: sticky-unpinned, follow died)."""
    st = _run_scenario([
        # Pinned at bottom of a 2000px transcript, 500px viewport.
        {"scrollTop": 1500, "scrollHeight": 2000, "clientHeight": 500},
        # Content above the tail re-renders 300px shorter → browser clamps
        # scrollTop from 1500 to 1200. Looks like an upward scroll.
        {"scrollTop": 1200, "scrollHeight": 1700, "clientHeight": 500},
    ])
    assert st["_scrollPinned"] is True
    assert st["_messageUserUnpinned"] is False


@_node_tests
def test_real_upward_scroll_still_unpins():
    """A genuine upward scroll (wheel intent stamped, no shrink) must still
    sticky-unpin exactly as before."""
    st = _run_scenario([
        {"scrollTop": 1500, "scrollHeight": 2000, "clientHeight": 500},
        {"scrollTop": 900, "scrollHeight": 2000, "clientHeight": 500},
    ], intents={"wheel": True})
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True


@_node_tests
def test_upward_scroll_during_shrink_with_wheel_intent_unpins():
    """Even when scrollHeight shrank, a recent wheel intent means the reader is
    genuinely scrolling — the guard must NOT swallow it."""
    st = _run_scenario([
        {"scrollTop": 1500, "scrollHeight": 2000, "clientHeight": 500},
        {"scrollTop": 800, "scrollHeight": 1900, "clientHeight": 500},
    ], intents={"wheel": True})
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True


@_node_tests
def test_growth_streaming_keeps_pin_baseline():
    """Baseline sanity: normal downward growth while pinned keeps the pin."""
    st = _run_scenario([
        {"scrollTop": 1500, "scrollHeight": 2000, "clientHeight": 500},
        {"scrollTop": 1600, "scrollHeight": 2100, "clientHeight": 500},
        {"scrollTop": 1700, "scrollHeight": 2200, "clientHeight": 500},
    ])
    assert st["_scrollPinned"] is True
    assert st["_messageUserUnpinned"] is False


# ── Fast-stream re-pin race (chasing the tail) ──────────────────────────────

@_node_tests
@pytest.mark.parametrize("intent", ["wheel", "key", "touch"])
def test_gentle_upward_scroll_releases_pin_near_tail(intent):
    """Incremental reader input must not need a full viewport to escape follow."""
    st = _run_scenario([
        {"scrollTop": 1500, "scrollHeight": 2000, "clientHeight": 500},
        {"scrollTop": 1495, "scrollHeight": 2000, "clientHeight": 500},
    ], intents={intent: True})
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True



@_node_tests
def test_chasing_reader_repins_when_catching_previous_tail():
    """Unpinned reader wheels down to the tail while content keeps growing:
    bottomDistance vs the CURRENT height always reads >80px, but they reached
    the PREVIOUS event's tail — must re-pin (was: chase forever, never re-pin)."""
    st = _run_scenario([
        # Unpinned, mid-transcript. Establish baselines (height 3000, vp 500).
        {"scrollTop": 1000, "scrollHeight": 3000, "clientHeight": 500},
        # Wheels down hard to the then-current bottom (3000-500=2500), but the
        # transcript has ALREADY grown to 3400 → bottomDistance=400 (>250 band).
        {"scrollTop": 2500, "scrollHeight": 3400, "clientHeight": 500, "inputTailHeight": 3000},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is True
    assert st["_messageUserUnpinned"] is False


@_node_tests
def test_downward_scroll_mid_transcript_stays_unpinned():
    """Scrolling down but landing far above the previous tail must NOT re-pin."""
    st = _run_scenario([
        {"scrollTop": 500, "scrollHeight": 3000, "clientHeight": 500},
        {"scrollTop": 900, "scrollHeight": 3400, "clientHeight": 500, "inputTailHeight": 3400},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True


@_node_tests
def test_stale_previous_callback_height_cannot_repin_reader():
    """The tail captured at input time outranks a stale callback height."""
    st = _run_scenario([
        {"scrollTop": 1620, "scrollHeight": 3000, "clientHeight": 500},
        {"scrollTop": 2420, "scrollHeight": 4200, "clientHeight": 500, "inputTailHeight": 4200},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True


@_node_tests
def test_input_tail_generation_is_consumed_once():
    """One wheel sample cannot authorize a later unrelated scroll event."""
    st = _run_scenario([
        {"scrollTop": 1000, "scrollHeight": 3000, "clientHeight": 500},
        {"scrollTop": 1200, "scrollHeight": 3400, "clientHeight": 500, "inputTailHeight": 3000},
        {"scrollTop": 2500, "scrollHeight": 4000, "clientHeight": 500},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True


@_node_tests
def test_scroll_if_pinned_delegates_single_initial_write_to_settle():
    """The hot path must not pre-write before the settle writer runs."""
    start = UI_JS.index("function scrollIfPinned()")
    end = UI_JS.index("function scrollToBottom()", start)
    fn_src = UI_JS[start:end]
    script = f"""
let directWrites=0;
let settleCalls=0;
let _scrollPinned=true;
let _messageUserUnpinned=false;
let _nearBottomCount=0;
const window={{_autoScrollFollow:true}};
const _messageBottomDistance=()=>1000;
const _recentMessageWheelIntent=()=>false;
const _recentMessageKeyScrollIntent=()=>false;
const _recentMessageTouchScrollIntent=()=>false;
const _recentNonMessageScrollIntent=()=>false;
const _setMessageScrollToBottom=()=>{{directWrites++;}};
const _settleMessageScrollToBottom=()=>{{settleCalls++;}};
{fn_src}
scrollIfPinned();
console.log(JSON.stringify({{directWrites,settleCalls}}));
"""
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {"directWrites": 0, "settleCalls": 1}


# ── Nested-input stale authority (#7494 review: Nested Input Leaves Stale
#    Authority) ─────────────────────────────────────────────────────────────────

def _extract_input_handler_body() -> str:
    """Extract the document-level wheel/touchmove intent handler body."""
    start = UI_JS.index("function _recordNonMessageScrollIntent(e){")
    end = UI_JS.index("function _recentNonMessageScrollIntent()", start)
    return UI_JS[start:end]


def _extract_gate_helper_source() -> str:
    """Extract the production targeting gate helper source."""
    start = UI_JS.index("function _isTranscriptScrollTarget(node,el,dir){")
    end = UI_JS.index("\n}", start) + 2
    return UI_JS[start:end]


def _run_input_handler(events, node_env=""):
    """Run synthesized wheel/touchmove events through the real handler body.

    events: list of {type, deltaY?, targetName} where targetName indexes
    `nodes` — a fake DOM with configurable nested scroll surfaces.
    """
    body = _extract_input_handler_body()
    gate = _extract_gate_helper_source()
    payload = json.dumps({"body": body, "gate": gate, "events": events})
    script = """
const payload = %s;
const nodes = {
  // Leaf directly inside the transcript (chain reaches `el`, as the
  // el.contains(target) guard upstream guarantees).
  bareTranscript: { inMessages: true, parentElement: null },
  nestedPane: {
    inMessages: true,
    scrollHeight: 900, clientHeight: 300, scrollTop: 100,
    styles: { overflowY: 'auto' },
  },
  nestedPaneBottomPinned: {
    inMessages: true,
    scrollHeight: 900, clientHeight: 300, scrollTop: 600,
    styles: { overflowY: 'auto' },
  },
  plainMessageBody: { inMessages: true, scrollHeight: 200, clientHeight: 200, parentElement: null },
};
const getComputedStyle = (n) => (n && n.styles) || { overflowY: 'visible' };
// Production targeting gate helper, injected verbatim from static/ui.js.
const _isTranscriptScrollTarget = %s;
// The production handler body calls document.getElementById and (via its
// guarded listener wiring) document.addEventListener, and stamps
// performance.now() — stub all three for Node.
const performance = { now: () => 0 };
const document = {
  getElementById: (id) => (id === 'messages' ? el : null),
  addEventListener: () => {},
};
const state = {
  _lastNonMessageScrollIntentMs: -Infinity,
  _messageScrollInputTailHeight: null,
  _messageScrollInputTailGeneration: 0,
  _messageScrollInputTailConsumedGeneration: 0,
  _messageScrollInputGeneration: 0,
  _messageUserUnpinned: false,
  _scrollPinned: true,
  _nearBottomCount: 0,
  _lastMessageWheelIntentMs: -Infinity,
  _lastMessageTouchScrollIntentMs: -Infinity,
  _messageTouchScrollActive: false,
  _lastMessageScrollIntentMs: -Infinity,
  _touchStartY: null,
};
const el = {
  id: 'messages',
  scrollTop: 1000, scrollHeight: 3000, clientHeight: 500,
  contains(node){ return !!(node && node.inMessages); },
};
const _freshProgrammaticScrollActive = () => false;
const _cancelBottomSettle = () => {};
// Link the fake leaf nodes to the transcript scroller AFTER `el` exists
// (mirrors the real DOM, where the target chain reaches `el`).
nodes.bareTranscript.parentElement = el;
nodes.plainMessageBody.parentElement = el;
nodes.nestedPane.parentElement = el;
nodes.nestedPaneBottomPinned.parentElement = el;
const _markMessageTouchScrollIntent = (active) => { state._messageTouchScrollActive = !!active; };
const _captureMessageScrollInputTail = (scroller) => {
  state._messageScrollInputGeneration += 1;
  state._messageScrollInputTailGeneration = state._messageScrollInputGeneration;
  state._messageScrollInputTailHeight = scroller && Number.isFinite(Number(scroller.scrollHeight))
    ? Number(scroller.scrollHeight) : null;
};
%s
for (const ev of payload.events) {
  const target = nodes[ev.targetName] || nodes.bareTranscript;
  const e = { type: ev.type, target };
  if (Object.prototype.hasOwnProperty.call(ev, 'deltaY')) e.deltaY = ev.deltaY;
  if (ev.type === 'touchmove') { e.touches = [{ clientY: ev.clientY || 0 }]; state._touchStartY = ev.startY != null ? ev.startY : 0; }
  // Execute the production body (which declares
  // _recordNonMessageScrollIntent), then CALL it with the synthesized event —
  // a declaration alone would make every scenario vacuous.
  const run = new Function(
    'el','e','nodes',
    '_freshProgrammaticScrollActive','_cancelBottomSettle',
    '_markMessageTouchScrollIntent','_captureMessageScrollInputTail',
    '_isTranscriptScrollTarget','state',
    `with(state){ ${payload.body} _recordNonMessageScrollIntent(e); }`
  );
  run(el, e, nodes, _freshProgrammaticScrollActive, _cancelBottomSettle,
      _markMessageTouchScrollIntent, _captureMessageScrollInputTail,
      _isTranscriptScrollTarget, state);
}
console.log(JSON.stringify({
  tailHeight: state._messageScrollInputTailHeight,
  tailGeneration: state._messageScrollInputTailGeneration,
  consumedGeneration: state._messageScrollInputTailConsumedGeneration,
  inputGeneration: state._messageScrollInputGeneration,
  unpinned: state._messageUserUnpinned,
  pinned: state._scrollPinned,
}));
""" % (payload, gate, node_env)
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@_node_tests
def test_nested_pane_wheel_does_not_capture_input_tail():
    """Wheel input consumed by a nested scrollable pane must NOT mint re-pin
    authority: it never scrolls the transcript, so the capture would sit
    unconsumed until a later layout scroll reused it (stale authority)."""
    observed = _run_input_handler([
        {"type": "wheel", "deltaY": -120, "targetName": "nestedPane"},
    ])
    assert observed["tailGeneration"] == 0, observed
    assert observed["tailHeight"] is None, observed


@_node_tests
def test_bare_transcript_wheel_still_captures_input_tail():
    """Wheel over a bare transcript message (no nested scroller in the path)
    keeps the capture: the catch-tail re-pin contract still works."""
    observed = _run_input_handler([
        {"type": "wheel", "deltaY": 120, "targetName": "bareTranscript"},
    ])
    assert observed["tailGeneration"] == 1, observed
    assert observed["tailHeight"] == 3000, observed


@_node_tests
def test_plain_message_body_wheel_still_captures_input_tail():
    """A wheel over plain message content whose target chain never crosses a
    nested scroller keeps the capture (the common reader path)."""
    observed = _run_input_handler([
        {"type": "wheel", "deltaY": 120, "targetName": "plainMessageBody"},
    ])
    assert observed["tailGeneration"] == 1, observed
    assert observed["tailHeight"] == 3000, observed


@_node_tests
def test_nested_pane_touchmove_does_not_capture_input_tail():
    """Same authority rule for touch: a touchmove on a nested scroll surface
    must not capture a transcript input tail."""
    observed = _run_input_handler([
        {"type": "touchmove", "targetName": "nestedPane", "startY": 100, "clientY": 160},
    ])
    assert observed["tailGeneration"] == 0, observed
    assert observed["tailHeight"] is None, observed


def _run_stale_authority_repro():
    """Full repro chain: unpinned reader → nested-pane wheel capture (old bug
    behavior) → stream growth → layout-driven downward transcript scroll.

    Runs the REAL production bodies for both the input handler and the scroll
    listener. Returns the post-chain pin state.
    """
    body = _extract_listener_body()
    input_body = _extract_input_handler_body()
    gate = _extract_gate_helper_source()
    payload = json.dumps({
        "body": body, "inputBody": input_body, "gate": gate,
        "scrollSamples": [
            # Callback 1: unpinned reader sitting 50px above the tail
            # (bottom edge 2950 vs tail 3000) — establishes _lastScrollTop.
            {"scrollTop": 2450, "scrollHeight": 3000, "clientHeight": 500},
            # Callback 2: an above-viewport layout insert (image load) pushes
            # content down: scrollTop 2450→2650, stream grew tail to 3800.
            # Pre-fix this consumed the nested-wheel capture (top+clientHeight
            # 3150 >= capturedTail 3000-80) and re-pinned; post-fix there is no
            # capture and bottomDistance 650 keeps the reader unpinned.
            {"scrollTop": 2650, "scrollHeight": 3800, "clientHeight": 500},
        ],
    })
    script = """
const payload = %s;
let _lastScrollTop = null;
let _lastMessageClientHeight = null;
let _lastMessageScrollHeight = null;
let _messageScrollInputTailHeight = null;
let _messageScrollInputTailGeneration = 0;
let _messageScrollInputTailConsumedGeneration = 0;
let _messageScrollInputGeneration = 0;
let _nearBottomCount = 0;
let _scrollPinned = false;
let _messageUserUnpinned = true;
let _newMessageCueVisible = false;
let _messagesTruncated = false;
const window = { _autoScrollFollow: true };
const noop = () => {};
const _cancelBottomSettle = noop;
const _clearNewMessageScrollCue = noop;
const _syncScrollToBottomCue = noop;
const _isSessionEndlessScrollEnabled = () => false;
const _setMessageScrollToBottom = noop;
const _recentMessageRenderArtifactWindow = () => false;
const _recentMessageTouchScrollIntent = () => false;
const _recentNonMessageScrollIntent = () => false;
const _recentMessageWheelIntent = () => false;
const _recentMessageKeyScrollIntent = () => false;
const el = {
  scrollTop: 1000, scrollHeight: 3000, clientHeight: 500,
  contains(node){ return !!(node && node.inMessages); },
};
const _freshProgrammaticScrollActive = () => false;
const _markMessageTouchScrollIntent = noop;
const nestedPane = {
  inMessages: true,
  scrollHeight: 900, clientHeight: 300, scrollTop: 100,
  styles: { overflowY: 'auto' },
};
const getComputedStyle = (n) => (n && n.styles) || { overflowY: 'visible' };
%s
const _captureMessageScrollInputTail = (scroller) => {
  _messageScrollInputGeneration += 1;
  _messageScrollInputTailGeneration = _messageScrollInputGeneration;
  _messageScrollInputTailHeight = scroller && Number.isFinite(Number(scroller.scrollHeight))
    ? Number(scroller.scrollHeight) : null;
};
// Scroll stage: real production listener body over the sample geometry.
const step = new Function(
  'el','window','console',
  '_lastScrollTop','_lastMessageClientHeight','_lastMessageScrollHeight',
  '_messageScrollInputTailHeight','_messageScrollInputTailGeneration','_messageScrollInputTailConsumedGeneration',
  '_nearBottomCount','_scrollPinned','_messageUserUnpinned','_newMessageCueVisible',
  '_cancelBottomSettle','_clearNewMessageScrollCue','_syncScrollToBottomCue',
  '_isSessionEndlessScrollEnabled','_messagesTruncated','_setMessageScrollToBottom',
  '_recentMessageRenderArtifactWindow','_recentMessageTouchScrollIntent',
  '_recentNonMessageScrollIntent','_recentMessageWheelIntent','_recentMessageKeyScrollIntent',
  payload.body + `return {_lastScrollTop,_lastMessageClientHeight,_lastMessageScrollHeight,_messageScrollInputTailHeight,_messageScrollInputTailGeneration,_messageScrollInputTailConsumedGeneration,_nearBottomCount,_scrollPinned,_messageUserUnpinned};`
);
let st = {_lastScrollTop, _lastMessageClientHeight, _lastMessageScrollHeight, _messageScrollInputTailHeight, _messageScrollInputTailGeneration, _messageScrollInputTailConsumedGeneration, _nearBottomCount, _scrollPinned, _messageUserUnpinned};
// Scroll callback 1: BEFORE the nested-pane wheel (real event order) —
// consumes nothing, just establishes _lastScrollTop.
{
  const s0 = payload.scrollSamples[0];
  el.scrollTop = s0.scrollTop; el.scrollHeight = s0.scrollHeight; el.clientHeight = s0.clientHeight;
  st = step(
    el, window, console,
    st._lastScrollTop, st._lastMessageClientHeight, st._lastMessageScrollHeight,
    st._messageScrollInputTailHeight, st._messageScrollInputTailGeneration, st._messageScrollInputTailConsumedGeneration,
    st._nearBottomCount, st._scrollPinned, st._messageUserUnpinned, false,
    noop, noop, noop, () => false, false, noop,
    _recentMessageRenderArtifactWindow, _recentMessageTouchScrollIntent,
    _recentNonMessageScrollIntent, _recentMessageWheelIntent, _recentMessageKeyScrollIntent
  );
}
// The nested-pane wheel fires BETWEEN callbacks: run the input stage NOW.
// The production body declares _recordNonMessageScrollIntent and consults
// document.getElementById/performance.now, so stub both and CALL the handler.
{
  const performance = { now: () => 0 };
  const document = {
    getElementById: (id) => (id === 'messages' ? el : null),
    addEventListener: () => {},
  };
  const inputRun = new Function(
    'el','e','nodes','document','performance',
    '_freshProgrammaticScrollActive','_cancelBottomSettle',
    '_markMessageTouchScrollIntent','_captureMessageScrollInputTail',
    '_isTranscriptScrollTarget',
    `let _lastNonMessageScrollIntentMs=-Infinity;
     let _lastMessageWheelIntentMs=-Infinity;
     let _messageTouchScrollActive=false;
     let _touchStartY=null;
     let _nearBottomCount=0;
     let _messageUserUnpinned=true;
     let _scrollPinned=false;
     ${payload.inputBody}
     _recordNonMessageScrollIntent(e);
     return {_nearBottomCount,_messageUserUnpinned,_scrollPinned};`
  );
  const inputOut = inputRun(el, { type: 'wheel', deltaY: -120, target: nestedPane }, { nestedPane },
    document, performance,
    _freshProgrammaticScrollActive, _cancelBottomSettle, _markMessageTouchScrollIntent,
    _captureMessageScrollInputTail, _isTranscriptScrollTarget);
  _messageUserUnpinned = inputOut._messageUserUnpinned;
  _scrollPinned = inputOut._scrollPinned;
  _nearBottomCount = inputOut._nearBottomCount;
}
// Sync the shared module-level state between the stages: in production the
// input handler and the scroll listener mutate the SAME script-scope lets, so
// the pre-fix capture minted above must be visible to the next scroll callback.
st._messageUserUnpinned = _messageUserUnpinned;
st._scrollPinned = _scrollPinned;
st._nearBottomCount = _nearBottomCount;
st._messageScrollInputTailHeight = _messageScrollInputTailHeight;
st._messageScrollInputTailGeneration = _messageScrollInputTailGeneration;
st._messageScrollInputTailConsumedGeneration = _messageScrollInputTailConsumedGeneration;
// Scroll callback 2: the layout-driven downward move. With the pre-fix
// unconditional capture this consumed the stale tail and re-pinned.
{
  const s1 = payload.scrollSamples[1];
  el.scrollTop = s1.scrollTop; el.scrollHeight = s1.scrollHeight; el.clientHeight = s1.clientHeight;
  st = step(
    el, window, console,
    st._lastScrollTop, st._lastMessageClientHeight, st._lastMessageScrollHeight,
    st._messageScrollInputTailHeight, st._messageScrollInputTailGeneration, st._messageScrollInputTailConsumedGeneration,
    st._nearBottomCount, st._scrollPinned, st._messageUserUnpinned, false,
    noop, noop, noop, () => false, false, noop,
    _recentMessageRenderArtifactWindow, _recentMessageTouchScrollIntent,
    _recentNonMessageScrollIntent, _recentMessageWheelIntent, _recentMessageKeyScrollIntent
  );
}
// Post-chain: sync final shared state back out of the listener.
_messageUserUnpinned = st._messageUserUnpinned;
_scrollPinned = st._scrollPinned;
console.log(JSON.stringify({pinned: st._scrollPinned, unpinned: st._messageUserUnpinned}));
""" % (payload, gate)
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@_node_tests
def test_nested_pane_wheel_cannot_repin_reader_via_later_layout_scroll():
    """Full stale-authority chain, production bodies end to end: a nested-pane
    wheel leaves no capture, so the later layout-driven downward transcript
    scroll must NOT re-pin the intentionally unpinned reader. Before the fix
    the capture existed, the layout scroll consumed it, caughtInputTail fired,
    and the reader was yanked to the bottom."""
    observed = _run_stale_authority_repro()
    assert observed == {"pinned": False, "unpinned": True}, observed


@_node_tests
def test_boundary_pinned_pane_wheel_down_still_captures_input_tail():
    """A wheel-DOWN starting over a nested pane that is ALREADY at its bottom
    boundary cannot scroll the pane: the browser chains the gesture onward to
    the transcript, so the capture must survive (#7494 review: Boundary
    Gestures Lose Re-Pinning). Pre-fix, the gate rejected the event solely for
    having a scrollable ancestor and the reader lost re-pin authority."""
    observed = _run_input_handler([
        {"type": "wheel", "deltaY": 120, "targetName": "nestedPaneBottomPinned"},
    ])
    assert observed["tailGeneration"] == 1, observed
    assert observed["tailHeight"] == 3000, observed


@_node_tests
def test_boundary_pinned_pane_wheel_up_does_not_capture_input_tail():
    """Same boundary pane, wheel-UP: the pane CAN scroll up, so the gesture is
    consumed there and must NOT mint transcript re-pin authority."""
    observed = _run_input_handler([
        {"type": "wheel", "deltaY": -120, "targetName": "nestedPaneBottomPinned"},
    ])
    assert observed["tailGeneration"] == 0, observed
    assert observed["tailHeight"] is None, observed


@_node_tests
def test_boundary_pinned_pane_touch_down_chains_capture():
    """Touch direction handling: with the finger below the start position the
    swipe scrolls content UP-toward-earlier-history (upward transcript intent,
    dy>0 → dir=-1); with the finger ABOVE the start (dy<0 → dir=+1) it scrolls
    the transcript DOWN toward the tail. A bottom-pinned pane only chains the
    dy<0 (downward) gesture — the touch analogue of the wheel case."""
    chained = _run_input_handler([
        {"type": "touchmove", "targetName": "nestedPaneBottomPinned",
         "startY": 400, "clientY": 300},
    ])
    assert chained["tailGeneration"] == 1, chained
    assert chained["tailHeight"] == 3000, chained
    consumed = _run_input_handler([
        {"type": "touchmove", "targetName": "nestedPaneBottomPinned",
         "startY": 300, "clientY": 400},
    ])
    assert consumed["tailGeneration"] == 0, consumed
    assert consumed["tailHeight"] is None, consumed


@_node_tests
def test_gate_helper_rejects_nested_scroller_and_accepts_transcript():
    """Unit scenarios for the targeting gate helper itself. The strict
    consumed-by-nested-pane invariant must hold for every dir, including the
    no-direction default (keyboard path fails closed)."""
    fn_src = _extract_gate_helper_source()
    script = f"""
// Node has no getComputedStyle; the production helper reads the global.
const getComputedStyle = (n) => (n && n.styles) || {{ overflowY: 'visible' }};
{fn_src}
const el = {{ id: 'messages' }};
// Nested vertical scroller between target and transcript: consumed.
const pane = {{ styles: {{ overflowY: 'auto' }}, scrollHeight: 900, clientHeight: 300, parentElement: el }};
const leaf = {{ parentElement: pane }};
// Same geometry but overflowY: visible — not a scroll surface.
const staticBox = {{ styles: {{ overflowY: 'visible' }}, scrollHeight: 900, clientHeight: 300, parentElement: el }};
const staticLeaf = {{ parentElement: staticBox }};
// Target IS the transcript scroller itself: capture.
// Node outside the transcript: fail closed (containment is part of the gate).
const detached = {{}};
console.log(JSON.stringify({{
  nestedPane: _isTranscriptScrollTarget(leaf, el),
  nestedPaneDirDown: _isTranscriptScrollTarget(leaf, el, 1),
  nestedPaneDirUp: _isTranscriptScrollTarget(leaf, el, -1),
  staticBox: _isTranscriptScrollTarget(staticLeaf, el),
  scrollerItself: _isTranscriptScrollTarget(el, el),
  outsideTranscript: _isTranscriptScrollTarget(detached, el),
  null: _isTranscriptScrollTarget(null, el),
}}));
"""
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {
        "nestedPane": False,
        "nestedPaneDirDown": False,
        "nestedPaneDirUp": False,
        "staticBox": True,
        "scrollerItself": True,
        "outsideTranscript": False,
        "null": False,
    }, observed


@_node_tests
def test_gate_helper_chains_boundary_pinned_pane_by_direction():
    """A nested pane pinned at the boundary in the gesture's direction cannot
    consume the gesture — the browser chains it to the transcript, so the
    capture must survive. The opposite direction still consumes (the pane CAN
    scroll that way)."""
    fn_src = _extract_gate_helper_source()
    script = f"""
const getComputedStyle = (n) => (n && n.styles) || {{ overflowY: 'visible' }};
{fn_src}
const el = {{ id: 'messages' }};
const mkPane = (scrollTop) => ({{
  styles: {{ overflowY: 'auto' }}, scrollHeight: 900, clientHeight: 300,
  scrollTop, parentElement: el,
}});
const atBottom = mkPane(600);   // scrollTop 600 = 900-300: bottom-pinned
const midPane = mkPane(200);    // mid-scroll: consumes both directions
const atTop = mkPane(0);        // top-pinned
const leafIn = (pane) => ({{ parentElement: pane }});
console.log(JSON.stringify({{
  bottomPinnedWheelDown: _isTranscriptScrollTarget(leafIn(atBottom), el, 1),
  bottomPinnedWheelUp: _isTranscriptScrollTarget(leafIn(atBottom), el, -1),
  bottomPinnedNoDir: _isTranscriptScrollTarget(leafIn(atBottom), el),
  bottomPinnedTouchUp: _isTranscriptScrollTarget(leafIn(atBottom), el, 1),
  midPaneWheelDown: _isTranscriptScrollTarget(leafIn(midPane), el, 1),
  midPaneWheelUp: _isTranscriptScrollTarget(leafIn(midPane), el, -1),
  topPinnedWheelUp: _isTranscriptScrollTarget(leafIn(atTop), el, -1),
  topPinnedWheelDown: _isTranscriptScrollTarget(leafIn(atTop), el, 1),
  boundaryEpsilon: _isTranscriptScrollTarget(leafIn(mkPane(599)), el, 1),
  containedBottom: (() => {{ const p = mkPane(600); p.styles.overscrollBehaviorY = 'contain'; return _isTranscriptScrollTarget(leafIn(p), el, 1); }})(),
  noneTop: (() => {{ const p = mkPane(0); p.styles.overscrollBehaviorY = 'none'; return _isTranscriptScrollTarget(leafIn(p), el, -1); }})(),
}}));
"""
    result = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {
        "bottomPinnedWheelDown": True,
        "bottomPinnedWheelUp": False,
        "bottomPinnedNoDir": False,
        "bottomPinnedTouchUp": True,
        "midPaneWheelDown": False,
        "midPaneWheelUp": False,
        "topPinnedWheelUp": True,
        "topPinnedWheelDown": False,
        "boundaryEpsilon": True,
        "containedBottom": False,
        "noneTop": False,
    }, observed
