"""Regression tests for the issue #5637 streaming stale-anchor guards.

Once the reader is up in history during a live stream, the anchor captured for a
same-frame restore goes stale as the streaming chunk grows content ABOVE the viewport:
the anchor's captured topOffset (and the absolute snapshot.top) no longer map to the
same content, so realigning to them yanks a still reader backward by a few hundred px.
The existing `snapshot.userUnpinned===true` fallback skip is defeated because the
scrollHeight-collapse scroll event re-pins the state machine (flips userUnpinned back to
false) mid-stream.

Two guards close this, both keyed on content-growth-since-capture + absence of real
input intent (NOT a scrollTop diff, which the browser's own overflow-anchor writes on an
overflow-anchor:auto container):
  1. `_restoreMessageViewportAnchor` refuses the realign write.
  2. the absolute `snapshot.top` fallback in `_restoreMessageScrollSnapshotSameFrame`
     refuses its write.

Every behavioral test below is designed to FAIL on the pre-guard code and PASS only with
the guard. Node-harness pattern (extractFunc + mock DOM) shared with the sibling scroll
regression suites.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
UI_JS_PATH = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)], cwd=str(ROOT),
            capture_output=True, text=True, timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null, inLine = false, inBlock = false, inRegex = false, prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i], n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) { if (c === '\\') { i += 2; continue; } if (c === str) str = null; i++; continue; }
    if (inRegex) { if (c === '\\') { i += 2; continue; } if (c === '/') inRegex = false; i++; continue; }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) { inRegex = true; i++; continue; }
    if (c === '{') depth++; else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}"""
    return prelude + body


# ---- realign guard (_restoreMessageViewportAnchor) -------------------------------

def _realign_harness(*, anchor_extra: str, cur_scroll_height: int, rect_top: int,
                     top_offset: int, active_intent: bool = False,
                     touch_like: bool = True) -> str:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    intent_js = "true" if active_intent else "false"
    touch_js = "true" if touch_like else "false"
    return _extract_func_script(js) + f"""
let writes = [];
let stTop = 90030;
const row = {{ getBoundingClientRect(){{ return {{ top: {rect_top} }}; }},
  getClientRects(){{ return [{{}}]; }}, dataset: {{ messageAnchorKey: 'k1' }} }};
const container = {{
  get scrollTop(){{ return stTop; }}, set scrollTop(v){{ writes.push(Math.round(v)); stTop = v; }},
  scrollHeight: {cur_scroll_height}, clientHeight: 427,
  getBoundingClientRect(){{ return {{ top: 0, bottom: 427 }}; }},
  querySelectorAll(){{ return [row]; }}, querySelector(){{ return row; }}, style: {{}},
}};
function $(id){{ return id === 'messages' ? container : null; }}
function _recentMessageScrollIntent(){{ return {intent_js}; }}
function _recentMessageTouchScrollIntent(){{ return {intent_js}; }}
function _isTouchLikeMessageViewport(){{ return {touch_js}; }}
let _programmaticScroll = false; let _programmaticScrollSetAt = 0;
const performance = {{ now(){{ return 1; }} }};
function _suppressBrowserOverflowAnchor(){{ return null; }}
function _deferClearProgrammaticScroll(){{}}
function requestAnimationFrame(cb){{ cb(); }}
function setTimeout(cb){{ cb(); return 1; }}
const anchor = Object.assign({{ rawIdx: 53, sessionIdx: 53, key: 'k1', topOffset: {top_offset} }}, {anchor_extra});
eval(extractFunc('_restoreMessageViewportAnchor'));
const returned = _restoreMessageViewportAnchor(anchor, 0);
console.log(JSON.stringify({{ returned, wrote: writes.length }}));
"""


def test_realign_refuses_stale_anchor_when_content_grew_no_intent():
    """Content grew since capture (90000->90453) and no input intent, realign delta -453
    -> REFUSE (return false, no write). Mutation: delete the guard block and it writes."""
    m = json.loads(_run_node(_realign_harness(
        anchor_extra="{scrollHeightAtCapture: 90000}",
        cur_scroll_height=90453, rect_top=-453, top_offset=0, active_intent=False,
    )))
    assert m["returned"] is False and m["wrote"] == 0


def test_realign_allows_fresh_anchor_no_growth():
    """No growth since capture -> realign runs normally even with the same delta."""
    m = json.loads(_run_node(_realign_harness(
        anchor_extra="{scrollHeightAtCapture: 90453}",
        cur_scroll_height=90453, rect_top=-453, top_offset=0, active_intent=False,
    )))
    assert m["returned"] is True and m["wrote"] == 1


def test_realign_allows_with_active_intent():
    """Recent real input intent -> keep the legitimate realign even if content grew."""
    m = json.loads(_run_node(_realign_harness(
        anchor_extra="{scrollHeightAtCapture: 90000}",
        cur_scroll_height=90453, rect_top=-453, top_offset=0, active_intent=True,
    )))
    assert m["returned"] is True and m["wrote"] == 1


def test_realign_backward_compatible_without_capture_geometry():
    """Legacy anchor without scrollHeightAtCapture -> guard is a no-op, prior behavior."""
    m = json.loads(_run_node(_realign_harness(
        anchor_extra="{}",
        cur_scroll_height=90453, rect_top=-453, top_offset=0, active_intent=False,
    )))
    assert m["returned"] is True and m["wrote"] == 1


def test_realign_allows_on_desktop_no_native_anchor():
    """Desktop regression (#5637 gate cert): the exact stale-anchor case
    (content grew, no intent, delta -453) but on a hover+fine-pointer viewport where
    `.messages` is `overflow-anchor:none`. There is no native anchoring layer to hold
    the reader, so the guard MUST NOT refuse — the semantic scrollTop realign has to
    run or the desktop reader is left unheld after above-viewport growth.
    Mutation: drop the `_touchHold&&` term from the realign guard and this FAILS
    (the write is wrongly refused on desktop)."""
    m = json.loads(_run_node(_realign_harness(
        anchor_extra="{scrollHeightAtCapture: 90000}",
        cur_scroll_height=90453, rect_top=-453, top_offset=0, active_intent=False,
        touch_like=False,
    )))
    assert m["returned"] is True and m["wrote"] == 1


# ---- fallback guard (_restoreMessageScrollSnapshotSameFrame) ---------------------

def _fallback_harness(*, snapshot_scroll_height, cur_scroll_height, snapshot_top,
                      active_intent: bool = False, touch_like: bool = True,
                      init_scroll_top: int = 90030,
                      anchor=None, anchor_row_content_pos=None, container_top: int = 0,
                      top_pad_now=None) -> str:
    """Node harness for the absolute-fallback path of _restoreMessageScrollSnapshotSameFrame.

    SCROLL-DEPENDENT geometry (round-3 gate-cert requirement): the anchor row's
    getBoundingClientRect().top is computed LIVE as `rowContentPos - scrollTop + container_top`
    — exactly how a real browser reports it — NOT a fixed constant. So after the fix
    writes `el.scrollTop += delta`, a subsequent rect read reflects the new position, and
    a certified hold number is physically realizable.

    anchor: dict for snapshot.anchor (with key/sessionIdx/topOffset[/topPadBefore]) or None.
    anchor_row_content_pos: the row's ABSOLUTE content position (document-space top). Its
      live viewport offset is rowContentPos - scrollTop. When None, the row lookup returns
      nothing (genuinely-gone anchor -> topPad-delta or raw).
    top_pad_now: current virtual top-spacer height (for the genuinely-gone topPad-delta path).
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    intent_js = "true" if active_intent else "false"
    touch_js = "true" if touch_like else "false"
    sh = "null" if snapshot_scroll_height is None else str(snapshot_scroll_height)
    anchor_js = "null" if anchor is None else json.dumps(anchor)
    row_present = anchor is not None and anchor_row_content_pos is not None
    row_pos_js = "0" if anchor_row_content_pos is None else str(anchor_row_content_pos)
    pad_now_js = "null" if top_pad_now is None else str(top_pad_now)
    return _extract_func_script(js) + f"""
let writes = [];
let stTop = {init_scroll_top};
const CONTAINER_TOP = {container_top};
const ROW_CONTENT_POS = {row_pos_js};
const ROW_PRESENT = {"true" if row_present else "false"};
const TOP_PAD_NOW = {pad_now_js};
// A real browser's rect.top for a row is (rowContentPos - scrollTop) relative to the
// document, so its offset below the container top is ROW_CONTENT_POS - scrollTop.
const _anchorRow = ROW_PRESENT ? {{
  getBoundingClientRect(){{ return {{ top: CONTAINER_TOP + (ROW_CONTENT_POS - stTop) }}; }},
  getClientRects(){{ return [{{}}]; }},
}} : null;
const _topSpacer = (TOP_PAD_NOW === null) ? null : {{
  style: {{ height: TOP_PAD_NOW + 'px' }},
}};
const el = {{
  get scrollTop(){{ return stTop; }}, set scrollTop(v){{ writes.push(Math.round(v)); stTop = v; }},
  scrollHeight: {cur_scroll_height}, clientHeight: 427,
  getBoundingClientRect(){{ return {{ top: CONTAINER_TOP, bottom: CONTAINER_TOP + 427 }}; }},
  querySelector(sel){{
    if (sel === '[data-virtual-spacer="before"]') return _topSpacer;
    return _anchorRow;
  }},
  querySelectorAll(sel){{ return _anchorRow ? [_anchorRow] : []; }},
}};
function $(id){{ return id === 'messages' ? el : null; }}
function _recentMessageScrollIntent(){{ return {intent_js}; }}
function _recentMessageTouchScrollIntent(){{ return {intent_js}; }}
function _isTouchLikeMessageViewport(){{ return {touch_js}; }}
// realign path fails (no anchor restore) so execution reaches the absolute fallback
function _restorePinnedMessageScrollSnapshot(){{ return false; }}
function _restoreMessageViewportAnchor(){{ return false; }}
function _remountMessageViewportAnchor(){{ return false; }}
let _messageUserUnpinned = false; let _scrollPinned = true; let _nearBottomCount = 5;
let _lastScrollTop = 0; let _lastMessageClientHeight = 0;
let _programmaticScroll = false; let _programmaticScrollSetAt = 0;
const performance = {{ now(){{ return 1; }} }};
function _deferClearProgrammaticScroll(){{}}
function requestAnimationFrame(cb){{ cb(); }}
function setTimeout(cb){{ cb(); return 1; }}
const snapshot = {{ anchor: {anchor_js}, top: {snapshot_top}, bottom: 40,
  scrollHeight: {sh}, pinned: false, userUnpinned: false }};
eval(extractFunc('_desktopAnchorRealignDelta'));
eval(extractFunc('_restoreMessageScrollSnapshotSameFrame'));
_restoreMessageScrollSnapshotSameFrame(snapshot);
console.log(JSON.stringify({{ wrote: writes.length, writes,
  messageUserUnpinned: _messageUserUnpinned, scrollPinned: _scrollPinned, finalScrollTop: Math.round(stTop) }}));
"""


def test_fallback_refuses_stale_snapshot_top_when_content_grew_no_intent():
    """Content grew since snapshot (90000->90453), reader not pinned, no intent, and the
    absolute snapshot.top write would move scrollTop >8px -> REFUSE. This is the -578
    on-device jump the userUnpinned check missed (re-pin flipped userUnpinned false).
    Mutation: delete the fallback guard block and scrollHistory gets the stale write."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90453, snapshot_top=89577,
        active_intent=False,
    )))
    assert m["writes"] == []
    assert m["messageUserUnpinned"] is True and m["scrollPinned"] is False


def test_fallback_allows_snapshot_top_when_no_growth():
    """No growth since snapshot -> the fallback keeps its authoritative absolute restore."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90453, cur_scroll_height=90453, snapshot_top=89577,
        active_intent=False,
    )))
    assert m["writes"] == [89577]


def test_fallback_backward_compatible_without_capture_scrollheight():
    """Legacy snapshot without scrollHeight -> guard no-op, prior absolute restore runs."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=None, cur_scroll_height=90453, snapshot_top=89577,
        active_intent=False,
    )))
    assert m["writes"] == [89577]


def test_fallback_allows_snapshot_top_with_active_intent():
    """Content grew since snapshot, but the reader has recent real input intent -> the
    fallback keeps its legitimate absolute restore (an actively-scrolling reader owns the
    snapshot). Mirrors the realign-guard active-intent case.
    Mutation: drop the `!_activeIntent` term from the fallback guard and this fails
    (the write would be wrongly refused)."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90453, snapshot_top=89577,
        active_intent=True,
    )))
    assert m["writes"] == [89577]


def test_fallback_allows_snapshot_top_on_desktop_no_native_anchor():
    """Desktop regression (#5637 gate cert): the exact stale-snapshot case (content grew,
    reader not pinned, no intent, absolute write would move >8px) but on a
    hover+fine-pointer viewport where `.messages` is `overflow-anchor:none`. With no
    native anchoring layer to hold the reader, the fallback MUST still WRITE rather than
    refuse-and-latch userUnpinned. With NO anchor on the snapshot (and no top-spacer
    geometry) there is nothing to realign against, so it keeps the raw snapshot.top —
    the same authoritative absolute write as before.
    Mutation: drop the `_fbTouchHold&&` term from the fallback guard and this FAILS
    (the write is wrongly refused and userUnpinned is latched on desktop)."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90453, snapshot_top=89577,
        active_intent=False, touch_like=False,
    )))
    assert m["writes"] == [89577]
    assert m["messageUserUnpinned"] is False and m["scrollPinned"] is True


# ---- round-3 desktop anchor-realign (PR #5742): app's own scrollTop += delta idiom -----
# These use SCROLL-DEPENDENT rect mocks (rect.top = rowContentPos - scrollTop), so the
# certified hold numbers are physically realizable in a real browser — the round-2
# gate-cert requirement that fixed-rect mocks violated.

def test_realign_holds_reader_on_above_viewport_growth_desktop():
    """Reader parked up in history on desktop; above-viewport content grew since capture.
    The anchor row was captured at topOffset 1000; the reader has ALREADY been carried
    down by the browser to scrollTop 1480 (partial), and the row's content position is
    2000 so its live offset is 2000-1480=520. The app idiom writes
    scrollTop += (currentOffset - capturedOffset) = 1480 + (520 - 1000) = 1000, landing
    the row back at its captured 1000px offset (2000 - 1000 = 1000). That HOLD is
    physically realizable: after the write, rect.top = 2000 - 1000 = 1000 = capturedOffset.
    Mutation: revert `_fbTarget` to the raw `target` (snapshot.top=1200) and this FAILS
    (the reader is left at 1200, a 200px drift from the held content position)."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90600, snapshot_top=1200,
        active_intent=False, touch_like=False,
        anchor={"key": "k1", "sessionIdx": 5, "topOffset": 1000},
        anchor_row_content_pos=2000, container_top=0, init_scroll_top=1480,
    )))
    # scrollTop += (currentOffset - capturedOffset) = 1480 + ((2000-1480) - 1000) = 1000
    assert m["writes"] == [1000]
    # and the row is now held at its captured offset — physically consistent
    assert m["finalScrollTop"] == 1000
    assert m["messageUserUnpinned"] is False and m["scrollPinned"] is True


def test_realign_is_noop_when_already_aligned_desktop():
    """No arbiter: when the reader is already at the captured offset (row content pos 2000,
    scrollTop 1000 -> live offset 1000 == captured 1000), the realign delta is 0 and no
    move happens (delta < clamp is still written as the same value, i.e. a no-op write to
    the same scrollTop). Proves the idiom does not perturb an already-correct position."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90600, snapshot_top=1200,
        active_intent=False, touch_like=False,
        anchor={"key": "k1", "sessionIdx": 5, "topOffset": 1000},
        anchor_row_content_pos=2000, container_top=0, init_scroll_top=1000,
    )))
    # currentOffset = 2000 - 1000 = 1000 == captured 1000 -> delta 0 -> scrollTop stays 1000
    assert m["finalScrollTop"] == 1000
    assert m["writes"] == [1000]


def test_realign_does_not_drift_on_below_viewport_tail_growth_desktop():
    """Below-viewport (tail) growth: the anchor row ABOVE the reader did NOT move
    (content pos 2000, scrollTop 1000 -> offset 1000 == captured 1000), only content
    below grew (scrollHeight 90000 -> 95000). The realign delta is 0 -> the reader is
    NOT pulled downward. This is the conveyor-belt drift the round-2 arbiter formula
    reintroduced; the app idiom cannot, because it keys on the ABOVE-viewport anchor row,
    whose offset is unchanged by tail growth.
    Mutation: a `snapshot.top + totalGrowth` formula would write 1000 + 5000 = 6000 and
    drift the reader down 5000px; this asserts the held 1000."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=95000, snapshot_top=1000,
        active_intent=False, touch_like=False,
        anchor={"key": "k1", "sessionIdx": 5, "topOffset": 1000},
        anchor_row_content_pos=2000, container_top=0, init_scroll_top=1000,
    )))
    assert m["finalScrollTop"] == 1000
    assert m["writes"] == [1000]


def test_realign_genuinely_gone_anchor_uses_toppad_delta_desktop():
    """Anchor row genuinely gone (lookup returns null), but the snapshot carries
    topPadBefore and the current virtual top-spacer is measurable. Mirror the topPad-delta
    idiom: shift scrollTop by (padNow - padBefore) so the reader is held by the same
    amount the content above moved. padBefore=800, padNow=1300 -> +500 from scrollTop 1000
    -> 1500.
    Mutation: drop the topPad-delta branch and it keeps raw snapshot.top (1200), a
    300px drift from the topPad-held 1500."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90500, snapshot_top=1200,
        active_intent=False, touch_like=False,
        anchor={"key": "gone", "sessionIdx": 999, "topOffset": 1000, "topPadBefore": 800},
        anchor_row_content_pos=None, container_top=0, init_scroll_top=1000,
        top_pad_now=1300,
    )))
    assert m["finalScrollTop"] == 1500
    assert m["writes"] == [1500]


def test_realign_genuinely_gone_anchor_no_geometry_keeps_raw_desktop():
    """Anchor row gone AND no topPad geometry available -> keep the raw snapshot.top
    (no guessing with an arbiter). snapshot.top=1200 -> writes 1200."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90500, snapshot_top=1200,
        active_intent=False, touch_like=False,
        anchor={"key": "gone", "sessionIdx": 999, "topOffset": 1000},
        anchor_row_content_pos=None, container_top=0, init_scroll_top=1000,
        top_pad_now=None,
    )))
    assert m["writes"] == [1200]


def test_realign_gone_anchor_null_toppadbefore_does_not_fling_desktop():
    """greptile P1: anchor row gone, a virtual top-spacer IS present (padNow=1300), but the
    snapshot carries NO captured topPadBefore (null). Number(null) is 0, so a naive
    isFinite(padBefore) check would treat padBefore as 0 and add the ENTIRE 1300px spacer
    to scrollTop (1000 -> 2300), flinging the reader far from their content. The guard
    requires an ACTUAL captured topPadBefore, so with null it keeps the raw snapshot.top.
    Mutation: drop the `_padBeforeRaw!=null` term and this FAILS (writes 2300, the fling)."""
    m = json.loads(_run_node(_fallback_harness(
        snapshot_scroll_height=90000, cur_scroll_height=90500, snapshot_top=1200,
        active_intent=False, touch_like=False,
        anchor={"key": "gone", "sessionIdx": 999, "topOffset": 1000, "topPadBefore": None},
        anchor_row_content_pos=None, container_top=0, init_scroll_top=1000,
        top_pad_now=1300,
    )))
    assert m["writes"] == [1200]




def _predicate_harness(*, pointer_coarse, computed_overflow_anchor, has_matchmedia=True,
                       inline_overflow_anchor="", ua="Mozilla/5.0 (Linux; Android 13)",
                       platform="Linux armv8l", max_touch_points=5) -> str:
    """Exercise the REAL _isTouchLikeMessageViewport + _browserOverflowAnchorActive
    + _isIOSWebKit against a fake element, mocking matchMedia, getComputedStyle and
    navigator. `inline_overflow_anchor` simulates the inline `overflowAnchor:'none'`
    that _restoreMessageViewportAnchor writes on #messages mid-realign — the value the
    computed probe would transiently read. `ua`/`platform`/`max_touch_points` drive the
    _isIOSWebKit branch (default: an Android touch device that is NOT iOS)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    mm = "true" if pointer_coarse else "false"
    has_mm = "true" if has_matchmedia else "false"
    return _extract_func_script(js) + f"""
const HAS_MM = {has_mm};
if (HAS_MM) {{
  globalThis.matchMedia = function(q){{ return {{ matches: (q === '(pointer:coarse)') ? {mm} : false }}; }};
}} else {{
  globalThis.matchMedia = undefined;
}}
// Node 18+ ships a built-in read-only `navigator`; a plain assignment is silently
// ignored, so force the mock in with defineProperty.
Object.defineProperty(globalThis, 'navigator', {{ configurable: true, value: {{ userAgent: {json.dumps(ua)}, platform: {json.dumps(platform)}, maxTouchPoints: {max_touch_points} }} }});
// getComputedStyle reflects the INLINE override first (as a real browser would during
// a realign burst), else the resting computed value.
const el = {{ style: {{ overflowAnchor: {json.dumps(inline_overflow_anchor)} }} }};
globalThis.getComputedStyle = function(node){{
  const inline = node && node.style && node.style.overflowAnchor;
  return {{ overflowAnchor: inline || {json.dumps(computed_overflow_anchor)} }};
}};
eval(extractFunc('_browserOverflowAnchorActive'));
eval(extractFunc('_isIOSWebKit'));
eval(extractFunc('_isTouchLikeMessageViewport'));
console.log(JSON.stringify({{ touchLike: _isTouchLikeMessageViewport(el) }}));
"""


def test_predicate_stays_true_on_touch_when_inline_anchor_clobbered_to_none():
    """The claim that motivated choosing matchMedia over the computed probe: on a touch
    device, when a prior realign tick has clobbered the inline `overflowAnchor` to 'none'
    on #messages (its own scroll-write side effect, restored next frame), the predicate
    MUST still report touch=true so the hold gate stays engaged across a realign burst.
    matchMedia('(pointer:coarse)') reflects the input device and can't be mutated by that
    inline write.
    Mutation: revert _isTouchLikeMessageViewport to `return _browserOverflowAnchorActive(el)`
    (the computed probe alone) and this FAILS — the probe reads the inline 'none' and
    misclassifies the touch device as desktop."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=True, computed_overflow_anchor="auto",
        inline_overflow_anchor="none",
    )))
    assert m["touchLike"] is True


def test_predicate_false_on_desktop_fine_pointer():
    """Desktop (fine pointer): matchMedia('(pointer:coarse)') is false and the resting
    computed overflow-anchor is 'none' -> predicate reports NOT touch, so the guards do
    not fire and desktop keeps its scroll restore."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=False, computed_overflow_anchor="none",
    )))
    assert m["touchLike"] is False


def test_predicate_falls_back_to_computed_probe_without_matchmedia():
    """Best-effort fallback: with no matchMedia available, the predicate uses the computed
    overflow-anchor probe. A resting 'auto' (touch) -> true."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=True, computed_overflow_anchor="auto", has_matchmedia=False,
    )))
    assert m["touchLike"] is True


# ---- iOS WebKit exclusion (_isIOSWebKit) — overflow-anchor is inert on iOS -------

def test_predicate_false_on_ios_iphone_despite_pointer_coarse():
    """iOS gate cert (#5637 round-2 RED): an iPhone is pointer:coarse with a resting
    computed overflow-anchor of 'auto', so the round-1 predicate would classify it as a
    hold-capable touch viewport and let the stale-anchor refusal fire. But overflow-anchor
    is INERT on iOS WebKit, so refusing the restore leaves a scrolled-up reader unheld —
    the same class as the desktop regression. The predicate MUST report NOT touch on iOS so
    the semantic realign is kept.
    Mutation: remove the `if(_isIOSWebKit()) return false;` line and this FAILS (iPhone is
    misclassified as a hold-capable touch viewport)."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=True, computed_overflow_anchor="auto",
        ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        platform="iPhone", max_touch_points=5,
    )))
    assert m["touchLike"] is False


def test_predicate_false_on_ipados13_masquerading_as_mac():
    """iPadOS 13+ reports a desktop UA + platform 'MacIntel' but has touch (maxTouchPoints>1),
    unlike a real Mac. overflow-anchor is inert there too, so it must be excluded from the
    refusal like the iPhone.
    Mutation: drop the `platform==='MacIntel' && maxTouchPoints>1` branch in _isIOSWebKit and
    this FAILS (iPad is treated as an anchor-capable touch device)."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=True, computed_overflow_anchor="auto",
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        platform="MacIntel", max_touch_points=5,
    )))
    assert m["touchLike"] is False


def test_predicate_true_on_android_not_ios():
    """Android Chrome: pointer:coarse, overflow-anchor works, and it is NOT iOS — this is
    the one platform where the refusal is safe, so the predicate stays true. Guards against
    an over-broad _isIOSWebKit that would also exclude Android.
    Mutation: broaden _isIOSWebKit to match any touch device (e.g. return maxTouchPoints>1)
    and this FAILS (Android would be wrongly excluded, re-opening the mobile jump)."""
    m = json.loads(_run_node(_predicate_harness(
        pointer_coarse=True, computed_overflow_anchor="auto",
        ua="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/120.0.0.0 Mobile Safari/537.36",
        platform="Linux armv8l", max_touch_points=5,
    )))
    assert m["touchLike"] is True

