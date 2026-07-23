"""Settlement-path echo suppression: provenance-safe identity-based dedup.

Tests exercise the real _completeSettledAnchorSceneForTurn() function through
Node.js, covering the fix for PR #6293 / #6187.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_body(src, name):
    start = src.find(f"function {name}")
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    assert params != -1, f"{name} params not found"
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} params did not close"
    brace = src.find("{", close)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:idx]
    raise AssertionError(f"{name} body did not close")


def _run_node_script(script):
    assert NODE, "node is required for DOM-executed anchor render tests"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_EXTRACT_FUNC_JS = """
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
""".strip()

_SETTLEMENT_JS_BOOT = """
const fs = require('fs');
const src = fs.readFileSync({src_path}, 'utf8');
{extract_func}
global.window = {{ chatActivityMode(){{ return 'compact_worklog'; }}, _chatActivityDisplayMode: 'compact_worklog', }};
global.S = {{ session: {{}} }};
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
eval(extractFunc('_anchorSceneActiveMode'));
eval(extractFunc('_anchorSceneRowDisplayHintForMode'));
function _anchorSceneFinalAnswerText(message){{ return message && (message.final_answer || message.content || ''); }}
function _anchorSceneRowsByMessageIndex(){{ return new Map(); }}
function _anchorSceneMessageRef(message){{ return String(message && message.id || ''); }}
function _anchorSceneTurnDurationForSettlement(_lastAsst, base){{ return base && base.turn_duration ? base.turn_duration : 0; }}
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
""".format(
    src_path=json.dumps(str(ROOT / "static" / "messages.js")),
    extract_func=_EXTRACT_FUNC_JS,
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_preserves_distinct_identity_same_text_across_tool_boundary():
    """Two prose rows with different local_ids but same text, separated by a
    tool row. Both must survive settlement (no global text-only suppression)."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Processing...', local_id:'live-prose:s:1', row_id:'r1', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'tool',   text:'Fetched data',  local_id:'live-tool:1',  row_id:'r2', tool_call_id:'tc-1', status:'completed'},
    {role:'prose', text:'Processing...', local_id:'live-prose:s:2', row_id:'r3', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    texts = [f"{r['role']}:{r['text']}:{r.get('local_id','')}" for r in result]
    # Both "Processing..." prose rows must survive (distinct local_ids)
    prose_rows = [r for r in texts if r.startswith("prose:")]
    assert len(prose_rows) == 2, f"Expected 2 prose rows, got {len(prose_rows)}: {prose_rows}"
    # Tool row must also be present
    tool_rows = [r for r in texts if r.startswith("tool:")]
    assert len(tool_rows) == 1, f"Expected 1 tool row, got {len(tool_rows)}: {tool_rows}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_coalesces_same_identity_echo():
    """Same local_id prose row with same text must be coalesced to one row."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Processing step 1', local_id:'live-prose:same', row_id:'rs', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Processing step 1', local_id:'live-prose:same', row_id:'rs', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    assert len(result) == 1, f"Expected 1 row, got {len(result)}: {result}"
    assert result[0]["local_id"] == "live-prose:same"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_two_projections_two_mirrors():
    """Two projected prose rows + two settled mirrors with DIFFERENT IDs.
    Mirrors consumed by provenance (text-key matching) → both distinct-identity
    projections survive even though their IDs differ from the settled rows."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map([
  [1, [
    // Settled mirrors have DIFFERENT local_ids than projected rows (realistic production)
    {role:'prose', text:'Processing...', local_id:'settled-prose:1', row_id:'settled-m1', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Processing...', local_id:'settled-prose:2', row_id:'settled-m2', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]]
]); }
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Processing...', local_id:'prose:s:1', row_id:'p1', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Processing...', local_id:'prose:s:2', row_id:'p2', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    # Both distinct-identity prose rows survive (mirrors consumed by text-key matching)
    assert len(result) == 2, f"Expected 2 rows (2 projections + 2 mirrors consumed), got {len(result)}: {result}"
    local_ids = {r["local_id"] for r in result}
    assert local_ids == {"prose:s:1", "prose:s:2"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_custom_non_live_projected_id():
    """Custom (non-live- prefixed) durable IDs survive settlement dedup."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Custom ID prose', local_id:'custom-id-1', row_id:'cr1', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Custom ID prose', local_id:'custom-id-2', row_id:'cr2', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    # Both custom-ID rows survive (no live- prefix required for identity dedup)
    assert len(result) == 2, f"Expected 2 rows, got {len(result)}: {result}"
    local_ids = {r["local_id"] for r in result}
    assert local_ids == {"custom-id-1", "custom-id-2"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_nested_identity_variants():
    """identity.local_id, identity.row_id, identity.event_id all work."""
    cases = [
        ({"local_id": "nested-local"}, "identity.local_id"),
        ({"row_id": "nested-row"}, "identity.row_id"),
        ({"event_id": "nested-event"}, "identity.event_id"),
    ]
    for identity_val, label in cases:
        script = (
            _SETTLEMENT_JS_BOOT
            + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Nested variant', identity:"""
            + json.dumps(identity_val)
            + """, row_id:'n1', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id, identity:r.identity}));
process.stdout.write(JSON.stringify(rows));
"""
        )
        result = _run_node_script(script)
        assert len(result) == 1, f"{label}: Expected 1 row, got {len(result)}: {result}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_same_id_latest_value_enrichment():
    """Same-ID projected row with different text keeps the LATEST value
    (enrichment, not stale-first)."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    {role:'prose', text:'Initial processing...', local_id:'live-prose:updating', row_id:'ru', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Updated processing...', local_id:'live-prose:updating', row_id:'ru', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    assert len(result) == 1, f"Expected 1 row (enriched), got {len(result)}: {result}"
    assert result[0]["text"] == "Updated processing...", f"Expected latest text, got: {result[0]['text']}"
    assert result[0]["local_id"] == "live-prose:updating"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_near_overlap_mirror_matching():
    """Settled mirror with >=80 char text that's a near-overlap of a projected
    row's text is consumed as a mirror (one-for-one)."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map([
  [1, [
    // Settled mirror with >80 char text that contains the projected text
    {role:'prose', text:'The quick brown fox jumps over the lazy dog near the river bank while the sun sets in the west creating a beautiful orange glow in the sky.', local_id:'settled-near:1', row_id:'sm1', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]]
]); }
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'Final answer',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    // Projected row with shorter but contained text (>80 chars on both sides)
    {role:'prose', text:'The quick brown fox jumps over the lazy dog near the river bank while the sun sets in the west creating a beautiful', local_id:'prose:near:1', row_id:'p1', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    # The mirror is consumed → only 1 projected row survives
    assert len(result) == 1, f"Expected 1 row (projection, mirror consumed via near-overlap), got {len(result)}: {[r['text'][:50] for r in result]}"
    assert result[0]["local_id"] == "prose:near:1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_final_answer_exact_and_prefix_removed():
    """Existing final-answer guards still work: exact match and near-prefix
    final-answer echoes are removed from settlement."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'The final answer is 42.', id:'asst-1'},
];
const projectedScene = {
  mode:'compact_worklog',
  final_answer:'The final answer is 42.',
  identity:{source_message_refs:['asst-1']},
  lifecycle:{},
  activity_rows:[
    // Near-overlap final-answer echo (long text >=80 chars, ≥90% ratio, pre-tool → survives)
    {role:'prose', text:'Once upon a time in a far away land there lived a wise old programmer who wrote clean code.', local_id:'echo-near', row_id:'en', source_event_type:'token', kind:'process_prose', status:'completed'},
    // Legitimate intermediate prose (short prefix, <80% overlap, must survive)
    {role:'prose', text:'The final', local_id:'legitimate-short-prefix', row_id:'lp', source_event_type:'token', kind:'process_prose', status:'completed'},
    // Exact final-answer echo (removed by exact final-answer match regardless of segment)
    {role:'prose', text:'The final answer is 42.', local_id:'echo-exact', row_id:'ee', source_event_type:'token', kind:'process_prose', status:'completed'},
    // Tool row — separates pre-tool narration from final segment
    {role:'tool', text:'Fetched data', local_id:'live-tool:1', row_id:'tr', tool_call_id:'tc-1', status:'completed'},
    // Near-prefix final-answer echo (now in FINAL segment → must be removed)
    {role:'prose', text:'The final answer is ', local_id:'live-prose:echo-prefix', row_id:'ep', source_event_type:'token', kind:'process_prose', status:'completed'},
    // Prose with different content (post-tool, distinct, must survive)
    {role:'prose', text:'Intermediate step description', local_id:'intermediate', row_id:'ir', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    texts = [r["text"] for r in result]
    # Exact final-answer echo removed
    assert "The final answer is 42." not in texts, f"Exact final-answer echo should be removed: {texts}"
    # Near-prefix final-answer echo (shorter prefix in final segment) removed
    near_prefix = next((t for t in texts if t == "The final answer is "), None)
    assert near_prefix is None, f"Near-prefix final-answer echo should be removed: {texts}"
    # Near-overlap final-answer echo (long text >=80 chars, ≥90% ratio with nothing) — NOT caught because final answer <80 chars
    near_overlap = next((t for t in texts if t.startswith("Once upon a time")), None)
    assert near_overlap is not None, f"Near-overlap prose (no 80-char partner) should survive: {texts}"
    # Short prefix (<80%) survives
    assert "The final" in texts, f"Short prefix should survive: {texts}"
    # Tool row survives
    assert "Fetched data" in texts, f"Tool row should survive: {texts}"
    # Different-content prose survives
    assert "Intermediate step description" in texts, f"Intermediate prose should survive: {texts}"
    # Total count check: 4 rows (tool + intermediate + short prefix + near-overlap)
    assert len(result) == 4, f"Expected 4 rows (tool + intermediate + short prefix + near-overlap), got {len(result)}: {texts}"
