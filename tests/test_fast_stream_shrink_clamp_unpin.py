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

Executed node-VM tests (behavioral) + source-string guards.
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


# ── Source-string guards ─────────────────────────────────────────────────────

def test_shrink_guard_declared():
    assert "let _lastMessageScrollHeight=null;" in UI_JS


def test_moved_up_gated_on_shrink_guard():
    assert "const movedUp=!grew&&!shrankNoIntent&&_lastScrollTop!==null&&top<_lastScrollTop-2;" in UI_JS


def test_shrink_guard_requires_no_user_intent():
    idx = UI_JS.index("const shrankNoIntent=")
    body = UI_JS[idx: idx + 900]
    for helper in (
        "_recentMessageTouchScrollIntent",
        "_recentMessageWheelIntent",
        "_recentMessageKeyScrollIntent",
        "_recentNonMessageScrollIntent",
        "_scrollbarDragActive",
    ):
        assert helper in body, f"shrankNoIntent must consult {helper}"


def test_shrink_tracker_reset_on_session_switch_and_stream_start():
    reset_idx = UI_JS.index("function _resetScrollDirectionTracker(){")
    assert "_lastMessageScrollHeight=null;" in UI_JS[reset_idx: reset_idx + 800]
    stream_idx = UI_JS.index("function _resetStreamScrollFollow(){")
    assert "_lastMessageScrollHeight=null;" in UI_JS[stream_idx: stream_idx + 900]


def test_unpin_breadcrumb_present():
    """A console.debug breadcrumb must name every sticky-unpin so a stranded
    follow can be diagnosed from the browser console."""
    assert "'[follow] sticky-unpin'" in UI_JS


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
  '_nearBottomCount','_scrollPinned','_messageUserUnpinned','_newMessageCueVisible',
  '_cancelBottomSettle','_clearNewMessageScrollCue','_syncScrollToBottomCue',
  '_isSessionEndlessScrollEnabled','_messagesTruncated','_setMessageScrollToBottom',
  '_recentMessageRenderArtifactWindow','_recentMessageTouchScrollIntent',
  '_recentNonMessageScrollIntent','_recentMessageWheelIntent','_recentMessageKeyScrollIntent',
  payload.body + `
return {_lastScrollTop,_lastMessageClientHeight,_lastMessageScrollHeight,_nearBottomCount,_scrollPinned,_messageUserUnpinned};
`);
let st = {_lastScrollTop, _lastMessageClientHeight, _lastMessageScrollHeight, _nearBottomCount, _scrollPinned, _messageUserUnpinned};
for (const s of payload.samples) {
  st = step(
    s, window, console2,
    st._lastScrollTop, st._lastMessageClientHeight, st._lastMessageScrollHeight,
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

def test_caught_prev_tail_guard_declared():
    assert "const caughtPrevTail=movedDown" in UI_JS
    assert "(top+el.clientHeight)>=(_prevMessageScrollHeightForRepin-80);" in UI_JS


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
        {"scrollTop": 2500, "scrollHeight": 3400, "clientHeight": 500},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is True
    assert st["_messageUserUnpinned"] is False


@_node_tests
def test_downward_scroll_mid_transcript_stays_unpinned():
    """Scrolling down but landing far above the previous tail must NOT re-pin."""
    st = _run_scenario([
        {"scrollTop": 500, "scrollHeight": 3000, "clientHeight": 500},
        {"scrollTop": 900, "scrollHeight": 3400, "clientHeight": 500},
    ], intents={"wheel": True}, start_unpinned=True)
    assert st["_scrollPinned"] is False
    assert st["_messageUserUnpinned"] is True
