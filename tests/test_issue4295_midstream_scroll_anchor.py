"""Regression for #4295: keep mid-stream reader anchor while streaming.

The fix needs a scroll restore that keeps the semantic message anchor authoritative
even when DOM height grows while the user is manually unpinned.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for i, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not terminate")


def _compact(text: str) -> str:
    return "".join(text.split())


def test_restore_message_scroll_snapshot_remounts_virtual_anchor_and_keeps_unpinned():
    """Drive the real restore helpers with a tiny DOM stub.

    This covers the #4295 failure mode where a mid-stream render rebuild has to
    remount a virtualized anchor before restoring the semantic reader position.
    The geometry is intentionally near-bottom after restore; old distance-based
    state inference would incorrectly re-pin the reader.
    """

    script = f"""
const assert = require('assert');
let _programmaticScroll = false;
let _messageVirtualWindowKey = 'old-window';
let _lastScrollTop = 0;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 2;
let _messageViewportAnchorRemounting = false;
let targetMounted = false;
const renderCalls = [];
const targetRow = {{
  getBoundingClientRect() {{ return {{ top: 140, bottom: 180 }}; }}
}};
const container = {{
  scrollTop: 300,
  scrollHeight: 905,
  clientHeight: 400,
  getBoundingClientRect() {{ return {{ top: 100, bottom: 500 }}; }},
  querySelector(selector) {{
    if (selector === '[data-msg-idx="20"]' && targetMounted) return targetRow;
    return null;
  }}
}};
function $(id) {{ return id === 'messages' ? container : null; }}
function requestAnimationFrame(fn) {{ fn(); }}
function setTimeout(fn) {{ fn(); }}
function _getVisibleMessagesWithIdx() {{
  return [{{ rawIdx: 5 }}, {{ rawIdx: 20 }}, {{ rawIdx: 42 }}];
}}
function _messageVisibleIndexForRawIdx(rawIdx, visWithIdx) {{
  return visWithIdx.findIndex((entry) => entry && entry.rawIdx === rawIdx);
}}
function _messageVirtualScrollTopForVisibleIdx(visWithIdx, visIdx, el) {{
  assert.strictEqual(visIdx, 1);
  assert.strictEqual(el, container);
  return 480;
}}
function renderMessages(options) {{
  renderCalls.push(options);
  assert.strictEqual(_messageViewportAnchorRemounting, true);
  targetMounted = true;
}}
{_function_body(UI_JS, "_restoreMessageViewportAnchor")}
{_function_body(UI_JS, "_remountMessageViewportAnchor")}
{_function_body(UI_JS, "_restoreMessageScrollSnapshot")}
_restoreMessageScrollSnapshot({{
  anchor: {{ rawIdx: 20, topOffset: 15 }},
  top: 222,
  bottom: 600,
  pinned: false,
  userUnpinned: true,
}});
assert.strictEqual(renderCalls.length, 1);
assert.deepStrictEqual(renderCalls[0], {{ preserveScroll: true }});
assert.strictEqual(_messageVirtualWindowKey, '');
assert.strictEqual(container.scrollTop, 505);
assert.strictEqual(_lastScrollTop, 505);
assert.strictEqual(_messageUserUnpinned, true);
assert.strictEqual(_scrollPinned, false);
assert.strictEqual(_nearBottomCount, 0);
assert.strictEqual(_programmaticScroll, false);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_live_anchor_rebuild_guard_holds_height_and_marks_reader_unpinned():
    """Live anchor rebuilds must not let the scroll container collapse mid-read."""

    script = f"""
const assert = require('assert');
let _messageUserUnpinned = true;
let _scrollPinned = false;
let _nearBottomCount = 2;
const messages = {{
  scrollTop: 2000,
  scrollHeight: 5000,
  clientHeight: 600,
}};
const inner = {{ style: {{ minHeight: '' }}, dataset: {{}} }};
function $(id) {{
  if (id === 'messages') return messages;
  if (id === 'msgInner') return inner;
  return null;
}}
{_function_body(UI_JS, "_prepareLiveAnchorScrollRebuildGuard")}
const snapshot = {{
  top: 2000,
  bottom: 0,
  scrollHeight: 5000,
  pinned: true,
  userUnpinned: false,
}};
const guard = _prepareLiveAnchorScrollRebuildGuard(snapshot);
assert.strictEqual(guard.readerAwayFromBottom, true);
assert.strictEqual(snapshot.pinned, false);
assert.strictEqual(snapshot.userUnpinned, true);
assert.strictEqual(snapshot.bottom, 2400);
assert.strictEqual(_messageUserUnpinned, true);
assert.strictEqual(_scrollPinned, false);
assert.strictEqual(_nearBottomCount, 0);
assert.strictEqual(inner.style.minHeight, '5000px');
assert.strictEqual(inner.dataset.liveAnchorScrollGuardPreviousMinHeight, '');
messages.scrollHeight = 5200;
const nestedSnapshot = {{
  top: 2000,
  bottom: 0,
  scrollHeight: 5200,
  pinned: true,
  userUnpinned: false,
}};
const nestedGuard = _prepareLiveAnchorScrollRebuildGuard(nestedSnapshot);
assert.strictEqual(nestedGuard.readerAwayFromBottom, true);
assert.strictEqual(inner.style.minHeight, '5200px');
assert.strictEqual(inner.dataset.liveAnchorScrollGuardPreviousMinHeight, '');
guard.release();
assert.strictEqual(inner.style.minHeight, '');
nestedGuard.release();
assert.strictEqual(inner.style.minHeight, '');
assert.strictEqual(Object.prototype.hasOwnProperty.call(inner.dataset, 'liveAnchorScrollGuardPreviousMinHeight'), false);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_live_anchor_rebuild_guard_keeps_pinned_follower_pinned_on_large_growth():
    """A PINNED follower must NOT be reclassified as unpinned when a large live
    render transiently pushes bottomDistance>250. Regression guard: the predicate
    must require an explicit non-follow signal (_messageUserUnpinned / _scrollPinned
    === false), not a raw scrollTop>0, or pinned live streams stop auto-following."""

    script = f"""
const assert = require('assert');
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _nearBottomCount = 2;
const messages = {{
  scrollTop: 4400,
  scrollHeight: 5500,
  clientHeight: 600,
}};
const inner = {{ style: {{ minHeight: '' }}, dataset: {{}} }};
function $(id) {{
  if (id === 'messages') return messages;
  if (id === 'msgInner') return inner;
  return null;
}}
{_function_body(UI_JS, "_prepareLiveAnchorScrollRebuildGuard")}
const snapshot = {{
  top: 4400,
  bottom: 0,
  scrollHeight: 5500,
  pinned: true,
  userUnpinned: false,
}};
const guard = _prepareLiveAnchorScrollRebuildGuard(snapshot);
// bottomDistance = 5500 - 4400 - 600 = 500 (>250), but the reader is pinned and
// has not manually unpinned -> must stay pinned, follow-scroll must NOT be skipped.
assert.strictEqual(guard.readerAwayFromBottom, false);
assert.strictEqual(guard.release, null);
assert.strictEqual(_messageUserUnpinned, false);
assert.strictEqual(_scrollPinned, true);
assert.strictEqual(snapshot.pinned, true);
assert.strictEqual(snapshot.userUnpinned, false);
assert.strictEqual(inner.style.minHeight, '');
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_restore_message_scroll_snapshot_keeps_intermediate_distance_state():
    script = f"""
const assert = require('assert');
let _programmaticScroll = false;
let _messageVirtualWindowKey = 'old-window';
let _lastScrollTop = 0;
let _messageUserUnpinned = true;
let _scrollPinned = false;
let _nearBottomCount = 0;
let _messageViewportAnchorRemounting = false;
const container = {{
  scrollTop: 10,
  scrollHeight: 800,
  clientHeight: 400,
  getBoundingClientRect() {{ return {{ top: 100, bottom: 500 }}; }},
  querySelector() {{ return null; }}
}};
function $(id) {{ return id === 'messages' ? container : null; }}
function requestAnimationFrame(fn) {{ fn(); }}
function setTimeout(fn) {{ fn(); }}
function _getVisibleMessagesWithIdx() {{ return []; }}
function _messageVisibleIndexForRawIdx() {{ return -1; }}
function _messageVirtualScrollTopForVisibleIdx() {{ throw new Error('not expected'); }}
function renderMessages() {{ throw new Error('not expected'); }}
{_function_body(UI_JS, "_restoreMessageViewportAnchor")}
{_function_body(UI_JS, "_remountMessageViewportAnchor")}
{_function_body(UI_JS, "_restoreMessageScrollSnapshot")}
_restoreMessageScrollSnapshot({{
  anchor: null,
  top: 200,
  bottom: 200,
  pinned: false,
  userUnpinned: false,
}});
assert.strictEqual(container.scrollTop, 200);
assert.strictEqual(_lastScrollTop, 200);
assert.strictEqual(_messageUserUnpinned, true);
assert.strictEqual(_scrollPinned, false);
assert.strictEqual(_nearBottomCount, 0);
assert.strictEqual(_programmaticScroll, false);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_restore_message_scroll_snapshot_prefers_semantic_anchor_with_virtual_fallback():
    body = _function_body(UI_JS, "_restoreMessageScrollSnapshot")
    helper = _function_body(UI_JS, "_remountMessageViewportAnchor")
    compact = _compact(body)
    helper_compact = _compact(helper)

    assert "snapshot.anchor&&typeof_restoreMessageViewportAnchor==='function'" in compact
    assert "if(!restoredViaAnchor&&typeof_remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor))" in compact
    assert "typeof_getVisibleMessagesWithIdx!=='function'" in helper_compact
    assert "_getVisibleMessagesWithIdx()" in helper_compact
    assert "_messageVisibleIndexForRawIdx(targetIdx,visWithIdx)" in helper_compact
    assert "_messageVirtualScrollTopForVisibleIdx(visWithIdx,visIdx,container)" in helper_compact
    assert "_messageVirtualWindowKey=''" in helper_compact
    assert "renderMessages({preserveScroll:true});" in helper_compact
    assert "_messageViewportAnchorRemounting=true" in helper_compact
    assert "setTimeout(()=>{_programmaticScroll=false;},0);" in helper_compact


def test_restore_message_scroll_snapshot_keeps_user_unpinned_state_authoritative_mid_stream():
    body = _function_body(UI_JS, "_restoreMessageScrollSnapshot")
    compact = _compact(body)

    assert "constbottomDistance=el.scrollHeight-el.scrollTop-el.clientHeight;" in compact
    assert "if(snapshot.userUnpinned===true){_messageUserUnpinned=true;_scrollPinned=false;_nearBottomCount=0;}elseif(snapshot.pinned===true){_messageUserUnpinned=false;_scrollPinned=true;_nearBottomCount=2;}else{" in compact
    assert "_messageUserUnpinned=false;_scrollPinned=false;_nearBottomCount=0;" not in compact


def test_same_frame_restore_uses_the_shared_anchor_remount_fallback():
    body = _function_body(UI_JS, "_restoreMessageScrollSnapshotSameFrame")
    compact = _compact(body)

    assert "if(!restoredViaAnchor&&typeof_remountMessageViewportAnchor==='function'&&_remountMessageViewportAnchor(snapshot.anchor))" in compact
    assert "_messageVirtualScrollTopForVisibleIdx" not in body
