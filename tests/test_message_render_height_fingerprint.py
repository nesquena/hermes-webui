"""#8228 re-review coverage (2026-10-15): identity-only height reuse, no tool
digest sealing, and the hidden-row-safe binary-search viewport anchor.

The maintainer's CORE-2 finding: the fingerprint/digest height-reuse machinery
shipped in the previous round never fired on tool sessions — the digest was
sealed inside _syncMessageVirtualHeightCache, which renderMessages calls BEFORE
it derives the settled S.toolCalls list, so the first seal hashed settled:null
and every later seal hashed the derived item (0 of 189 heights kept on a
virtualized tool transcript) — and it cost ~63 ms per append on large tool
sessions. The re-review explicitly accepted restoring identity-only matching
and shipping the anchor fix alone. These tests pin the accepted route:

  * same message OBJECT across syncs reuses its measured height (the
    in-session append shape),
  * fresh objects never reuse — including byte-equivalent ones (the
    loadSession(force:true) shape re-measures; conservative direction),
  * every render-affecting change the review listed still refuses reuse
    (they all replace the message object, so identity never matches),
  * a sync pass seals NO tool digest (entries carry identity only) — the
    per-append hashing cost is gone,
  * the matcher is pure identity (no helper dependency).

CORE-1 (the binary-search anchor picking the wrong row when hidden
display:none rows break the monotonic-bottom assumption) is proven in the
mocked rect-driven tests below AND in the real-browser gate
tests/browser_virtual_transcript_anchor.py (which now imports a tool-call
transcript).

Sensitivity note: on a tree WITH the fingerprint machinery (the previous
round's head) the byte-equivalent fresh-object case reuses and the no-seal
assertion fails — those two tests are the red bite that pins the revert.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
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
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
"""


# The helpers the PREVIOUS round's fingerprint machinery needed. Evaluated inside
# try/catch: on the previous head they exist and the matcher uses them (making the
# red assertions bite); on this head they are gone and the identity-only matcher
# does not need them. Never required for correctness.
_HELPER_EVAL = """\
try {
  eval(extractFunc('_messageRenderHeightFingerprint'));
  eval(extractFunc('_addMessageRenderHash'));
  eval(extractFunc('_addBoundedHash'));
  eval(extractFunc('_hashObjectInto'));
  eval(extractFunc('_messageToolRenderDigest'));
  eval(extractFunc('_messageToolRenderIndex'));
} catch (e) {}
"""

_TWO_PASS_PROLOGUE = """\
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT = 140;
let _messageVirtualHeightCache = [];
let _messageVirtualHeightCacheEntries = [];
let _messageVirtualHeightCacheLen = 0;
let _messageVirtualHeightCacheSrc = null;
let _messageVirtualEstimatedRowHeight = 140;
let _messageVirtualWindowKey = '';
function _clearMessageVirtualHeightCache() {
  _messageVirtualHeightCache = [];
  _messageVirtualHeightCacheEntries = [];
  _messageVirtualHeightCacheLen = 0;
  _messageVirtualHeightCacheSrc = null;
  _messageVirtualEstimatedRowHeight = MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHT;
  _messageVirtualWindowKey = '';
}
const window = {};
"""

_EVAL_MATCHER_BLOCK = """\
eval(extractFunc('_messageVirtualHeightEntryMatches'));
eval(extractFunc('_messageVirtualHeightPrefixEntryMatches'));
eval(extractFunc('_syncMessageVirtualHeightCache'));
"""

_TWO_PASS_BODY = """\
const round1Messages = __ROUND1__;
const round2Messages = __ROUND2__;
let S = {messages: round1Messages};
_syncMessageVirtualHeightCache(round1Messages.map((m, i) => ({rawIdx: i, m})));
// Record a measured height for the first pass's entries.
_messageVirtualHeightCache = round1Messages.map(() => 300);
// Refresh: S.messages is replaced wholesale (the loadSession(force:true)
// shape) and the real second sync runs the reuse decision against the state
// recorded by the first pass.
S = {messages: round2Messages};
_syncMessageVirtualHeightCache(round2Messages.map((m, i) => ({rawIdx: i, m})));
console.log(JSON.stringify({cache: _messageVirtualHeightCache}));
"""


def _run_two_pass(round1_js: str, round2_js: str) -> dict:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = (
        _extract_func_script(js)
        + _TWO_PASS_PROLOGUE
        + _HELPER_EVAL
        + _EVAL_MATCHER_BLOCK
        + _TWO_PASS_BODY.replace("__ROUND1__", round1_js).replace("__ROUND2__", round2_js)
    )
    return json.loads(_run_node(source))


def _assert_reused(metrics: dict) -> None:
    assert metrics["cache"] == [300], "same message object must reuse the measured height"


def _assert_not_reused(metrics: dict) -> None:
    assert metrics["cache"][0] is None, (
        "a fresh object (any content) must NOT reuse a height measured for a different object"
    )


# ── identity-only reuse: the accepted CORE-2 route ──────────────────────────

def test_reuse_for_same_object_across_syncs():
    """In-session append shape: S.messages keeps the SAME message objects, so
    the measured heights carry over without any hashing."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    body = _TWO_PASS_PROLOGUE + _HELPER_EVAL + _EVAL_MATCHER_BLOCK + """
const m = {id: 'm1', role: 'assistant', content: 'hello world'};
let S = {messages: [m]};
_syncMessageVirtualHeightCache([{rawIdx: 0, m}]);
_messageVirtualHeightCache = [300];
// Same object identity across the refresh (the in-session append shape) —
// NOT a loadSession(force) object replacement.
S = {messages: [m]};
_syncMessageVirtualHeightCache([{rawIdx: 0, m}]);
console.log(JSON.stringify({cache: _messageVirtualHeightCache}));
"""
    metrics = json.loads(_run_node(_extract_func_script(js) + body))
    _assert_reused(metrics)


def test_no_reuse_for_fresh_objects_identical_content():
    """RED bite: the previous round reused byte-equivalent FRESH objects via the
    render fingerprint; the accepted route re-measures them (identity never
    matches across loadSession(force:true))."""
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: 'hello world'}]",
        "[{id: 'm1', role: 'assistant', content: 'hello world'}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_for_fresh_objects_with_tool_chain():
    """Same as above with a tool chain: fresh objects with identical tool data
    must still re-measure (the previous round reused via the tool digest)."""
    body = (
        "[{id: 'a1', role: 'assistant', content: 'run it',"
        " tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]},"
        " {id: 't1', role: 'tool', tool_call_id: 'tc1', content: 'identical result'}]"
    )
    metrics = _run_two_pass(body, body)
    _assert_not_reused(metrics)


# ── render-affecting changes the review listed: all still refuse reuse ──────
# (each case replaces the message object, so identity never matches — the same
# conservative outcome the fingerprint machinery produced, at zero cost.)

def test_no_reuse_on_reasoning_content_only_change():
    """reasoning_content is the field the row renderer reads FIRST; the review's
    74.6px -> 300px case. A fresh object with changed reasoning must re-measure."""
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: 'answer', reasoning_content: 'brief thought'}]",
        "[{id: 'm1', role: 'assistant', content: 'answer',"
        " reasoning_content: 'brief thought, now dramatically longer so the row grows tall'}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_equal_length_middle_content_change():
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: 'A'.repeat(200) + 'MIDDLE' + 'A'.repeat(200)}]",
        "[{id: 'm1', role: 'assistant', content: 'A'.repeat(200) + 'MUTATE' + 'A'.repeat(200)}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_status_card_object_change():
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: '',"
        " _statusCard: {state: 'running', step: 1, label: 'fetching'}}]",
        "[{id: 'm1', role: 'assistant', content: '',"
        " _statusCard: {state: 'done', step: 2, label: 'fetching'}}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_activity_scene_change():
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: '',"
        " _anchor_activity_scene: {version: 'activity_scene_v1', steps: [{id: 's1', text: 'one'}]}}]",
        "[{id: 'm1', role: 'assistant', content: '',"
        " _anchor_activity_scene: {version: 'activity_scene_v1',"
        " steps: [{id: 's1', text: 'one'}, {id: 's2', text: 'two'}]}}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_partial_tool_args_change():
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: '',"
        " _partial_tool_calls: [{id: 'tc1', name: 'shell', args: {cmd: 'ls', flags: '-la'}}]}]",
        "[{id: 'm1', role: 'assistant', content: '',"
        " _partial_tool_calls: [{id: 'tc1', name: 'shell', args: {cmd: 'ls', flags: '-lh'}}]}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_tool_argument_change_constant_count():
    metrics = _run_two_pass(
        "[{id: 'm1', role: 'assistant', content: 'same text',"
        " tool_calls: [{id: 'tc1', name: 'shell', type: 'function',"
        " function: {name: 'shell', arguments: '{\"cmd\":\"ls\"}'}}]}]",
        "[{id: 'm1', role: 'assistant', content: 'same text',"
        " tool_calls: [{id: 'tc1', name: 'shell', type: 'function',"
        " function: {name: 'shell', arguments: '{\"cmd\":\"cd\"}'}}]}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_tool_result_change():
    """Only a role:'tool' result changed; the result renders inside the
    assistant row but lives on another message. Fresh objects -> re-measure."""
    metrics = _run_two_pass(
        "[{id: 'a1', role: 'assistant', content: 'run it',"
        " tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]},"
        " {id: 't1', role: 'tool', tool_call_id: 'tc1', content: 'result-A'}]",
        "[{id: 'a1', role: 'assistant', content: 'run it',"
        " tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]},"
        " {id: 't1', role: 'tool', tool_call_id: 'tc1', content: 'result-B'}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_middle_only_tool_result_change():
    metrics = _run_two_pass(
        "[{id: 'a1', role: 'assistant', content: 'run it',"
        " tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]},"
        " {id: 't1', role: 'tool', tool_call_id: 'tc1',"
        " content: 'X'.repeat(3000) + 'MID' + 'X'.repeat(3000)}]",
        "[{id: 'a1', role: 'assistant', content: 'run it',"
        " tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]},"
        " {id: 't1', role: 'tool', tool_call_id: 'tc1',"
        " content: 'X'.repeat(3000) + 'MUT' + 'X'.repeat(3000)}]",
    )
    _assert_not_reused(metrics)


def test_no_reuse_on_settled_tool_call_metadata_change():
    """The settled S.toolCalls item renders into the row; fresh assistant
    objects with a changed settled snippet must re-measure."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    body = _TWO_PASS_PROLOGUE + _HELPER_EVAL + _EVAL_MATCHER_BLOCK + """
const assistantRound1 = {id: 'a1', role: 'assistant', content: 'run it',
  tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]};
const assistantRound2 = {id: 'a1', role: 'assistant', content: 'run it',
  tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]};
const toolMsg = {id: 't1', role: 'tool', tool_call_id: 'tc1', content: 'stable result'};
let S = {messages: [assistantRound1, toolMsg], toolCalls: [{tid: 'tc1', name: 'shell', done: true, snippet: 'out-A'}]};
_syncMessageVirtualHeightCache([{rawIdx: 0, m: assistantRound1}]);
_messageVirtualHeightCache = [300];
S = {messages: [assistantRound2, toolMsg], toolCalls: [{tid: 'tc1', name: 'shell', done: true, snippet: 'out-B'}]};
_syncMessageVirtualHeightCache([{rawIdx: 0, m: assistantRound2}]);
console.log(JSON.stringify({cache: _messageVirtualHeightCache}));
"""
    metrics = json.loads(_run_node(_extract_func_script(js) + body))
    _assert_not_reused(metrics)


# ── the sync pass seals NO tool digest (CORE-2 perf) ────────────────────────

def _run_sync_probe() -> dict:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = (
        _extract_func_script(js)
        + _TWO_PASS_PROLOGUE
        + _HELPER_EVAL
        + _EVAL_MATCHER_BLOCK
        + """
const m = {id: 'm1', role: 'assistant', content: 'hello world',
  tool_calls: [{id: 'tc1', type: 'function', function: {name: 'shell', arguments: '{}'}}]};
let S = {messages: [m]};
_syncMessageVirtualHeightCache([{rawIdx: 0, m}]);
const entry = _messageVirtualHeightCacheEntries[0];
console.log(JSON.stringify({
  hasTh: !!(entry && typeof entry._th === 'string'),
  keys: entry ? Object.keys(entry) : [],
}));
"""
    )
    return json.loads(_run_node(source))


def test_sync_pass_seals_no_tool_digest():
    """RED bite: the previous round sealed the tool digest into every cache
    entry (settled:null on the first pass, the derived item later — the two
    never matched) and paid that walk on every length-change sync, ~63 ms per
    append on a 3.6 MB tool session. Entries must carry identity only."""
    out = _run_sync_probe()
    assert out["hasTh"] is False, "cache entries must not carry a sealed tool digest"


# ── the matcher is pure identity, with no helper dependency ─────────────────

def _run_matcher_probe() -> dict:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = (
        _extract_func_script(js)
        + "const window = {};\n"
        + "eval(extractFunc('_messageVirtualHeightEntryMatches'));\n"
        + """
const a = {id: 'm1', role: 'assistant', content: 'hello world'};
const b = {id: 'm1', role: 'assistant', content: 'hello world'};
const same = _messageVirtualHeightEntryMatches({rawIdx: 0, m: a}, {rawIdx: 0, m: a});
const fresh = _messageVirtualHeightEntryMatches({rawIdx: 0, m: a}, {rawIdx: 0, m: b});
const empty = _messageVirtualHeightEntryMatches(null, {rawIdx: 0, m: b});
console.log(JSON.stringify({same, fresh, empty}));
"""
    )
    return json.loads(_run_node(source))


def test_matcher_is_identity_only():
    """Extracted alone (upstream extractFunc pattern) with NO helper functions
    evaluated, the matcher must still work: identity reuse, fresh objects never
    match, missing entries fail. This is the fail-closed-by-construction shape
    of the accepted route."""
    out = _run_matcher_probe()
    assert out["same"] is True, "same message object must match"
    assert out["fresh"] is False, "fresh objects must never match"
    assert out["empty"] is False, "null entry must not match"


# ── the binary-search anchor (CORE-1): hidden rows must not break the search ─

_ANCHOR_NODE_BODY = r"""
const window = {};
function makeRows(scenario){
  return scenario.rects.map((rect, i) => {
    const cls = rect.cls || [];
    return {
      dataset: {msgIdx: String(i * 3), sessionMsgIdx: String(500 + i * 3), messageAnchorKey: 'k' + i},
      classList: { contains(name){ return cls.indexOf(name) >= 0; } },
      getBoundingClientRect(){ return rect; },
    };
  });
}
function isHidden(row){
  return row.classList.contains('assistant-segment-worklog-source')
      || row.classList.contains('assistant-segment-anchor');
}
const container = {
  _rows: [],
  _top: 0,
  scrollTop: 0,
  scrollHeight: 5000,
  getBoundingClientRect(){ return {top: container._top, bottom: container._top + 720}; },
  querySelector(sel){ return null; },
  // The production selector is '[data-msg-idx]:not(.assistant-segment-worklog-source)
  // :not(.assistant-segment-anchor)'; a bare '[data-msg-idx]' query (the
  // pre-fix shape) returns EVERYTHING including the hidden rows.
  querySelectorAll(sel){
    if (sel.indexOf(':not(') >= 0) return container._rows.filter(r => !isHidden(r));
    return container._rows;
  },
};
function $(id){ return id === 'messages' ? container : null; }
function _messageSessionIndexForRawIdx(rawIdx){ return rawIdx + 1000; }
eval(extractFunc('_captureMessageViewportAnchor'));

// Reference predicate: the first row (in DOM order, laid-out rows only) whose
// bottom edge crosses the container's top edge — exactly what the per-row
// fallback scan implements. Returns the row's OWN data-msg-idx.
function referenceHitRaw(rows){
  for (let i = 0; i < rows.length; i++){
    if (rows[i].getBoundingClientRect().bottom > container._top + 1){
      return Number(rows[i].dataset.msgIdx);
    }
  }
  return null;
}
function laidOut(rows){ return rows.filter(r => !isHidden(r)); }
function runScenario(scenario){
  container._top = scenario.containerTop || 0;
  container._rows = makeRows(scenario);
  const anchor = _captureMessageViewportAnchor();
  const refRaw = referenceHitRaw(laidOut(container._rows));
  return {
    fastIdx: anchor ? anchor.rawIdx : null,
    refIdx: refRaw,
    sessionIdx: anchor ? anchor.sessionIdx : null,
  };
}
const scenarios = [
  // All laid out (baseline coverage): row straddling the edge, fully above, edge cases.
  {containerTop: 0, rects: [{top: -120, bottom: -40}, {top: -40, bottom: 40}, {top: 40, bottom: 160}, {top: 160, bottom: 340}]},
  {containerTop: 0, rects: [{top: -400, bottom: -200}, {top: -200, bottom: -100}, {top: -100, bottom: 20}, {top: 20, bottom: 300}]},
  {containerTop: 0, rects: [{top: -800, bottom: -700}, {top: -700, bottom: -650}, {top: -650, bottom: -640}, {top: -640, bottom: 100}]},
  {containerTop: 0, rects: [{top: 5, bottom: 50}, {top: 50, bottom: 90}]},
  {containerTop: 0, rects: [{top: -50, bottom: 0.5}, {top: 0.5, bottom: 10}]},
  // CORE-1: a hidden worklog-source row (all-zero rect) between laid-out rows
  // breaks the monotonic-bottom assumption — the search must skip it. On the
  // pre-fix head the binary search over [50, 0, 100, 150] lands on the third
  // row (rawIdx 6) instead of the first (rawIdx 0).
  {containerTop: 0, rects: [
    {top: -100, bottom: 50},
    {top: 0, bottom: 0, cls: ['assistant-segment-worklog-source']},
    {top: 50, bottom: 100},
    {top: 100, bottom: 150},
  ]},
  // CORE-1: hidden assistant-segment-anchor row mid-list (display:none too).
  {containerTop: 0, rects: [
    {top: -100, bottom: 50},
    {top: 0, bottom: 0, cls: ['assistant-segment-anchor']},
    {top: 50, bottom: 100},
    {top: 100, bottom: 150},
  ]},
  // CORE-1: hidden row at the head with the container scrolled deep (negative
  // top): a zero rect satisfies bottom > top+1, so an unfiltered search anchors
  // ON the hidden row; the fix must anchor on the first laid-out row.
  {containerTop: -100, rects: [
    {top: 0, bottom: 0, cls: ['assistant-segment-worklog-source']},
    {top: -80, bottom: 20},
    {top: 20, bottom: 120},
  ]},
  // Hidden row at the head, container at 0: zero rect stays "before" the
  // predicate, search must land on the first laid-out row either way.
  {containerTop: 0, rects: [
    {top: 0, bottom: 0, cls: ['assistant-segment-worklog-source']},
    {top: -50, bottom: 40},
    {top: 40, bottom: 120},
  ]},
];
const fastResults = scenarios.map(runScenario);
// Fallback comparison at scenario index 1 — the per-row scan must find the same
// row and the fallback counter must bite.
window.__vmFastAnchor = false;
window.__vmAnchorFallbackRowReads = 0;
container._top = 0;
container._rows = makeRows(scenarios[1]);
const slowAnchor = _captureMessageViewportAnchor();
const slowReads = window.__vmAnchorFallbackRowReads;
const slowRefRaw = referenceHitRaw(laidOut(container._rows));
// Fallback with hidden rows (scenario 6): the scan must skip the hidden row.
window.__vmAnchorFallbackRowReads = 0;
container._top = 0;
container._rows = makeRows(scenarios[6]);
const slowHiddenAnchor = _captureMessageViewportAnchor();
const slowHiddenRefRaw = referenceHitRaw(laidOut(container._rows));
// Empty row set: falls through and returns null without throwing.
window.__vmFastAnchor = true;
container._rows = [];
const emptyAnchor = _captureMessageViewportAnchor();
console.log(JSON.stringify({
  fastResults,
  slowIdx: slowAnchor ? slowAnchor.rawIdx : null,
  slowRefIdx: slowRefRaw,
  slowReads,
  slowHiddenIdx: slowHiddenAnchor ? slowHiddenAnchor.rawIdx : null,
  slowHiddenRefIdx: slowHiddenRefRaw,
  emptyOk: emptyAnchor === null,
}));
"""


def _run_anchor_node() -> dict:
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + _ANCHOR_NODE_BODY
    return json.loads(_run_node(source))


def test_binary_search_anchor_matches_reference_predicate():
    """The fast anchor must return the same row as the first-covering predicate
    the per-row scan implements, INCLUDING transcripts with hidden
    display:none rows interleaved (the CORE-1 regression)."""
    out = _run_anchor_node()
    for case in out["fastResults"]:
        assert case["fastIdx"] == case["refIdx"], (
            f"fast anchor picked row {case['fastIdx']} but the reference predicate "
            f"picks {case['refIdx']}: {case}"
        )
        assert case["sessionIdx"] is not None, "fast anchor must carry a session index"


def test_binary_search_anchor_falls_back_safely():
    """Fast path off -> the per-row scan returns the same anchor, skips hidden
    rows, and the __vmAnchorFallbackRowReads instrumentation bites. An empty
    row set must fall through to null without throwing."""
    out = _run_anchor_node()
    assert out["slowIdx"] == out["slowRefIdx"], (
        "fallback scan must find the same first-covering row as the reference predicate"
    )
    assert out["slowReads"] >= 1, "fallback loop must be instrumented"
    assert out["slowHiddenIdx"] == out["slowHiddenRefIdx"], (
        "fallback scan must skip hidden display:none rows"
    )
    assert out["emptyOk"] is True, "empty row set must return null, not throw"
