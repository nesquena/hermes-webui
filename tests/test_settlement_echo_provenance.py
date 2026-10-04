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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_projected_running_thinking_with_settled_replacement():
    """A projected RUNNING thinking row that is replaced by settled thinking
    must not reserve a mirror slot: the projected row is discarded by
    _anchorSceneSettleLiveRunningRow() at settlement, so if its text had been
    counted as a projected mirror, the settled replacement would be consumed
    as a mirror and the thinking would disappear entirely. Exactly one
    completed/settled thinking row must remain."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map([
  [1, [
    // Settled replacement thinking (production: settled rows carry their own
    // independently derived durable IDs, NOT the projected row's local_id).
    {role:'thinking', text:'Reasoning about the request', local_id:'settled-thinking:1', row_id:'st1', source_event_type:'reasoning', status:'completed'},
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
    // Projected RUNNING thinking row — hasSettledThinking=true, so settlement
    // discards it. Its text must NOT allocate a mirror slot.
    {role:'thinking', text:'Reasoning about the request', local_id:'live-thinking:1', row_id:'lt1', source_event_type:'reasoning', status:'running'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id, status:r.status}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    result = _run_node_script(script)
    thinking_rows = [r for r in result if r["role"] == "thinking"]
    # Exactly ONE thinking row remains — the settled replacement, completed.
    assert len(thinking_rows) == 1, f"Expected exactly 1 thinking row, got {len(thinking_rows)}: {result}"
    assert thinking_rows[0]["status"] == "completed", f"Expected completed settled thinking, got: {thinking_rows[0]}"
    assert thinking_rows[0]["local_id"] == "settled-thinking:1", f"Expected the settled replacement, got: {thinking_rows[0]}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_opaque_stream_owned_row_sealed():
    """Opaque/provider-ID stream-owned rows (no `live-` prefix, but with a
    stream owner and no settled assistant index) are still live identities:
    a running row of this shape is sealed to completed at settlement instead
    of leaking through as `running` (restored master fallback)."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map(); }
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
// Full payload/tool assertion matrix: every role shape of an opaque
// stream-owned running row must seal status, payload.status and (tool only)
// payload.done / tool.done, and must NOT fabricate fields for other roles.
const cases = [
  {
    name:'thinking',
    row:{role:'thinking', text:'Provider reasoning', local_id:'provider-call-abc123', row_id:'pr1', stream_id:'stream-1', source_event_type:'reasoning', status:'running', payload:{status:'running', text:'Provider reasoning'}},
  },
  {
    name:'prose',
    row:{role:'prose', text:'Provider prose', local_id:'provider-call-def456', row_id:'pr2', stream_id:'stream-1', source_event_type:'token', kind:'process_prose', status:'running', payload:{status:'running', text:'Provider prose'}},
  },
  {
    name:'tool',
    row:{role:'tool', text:'Fetched data', local_id:'provider-tool-ghi789', row_id:'pr3', tool_call_id:'tc-1', stream_id:'stream-1', source_event_type:'tool', status:'running', payload:{status:'running', done:false}, tool:{name:'fetch', done:false}},
  },
];
const out = cases.map(c => {
  const scene = _completeSettledAnchorSceneForTurn(messages, 1, {
    mode:'compact_worklog',
    final_answer:'Final answer',
    identity:{source_message_refs:['asst-1']},
    lifecycle:{},
    activity_rows:[c.row],
  });
  const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id, status:r.status, payload:r.payload, tool:r.tool}));
  return {name:c.name, count:rows.length, row:rows[0] || null};
});
process.stdout.write(JSON.stringify(out));
"""
    )
    result = _run_node_script(script)
    by_role = {case["name"]: case for case in result}
    assert sorted(by_role) == ["prose", "thinking", "tool"], f"Expected all three role shapes, got: {by_role}"
    for name in ("thinking", "prose", "tool"):
        case = by_role[name]
        assert case["count"] == 1, f"[{name}] Expected 1 row, got {case['count']}: {case}"
        row = case["row"]
        assert row["status"] == "completed", f"[{name}] Opaque stream-owned running row must be sealed to completed, got: {row}"
        assert row["payload"]["status"] == "completed", f"[{name}] payload.status must be sealed too, got: {row['payload']}"
        # payload payload (non-tool) fields survive the seal untouched
        assert row["payload"].get("text") in (None, "Provider reasoning", "Provider prose"), f"[{name}] payload payload fields must survive: {row['payload']}"
        if name == "tool":
            assert row["payload"]["done"] is True, f"[tool] payload.done must be sealed, got: {row['payload']}"
            assert row["tool"]["done"] is True, f"[tool] tool.done must be sealed, got: {row['tool']}"
            assert row["tool"]["name"] == "fetch", f"[tool] tool payload fields must survive: {row['tool']}"
        else:
            # No fabricated tool completion state on textual roles.
            assert "done" not in row["payload"], f"[{name}] must not gain payload.done, got: {row['payload']}"
            assert not (row.get("tool") or {}), f"[{name}] must not gain a tool object, got: {row.get('tool')}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_assistant_index_negative_control():
    """A stream-owned row that ALSO carries a settled assistant index is NOT a
    live identity (negative control): its running status must NOT be sealed,
    because it belongs to a settled message, not the live projection."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map(); }
const messages = [
  {role:'user', content:'Prompt', id:'user-1'},
  {role:'assistant', content:'Final answer', id:'asst-1'},
];
// Negative-control matrix: same three role shapes, each ALSO carrying a
// settled assistant index → not a live identity, so nothing may be sealed.
const cases = [
  {
    name:'thinking',
    row:{role:'thinking', text:'Settled-index thinking', local_id:'settled-idx:1', row_id:'si1', stream_id:'stream-1', group:{assistant_msg_idx:1}, source_event_type:'reasoning', status:'running', payload:{status:'running'}},
  },
  {
    name:'prose',
    row:{role:'prose', text:'Settled-index prose', local_id:'settled-idx:2', row_id:'si2', stream_id:'stream-1', group:{assistant_msg_idx:1}, source_event_type:'token', kind:'process_prose', status:'running', payload:{status:'running'}},
  },
  {
    name:'tool',
    row:{role:'tool', text:'Settled-index tool', local_id:'settled-idx:3', row_id:'si3', tool_call_id:'tc-9', stream_id:'stream-1', group:{assistant_msg_idx:1}, source_event_type:'tool', status:'running', payload:{status:'running', done:false}, tool:{name:'fetch', done:false}},
  },
];
const out = cases.map(c => {
  const scene = _completeSettledAnchorSceneForTurn(messages, 1, {
    mode:'compact_worklog',
    final_answer:'Final answer',
    identity:{source_message_refs:['asst-1']},
    lifecycle:{},
    activity_rows:[c.row],
  });
  const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id, status:r.status, payload:r.payload, tool:r.tool}));
  return {name:c.name, count:rows.length, row:rows[0] || null};
});
process.stdout.write(JSON.stringify(out));
"""
    )
    result = _run_node_script(script)
    by_role = {case["name"]: case for case in result}
    assert sorted(by_role) == ["prose", "thinking", "tool"], f"Expected all three role shapes, got: {by_role}"
    for name in ("thinking", "prose", "tool"):
        case = by_role[name]
        assert case["count"] == 1, f"[{name}] Expected 1 row, got {case['count']}: {case}"
        row = case["row"]
        # Not a live identity → settle leaves the running status untouched
        # (no fabricated sealing of a settled-index row), for payload/tool too.
        assert row["status"] == "running", f"[{name}] Assistant-index row must NOT be sealed, got: {row}"
        assert row["payload"]["status"] == "running", f"[{name}] payload.status must NOT be sealed, got: {row['payload']}"
        if name == "tool":
            assert row["payload"]["done"] is False, f"[tool] payload.done must NOT be sealed, got: {row['payload']}"
            assert row["tool"]["done"] is False, f"[tool] tool.done must NOT be sealed, got: {row['tool']}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_mirror_capacity_is_role_scoped_thinking_first():
    """Adversarial regression (real _completeSettledAnchorSceneForTurn):
    mirror capacity must be keyed by ROLE + normalized text, never by text
    alone.

    Production shape: the content-parts path emits prose and thinking rows as
    independent rows with their own durable IDs and never rejects identical
    text across roles. So a settled THINKING row (thinking-first order) and a
    settled PROSE row can both share the exact normalized text of a surviving
    PROJECTED PROSE row. With text-only capacity the settled thinking row
    consumes the single slot, the real settled prose row survives (duplicate
    prose) and the thinking is lost.
    """
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map([
  [1, [
    // Thinking-first: settled THINKING precedes the settled prose mirror.
    // Raw text differs from the projected row; normalized text is identical.
    {role:'thinking', text:'Checking   The Cache Layer', local_id:'settled-thinking:9', row_id:'st9', source_event_type:'reasoning', status:'completed'},
    // Genuine settled prose mirror of the projected prose row below,
    // with its own independent durable ID.
    {role:'prose', text:'checking the cache layer', local_id:'settled-prose:9', row_id:'sp9', source_event_type:'token', kind:'process_prose', status:'completed'},
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
    // Surviving PROJECTED prose row sharing the normalized text above.
    {role:'prose', text:'  Checking the cache layer ', local_id:'live-prose:9', row_id:'lp9', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id, status:r.status}));
process.stdout.write(JSON.stringify({
  rows:rows,
  projected_text:'  Checking the cache layer ',
  settled_thinking_text:'Checking   The Cache Layer',
  settled_prose_text:'checking the cache layer',
}));
"""
    )
    data = _run_node_script(script)
    rows = data["rows"]
    norm = lambda value: " ".join(str(value).split()).lower()
    assert (
        norm(data["projected_text"])
        == norm(data["settled_thinking_text"])
        == norm(data["settled_prose_text"])
    ), f"Fixture precondition: normalized texts must be identical, got {data}"
    prose_rows = [r for r in rows if r["role"] == "prose"]
    thinking_rows = [r for r in rows if r["role"] == "thinking"]
    # ONLY the prose mirror may be consumed: the projected prose row survives
    # once, and the real settled prose echo is suppressed.
    assert len(prose_rows) == 1, (
        f"Expected exactly 1 prose row (settled prose echo consumed), got {len(prose_rows)}: {rows}"
    )
    assert prose_rows[0]["local_id"] == "live-prose:9", f"Expected the projected prose row to survive, got: {prose_rows[0]}"
    # ...and the settled THINKING row must survive with its own identity.
    assert len(thinking_rows) == 1, f"Expected the settled thinking row to survive, got {len(thinking_rows)}: {rows}"
    assert thinking_rows[0]["local_id"] == "settled-thinking:9", f"Expected the settled thinking row, got: {thinking_rows[0]}"
    assert thinking_rows[0]["status"] == "completed", f"Expected completed settled thinking, got: {thinking_rows[0]}"
    assert len(rows) == 2, f"Expected exactly 2 rows (1 prose + 1 thinking), got {len(rows)}: {rows}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settlement_repeated_same_id_snapshot_allocates_single_mirror_slot():
    """Control for the role+text re-keying: repeated projections of the SAME
    durable identity (snapshot resends) must still allocate exactly ONE mirror
    slot per identity, so only one settled echo is consumed — not one per
    snapshot. Guards against the re-keying inflating mirror capacity."""
    script = (
        _SETTLEMENT_JS_BOOT
        + """
function _anchorSceneRowsByMessageIndex(){ return new Map([
  [1, [
    {role:'prose', text:'Processing the snapshot', local_id:'settled-prose:a', row_id:'spa', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Processing the snapshot', local_id:'settled-prose:b', row_id:'spb', source_event_type:'token', kind:'process_prose', status:'completed'},
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
    // Two snapshots of the SAME durable identity and SAME normalized text.
    {role:'prose', text:'Processing the snapshot', local_id:'live-prose:dup', row_id:'rd', source_event_type:'token', kind:'process_prose', status:'completed'},
    {role:'prose', text:'Processing the snapshot', local_id:'live-prose:dup', row_id:'rd', source_event_type:'token', kind:'process_prose', status:'completed'},
  ]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
const rows = (scene && scene.activity_rows || []).map(r => ({role:r.role, text:r.text, local_id:r.local_id}));
process.stdout.write(JSON.stringify(rows));
"""
    )
    rows = _run_node_script(script)
    assert len(rows) == 2, (
        f"Repeated same-ID snapshots must hold ONE slot (1 projected + 1 settled echo), got {len(rows)}: {rows}"
    )
    projected = [r for r in rows if r["local_id"] == "live-prose:dup"]
    assert len(projected) == 1, f"Snapshots of one identity must coalesce into one row, got: {rows}"
    settled = [r for r in rows if r["local_id"].startswith("settled-prose:")]
    assert [r["local_id"] for r in settled] == ["settled-prose:b"], (
        f"Exactly one settled echo (the second) must survive, got: {settled}"
    )
