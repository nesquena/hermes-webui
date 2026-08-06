"""Regression tests for the virtualization scroll-compensation jump-back.

Root cause: `_compensateScrollForMeasurementDelta` re-found its viewport anchor
ONLY by rawIdx and bailed with a bare `if(!row) return` when that row was
recycled out of the render window. On big sessions containing multi-thousand-px
turns, a large scroll delta recycles the anchor row, so the estimated->measured
topPad swap (a scrollHeight change of tens of thousands of px) hit scrollTop
uncompensated and threw the viewport to the top -- the recurring mobile scroll
jump-back.

Fix: when the rawIdx row is gone, fall back to (a) the stable sessionIdx anchor,
and (b) if that is also unrendered, compensate by the top-spacer (topPad) height
delta captured before the re-render.

Every behavioral test below is designed to FAIL on the known-buggy version
(bare `if(!row) return`) and PASS only on the fixed version.
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
    # extractFunc brace-matches the function body but SKIPS braces that live
    # inside string / template / regex literals and comments, so a future edit
    # adding e.g. `warn('expected {k}')` inside an extracted function cannot
    # desync the depth counter (greptile P2 on this PR). Built with a plain
    # string (not an f-string) so the JS braces need no doubling.
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;      // current string/template delimiter, or null
  let inLine = false;  // inside // line comment
  let inBlock = false; // inside /* block comment */
  let inRegex = false; // inside / regex literal /
  let prev = '';       // last significant (non-space) code char, for regex detection
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
    // A '/' starts a regex only where a value is expected, i.e. after an
    // operator/paren/comma — not after an identifier/number/closing paren.
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


def test_compensate_recovers_via_session_idx_when_rawidx_row_recycled():
    """When the rawIdx anchor row is recycled out of the render window but the
    SAME row is still locatable by its stable data-session-msg-idx, the
    compensation must recover via the sessionIdx lookup and shift scrollTop by
    the measured delta -- NOT bail out (the buggy `if(!row) return`)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 20000;
let scrollHistory = [];
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollHistory.push(v); scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 0, bottom: 600}; },
  classList: { add(){}, remove(){} },
  querySelector(selector){
    // rawIdx row is RECYCLED OUT (gone); the sessionIdx row IS still rendered.
    if(selector.indexOf('[data-msg-idx="42"]') !== -1) return null;
    if(selector.indexOf('[data-session-msg-idx="42"]') !== -1){
      return { getBoundingClientRect(){ return {top: 150}; } };
    }
    if(selector.indexOf('virtual-spacer') !== -1) return {style:{height:'1000px'}};
    return null;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  return {rawIdx: 42, sessionIdx: 42, topOffset: 100, topPadBefore: 1000};
}
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _programmaticScrollResetTimer = 0;
let _lastScrollTop = 0;
const performance = { now(){ return 1000; } };
function clearTimeout(){}
function setTimeout(cb){ cb(); return 1; }
function _deferClearProgrammaticScroll(){}
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{});
console.log(JSON.stringify({scrollHistory}));
"""
    metrics = json.loads(_run_node(source))
    # sessionIdx row found at top=150; anchor.topOffset=100 -> delta = 150-100 = 50
    # scrollTop shifts 20000 + 50 = 20050 (recovered, NOT abandoned).
    assert metrics["scrollHistory"] == [20050], (
        "compensation must recover via data-session-msg-idx when the rawIdx row "
        "is recycled out (buggy code bailed with `if(!row) return`, leaving "
        "scrollHistory empty)"
    )


def test_compensate_uses_toppad_delta_when_anchor_row_fully_recycled():
    """When BOTH the rawIdx and sessionIdx rows are recycled out (large scroll
    delta on a big virtualized session), the compensation must fall back to the
    top-spacer (topPad) height delta so the huge estimated->measured scrollHeight
    lurch does not throw the viewport to the top. Buggy code abandoned entirely
    and left scrollTop uncompensated."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 20500;
let scrollHistory = [];
// topPad shrank from 20000 (estimated) to 1000 (measured): a -19000px lurch.
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollHistory.push(v); scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 0, bottom: 600}; },
  classList: { add(){}, remove(){} },
  querySelector(selector){
    // both anchor lookups miss (row recycled out entirely)
    if(selector.indexOf('data-msg-idx') !== -1) return null;
    if(selector.indexOf('data-session-msg-idx') !== -1) return null;
    // the top spacer now measures 1000px (was 20000 at capture time)
    if(selector.indexOf('virtual-spacer') !== -1) return {style:{height:'1000px'}};
    return null;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  return {rawIdx: 42, sessionIdx: 42, topOffset: 100, topPadBefore: 20000};
}
let _programmaticScroll = false;
let _programmaticScrollSetAt = 0;
let _lastScrollTop = 0;
const performance = { now(){ return 1000; } };
function clearTimeout(){}
function setTimeout(cb){ cb(); return 1; }
function _deferClearProgrammaticScroll(){}
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{});
console.log(JSON.stringify({scrollHistory}));
"""
    metrics = json.loads(_run_node(source))
    # padDelta = topPadAfter(1000) - topPadBefore(20000) = -19000
    # scrollTop = max(0, 20500 + (-19000)) = 1500 -- compensated for the lurch.
    assert metrics["scrollHistory"] == [1500], (
        "compensation must shift scrollTop by the topPad delta (-19000) when the "
        "anchor row is fully recycled; buggy code left scrollTop uncompensated "
        "(empty scrollHistory) and the viewport was thrown to the top"
    )


def test_compensate_still_bails_cleanly_when_no_toppad_before_captured():
    """Defensive: if the captured anchor has no topPadBefore (older shape) AND
    the row is gone, the fallback must NOT throw and must NOT mutate scrollTop."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let scrollTopValue = 500;
let scrollTopMutated = false;
const container = {
  get scrollTop(){ return scrollTopValue; },
  set scrollTop(v){ scrollTopMutated = true; scrollTopValue = v; },
  getBoundingClientRect(){ return {top: 0, bottom: 600}; },
  classList: { add(){}, remove(){} },
  querySelector(selector){
    if(selector.indexOf('virtual-spacer') !== -1) return {style:{height:'1000px'}};
    return null;  // no anchor row by any lookup
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _captureMessageViewportAnchor(){
  // no topPadBefore field (older capture shape) -> fallback must no-op safely
  return {rawIdx: 42, sessionIdx: 42, topOffset: 100};
}
let _programmaticScroll = false;
let _lastScrollTop = 0;
const performance = { now(){ return 1000; } };
function clearTimeout(){}
function setTimeout(cb){ cb(); return 1; }
function _deferClearProgrammaticScroll(){}
function requestAnimationFrame(cb){ cb(); }
eval(extractFunc('_compensateScrollForMeasurementDelta'));
_compensateScrollForMeasurementDelta(()=>{});
console.log(JSON.stringify({scrollTopMutated}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["scrollTopMutated"] is False, (
        "with no topPadBefore captured and no anchor row, the fallback must "
        "leave scrollTop untouched (NaN-guarded), not throw or write garbage"
    )


def test_extract_func_skips_braces_inside_string_and_regex_literals():
    """The Node-harness extractFunc must brace-match on real code structure only,
    skipping braces inside string / template / regex literals and comments. A
    naive depth counter desyncs on a bare '{' or '}' inside a string literal and
    truncates the extract (greptile P2). This locks the robust behavior."""
    tricky = (
        "function _tricky(){\n"
        "  const a = 'has a bare } brace';\n"
        "  const b = \"and an open { one\";\n"
        "  const c = `template ${'x'} literal`;\n"
        "  const re = /\\}[{]/;  // regex with unbalanced-looking braces\n"
        "  // a line comment with } and {\n"
        "  /* block comment with { and } */\n"
        "  return a.length + b.length + c.length + (re ? 1 : 0);\n"
        "}\n"
    )
    source = _extract_func_script(tricky) + """
const extracted = extractFunc('_tricky');
// The extract must end at the REAL closing brace (full function), not a premature
// one desynced by a string/regex brace. Eval it and call it to prove it is whole.
eval(extracted);
console.log(JSON.stringify({
  endsAtRealBrace: extracted.trimEnd().endsWith('}'),
  hasReturn: extracted.indexOf('return a.length') !== -1,
  callable: typeof _tricky === 'function',
  result: _tricky(),
}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["hasReturn"] is True, "extract truncated before the return statement"
    assert metrics["endsAtRealBrace"] is True
    assert metrics["callable"] is True
    # 'has a bare } brace'(18) + 'and an open { one'(17) + 'template x literal'(18) + 1
    assert metrics["result"] == 54
