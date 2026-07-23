"""Regression tests for the non-virtualized user-row content-visibility collapse
jump-back (follow-up to #5637 / #5638).

Root cause (proven in a mobile-emulated Playwright repro on an isolated debug
instance): when a reader has transcript virtualization disabled
(`_virtualizeTranscript === false`, the #4325 opt-out), renderMessages() renders
every row with no windowing and NEVER runs the virtualized measure pass
(`_updateMessageVirtualMeasurements` early-returns when
`!virtualWindow.virtualized`). Under `@media (pointer: coarse)`,
`.msg-row[data-role="user"]` carries `content-visibility: auto;
contain-intrinsic-size: auto 96px`. Every renderMessages() rebuild does
`inner.innerHTML=''` then recreates all rows as FRESH elements; a fresh,
off-screen tall user row (e.g. a long paste measuring thousands of px) reserves
only the flat estimate instead of its real height, so scrollHeight shrinks by
(realHeight - estimate) and the browser force-clamps scrollTop -> the viewport
jumps backward (`JS=none`, a browser clamp, which is why no JS scrollTop-write
compensation catches it). Desktop rests at content-visibility:visible so
intrinsic-size is inert there.

Three coordinated pieces fix it, each covered below:
1. `_estimateUserRowIntrinsicHeight` weights CJK / full-width characters as ~2
   columns, so a Chinese/Japanese/Korean paste (which wraps at ~24 chars/line,
   not 48) reserves close to its real height even before it is ever measured.
2. `_applyUserRowIntrinsicHeight` reserves `max(remembered, estimate)`, so a
   PARTIAL-paint remembered height (a row taller than the viewport only ever
   paints its intersecting slice under content-visibility:auto) can never
   under-reserve below the content estimate.
3. `_rememberRenderedUserRowIntrinsicHeights` (called pre-wipe inside
   renderMessages, and the non-virtualized analog of #5638's measure pass) only
   persists a height from a row currently within the viewport (painted =>
   trustworthy) and floors it at the estimate.

Each behavioral test is written to FAIL on the known-buggy version and PASS only
on the fixed version (mutation notes inline).
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
            [NODE, str(script_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    # Brace-matches a function body while skipping braces inside string / template
    # / regex literals and comments (same hardened extractor as the sibling #5638
    # suite).
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;
  let inLine = false;
  let inBlock = false;
  let inRegex = false;
  let prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) {
      if (c === '\\') { i += 2; continue; }
      if (c === str) str = null;
      i++; continue;
    }
    if (inRegex) {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') inRegex = false;
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) {
      inRegex = true; i++; continue;
    }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}"""
    return prelude + body


def _dom_prelude() -> str:
    """Fake DOM: a $('id') helper returning a fake #messages container and
    #msgInner, plus user rows with getBoundingClientRect + dataset + style, so
    the extracted _rememberRenderedUserRowIntrinsicHeights can run headless.

    The container is 600px tall (viewport). A row's `top` is where it sits
    relative to the container top; a row is "in view" when it straddles the
    [0, 600] band widened by a one-screen margin.
    """
    return r"""
var _userRowIntrinsicHeightBySessionIdx = Object.create(null);
function makeRow(role, sessionMsgIdx, rectTop, rectHeight, rawText){
  return {
    _top: rectTop, _height: rectHeight,
    style: { containIntrinsicSize: '' },
    dataset: { role: role, sessionMsgIdx: String(sessionMsgIdx),
               rawText: rawText || '', msgIdx: String(sessionMsgIdx) },
    classList: { contains(){ return false; } },
    getAttribute(){ return null; },
    getBoundingClientRect(){ return { top: this._top, bottom: this._top + this._height, height: this._height }; },
  };
}
var __rows = [];
var __container = { getBoundingClientRect(){ return { top: 0, bottom: 600, height: 600 }; } };
var __inner = { querySelectorAll(){ return __rows.slice(); } };
function $(id){ if(id==='messages') return __container; if(id==='msgInner') return __inner; return null; }
"""


def test_estimate_weights_cjk_as_double_width():
    """A CJK (full-width) paste must reserve ~2x the height a same-length latin
    paste would, because CJK glyphs occupy ~2 columns and wrap at ~24 chars/line.
    The pre-fix estimate counted every char as 1 column, badly under-reserving a
    Chinese/Japanese/Korean row -> off-screen collapse -> jump-back.

    Mutation: revert the per-char width weighting (count `columns += 1` for all)
    and the CJK estimate drops to the latin value, failing the >1.7x assertion.
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
// Equal CHARACTER counts; the CJK one has ~2x the visual columns.
const latin = 'a'.repeat(1200);
const cjk = '\u4e2d'.repeat(1200);   // 1200 CJK ideographs
console.log(JSON.stringify({
  latin: _estimateUserRowIntrinsicHeight(latin),
  cjk: _estimateUserRowIntrinsicHeight(cjk),
}));
"""
    m = json.loads(_run_node(source))
    assert m["cjk"] > m["latin"] * 1.7, (
        "a CJK paste must reserve ~2x a same-length latin paste (full-width "
        f"columns); got cjk={m['cjk']} latin={m['latin']}"
    )


def test_estimate_short_row_still_floors_at_96():
    """No regression: a short row must never reserve less than today's 96px."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
console.log(JSON.stringify({
  empty: _estimateUserRowIntrinsicHeight(''),
  hi: _estimateUserRowIntrinsicHeight('hi'),
  cjkShort: _estimateUserRowIntrinsicHeight('\u4f60\u597d'),
}));
"""
    m = json.loads(_run_node(source))
    assert m["empty"] == 96 and m["hi"] == 96 and m["cjkShort"] == 96, (
        f"short rows must floor at 96px; got {m!r}"
    )


def test_apply_reserves_max_of_remembered_and_estimate():
    """_applyUserRowIntrinsicHeight must reserve max(remembered, estimate). A
    remembered height can be a PARTIAL paint (a row taller than the viewport only
    paints its intersecting slice), so when the content estimate is LARGER it must
    win -- otherwise a partial 1500px remembered value under-reserves a really
    3000px row and scrollHeight collapses on the next rebuild.

    Mutation: change `Math.max(remembered, estimate)` back to
    "remembered if >0 else estimate" and this fails (the small remembered value is
    used even though the estimate is larger).
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _dom_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
// A big CJK paste whose real height >> a stale partial-paint remembered value.
const longCjk = '\u4e2d'.repeat(1500);
const estimate = _estimateUserRowIntrinsicHeight(longCjk);
// Remember a PARTIAL paint far below the estimate (the viewport-slice trap).
_rememberUserRowIntrinsicHeight(4, 900);
const row = makeRow('user', 4, 0, 0, longCjk);
_applyUserRowIntrinsicHeight(row, longCjk);
console.log(JSON.stringify({ reserved: row.style.containIntrinsicSize, estimate: estimate }));
"""
    m = json.loads(_run_node(source))
    reserved_px = int("".join(ch for ch in m["reserved"] if ch.isdigit()))
    assert reserved_px == m["estimate"], (
        "apply must reserve the larger content estimate when the remembered value "
        f"is a smaller partial paint; got {m['reserved']!r}, estimate {m['estimate']}"
    )
    assert reserved_px > 900, "the small partial-paint remembered value must not win"


def test_apply_still_prefers_a_taller_remembered_full_measurement():
    """The max() must not regress the #5638 case: when a full measurement (taller
    than the estimate) was remembered, the rebuild still reserves that real height.
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _dom_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
_rememberUserRowIntrinsicHeight(7, 4901);
const row = makeRow('user', 7, 0, 0, 'short at build time');
_applyUserRowIntrinsicHeight(row, 'short at build time');
console.log(JSON.stringify({ reserved: row.style.containIntrinsicSize }));
"""
    m = json.loads(_run_node(source))
    assert m["reserved"] == "auto 4901px", (
        f"a taller remembered full measurement must still win; got {m['reserved']!r}"
    )


def test_remember_persists_only_in_viewport_rows():
    """_rememberRenderedUserRowIntrinsicHeights must persist a height ONLY for a
    row currently within (or straddling) the viewport band. A fully off-screen row
    reports its collapsed content-visibility reserve, not its real height;
    persisting THAT would poison the remembered map and defeat the estimate
    backstop for a never-seen row.

    Setup: one in-view user row (top 100, height 400 -> within [0,600]) whose
    measurement 400 exceeds its tiny estimate, and one far-off-screen user row
    (top 9000) whose reported height is a bogus small "reserve". Only the in-view
    row's height may be remembered.

    Mutation: drop the `if(!inView) continue;` guard and the off-screen row's bogus
    height gets persisted, failing the assertion that its key stays unset.
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _dom_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_rememberRenderedUserRowIntrinsicHeights'));
// In-view row at session idx 1: straddles the [0,600] viewport, measured 400px.
// Give it empty rawText so its estimate is the 96 floor and the 400 measurement wins.
const inView = makeRow('user', 1, 100, 400, '');
// Far off-screen row at session idx 2: top 9000 (way past the one-screen margin),
// reports a bogus small collapsed 96px reserve.
const offScreen = makeRow('user', 2, 9000, 96, '');
__rows = [inView, offScreen];
_rememberRenderedUserRowIntrinsicHeights();
console.log(JSON.stringify({
  inViewRemembered: _userRowIntrinsicHeightBySessionIdx[1] || 0,
  offScreenRemembered: _userRowIntrinsicHeightBySessionIdx[2] || 0,
  offScreenHasKey: (2 in _userRowIntrinsicHeightBySessionIdx),
}));
"""
    m = json.loads(_run_node(source))
    assert m["inViewRemembered"] == 400, (
        f"the in-view painted row's real 400px must be remembered; got {m['inViewRemembered']}"
    )
    assert not m["offScreenHasKey"], (
        "a fully off-screen row's collapsed reserve must NOT be persisted "
        f"(poisons the map); got remembered={m['offScreenRemembered']}"
    )


def test_remember_floors_persisted_height_at_estimate():
    """When an in-view row is TALLER than the viewport it only paints its slice, so
    its measured height is a partial value. The persisted height must be floored at
    the content estimate so a partial paint can never store a value below a
    reasonable full-row guess.

    Setup: an in-view CJK row whose measured height (500, a partial slice) is far
    below its content estimate. The remembered value must be the estimate, not 500.

    Mutation: remove the `Math.max(measured, estimate)` floor in the capture and
    the small 500 partial paint gets stored, failing the assertion.
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _dom_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_rememberRenderedUserRowIntrinsicHeights'));
const longCjk = '\u4e2d'.repeat(1500);
const estimate = _estimateUserRowIntrinsicHeight(longCjk);
// In-view but only a 500px slice painted (row is taller than the 600px viewport).
const partial = makeRow('user', 3, 0, 500, longCjk);
__rows = [partial];
_rememberRenderedUserRowIntrinsicHeights();
console.log(JSON.stringify({
  remembered: _userRowIntrinsicHeightBySessionIdx[3] || 0,
  estimate: estimate,
}));
"""
    m = json.loads(_run_node(source))
    assert m["remembered"] == m["estimate"], (
        "a partial in-view paint must be floored at the content estimate; "
        f"got remembered={m['remembered']}, estimate={m['estimate']}"
    )
    assert m["remembered"] > 500, "the small partial slice must not be stored as-is"


def test_rendermessages_calls_prewipe_capture_before_wipe():
    """The pre-wipe capture must be invoked BEFORE `inner.innerHTML=''` in
    renderMessages, so it reads the still-laid-out old rows. A post-wipe (or
    post-render) read would see fresh, never-painted rows reporting their collapsed
    reserve.

    Structural guard: in the renderMessages source the
    `_rememberRenderedUserRowIntrinsicHeights()` call must appear before the first
    `inner.innerHTML=''`. Mutation: move the call after the wipe and this fails.
    """
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
eval(extractFunc('renderMessages'));
const body = renderMessages.toString();
const capIdx = body.indexOf('_rememberRenderedUserRowIntrinsicHeights(');
// Match the ACTUAL wipe statement (with trailing semicolon), not the prose
// mentions of `inner.innerHTML=''` in comments above it. Use the LAST occurrence
// so a comment reference earlier in the body cannot shadow the real wipe.
const wipeIdx = body.lastIndexOf("inner.innerHTML='';");
console.log(JSON.stringify({ capIdx: capIdx, wipeIdx: wipeIdx }));
"""
    m = json.loads(_run_node(source))
    assert m["capIdx"] > 0, "renderMessages must call _rememberRenderedUserRowIntrinsicHeights"
    assert m["wipeIdx"] > 0, "renderMessages must wipe inner.innerHTML"
    assert m["capIdx"] < m["wipeIdx"], (
        "the pre-wipe capture must run BEFORE inner.innerHTML='' so it reads the "
        f"still-laid-out rows; capIdx={m['capIdx']} wipeIdx={m['wipeIdx']}"
    )
