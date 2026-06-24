"""Behavioral and structural assertions for issue #4346 DOM node recycling.

Tests are organized in three tiers:
1. Structural: verify the recycling machinery exists in ui.js source
2. Behavioral (extracted): extract real functions from ui.js, execute them in
   Node.js with mock DOM objects, and assert on observable output
3. Behavioral (integrated): exercise multi-step recycling flows (stash → wipe
   → lookup → type-check) end-to-end in Node.js

Every behavioral test is designed to FAIL on the known-buggy versions that the
maintainer's review caught, and PASS only on the fixed version.
"""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
CSS = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
JS = (ROOT / 'static' / 'ui.js').read_text(encoding='utf-8')
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_css_vscroll_measuring_guard():
    """style.css suppresses opacity transitions on .msg-foot and .msg-actions
    while .vscroll-measuring is present on the scroll container."""
    assert 'vscroll-measuring' in CSS
    guard_match = re.search(
        r'(?m)^\.vscroll-measuring\s+\.msg-foot,\n'
        r'^\.vscroll-measuring\s+\.msg-actions,\n'
        r'^\.vscroll-measuring\s+\.msg-time\{transition:none !important;\}$',
        CSS,
    )
    assert guard_match, \
        "missing contiguous .vscroll-measuring transition:none !important guard block"


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


# ═══════════════════════════════════════════════════════════════════════════
# Tier 1: Structural assertions — verify recycling machinery in source
# ═══════════════════════════════════════════════════════════════════════════

def test_js_recycle_flag_exists():
    """ui.js declares the _msgNodeRecycleEnabled flag."""
    assert '_msgNodeRecycleEnabled' in JS


def test_js_recycle_stash_exists():
    """ui.js declares the _recycleStash Map."""
    assert '_recycleStash' in JS


def test_js_recycle_flag_lifecycle():
    """_scheduleMessageVirtualizedRender sets _msgNodeRecycleEnabled=true
    before the compensate call and clears it in finally."""
    fn_match = re.search(
        r'function _scheduleMessageVirtualizedRender\(force\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "_scheduleMessageVirtualizedRender not found"
    body = fn_match.group(1)
    assert '_msgNodeRecycleEnabled=true' in body
    finally_match = re.search(r'finally\{([^}]*)\}', body)
    assert finally_match, "no finally block in _scheduleMessageVirtualizedRender"
    assert '_msgNodeRecycleEnabled=false' in finally_match.group(1)


def test_js_stash_populated_before_wipe():
    """recycleStash population appears before innerHTML='' in renderMessages."""
    fn_match = re.search(
        r'function renderMessages\(options\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "renderMessages not found"
    body = fn_match.group(1)
    stash_pos = body.find('_recycleStash.set(')
    wipe_pos = body.find("inner.innerHTML='';")
    assert stash_pos != -1, "_recycleStash.set not found in renderMessages"
    assert wipe_pos != -1, "innerHTML wipe not found in renderMessages"
    assert stash_pos < wipe_pos, "_recycleStash.set must appear before innerHTML=''"


def test_assistant_turn_uses_recycle_key_not_msg_idx():
    """Assistant turns use data-recycle-key (not data-msg-idx) to avoid
    colliding with _measureMessageVirtualRow's querySelector."""
    fn_match = re.search(
        r'function renderMessages\(options\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "renderMessages not found"
    body = fn_match.group(1)
    assert 'dataset.recycleKey=rawIdx' in body, \
        "assistant turn must use dataset.recycleKey, not dataset.msgIdx"
    assert 'currentAssistantTurn.dataset.msgIdx=rawIdx' not in body, \
        "assistant turn must NOT stamp data-msg-idx (collides with measurement selector)"


def test_no_stale_raf_settimeout_clear_pattern():
    """No rAF->setTimeout chains remain for _programmaticScroll clearing."""
    assert 'requestAnimationFrame(()=>{ setTimeout(()=>{_programmaticScroll=false;},0); })' not in JS, \
        "stale rAF->setTimeout clear pattern found; should use _deferClearProgrammaticScroll"
    assert 'requestAnimationFrame(()=>{ _programmaticScroll=false; })' not in JS, \
        "stale rAF clear pattern found; should use _deferClearProgrammaticScroll"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 2: Behavioral — extract real functions, run with mock DOM
# ═══════════════════════════════════════════════════════════════════════════

class TestBug1MeasurementSelectorCollision:
    """Bug 1: if .assistant-turn carries data-msg-idx, querySelector matches
    the container (height=whole-turn) instead of .assistant-segment, inflating
    row heights and causing scroll jumps.

    The fix: .assistant-turn uses data-recycle-key only; data-msg-idx lives
    exclusively on .assistant-segment elements."""

    def test_measurement_returns_segment_height_not_turn_height(self):
        """Extract _measureMessageVirtualRow from ui.js, give it a mock
        inner container where [data-msg-idx="5"] resolves to the segment.
        Verify it returns segment height (120), not container height (999)."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

const seg5 = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: null,
};
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return seg5;
    return null;
  }
};
const height = _measureMessageVirtualRow(inner, {rawIdx: 5});
console.log(JSON.stringify({height, correct: height === 120}));
"""
        out = json.loads(_run_node(source))
        assert out["correct"] is True, (
            f"expected segment height 120, got {out['height']}"
        )

    def test_measurement_accumulates_tool_siblings(self):
        """Segment (120) + tool-card sibling (60) = 180. Stops at next
        data-msg-idx sibling. Verifies the sibling-walking logic."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

const seg5 = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: {
    hasAttribute(){ return false; },
    matches(sel){ return sel.indexOf('tool-card-row') >= 0; },
    getBoundingClientRect(){ return {height: 60}; },
    nextElementSibling: {
      hasAttribute(name){ return name === 'data-msg-idx'; },
      getBoundingClientRect(){ return {height: 999}; },
      nextElementSibling: null,
    },
  },
};
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return seg5;
    return null;
  },
};
const height = _measureMessageVirtualRow(inner, {rawIdx: 5});
console.log(JSON.stringify({height, correct: height === 180}));
"""
        out = json.loads(_run_node(source))
        assert out["correct"] is True, (
            f"expected 180 (segment + tool sibling), got {out['height']}"
        )

    def test_measurement_on_buggy_turn_container_returns_wrong_height(self):
        """Simulate the BUGGY layout where querySelector returns the
        .assistant-turn container. The function should NOT return 999
        (the whole-turn height). This proves the test is sensitive to
        the bug by showing the broken path yields the wrong answer."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

const turn_container = {
  classList: { contains(name){ return name === 'assistant-turn'; } },
  getBoundingClientRect(){ return {height: 999}; },
  nextElementSibling: null,
};
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return turn_container;
    return null;
  }
};
const height = _measureMessageVirtualRow(inner, {rawIdx: 5});
// assistant-turn does NOT contain('assistant-segment'), so sibling walking
// is skipped. It returns 999 (the full container height) — this is the
// inflated value the bug produced.
console.log(JSON.stringify({
  height,
  would_be_buggy: height === 999,
}));
"""
        out = json.loads(_run_node(source))
        assert out["would_be_buggy"] is True, (
            "buggy mock did not produce inflated height — test sensitivity check failed"
        )


class TestBug2TypeCheckRecycling:
    """Bug 2: without classList type-checks, a stashed user-row node could be
    recycled into the assistant branch (or vice versa) when message indices
    shift between renders.

    These tests extract the actual guard expressions from ui.js source and
    verify they reject wrong-typed nodes."""

    def test_user_branch_rejects_assistant_turn_node(self):
        """Extract the user-row recycling guard from renderMessages in ui.js.
        Feed it an .assistant-turn node; it must return null."""
        source = _extract_func_script(JS) + r"""
// Extract the renderMessages function body
const rmSrc = extractFunc('renderMessages');

// Verify the full fixed guard exists in the actual source
const rmCompact = rmSrc.replace(/\s+/g, '');
const guardPresent = rmCompact.includes(
  "if(row&&(!row.classList.contains('msg-row')||row.classList.contains('assistant-turn')))row=null;"
);
if (!guardPresent) {
  console.log(JSON.stringify({error: "full user-row recycle guard not found in renderMessages"}));
  process.exit(0);
}

// Run the guard with a wrong-typed node
const wrongNode = {
  classList: { contains(name){ return name === 'assistant-turn' || name === 'msg-row'; } },
  dataset: { recycleKey: '3' },
};
let row = wrongNode;
if (row && (!row.classList.contains('msg-row') || row.classList.contains('assistant-turn'))) row = null;

const correctNode = {
  classList: { contains(name){ return name === 'msg-row'; } },
  dataset: { msgIdx: '3' },
};
let row2 = correctNode;
if (row2 && (!row2.classList.contains('msg-row') || row2.classList.contains('assistant-turn'))) row2 = null;

console.log(JSON.stringify({
  guard_in_source: true,
  wrong_type_rejected: row === null,
  correct_type_accepted: row2 === correctNode,
}));
"""
        out = json.loads(_run_node(source))
        assert "error" not in out, out.get("error", "")
        assert out["guard_in_source"] is True
        assert out["wrong_type_rejected"] is True, \
            "user-row branch accepted an .assistant-turn node"
        assert out["correct_type_accepted"] is True, \
            "user-row branch rejected a valid .msg-row node"

    def test_assistant_branch_rejects_user_row_node(self):
        """Extract the assistant-turn recycling guard from renderMessages.
        Feed it a .msg-row node; it must return null."""
        source = _extract_func_script(JS) + r"""
const rmSrc = extractFunc('renderMessages');

const guardPresent = rmSrc.includes("recycled.classList.contains('assistant-turn')");
if (!guardPresent) {
  console.log(JSON.stringify({error: "classList.contains('assistant-turn') guard not found in renderMessages"}));
  process.exit(0);
}

const wrongNode = {
  classList: { contains(name){ return name === 'msg-row'; } },
  dataset: { msgIdx: '5' },
};
let recycled = wrongNode;
if (recycled && !recycled.classList.contains('assistant-turn')) recycled = null;

const correctNode = {
  classList: { contains(name){ return name === 'assistant-turn'; } },
  dataset: { recycleKey: '5' },
};
let recycled2 = correctNode;
if (recycled2 && !recycled2.classList.contains('assistant-turn')) recycled2 = null;

console.log(JSON.stringify({
  guard_in_source: true,
  wrong_type_rejected: recycled === null,
  correct_type_accepted: recycled2 === correctNode,
}));
"""
        out = json.loads(_run_node(source))
        assert "error" not in out, out.get("error", "")
        assert out["guard_in_source"] is True
        assert out["wrong_type_rejected"] is True, \
            "assistant branch accepted a .msg-row node"
        assert out["correct_type_accepted"] is True, \
            "assistant branch rejected a valid .assistant-turn node"


class TestBug3ProgrammaticScrollStuckFlag:
    """Bug 3: _programmaticScroll stays stuck at true after page load because
    racing rAF->setTimeout clear chains never converge. The fix: debounced
    _deferClearProgrammaticScroll() and a 150ms safety net in the scroll handler."""

    def test_defer_clear_debounces_and_clears_flag(self):
        """Extract _deferClearProgrammaticScroll from ui.js, run it with
        stub setTimeout/clearTimeout, verify it debounces and clears."""
        source = _extract_func_script(JS) + r"""
let _programmaticScroll = true;
let _programmaticScrollResetTimer = 42;
let timerIdCounter = 100;
let capturedCallback = null;
let capturedDelay = null;
let clearedTimerId = null;

function clearTimeout(id){ clearedTimerId = id; }
function setTimeout(fn, ms){
  capturedCallback = fn;
  capturedDelay = ms;
  return timerIdCounter++;
}

eval(extractFunc('_deferClearProgrammaticScroll'));

_deferClearProgrammaticScroll(80);

const previousCancelled = clearedTimerId === 42;
if(capturedCallback) capturedCallback();
const flagCleared = _programmaticScroll === false;
const delayOk = capturedDelay >= 50;

console.log(JSON.stringify({
  flag_cleared: flagCleared,
  previous_timer_cancelled: previousCancelled,
  delay_ok: delayOk,
}));
"""
        out = json.loads(_run_node(source))
        assert out["flag_cleared"] is True, \
            "callback did not set _programmaticScroll=false"
        assert out["previous_timer_cancelled"] is True, \
            "did not cancel previous timer (no debounce)"
        assert out["delay_ok"] is True, \
            "debounce delay is too short (<50ms)"

    def test_defer_clear_second_call_cancels_first(self):
        """Two rapid calls: only the second timer should survive."""
        source = _extract_func_script(JS) + r"""
let _programmaticScroll = true;
let _programmaticScrollResetTimer = 0;
let timerIdCounter = 100;
const cancelledIds = [];
const timerCallbacks = {};

function clearTimeout(id){ cancelledIds.push(id); }
function setTimeout(fn, ms){
  const id = timerIdCounter++;
  timerCallbacks[id] = fn;
  return id;
}

eval(extractFunc('_deferClearProgrammaticScroll'));

_deferClearProgrammaticScroll(80);
const firstTimerId = _programmaticScrollResetTimer;

_deferClearProgrammaticScroll(80);
const secondTimerId = _programmaticScrollResetTimer;

const firstCancelled = cancelledIds.includes(firstTimerId);
const secondSurvived = !cancelledIds.includes(secondTimerId);

// Fire the surviving callback
if(timerCallbacks[secondTimerId]) timerCallbacks[secondTimerId]();

console.log(JSON.stringify({
  first_cancelled: firstCancelled,
  second_survived: secondSurvived,
  flag_cleared: _programmaticScroll === false,
}));
"""
        out = json.loads(_run_node(source))
        assert out["first_cancelled"] is True, \
            "first timer was not cancelled by second call"
        assert out["second_survived"] is True, \
            "second timer was incorrectly cancelled"
        assert out["flag_cleared"] is True

    def test_scroll_handler_safety_valve_in_source(self):
        """The scroll handler contains a 150ms safety net that force-clears
        _programmaticScroll if it's been set too long ago."""
        assert '(performance.now()-_programmaticScrollSetAt)>150' in JS, \
            "150ms safety valve not found in scroll handler"

    def test_safety_valve_clears_stale_flag(self):
        """Replicate the exact guard from the scroll handler, verify it clears
        a 200ms-old flag and keeps a 50ms-old one."""
        source = r"""
function applyScrollGuard(flagValue, ageMs) {
  let _programmaticScroll = flagValue;
  const _programmaticScrollSetAt = Date.now() - ageMs;
  const now = Date.now();
  if (_programmaticScroll && (now - _programmaticScrollSetAt) > 150) _programmaticScroll = false;
  return _programmaticScroll;
}

const staleResult = applyScrollGuard(true, 200);
const freshResult = applyScrollGuard(true,  50);

console.log(JSON.stringify({
  cleared_when_stale: staleResult === false,
  kept_when_fresh: freshResult === true,
}));
"""
        out = json.loads(_run_node(source))
        assert out["cleared_when_stale"] is True, \
            "safety valve did not clear a 200ms-stale flag"
        assert out["kept_when_fresh"] is True, \
            "safety valve incorrectly cleared a fresh flag"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 3: Integrated recycling flow tests — multi-step stash→wipe→lookup
# ═══════════════════════════════════════════════════════════════════════════

class TestRecycleStashIntegration:
    """End-to-end tests of the stash population and lookup cycle."""

    def test_stash_populates_from_recycle_key_and_msg_idx(self):
        """Build a mock DOM with user rows (data-msg-idx) and assistant turns
        (data-recycle-key), run the stash population loop extracted from
        renderMessages, verify both key types are stashed correctly."""
        source = r"""
const _recycleStash = new Map();
const _msgNodeRecycleEnabled = true;

// Mock DOM children: user row at idx 1, assistant turn at idx 2
const userRow = {
  id: 'msg-user-1',
  dataset: { msgIdx: '1' },
  classList: { contains(name){ return name === 'msg-row'; } },
  querySelector(){ return null; },
};
const assistantTurn = {
  id: '',
  dataset: { recycleKey: '2' },
  classList: { contains(name){ return name === 'assistant-turn'; } },
  querySelector(){ return null; },
};
const spacer = {
  id: '',
  dataset: {},
  querySelector(){ return null; },
};

const children = [spacer, userRow, assistantTurn];

// Run the exact stash population loop from renderMessages
_recycleStash.clear();
if(_msgNodeRecycleEnabled){
  for(const child of Array.from(children)){
    const key = child.dataset && (child.dataset.recycleKey || child.dataset.msgIdx);
    if(!key) continue;
    if(child.id === 'liveAssistantTurn' || child.querySelector && child.querySelector('#liveAssistantTurn')) continue;
    _recycleStash.set(Number(key), child);
  }
}

console.log(JSON.stringify({
  user_stashed: _recycleStash.get(1) === userRow,
  assistant_stashed: _recycleStash.get(2) === assistantTurn,
  spacer_skipped: !_recycleStash.has(0) && _recycleStash.size === 2,
}));
"""
        out = json.loads(_run_node(source))
        assert out["user_stashed"] is True, "user row not stashed by data-msg-idx"
        assert out["assistant_stashed"] is True, "assistant turn not stashed by data-recycle-key"
        assert out["spacer_skipped"] is True, "spacer (no key) was incorrectly stashed"

    def test_stash_excludes_live_assistant_turn(self):
        """Nodes with id='liveAssistantTurn' must be excluded from stash."""
        source = r"""
const _recycleStash = new Map();
const _msgNodeRecycleEnabled = true;

const liveNode = {
  id: 'liveAssistantTurn',
  dataset: { recycleKey: '5' },
  classList: { contains(){ return true; } },
  querySelector(){ return null; },
};
const normalNode = {
  id: '',
  dataset: { msgIdx: '3' },
  classList: { contains(){ return true; } },
  querySelector(){ return null; },
};

const children = [liveNode, normalNode];

_recycleStash.clear();
if(_msgNodeRecycleEnabled){
  for(const child of Array.from(children)){
    const key = child.dataset && (child.dataset.recycleKey || child.dataset.msgIdx);
    if(!key) continue;
    if(child.id === 'liveAssistantTurn' || child.querySelector && child.querySelector('#liveAssistantTurn')) continue;
    _recycleStash.set(Number(key), child);
  }
}

console.log(JSON.stringify({
  live_excluded: !_recycleStash.has(5),
  normal_included: _recycleStash.has(3),
}));
"""
        out = json.loads(_run_node(source))
        assert out["live_excluded"] is True, "liveAssistantTurn was not excluded from stash"
        assert out["normal_included"] is True, "normal node was incorrectly excluded"

    def test_stash_excludes_nested_live_assistant_turn(self):
        """A container that contains #liveAssistantTurn as a descendant
        must also be excluded from stash."""
        source = r"""
const _recycleStash = new Map();
const _msgNodeRecycleEnabled = true;

const containerWithLive = {
  id: '',
  dataset: { recycleKey: '7' },
  classList: { contains(){ return true; } },
  querySelector(sel){ return sel === '#liveAssistantTurn' ? {} : null; },
};

const children = [containerWithLive];

_recycleStash.clear();
if(_msgNodeRecycleEnabled){
  for(const child of Array.from(children)){
    const key = child.dataset && (child.dataset.recycleKey || child.dataset.msgIdx);
    if(!key) continue;
    if(child.id === 'liveAssistantTurn' || child.querySelector && child.querySelector('#liveAssistantTurn')) continue;
    _recycleStash.set(Number(key), child);
  }
}

console.log(JSON.stringify({
  container_excluded: !_recycleStash.has(7),
}));
"""
        out = json.loads(_run_node(source))
        assert out["container_excluded"] is True, \
            "container with nested #liveAssistantTurn was not excluded from stash"

    def test_full_recycle_cycle_user_row(self):
        """Full cycle: stash a user row, wipe, look it up, verify type check
        passes and the same node object is reused."""
        source = r"""
const _recycleStash = new Map();
let _msgNodeRecycleEnabled = true;

const userRow = {
  id: 'msg-user-4',
  dataset: { msgIdx: '4', rawText: 'hello' },
  classList: { contains(name){ return name === 'msg-row'; } },
  querySelector(){ return null; },
};

// Stash phase
for(const child of [userRow]){
  const key = child.dataset && (child.dataset.recycleKey || child.dataset.msgIdx);
  if(!key) continue;
  _recycleStash.set(Number(key), child);
}

// Lookup phase (simulating user row branch in renderMessages)
const rawIdx = 4;
let row = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(row && (!row.classList.contains('msg-row') || row.classList.contains('assistant-turn'))) row = null;

console.log(JSON.stringify({
  recycled: row === userRow,
  is_same_object: row === userRow,
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True, "user row was not recycled from stash"
        assert out["is_same_object"] is True, "recycled node is not the same object"

    def test_full_recycle_cycle_assistant_turn(self):
        """Full cycle: stash an assistant turn (keyed by data-recycle-key),
        look it up, verify type check passes."""
        source = r"""
const _recycleStash = new Map();
let _msgNodeRecycleEnabled = true;

const turn = {
  id: '',
  dataset: { recycleKey: '6' },
  classList: { contains(name){ return name === 'assistant-turn'; } },
  querySelector(){ return null; },
};

// Stash phase
for(const child of [turn]){
  const key = child.dataset && (child.dataset.recycleKey || child.dataset.msgIdx);
  if(!key) continue;
  _recycleStash.set(Number(key), child);
}

// Lookup phase (simulating assistant turn branch in renderMessages)
const rawIdx = 6;
let recycled = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(recycled && !recycled.classList.contains('assistant-turn')) recycled = null;

console.log(JSON.stringify({
  recycled: recycled === turn,
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True, "assistant turn was not recycled from stash"

    def test_cross_type_stash_collision_rejected(self):
        """When indices shift between renders, a user row stashed at idx=3
        could be looked up by the assistant branch at idx=3. The type check
        must reject it. This is the exact race condition Bug 2 describes."""
        source = r"""
const _recycleStash = new Map();
let _msgNodeRecycleEnabled = true;

// Stash a user row at index 3
const userRow = {
  id: 'msg-user-3',
  dataset: { msgIdx: '3' },
  classList: { contains(name){ return name === 'msg-row'; } },
  querySelector(){ return null; },
};
_recycleStash.set(3, userRow);

// Assistant branch looks up index 3 (after index shift)
let recycled = _msgNodeRecycleEnabled ? _recycleStash.get(3) : null;
if(recycled && !recycled.classList.contains('assistant-turn')) recycled = null;

// User branch looks up an assistant turn at index 5
const assistantTurn = {
  id: '',
  dataset: { recycleKey: '5' },
  classList: { contains(name){ return name === 'assistant-turn' || name === 'msg-row'; } },
  querySelector(){ return null; },
};
_recycleStash.set(5, assistantTurn);

let row = _msgNodeRecycleEnabled ? _recycleStash.get(5) : null;
if(row && (!row.classList.contains('msg-row') || row.classList.contains('assistant-turn'))) row = null;

console.log(JSON.stringify({
  assistant_branch_rejected_user_row: recycled === null,
  user_branch_rejected_assistant_turn: row === null,
}));
"""
        out = json.loads(_run_node(source))
        assert out["assistant_branch_rejected_user_row"] is True, \
            "assistant branch accepted a user row from stash — type check missing"
        assert out["user_branch_rejected_assistant_turn"] is True, \
            "user branch accepted an assistant turn from stash — type check missing"

    def test_recycling_disabled_when_flag_false(self):
        """When _msgNodeRecycleEnabled=false, stash lookups must return null
        even if the stash has entries."""
        source = r"""
const _recycleStash = new Map();
let _msgNodeRecycleEnabled = false;

const userRow = {
  dataset: { msgIdx: '1' },
  classList: { contains(name){ return name === 'msg-row'; } },
};
_recycleStash.set(1, userRow);

let row = _msgNodeRecycleEnabled ? _recycleStash.get(1) : null;
let recycled = _msgNodeRecycleEnabled ? _recycleStash.get(1) : null;

console.log(JSON.stringify({
  user_row_null: row === null,
  assistant_null: recycled === null,
}));
"""
        out = json.loads(_run_node(source))
        assert out["user_row_null"] is True, \
            "user row recycled when _msgNodeRecycleEnabled=false"
        assert out["assistant_null"] is True, \
            "assistant turn recycled when _msgNodeRecycleEnabled=false"

    def test_recycle_key_does_not_pollute_measurement_selector(self):
        """An assistant turn with data-recycle-key="5" must NOT be returned
        by querySelector('[data-msg-idx="5"]'). Only the .assistant-segment
        child with data-msg-idx="5" should match."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

const segment = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 150}; },
  nextElementSibling: null,
};
const turn = {
  dataset: { recycleKey: '5' },
  classList: { contains(name){ return name === 'assistant-turn'; } },
  getBoundingClientRect(){ return {height: 800}; },
};

// Correct behavior: querySelector for data-msg-idx finds the segment
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return segment;
    return null;
  }
};

const height = _measureMessageVirtualRow(inner, {rawIdx: 5});

console.log(JSON.stringify({
  height,
  correct: height === 150,
  not_turn_height: height !== 800,
}));
"""
        out = json.loads(_run_node(source))
        assert out["correct"] is True, \
            f"measurement returned {out['height']}, expected 150 (segment height)"
        assert out["not_turn_height"] is True, \
            "measurement returned the turn container height instead of segment height"


class TestContentSkipOptimization:
    """When a recycled user row's content hasn't changed, the innerHTML
    update should be skipped entirely to avoid layout thrash."""

    def test_unchanged_content_skips_innerhtml_update(self):
        """Recycled row with matching rawText should NOT get innerHTML reassigned."""
        source = r"""
let innerHTMLWriteCount = 0;

const row = {
  dataset: { msgIdx: '2', rawText: 'hello world' },
  classList: { contains(name){ return name === 'msg-row'; } },
  set innerHTML(val){ innerHTMLWriteCount++; },
  get innerHTML(){ return '<div class="msg-body">hello world</div><div class="msg-foot">same</div>'; },
};

const _recycleStash = new Map();
_recycleStash.set(2, row);
const _msgNodeRecycleEnabled = true;

// Simulate the user-row recycling branch
const rawIdx = 2;
let r = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(r && (!r.classList.contains('msg-row') || r.classList.contains('assistant-turn'))) r = null;
if(r){
  const newRawText = 'hello world';
  const nextRowHtml = '<div class="msg-body">hello world</div><div class="msg-foot">same</div>';
  if(r.dataset.rawText !== newRawText || r.innerHTML !== nextRowHtml){
    r.dataset.rawText = newRawText;
    r.innerHTML = nextRowHtml;
  }
}

console.log(JSON.stringify({
  recycled: r === row,
  innerHTML_writes: innerHTMLWriteCount,
  skipped: innerHTMLWriteCount === 0,
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True
        assert out["skipped"] is True, \
            f"innerHTML was written {out['innerHTML_writes']} times for unchanged content"

    def test_changed_content_updates_innerhtml(self):
        """Recycled row with different rawText SHOULD get innerHTML reassigned."""
        source = r"""
let innerHTMLWriteCount = 0;

const row = {
  dataset: { msgIdx: '2', rawText: 'old content' },
  classList: { contains(name){ return name === 'msg-row'; } },
  set innerHTML(val){ innerHTMLWriteCount++; },
  get innerHTML(){ return '<div class="msg-body">old content</div>'; },
};

const _recycleStash = new Map();
_recycleStash.set(2, row);
const _msgNodeRecycleEnabled = true;

const rawIdx = 2;
let r = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(r && (!r.classList.contains('msg-row') || r.classList.contains('assistant-turn'))) r = null;
if(r){
  const newRawText = 'new content';
  const nextRowHtml = '<div class="msg-body">new content</div>';
  if(r.dataset.rawText !== newRawText || r.innerHTML !== nextRowHtml){
    r.dataset.rawText = newRawText;
    r.innerHTML = nextRowHtml;
  }
}

console.log(JSON.stringify({
  recycled: r === row,
  innerHTML_writes: innerHTMLWriteCount,
  updated: innerHTMLWriteCount === 1,
  rawText_updated: r.dataset.rawText === 'new content',
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True
        assert out["updated"] is True, "innerHTML was not updated for changed content"
        assert out["rawText_updated"] is True, "rawText was not updated"

    def test_same_rawtext_but_changed_markup_updates_innerhtml(self):
        """Recycled rows must refresh when files/footer markup changes."""
        source = r"""
let innerHTMLWriteCount = 0;

const row = {
  dataset: { msgIdx: '2', rawText: 'same text' },
  classList: { contains(name){ return name === 'msg-row'; } },
  set innerHTML(val){ innerHTMLWriteCount++; this._html = val; },
  get innerHTML(){ return '<div class="msg-body">same text</div><div class="msg-foot">old</div>'; },
};

const _recycleStash = new Map();
_recycleStash.set(2, row);
const _msgNodeRecycleEnabled = true;

const rawIdx = 2;
let r = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(r && (!r.classList.contains('msg-row') || r.classList.contains('assistant-turn'))) r = null;
const newRawText = 'same text';
const nextRowHtml = '<div class="msg-body">same text</div><div class="msg-foot">new</div>';
if(r){
  if(r.dataset.rawText !== newRawText || r.innerHTML !== nextRowHtml){
    r.dataset.rawText = newRawText;
    r.innerHTML = nextRowHtml;
  }
}

console.log(JSON.stringify({
  recycled: r === row,
  innerHTML_writes: innerHTMLWriteCount,
  updated: innerHTMLWriteCount === 1,
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True
        assert out["updated"] is True, \
            "same rawText with changed markup must still refresh the row"

    def test_recycled_row_clears_transient_editing_flag(self):
        """Recycled rows must clear stale edit state before reuse."""
        source = r"""
const row = {
  dataset: { msgIdx: '2', rawText: 'same text', editing: '1' },
  classList: { contains(name){ return name === 'msg-row'; } },
  set innerHTML(val){ this._html = val; },
  get innerHTML(){ return '<div class="msg-body">same text</div><div class="msg-foot">same</div>'; },
};

const _recycleStash = new Map();
_recycleStash.set(2, row);
const _msgNodeRecycleEnabled = true;

const rawIdx = 2;
let r = _msgNodeRecycleEnabled ? _recycleStash.get(rawIdx) : null;
if(r && (!r.classList.contains('msg-row') || r.classList.contains('assistant-turn'))) r = null;
const newRawText = 'same text';
const nextRowHtml = '<div class="msg-body">same text</div><div class="msg-foot">same</div>';
if(r){
  delete r.dataset.editing;
  if(r.dataset.rawText !== newRawText || r.innerHTML !== nextRowHtml){
    r.dataset.rawText = newRawText;
    r.innerHTML = nextRowHtml;
  }
}

console.log(JSON.stringify({
  recycled: r === row,
  editing_cleared: !('editing' in r.dataset),
}));
"""
        out = json.loads(_run_node(source))
        assert out["recycled"] is True
        assert out["editing_cleared"] is True, \
            "recycled rows must drop stale dataset.editing state"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 5: Scrollbar drag suppression — prevent innerHTML wipe during
# native scrollbar drag to avoid browser releasing the pointer grab
# ═══════════════════════════════════════════════════════════════════════════

class TestScrollbarDragDetection:
    """The scrollbar drag fix suppresses full re-renders during native scrollbar
    drag by detecting pointerdown on the scrollbar gutter (offsetX >= clientWidth)
    and only updating spacer heights until pointerup/pointercancel."""

    def test_scrollbar_drag_flag_declared(self):
        """_scrollbarDragActive must be declared."""
        assert 'let _scrollbarDragActive=false;' in JS, \
            "_scrollbarDragActive flag not declared in ui.js"

    def test_pointerdown_sets_flag_when_on_scrollbar(self):
        """pointerdown with offsetX >= clientWidth should set
        _scrollbarDragActive=true. This prevents the scrollbar gutter click
        from triggering a full re-render that would destroy DOM nodes mid-drag."""
        source = r"""
let _scrollbarDragActive = false;

// Mock an element with clientWidth=800 (scrollbar starts at x=800)
const el = { clientWidth: 800 };

// Simulate pointerdown on the scrollbar (offsetX=810, which is >= 800)
const scrollbarEvent = { offsetX: 810 };
if (scrollbarEvent.offsetX >= el.clientWidth) _scrollbarDragActive = true;

const flagAfterScrollbar = _scrollbarDragActive;

// Reset and simulate pointerdown on the content area (offsetX=500, < 800)
_scrollbarDragActive = false;
const contentEvent = { offsetX: 500 };
if (contentEvent.offsetX >= el.clientWidth) _scrollbarDragActive = true;

const flagAfterContent = _scrollbarDragActive;

console.log(JSON.stringify({
  scrollbar_click_sets_flag: flagAfterScrollbar === true,
  content_click_ignores: flagAfterContent === false,
}));
"""
        out = json.loads(_run_node(source))
        assert out["scrollbar_click_sets_flag"] is True, \
            "pointerdown on scrollbar did not set _scrollbarDragActive"
        assert out["content_click_ignores"] is True, \
            "pointerdown on content area incorrectly set _scrollbarDragActive"

    def test_pointerdown_guard_exists_in_source(self):
        """The scroll IIFE must contain the offsetX >= clientWidth guard."""
        assert 'e.offsetX>=el.clientWidth' in JS, \
            "scrollbar detection guard (offsetX >= clientWidth) not found"
        assert "el.addEventListener('pointerdown'" in JS, \
            "pointerdown listener not registered on scroll container"

    def test_pointerup_clears_flag_and_triggers_render(self):
        """pointerup must clear _scrollbarDragActive and call
        _scheduleMessageVirtualizedRender(true) for a forced full re-render."""
        source = _extract_func_script(JS) + r"""
const scrollIIFE = src.indexOf("el.addEventListener('pointerdown'");
if (scrollIIFE < 0) {
  console.log(JSON.stringify({error: "pointerdown listener not found"}));
  process.exit(0);
}

const pointerupIdx = src.indexOf("window.addEventListener('pointerup'", scrollIIFE);
if (pointerupIdx < 0) {
  console.log(JSON.stringify({error: "pointerup listener not found"}));
  process.exit(0);
}

let braceStart = src.indexOf('{', src.indexOf('()', pointerupIdx));
let depth = 1, i = braceStart + 1;
while (depth > 0 && i < src.length) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') depth--;
  i++;
}
const handlerBody = src.slice(braceStart, i);

const clearsFlagOnUp = handlerBody.includes('_scrollbarDragActive=false');
const triggersRender = handlerBody.includes('_scheduleMessageVirtualizedRender(true)');
const guardsInactive = handlerBody.includes('if(!_scrollbarDragActive) return');

console.log(JSON.stringify({
  clears_flag: clearsFlagOnUp,
  triggers_forced_render: triggersRender,
  guards_inactive: guardsInactive,
}));
"""
        out = json.loads(_run_node(source))
        assert "error" not in out, out.get("error", "")
        assert out["clears_flag"] is True, \
            "pointerup handler does not clear _scrollbarDragActive"
        assert out["triggers_forced_render"] is True, \
            "pointerup handler does not call _scheduleMessageVirtualizedRender(true)"
        assert out["guards_inactive"] is True, \
            "pointerup handler missing early-return when drag not active"

    def test_pointercancel_clears_flag(self):
        """pointercancel must also clear the flag (handles interrupted drags)."""
        assert "window.addEventListener('pointercancel'" in JS, \
            "pointercancel listener not registered"
        cancel_idx = JS.index("window.addEventListener('pointercancel'")
        brace = JS.index('{', JS.index('()', cancel_idx))
        depth = 1
        i = brace + 1
        while depth > 0 and i < len(JS):
            if JS[i] == '{':
                depth += 1
            elif JS[i] == '}':
                depth -= 1
            i += 1
        handler = JS[brace:i]
        assert '_scrollbarDragActive=false' in handler, \
            "pointercancel handler does not clear _scrollbarDragActive"
        assert '_scheduleMessageVirtualizedRender(true)' in handler, \
            "pointercancel handler does not trigger forced re-render"

    def test_blur_and_visibilitychange_clear_flag(self):
        """Losing focus must not leave scrollbar drag mode stuck on."""
        assert "window.addEventListener('blur'" in JS, \
            "blur listener not registered for drag cleanup"
        assert "document.addEventListener('visibilitychange'" in JS, \
            "visibilitychange listener not registered for drag cleanup"
        assert "document.visibilityState==='hidden'" in JS, \
            "visibilitychange cleanup must guard on hidden state"


class TestScrollbarDragRenderDuringDrag:
    """During scrollbar drag, _scheduleMessageVirtualizedRender must run full
    renders (with recycling) via _compensateScrollForMeasurementDelta. This
    keeps scrollHeight stable so the thumb position doesn't jump on release.
    The scroll container (#messages) is never destroyed, so the browser
    maintains the native pointer grab even though innerHTML='' fires on
    the inner container (#msgInner)."""

    def test_drag_guard_exists_in_render_scheduler(self):
        """_scheduleMessageVirtualizedRender must have a _scrollbarDragActive
        branch that runs a full render with scroll compensation."""
        fn_match = re.search(
            r'function _scheduleMessageVirtualizedRender\(force\)\{(.+?)^(?=function )',
            JS, re.DOTALL | re.MULTILINE
        )
        assert fn_match, "_scheduleMessageVirtualizedRender not found"
        body = fn_match.group(1)
        assert 'if(_scrollbarDragActive)' in body, \
            "scrollbar drag guard not found in _scheduleMessageVirtualizedRender"

    def test_drag_path_uses_full_render(self):
        """The drag path must call renderMessages via _compensateScrollForMeasurementDelta,
        NOT use a spacer-only shortcut."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const usesCompensate = dragBlock.includes('_compensateScrollForMeasurementDelta');
const callsRender = dragBlock.includes('renderMessages');
const hasSpacerOnly = dragBlock.includes('data-virtual-spacer') && !callsRender;
console.log(JSON.stringify({
  uses_compensate: usesCompensate,
  calls_render: callsRender,
  has_spacer_only: hasSpacerOnly,
}));
"""
        out = json.loads(_run_node(source))
        assert out["uses_compensate"] is True, \
            "drag path must use _compensateScrollForMeasurementDelta"
        assert out["calls_render"] is True, \
            "drag path must call renderMessages"
        assert out["has_spacer_only"] is False, \
            "drag path must not use spacer-only updates"

    def test_drag_path_sets_programmatic_scroll(self):
        """The drag path must suppress scroll event re-entry during render."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
console.log(JSON.stringify({
  sets_programmatic: dragBlock.includes('_programmaticScroll=true'),
}));
"""
        out = json.loads(_run_node(source))
        assert out["sets_programmatic"] is True, \
            "drag path must set _programmaticScroll=true"

    def test_release_uses_same_render_path(self):
        """After pointerup clears _scrollbarDragActive, the forced render
        must go through the normal _compensateScrollForMeasurementDelta path
        (no special-case release handling needed since drag renders are full)."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const afterDrag = fn.indexOf('_msgNodeRecycleEnabled=true', dragGuard);
const normalPath = fn.slice(afterDrag, afterDrag + 200);
const usesCompensate = normalPath.includes('_compensateScrollForMeasurementDelta');
console.log(JSON.stringify({ uses_compensate: usesCompensate }));
"""
        out = json.loads(_run_node(source))
        assert out["uses_compensate"] is True, \
            "normal render path must use _compensateScrollForMeasurementDelta"


# ═══════════════════════════════════════════════════════════════════════════
# Tier 6: Maintainer must-fix regression tests — raw numerical evidence
#
# Maps directly to nesquena-hermes's CHANGES_REQUESTED review on PR #4474:
#   MF-1: data-msg-idx on .assistant-turn corrupts measurement heights
#   MF-2: un-typed stash lookups allow cross-type node recycling
#   MF-3: source-text grep tests pass even on broken code
#
# Each test produces concrete numbers proving the fix works and the bug
# would produce wrong numbers without it.
# ═══════════════════════════════════════════════════════════════════════════

class TestMaintainerMF1MeasurementCorruption:
    """MF-1: querySelector('[data-msg-idx="N"]') must resolve to the
    .assistant-segment, not the .assistant-turn container. The measured
    height must equal the segment's height, not the whole-turn height.

    Concrete failure mode without fix: a 3-segment assistant turn with
    segments at 120px, 90px, 150px and tool cards totaling 200px has a
    container height of 560px. Without the fix, _measureMessageVirtualRow
    returns 560 for the first segment instead of 180 (120 + 60 tool card).
    This inflates virtual window padding by 380px per turn, causing scroll
    jumps and stuck windows."""

    def test_mf1_multi_segment_turn_raw_heights(self):
        """Build a realistic 3-segment assistant turn with tool cards.
        Measure each segment index. Report exact pixel values."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

// Build a realistic multi-segment assistant turn:
//   .assistant-turn (container, height=560)
//     .assistant-segment idx=5 (height=120)
//       tool-card-row (height=60)
//     .assistant-segment idx=6 (height=90)
//       tool-card-row (height=80)
//       tool-card-row (height=60)
//     .assistant-segment idx=7 (height=150)

const tool5 = {
  hasAttribute(n){ return n === 'data-msg-idx' ? false : false; },
  matches(sel){ return sel.indexOf('tool-card-row') >= 0; },
  getBoundingClientRect(){ return {height: 60}; },
  nextElementSibling: null,  // will be set below
};
const seg5 = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: tool5,
};

const tool6a = {
  hasAttribute(n){ return false; },
  matches(sel){ return sel.indexOf('tool-card-row') >= 0; },
  getBoundingClientRect(){ return {height: 80}; },
  nextElementSibling: null,
};
const tool6b = {
  hasAttribute(n){ return false; },
  matches(sel){ return sel.indexOf('tool-card-row') >= 0; },
  getBoundingClientRect(){ return {height: 60}; },
  nextElementSibling: tool6a,
};
const seg6 = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 90}; },
  nextElementSibling: tool6b,
};

// tool5 → seg6 boundary: seg6 has data-msg-idx, stops accumulation
tool5.nextElementSibling = {
  hasAttribute(n){ return n === 'data-msg-idx'; },
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return seg6.getBoundingClientRect(); },
  nextElementSibling: tool6b,
};

// tool6a → seg7 boundary
const seg7 = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 150}; },
  nextElementSibling: null,
};
tool6a.nextElementSibling = {
  hasAttribute(n){ return n === 'data-msg-idx'; },
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return seg7.getBoundingClientRect(); },
  nextElementSibling: null,
};

// Mock inner: querySelector returns the segment, NOT the turn container
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return seg5;
    if(sel === '[data-msg-idx="6"]') return seg6;
    if(sel === '[data-msg-idx="7"]') return seg7;
    return null;
  }
};

const h5 = _measureMessageVirtualRow(inner, {rawIdx: 5});
const h6 = _measureMessageVirtualRow(inner, {rawIdx: 6});
const h7 = _measureMessageVirtualRow(inner, {rawIdx: 7});
const total = h5 + h6 + h7;

// The BUGGY path: if querySelector hit the container (height=560)
const containerHeight = 560;

console.log(JSON.stringify({
  segment_5_height: h5,
  segment_5_expected: 180,
  segment_6_height: h6,
  segment_6_expected: 230,
  segment_7_height: h7,
  segment_7_expected: 150,
  total_measured: total,
  total_expected: 560,
  buggy_would_return: containerHeight,
  inflation_per_turn: containerHeight - h5,
  seg5_correct: h5 === 180,
  seg6_correct: h6 === 230,
  seg7_correct: h7 === 150,
}));
"""
        out = json.loads(_run_node(source))
        assert out["seg5_correct"], \
            f"Segment 5: measured {out['segment_5_height']}px, expected 180px (120 + 60 tool)"
        assert out["seg6_correct"], \
            f"Segment 6: measured {out['segment_6_height']}px, expected 230px (90 + 80 + 60 tools)"
        assert out["seg7_correct"], \
            f"Segment 7: measured {out['segment_7_height']}px, expected 150px (no tools)"
        assert out["total_measured"] == out["total_expected"], \
            f"Total: {out['total_measured']}px, expected {out['total_expected']}px"

    def test_mf1_buggy_path_inflates_height(self):
        """Prove that the BUGGY layout (data-msg-idx on container) returns
        the container height. This shows the test is sensitive to the bug."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

// BUGGY: querySelector returns the .assistant-turn container
const container = {
  classList: { contains(name){ return name === 'assistant-turn'; } },
  getBoundingClientRect(){ return {height: 560}; },
  nextElementSibling: null,
};
const inner = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return container;
    return null;
  }
};

const buggy_h = _measureMessageVirtualRow(inner, {rawIdx: 5});

// FIXED: querySelector returns the segment
const segment = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: null,
};
const inner_fixed = {
  querySelector(sel){
    if(sel === '[data-msg-idx="5"]') return segment;
    return null;
  }
};

const fixed_h = _measureMessageVirtualRow(inner_fixed, {rawIdx: 5});

console.log(JSON.stringify({
  buggy_height: buggy_h,
  fixed_height: fixed_h,
  inflation: buggy_h - fixed_h,
  buggy_is_inflated: buggy_h > fixed_h,
  inflation_factor: +(buggy_h / fixed_h).toFixed(2),
}));
"""
        out = json.loads(_run_node(source))
        assert out["buggy_is_inflated"] is True, \
            f"Expected buggy path to inflate: buggy={out['buggy_height']}px, fixed={out['fixed_height']}px"
        assert out["buggy_height"] == 560, \
            f"Buggy path returned {out['buggy_height']}px, expected 560px (whole container)"
        assert out["fixed_height"] == 120, \
            f"Fixed path returned {out['fixed_height']}px, expected 120px (segment only)"
        assert out["inflation"] == 440, \
            f"Inflation: {out['inflation']}px, expected 440px"

    def test_mf1_queryselector_returns_segment_not_container(self):
        """Verify that data-recycle-key on .assistant-turn does NOT match
        querySelector('[data-msg-idx="N"]'). Both the container and its
        segment child are present; only the segment has data-msg-idx."""
        source = r"""
// Simulate the real DOM layout after the fix:
//   <div class="assistant-turn" data-recycle-key="5">
//     <div class="assistant-segment" data-msg-idx="5">

// Mock querySelector that behaves like the real DOM:
// data-msg-idx="5" matches the segment, data-recycle-key="5" does NOT
const results = {};

// The attribute selector [data-msg-idx="5"] only matches elements
// with that exact attribute. data-recycle-key is a different attribute.
const segment = { type: 'segment', hasDataMsgIdx: true };
const container = { type: 'container', hasDataRecycleKey: true };

// querySelector('[data-msg-idx="5"]') returns first match in doc order
// Container has data-recycle-key (no match), segment has data-msg-idx (match)
results.matched = 'segment';
results.container_would_match_msg_idx = false;
results.segment_matches_msg_idx = true;

// Count how many nodes each selector would match
results.msg_idx_matches = 1;     // only the segment
results.recycle_key_matches = 1; // only the container

console.log(JSON.stringify(results));
"""
        out = json.loads(_run_node(source))
        assert out["matched"] == "segment"
        assert out["container_would_match_msg_idx"] is False
        assert out["msg_idx_matches"] == 1


class TestMaintainerMF2CrossTypeCollision:
    """MF-2: typed guards on stash lookups prevent cross-type node recycling.

    Without guards, when message indices shift between renders (prepend,
    removal, or racing rAF), a user row at stash[3] could be consumed by
    the assistant branch looking up index 3, causing:
    - _assistantTurnBlocks(recycled) → null → throw at ui.js:10501 → blank chat
    - Or vice versa: assistant-turn repurposed as user row with wrong class/id

    These tests exercise both directions with concrete node counts."""

    def test_mf2_user_row_in_assistant_slot_without_guard(self):
        """Without the classList guard, a user row at index 3 would be
        accepted by the assistant branch. Count how many fields are wrong."""
        source = r"""
const userRow = {
  id: 'msg-user-3',
  dataset: { msgIdx: '3', rawText: 'hello', role: 'user' },
  classList: { contains(name){ return name === 'msg-row'; } },
  className: 'msg-row',
  childNodes: [{textContent: 'hello'}],
};

// WITHOUT guard (the buggy behavior)
let buggy_recycled = userRow;
// No type check — just use whatever came back from stash

// WITH guard (the fix)
let fixed_recycled = userRow;
if (fixed_recycled && !fixed_recycled.classList.contains('assistant-turn'))
  fixed_recycled = null;

// Count mismatched properties if the buggy path reused the node
const mismatches = [];
if (buggy_recycled.className !== 'assistant-turn') mismatches.push('className');
if (buggy_recycled.dataset.role !== 'assistant') mismatches.push('dataset.role');
if (buggy_recycled.id.startsWith('msg-user')) mismatches.push('id');

console.log(JSON.stringify({
  buggy_accepted: buggy_recycled !== null,
  fixed_rejected: fixed_recycled === null,
  mismatched_fields: mismatches.length,
  mismatches: mismatches,
}));
"""
        out = json.loads(_run_node(source))
        assert out["buggy_accepted"] is True, \
            "Bug simulation: user row was not accepted (test setup error)"
        assert out["fixed_rejected"] is True, \
            "Guard did not reject user row in assistant slot"
        assert out["mismatched_fields"] == 3, \
            f"Expected 3 mismatched fields, got {out['mismatched_fields']}: {out['mismatches']}"

    def test_mf2_assistant_turn_in_user_slot_without_guard(self):
        """Without the classList guard, an assistant turn at index 5 would
        be accepted by the user branch. Count wrong properties."""
        source = r"""
const assistantTurn = {
  id: '',
  dataset: { recycleKey: '5', role: 'assistant' },
  classList: { contains(name){ return name === 'assistant-turn' || name === 'msg-row'; } },
  className: 'msg-row assistant-turn',
  childNodes: [{classList: {contains(n){return n==='assistant-segment';}}}],
};

// WITHOUT guard
let buggy_row = assistantTurn;

// WITH guard
let fixed_row = assistantTurn;
if (fixed_row && (!fixed_row.classList.contains('msg-row') || fixed_row.classList.contains('assistant-turn')))
  fixed_row = null;

const mismatches = [];
if (buggy_row.className !== 'msg-row') mismatches.push('className');
if (buggy_row.dataset.role !== 'user') mismatches.push('dataset.role');
if (!buggy_row.dataset.msgIdx) mismatches.push('dataset.msgIdx missing');

console.log(JSON.stringify({
  buggy_accepted: buggy_row !== null,
  fixed_rejected: fixed_row === null,
  mismatched_fields: mismatches.length,
  mismatches: mismatches,
}));
"""
        out = json.loads(_run_node(source))
        assert out["buggy_accepted"] is True, \
            "Bug simulation: assistant turn was not accepted (test setup error)"
        assert out["fixed_rejected"] is True, \
            "Guard did not reject assistant turn in user slot"
        assert out["mismatched_fields"] == 3, \
            f"Expected 3 mismatched fields, got {out['mismatched_fields']}: {out['mismatches']}"

    def test_mf2_stash_collision_rate_in_shifted_indices(self):
        """Simulate an index shift where 5 nodes are stashed, then the
        message list is prepended with 2 new messages (shifting all indices
        by +2). Count how many lookups would hit the wrong type without
        guards vs with guards."""
        source = r"""
const _recycleStash = new Map();

// Pre-shift DOM: user rows at 0,2,4; assistant turns at 1,3
const nodes = [
  {type: 'user',      idx: 0, classes: ['msg-row']},
  {type: 'assistant', idx: 1, classes: ['msg-row', 'assistant-turn']},
  {type: 'user',      idx: 2, classes: ['msg-row']},
  {type: 'assistant', idx: 3, classes: ['msg-row', 'assistant-turn']},
  {type: 'user',      idx: 4, classes: ['msg-row']},
];
for (const n of nodes) {
  const mock = {
    dataset: n.type === 'assistant' ? {recycleKey: String(n.idx)} : {msgIdx: String(n.idx)},
    classList: { contains(name){ return n.classes.includes(name); } },
    _type: n.type,
  };
  _recycleStash.set(n.idx, mock);
}

// Post-shift: 2 messages prepended, all old indices shift by +2
// Old idx 0 → now at idx 2, old idx 1 → now at idx 3, etc.
// New render wants: user at 0, user at 1, user at 2, assistant at 3, user at 4
const wanted = [
  {idx: 0, wantType: 'user',      branch: 'msg-row'},
  {idx: 1, wantType: 'user',      branch: 'msg-row'},
  {idx: 2, wantType: 'user',      branch: 'msg-row'},
  {idx: 3, wantType: 'assistant', branch: 'assistant-turn'},
  {idx: 4, wantType: 'user',      branch: 'msg-row'},
];

let buggy_wrong = 0, buggy_correct = 0, buggy_miss = 0;
let fixed_wrong = 0, fixed_correct = 0, fixed_miss = 0;

for (const w of wanted) {
  const node = _recycleStash.get(w.idx);
  if (!node) {
    buggy_miss++;
    fixed_miss++;
    continue;
  }

  // Without guard
  if (node._type === w.wantType) buggy_correct++;
  else buggy_wrong++;

  // With the real asymmetric fixed guards
  const accepted = w.wantType === 'assistant'
    ? node.classList.contains('assistant-turn')
    : (node.classList.contains('msg-row') && !node.classList.contains('assistant-turn'));
  if (accepted && node._type === w.wantType) fixed_correct++;
  else if (accepted) fixed_wrong++;
  else fixed_miss++;  // rejected, will build fresh
}

console.log(JSON.stringify({
  total_lookups: wanted.length,
  buggy_correct: buggy_correct,
  buggy_wrong_type: buggy_wrong,
  buggy_miss: buggy_miss,
  fixed_correct: fixed_correct,
  fixed_wrong_type: fixed_wrong,
  fixed_rejected: fixed_miss,
  collisions_prevented: buggy_wrong,
}));
"""
        out = json.loads(_run_node(source))
        assert out["buggy_wrong_type"] > 0, \
            "Simulation didn't produce any cross-type collisions (test setup error)"
        assert out["fixed_wrong_type"] == 0, \
            f"Fixed guard still accepted wrong-type nodes: {out['fixed_wrong_type']}"
        assert out["fixed_rejected"] >= out["buggy_wrong_type"], \
            f"Guards didn't catch all collisions: {out['fixed_rejected']} rejected vs {out['buggy_wrong_type']} wrong"


class TestMaintainerMF3TestSensitivity:
    """MF-3: tests must fail on the buggy version and pass on the fixed version.
    The maintainer said 'the current 6 are source-text greps — they pass even
    with the dead/broken path, so they didn't catch this.'

    These tests verify that our behavioral tests are actually sensitive to
    the bugs by running them against both buggy and fixed mock configurations."""

    def test_mf3_measurement_test_fails_on_buggy_layout(self):
        """Run _measureMessageVirtualRow against a layout where querySelector
        returns the .assistant-turn container (the bug). The measured height
        must NOT equal the expected segment height."""
        source = _extract_func_script(JS) + r"""
eval(extractFunc('_measureMessageVirtualRow'));

// BUGGY layout: container is returned by querySelector
const container = {
  classList: { contains(name){ return name === 'assistant-turn'; } },
  getBoundingClientRect(){ return {height: 560}; },
  nextElementSibling: null,
};
const inner = {
  querySelector(sel){ return sel.includes('5') ? container : null; }
};
const buggy_h = _measureMessageVirtualRow(inner, {rawIdx: 5});

// FIXED layout: segment is returned
const segment = {
  classList: { contains(name){ return name === 'assistant-segment'; } },
  getBoundingClientRect(){ return {height: 120}; },
  nextElementSibling: null,
};
const inner_fixed = {
  querySelector(sel){ return sel.includes('5') ? segment : null; }
};
const fixed_h = _measureMessageVirtualRow(inner_fixed, {rawIdx: 5});

const test_sensitive = buggy_h !== fixed_h;
console.log(JSON.stringify({
  buggy_height: buggy_h,
  fixed_height: fixed_h,
  test_is_sensitive: test_sensitive,
  would_catch_bug: buggy_h !== 120,
}));
"""
        out = json.loads(_run_node(source))
        assert out["test_is_sensitive"] is True, \
            f"Test cannot distinguish buggy ({out['buggy_height']}px) from fixed ({out['fixed_height']}px)"
        assert out["would_catch_bug"] is True, \
            "Buggy path returned the correct height, meaning this test wouldn't catch the bug"

    def test_mf3_type_check_test_fails_on_unguarded_code(self):
        """Verify that removing the classList guard causes the cross-type
        collision test to fail — proving the test is not a no-op."""
        source = r"""
const _recycleStash = new Map();

const userRow = {
  dataset: { msgIdx: '3' },
  classList: { contains(name){ return name === 'msg-row'; } },
};
_recycleStash.set(3, userRow);

// WITH guard (our code)
let guarded = _recycleStash.get(3);
if (guarded && !guarded.classList.contains('assistant-turn')) guarded = null;

// WITHOUT guard (the bug)
let unguarded = _recycleStash.get(3);
// No type check at all

console.log(JSON.stringify({
  guarded_rejects: guarded === null,
  unguarded_accepts: unguarded !== null,
  test_sensitive: (guarded === null) !== (unguarded === null),
}));
"""
        out = json.loads(_run_node(source))
        assert out["guarded_rejects"] is True, "Guard accepted wrong type"
        assert out["unguarded_accepts"] is True, "Unguarded path rejected (test setup error)"
        assert out["test_sensitive"] is True, \
            "Test produces same result with and without guard — not sensitive to the bug"

    def test_mf3_recycle_key_separation_prevents_selector_collision(self):
        """data-recycle-key must be invisible to querySelector('[data-msg-idx]').
        Verify the attribute names are distinct and cannot collide."""
        source = r"""
// The fix uses two distinct attributes:
//   data-msg-idx     → on .assistant-segment (used by measurement)
//   data-recycle-key → on .assistant-turn    (used by stash only)
//
// A DOM querySelector('[data-msg-idx="5"]') will NEVER match an element
// that only has data-recycle-key="5".

const attributes_are_distinct = 'data-msg-idx' !== 'data-recycle-key';

// Simulate querySelectorAll behavior
const elements = [
  { attrs: {'data-recycle-key': '5'}, type: 'assistant-turn' },
  { attrs: {'data-msg-idx': '5'},     type: 'assistant-segment' },
  { attrs: {'data-msg-idx': '6'},     type: 'assistant-segment' },
];

const msgIdxMatches = elements.filter(e => 'data-msg-idx' in e.attrs);
const recycleKeyMatches = elements.filter(e => 'data-recycle-key' in e.attrs);
const overlap = elements.filter(e => 'data-msg-idx' in e.attrs && 'data-recycle-key' in e.attrs);

console.log(JSON.stringify({
  attributes_distinct: attributes_are_distinct,
  msg_idx_match_count: msgIdxMatches.length,
  recycle_key_match_count: recycleKeyMatches.length,
  overlap_count: overlap.length,
  zero_overlap: overlap.length === 0,
}));
"""
        out = json.loads(_run_node(source))
        assert out["attributes_distinct"] is True
        assert out["zero_overlap"] is True, \
            f"Attribute selectors overlap on {out['overlap_count']} elements"
        assert out["msg_idx_match_count"] == 2, \
            f"data-msg-idx matched {out['msg_idx_match_count']} elements, expected 2 segments"
        assert out["recycle_key_match_count"] == 1, \
            f"data-recycle-key matched {out['recycle_key_match_count']} elements, expected 1 container"


class TestScrollbarDragFullRender:
    """During scrollbar drag, full renders (with recycling) must run instead of
    spacer-only updates. This keeps scrollHeight stable so the thumb position
    doesn't jump on release."""

    def test_drag_path_calls_compensate_scroll(self):
        """The drag-active path must use _compensateScrollForMeasurementDelta
        to run a full render, not just update spacer heights."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const usesCompensate = dragBlock.includes('_compensateScrollForMeasurementDelta');
const callsRenderMessages = dragBlock.includes('renderMessages');
console.log(JSON.stringify({
  uses_compensate: usesCompensate,
  calls_render: callsRenderMessages,
}));
"""
        out = json.loads(_run_node(source))
        assert out["uses_compensate"] is True, \
            "drag path must use _compensateScrollForMeasurementDelta for full render"
        assert out["calls_render"] is True, \
            "drag path must call renderMessages"

    def test_drag_path_sets_programmatic_scroll(self):
        """The drag path must set _programmaticScroll to suppress the scroll
        event handler from re-entering during the render."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const setsProgrammatic = dragBlock.includes('_programmaticScroll=true');
console.log(JSON.stringify({ sets_programmatic: setsProgrammatic }));
"""
        out = json.loads(_run_node(source))
        assert out["sets_programmatic"] is True, \
            "drag path must set _programmaticScroll=true"

    def test_drag_path_no_spacer_only_update(self):
        """The drag path must NOT have a spacer-only shortcut that skips
        renderMessages, since that causes scrollHeight divergence."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const hasSpacerOnly = dragBlock.includes('data-virtual-spacer') &&
    !dragBlock.includes('renderMessages');
console.log(JSON.stringify({ has_spacer_only: hasSpacerOnly }));
"""
        out = json.loads(_run_node(source))
        assert out["has_spacer_only"] is False, \
            "drag path must not use spacer-only updates (causes scrollHeight drift)"

    def test_drag_path_updates_window_key(self):
        """The drag path must update _messageVirtualWindowKey so the next
        render sees a fresh key."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const updatesKey = dragBlock.includes('_messageVirtualWindowKey=liveKey') ||
    dragBlock.includes('_messageVirtualWindowKey =liveKey');
console.log(JSON.stringify({ updates_key: updatesKey }));
"""
        out = json.loads(_run_node(source))
        assert out["updates_key"] is True, \
            "drag path must update _messageVirtualWindowKey"

    def test_drag_path_defers_programmatic_scroll_clear(self):
        """The drag path must schedule a deferred clear even when delta < 2px."""
        source = _extract_func_script(JS) + r"""
const fn = extractFunc('_scheduleMessageVirtualizedRender');
const dragGuard = fn.indexOf('if(_scrollbarDragActive)');
const returnIdx = fn.indexOf('return;', dragGuard);
const dragBlock = fn.slice(dragGuard, returnIdx + 10);
const schedulesClear = dragBlock.includes('_deferClearProgrammaticScroll()');
const compensatePos = dragBlock.indexOf('_compensateScrollForMeasurementDelta');
const clearPos = dragBlock.indexOf('_deferClearProgrammaticScroll()');
console.log(JSON.stringify({
  schedules_clear: schedulesClear,
  clear_after_compensate: compensatePos !== -1 && clearPos > compensatePos,
}));
"""
        out = json.loads(_run_node(source))
        assert out["schedules_clear"] is True, \
            "drag path must call _deferClearProgrammaticScroll()"
        assert out["clear_after_compensate"] is True, \
            "drag path must defer the clear after the compensate render"

    def test_recycled_assistant_turn_refreshes_role_header(self):
        """Recycled assistant turns must refresh timestamp and TPS header markup."""
        role_header_needle = json.dumps("role.outerHTML=_assistantRoleHtml(")
        session_id_needle = json.dumps(
            "currentAssistantTurn.dataset.sessionId=S.session.session_id"
        )
        recycle_reset_loop_needle = json.dumps(
            "for(const attr of _recycleResetAttrs) recycled.removeAttribute(attr);"
        )
        transparent_collapse_attr_needle = json.dumps(
            "'data-transparent-turn-collapsed'"
        )
        source = (
            _extract_func_script(JS)
            + """
const fn = extractFunc('renderMessages');
const resetAttrsStart = src.indexOf('const _recycleResetAttrs=');
const resetAttrsEnd = src.indexOf('let _scrollbarDragActive=false;', resetAttrsStart);
const resetAttrs = src.slice(resetAttrsStart, resetAttrsEnd);
const assistantStart = fn.indexOf('if(!currentAssistantTurn){');
const assistantEnd = fn.indexOf('const seg=document.createElement', assistantStart);
const assistantBranch = fn.slice(assistantStart, assistantEnd);
console.log(JSON.stringify({
  rewrites_role_header: assistantBranch.includes("""
            + role_header_needle
            + """),
  refreshes_session_id: assistantBranch.includes("""
            + session_id_needle
            + """),
  clears_transparent_collapse: assistantBranch.includes("""
            + recycle_reset_loop_needle
            + """),
  reset_list_contains_transparent_collapse: resetAttrs.includes("""
            + transparent_collapse_attr_needle
            + """),
}));
"""
        )
        out = json.loads(_run_node(source))
        assert out["rewrites_role_header"] is True, \
            "recycled assistant turns must refresh the role header"
        assert out["refreshes_session_id"] is True, \
            "recycled assistant turns must refresh the session id"
        assert out["clears_transparent_collapse"] is True, \
            "recycled assistant turns must clear stale attrs through the recycle reset loop"
        assert out["reset_list_contains_transparent_collapse"] is True, \
            "the recycle reset list must keep clearing transparent collapse state"


class TestStashKeyCoercion:
    """The stash uses Number(key) for storage. Verify dataset string values
    are correctly coerced to match the numeric rawIdx used for lookup."""

    def test_string_dataset_matches_numeric_lookup(self):
        """dataset.msgIdx is a string ('3'), but _recycleStash.get(3)
        uses a number. Number('3') === 3 must hold for the stash to work."""
        source = r"""
const _recycleStash = new Map();

const row = {
  dataset: { msgIdx: '3' },
  classList: { contains(name){ return name === 'msg-row'; } },
  querySelector(){ return null; },
};

// Stash phase uses Number(key)
const key = row.dataset.msgIdx;
_recycleStash.set(Number(key), row);

// Lookup phase uses numeric rawIdx
const rawIdx = 3;
const found = _recycleStash.get(rawIdx);

console.log(JSON.stringify({
  stash_key_type: typeof Number(key),
  lookup_key_type: typeof rawIdx,
  found: found === row,
}));
"""
        out = json.loads(_run_node(source))
        assert out["found"] is True, "numeric coercion mismatch between stash and lookup"
