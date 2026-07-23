"""Regression tests for the mobile scroll jump-back caused by user-row
content-visibility scrollHeight collapse (#5637 / #5638 follow-up).

Root cause (proven on-device + in mobile-emulated Playwright): under
`@media (pointer: coarse)`, `.msg-row[data-role="user"]` carries
`content-visibility: auto; contain-intrinsic-size: auto 96px`. A virtualization
wipe-and-rebuild (`renderMessages`) recreates the user row as a FRESH element,
which discards content-visibility:auto's last-remembered size. An off-screen tall
user row (e.g. a long paste measuring thousands of px) therefore falls back to the
flat 96px estimate the instant it is rebuilt, collapsing scrollHeight by
(realHeight - 96). The browser then either force-clamps scrollTop (the dTop≈dH
"layer-1" jump) or re-anchors to a far row (the dTop≫dH browser re-anchor jump) --
both mobile jump-back classes trace to this one collapse. Desktop rests at
content-visibility:visible so intrinsic-size is inert there (why desktop never
reproduces).

Fix: remember each user row's height keyed by its stable session-relative index,
and apply it as an inline `contain-intrinsic-size` both when the row is (re)built
and when it is measured. A content-length estimate reserves the bulk before the
row has ever been measured so even the first fresh-element frame does not collapse.

Every behavioral test below is designed to FAIL on the known-buggy version (no
inline intrinsic-size written -> the row keeps the flat 96px stylesheet estimate)
and PASS only on the fixed version.
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
    # / regex literals and comments (same hardened extractor as the sibling vscroll
    # suites). Built with a plain string so JS braces need no doubling.
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


def _fake_row_prelude() -> str:
    """A minimal fake DOM row/element supporting .style.containIntrinsicSize,
    .dataset, and getBoundingClientRect, shared by the tests. Also declares the
    module-level backing store the extracted helpers close over."""
    return r"""
var _userRowIntrinsicHeightBySessionIdx = Object.create(null);
function makeRow(role, sessionMsgIdx, measuredHeight){
  return {
    style: { containIntrinsicSize: '' },
    dataset: { role: role, sessionMsgIdx: String(sessionMsgIdx) },
    classList: { contains(){ return false; } },
    getBoundingClientRect(){ return { height: measuredHeight }; },
  };
}
"""


def test_estimate_reserves_more_than_96px_for_a_tall_user_message():
    """A long user message must estimate an intrinsic height well above the flat
    96px stylesheet fallback, so a rebuilt off-screen row reserves close to its
    real height and scrollHeight does not collapse."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
// ~2000 chars across the mobile bubble width => tens of lines => >>96px.
const longText = 'x'.repeat(2000);
const shortText = 'hi';
console.log(JSON.stringify({
  tall: _estimateUserRowIntrinsicHeight(longText),
  short: _estimateUserRowIntrinsicHeight(shortText),
}));
"""
    m = json.loads(_run_node(source))
    # A 2000-char row wraps to ~42 lines -> ~948px, far above 96.
    assert m["tall"] > 800, (
        "a long user message must reserve far more than the flat 96px estimate; "
        f"got {m['tall']}"
    )
    # A short row must never reserve LESS than today's 96px floor (no regression).
    assert m["short"] == 96, f"short row must floor at 96px, got {m['short']}"


def test_apply_uses_remembered_measured_height_over_estimate():
    """When a row's real measured height has been remembered (from a prior measure
    pass), _applyUserRowIntrinsicHeight must write THAT exact height onto the
    rebuilt row's inline contain-intrinsic-size -- not the 96px stylesheet default
    and not the coarser content estimate."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _fake_row_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
// Remember that the user row at sessionIdx 7 really measured 4901px earlier.
_rememberUserRowIntrinsicHeight(7, 4901);
// Now a wipe rebuilds it as a fresh element (short rawText in hand at build time).
const rebuilt = makeRow('user', 7, /*unused*/0);
_applyUserRowIntrinsicHeight(rebuilt, 'short text at build time');
console.log(JSON.stringify({ intrinsic: rebuilt.style.containIntrinsicSize }));
"""
    m = json.loads(_run_node(source))
    assert m["intrinsic"] == "auto 4901px", (
        "rebuilt user row must reserve its remembered measured height (4901px), "
        f"not the 96px default; got {m['intrinsic']!r}. On the buggy version no "
        "inline intrinsic-size is written and the row keeps the collapsing 96px "
        "stylesheet estimate."
    )


def test_apply_falls_back_to_estimate_before_first_measure():
    """A never-measured tall row (no remembered height) must still reserve a
    content-derived estimate >> 96px at build time, so even the very first
    fresh-element frame does not collapse scrollHeight."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _fake_row_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
// sessionIdx 99 was never measured/remembered.
const fresh = makeRow('user', 99, 0);
const longText = 'y'.repeat(1500);
_applyUserRowIntrinsicHeight(fresh, longText);
const val = fresh.style.containIntrinsicSize; // 'auto <N>px'
const px = parseInt(String(val).replace(/[^0-9]/g,''), 10);
console.log(JSON.stringify({ intrinsic: val, px }));
"""
    m = json.loads(_run_node(source))
    assert m["px"] > 600, (
        "a never-measured tall row must reserve an estimate well above 96px at "
        f"build time; got {m['intrinsic']!r}"
    )


def test_measure_persists_user_row_height_and_writes_inline_intrinsic():
    """_measureMessageVirtualRow, after measuring a user row, must (a) write the
    measured height inline as contain-intrinsic-size on the measured element and
    (b) remember it so the NEXT rebuild of that sessionIdx reserves the real
    height. The buggy version never touched intrinsic-size, so a rebuilt row
    collapsed to 96px."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _fake_row_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
eval(extractFunc('_measureMessageVirtualRow'));
// The measured user row lives in the fake inner keyed by data-msg-idx.
const measuredRow = makeRow('user', 7, 3200);
const inner = {
  querySelector(selector){
    if(selector.indexOf('[data-msg-idx="42"]') !== -1) return measuredRow;
    return null;
  }
};
// Measure it (rawIdx 42 maps to sessionIdx 7).
const h = _measureMessageVirtualRow(inner, { rawIdx: 42 });
// (a) the measured element got its real height reserved inline...
const inlineOnMeasured = measuredRow.style.containIntrinsicSize;
// (b) ...and a subsequent rebuild of sessionIdx 7 reserves that same height.
const rebuilt = makeRow('user', 7, 0);
_applyUserRowIntrinsicHeight(rebuilt, 'short');
console.log(JSON.stringify({
  measuredHeight: h,
  inlineOnMeasured: inlineOnMeasured,
  rebuiltIntrinsic: rebuilt.style.containIntrinsicSize,
}));
"""
    m = json.loads(_run_node(source))
    assert m["measuredHeight"] == 3200, f"expected measured height 3200, got {m['measuredHeight']}"
    assert m["inlineOnMeasured"] == "auto 3200px", (
        "measure pass must write the real height inline on the measured user row; "
        f"got {m['inlineOnMeasured']!r} (buggy version wrote nothing)"
    )
    assert m["rebuiltIntrinsic"] == "auto 3200px", (
        "a rebuild after measuring must reserve the remembered 3200px, not 96px; "
        f"got {m['rebuiltIntrinsic']!r}"
    )


def test_measure_ignores_assistant_rows_for_intrinsic_writeback():
    """The intrinsic-size writeback must apply ONLY to user rows (assistant rows
    are content-visibility:visible on mobile per #5638; writing intrinsic-size on
    them would be meaningless and could mask a real regression). Measuring an
    assistant row must NOT write an inline intrinsic-size."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _fake_row_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
eval(extractFunc('_measureMessageVirtualRow'));
const assistantRow = makeRow('assistant', 5, 1200);
const inner = {
  querySelector(selector){
    if(selector.indexOf('[data-msg-idx="10"]') !== -1) return assistantRow;
    return null;
  }
};
_measureMessageVirtualRow(inner, { rawIdx: 10 });
console.log(JSON.stringify({ inline: assistantRow.style.containIntrinsicSize }));
"""
    m = json.loads(_run_node(source))
    assert m["inline"] == "", (
        "assistant rows must NOT get an inline intrinsic-size writeback; "
        f"got {m['inline']!r}"
    )


def test_cache_cleared_on_session_switch_prevents_stale_height_bleed():
    """Greptile #5672 review: the module-level height cache is keyed by
    session-relative index (_messageSessionIndexBase()+rawIdx), and the base is 0
    for the common non-offset session, so keys collide across sessions. Without a
    clear on session switch, a new session's off-screen user row at the same key
    inherits the previous session's remembered height and inflates scrollHeight.

    _clearUserRowIntrinsicHeightCache() (wired into _clearMessageVirtualHeightCache,
    which _resetMessageRenderWindow calls on session switch) must empty the cache so
    a rebuilt row at a colliding key falls back to the content estimate, NOT the
    stale remembered height.

    Mutation: make _clearUserRowIntrinsicHeightCache a no-op and this fails (the
    rebuilt row reserves the stale 5000px instead of the ~short estimate)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _fake_row_prelude() + r"""
eval(extractFunc('_rememberUserRowIntrinsicHeight'));
eval(extractFunc('_estimateUserRowIntrinsicHeight'));
eval(extractFunc('_applyUserRowIntrinsicHeight'));
eval(extractFunc('_clearUserRowIntrinsicHeightCache'));
// Session A: a tall user row at session-relative index 3 measured 5000px.
_rememberUserRowIntrinsicHeight(3, 5000);
const beforeClear = makeRow('user', 3, 0);
_applyUserRowIntrinsicHeight(beforeClear, 'x');   // would reserve the remembered 5000
// Session switch clears the cache.
_clearUserRowIntrinsicHeightCache();
// Session B: a SHORT user row at the SAME colliding key 3, never measured here.
const afterClear = makeRow('user', 3, 0);
_applyUserRowIntrinsicHeight(afterClear, 'hi');   // must fall back to the estimate
const estimate = _estimateUserRowIntrinsicHeight('hi');
console.log(JSON.stringify({
  beforeClear: beforeClear.style.containIntrinsicSize,
  afterClear: afterClear.style.containIntrinsicSize,
  estimate: 'auto ' + estimate + 'px',
}));
"""
    m = json.loads(_run_node(source))
    assert m["beforeClear"] == "auto 5000px", (
        "sanity: before the clear, the remembered 5000px must be reserved; "
        f"got {m['beforeClear']!r}"
    )
    assert m["afterClear"] == m["estimate"], (
        "after a session switch clear, a rebuilt row at the colliding key must fall "
        f"back to the content estimate ({m['estimate']}), NOT the stale remembered "
        f"5000px; got {m['afterClear']!r} (buggy: cache not cleared → stale bleed)"
    )
    assert m["afterClear"] != "auto 5000px", "stale height must not survive the clear"

