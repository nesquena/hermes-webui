"""#7653 re-gate round 16 — the browser keys combine owner AND occurrence.

Round 15 scoped the browser's render/dedup keys by occurrence, but each of the
three key builders reads the owner FIRST and returns as soon as it has one, so
a row naming BOTH coordinates only ever used the owner:

    if(owner!=null) return `call:${id}@${owner}`;      // occurrence ignored
    if(occ!=null)   return `call:${id}#${occ}`;

That was sufficient while every occurrence of an id lived in a DIFFERENT
assistant message. This round's server repair makes one assistant message emit
an id twice (the reviewer's ``call_0: echo FIRST`` then ``call_0: exit 3``), so
the server now hands the browser rows that name owner 1 AND occurrences 0 and
1. An owner-only browser key gives those two rows the same key again and
``_anchorSceneRowsForRendering`` collapses them back into one card — the exact
defect item 3 describes, one layer further down.

The three key builders must therefore combine the coordinates, matching the
server's ``tool:<tid>@<owner>#<occ>``:

- ``static/ui.js`` ``_anchorSceneToolRowLogicalKey`` (scene render key, both
  settled renderers)
- ``static/ui.js`` ``_toolIdentity`` (live worklog step dedup)
- ``static/ui.js`` ``_toolDisclosureIdentity`` (worklog disclosure state)

Every other shape is unchanged: owner-only rows keep ``call:<tid>@<owner>``,
occurrence-only (live stream-born) rows keep ``call:<tid>#<occ>``, and a row
naming neither keeps the bare ``call:<tid>``.

These tests drive the REAL ui.js functions (extracted from source, never
re-implemented).
"""

import json
import shutil
from pathlib import Path

import pytest

from tests import _node_script
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def js(*names):
    """Real source of the named ui.js functions — never a re-implementation."""
    return "\n".join(extract_function(UI_JS, name) for name in names)


def run_node(script):
    # The harness embeds real ui.js functions; run it from a file so the argv
    # budget can never bite (see tests/_node_script.py).
    out = _node_script.run_node_script(script, node=str(NODE), cwd=ROOT, timeout=30)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout
    return out.stdout


def _row(owner, occurrence, command, is_error):
    """A hydrated scene row naming BOTH its owner and its occurrence."""
    return {
        "role": "tool",
        "kind": "tool_completed",
        "status": "error" if is_error else "completed",
        "tool_call_id": "call_0",
        "name": "terminal",
        "group": {"assistant_msg_idx": owner},
        "payload": {"assistant_msg_idx": owner, "tid": "call_0", "occurrence": occurrence},
        "tool": {
            "id": "call_0",
            "name": "terminal",
            "args": {"command": command},
            "snippet": command,
            "done": True,
            "is_error": is_error,
        },
    }


def json_row(occurrence, owner, command, is_error):
    """Serialise one hydrated row for a node harness literal."""
    return json.dumps(_row(owner, occurrence, command, is_error))


# The reviewer's repro as the server now emits it: ONE assistant message (owner
# 1) emitting ``call_0`` twice, occurrences 0 and 1.
SCENE_JS = """
const scene={final_answer:'done',activity_rows:[
%s,
%s
]};
""" % (
    json_row(0, 1, "echo FIRST", False),
    json_row(1, 1, "exit 3", True),
)


# ---------------------------------------------------------------------------
# _anchorSceneToolRowLogicalKey
# ---------------------------------------------------------------------------


def test_render_key_combines_owner_and_occurrence():
    """RED core: two occurrences sharing one owner must not share a key."""
    script = js("_anchorSceneToolRowLogicalKey") + """
const assert=require('assert');
const rows=%s;
const keys=rows.map(r=>_anchorSceneToolRowLogicalKey(r));
assert.notStrictEqual(keys[0],keys[1],
  'two occurrences of one id in ONE assistant message must produce DISTINCT keys, got '+JSON.stringify(keys));
assert.strictEqual(keys[0],'call:call_0@1#0',keys[0]);
assert.strictEqual(keys[1],'call:call_0@1#1',keys[1]);
console.log('OK');
""" % ("[" + json_row(0, 1, "echo FIRST", False) + "," + json_row(1, 1, "exit 3", True) + "]")
    run_node(script)


def test_render_key_keeps_the_settled_renderer_at_two_cards():
    """The end-to-end surface: the settled renderer must not collapse them."""
    script = js(
        "_anchorSceneToolRowLogicalKey",
        "_anchorSceneMergeToolRows",
        "_anchorSceneIsSettledSuccessfulCompression",
        "_anchorSceneProseMatchesFinalAnswer",
        "_anchorSceneProseDuplicatesFinalAnswer",
        "_anchorSceneRowsForRendering",
    ) + SCENE_JS + """
const assert=require('assert');
const rows=_anchorSceneRowsForRendering(scene,{settled:true});
const tools=rows.filter(r=>r.role==='tool');
assert.strictEqual(tools.length,2,
  'two occurrences of one id in one assistant message must stay two cards, got '+tools.length);
const byCmd={};
for(const r of tools){ byCmd[(r.tool&&r.tool.args&&r.tool.args.command)]=r; }
assert.strictEqual(byCmd['echo FIRST']!==undefined,true,'the echo FIRST card is missing');
assert.strictEqual(byCmd['exit 3']!==undefined,true,'the exit 3 card is missing');
const err=(r)=>!!((r.tool&&r.tool.is_error)||(r.payload&&r.payload.is_error)||r.status==='error');
assert.strictEqual(err(byCmd['echo FIRST']),false,'echo FIRST must stay successful');
assert.strictEqual(err(byCmd['exit 3']),true,'exit 3 must stay failed');
console.log('OK');
"""
    run_node(script)


def test_render_key_owner_only_row_is_unchanged():
    """Non-goal control: a row naming an owner but no occurrence is unchanged."""
    script = js("_anchorSceneToolRowLogicalKey") + """
const assert=require('assert');
const row={role:'tool',tool_call_id:'call_0',group:{assistant_msg_idx:4},payload:{assistant_msg_idx:4}};
assert.strictEqual(_anchorSceneToolRowLogicalKey(row),'call:call_0@4',
  'a legacy owner-scoped row must keep the pre-round-16 key');
console.log('OK');
"""
    run_node(script)


def test_render_key_occurrence_only_row_is_unchanged():
    """Non-goal control: live stream-born rows keep the occurrence-only key."""
    script = js("_anchorSceneToolRowLogicalKey") + """
const assert=require('assert');
const live=(occ)=>({role:'tool',tool_call_id:'call_0',payload:{occurrence:occ}});
assert.strictEqual(_anchorSceneToolRowLogicalKey(live(0)),'call:call_0#0');
assert.strictEqual(_anchorSceneToolRowLogicalKey(live(1)),'call:call_0#1');
assert.strictEqual(_anchorSceneToolRowLogicalKey({role:'tool',tool_call_id:'call_0'}),'call:call_0');
console.log('OK');
"""
    run_node(script)


def test_render_key_same_occurrence_twice_still_collapses():
    """Non-goal control: the same occurrence seen twice is still one card."""
    script = js(
        "_anchorSceneToolRowLogicalKey",
        "_anchorSceneMergeToolRows",
        "_anchorSceneIsSettledSuccessfulCompression",
        "_anchorSceneProseMatchesFinalAnswer",
        "_anchorSceneProseDuplicatesFinalAnswer",
        "_anchorSceneRowsForRendering",
    ) + """
const assert=require('assert');
const scene={final_answer:'done',activity_rows:[
%s,
%s
]};
const rows=_anchorSceneRowsForRendering(scene,{settled:true});
assert.strictEqual(rows.filter(r=>r.role==='tool').length,1,
  'the same occurrence seen twice must still collapse to one card');
console.log('OK');
""" % (json_row(0, 1, "echo FIRST", False), json_row(0, 1, "echo FIRST", False))
    run_node(script)


# ---------------------------------------------------------------------------
# _toolIdentity / _toolDisclosureIdentity — the live worklog step dedup
# ---------------------------------------------------------------------------


def test_worklog_step_identity_combines_owner_and_occurrence():
    """Two occurrences of one id in one step are two cards, not one."""
    script = js("_toolIdentity", "_toolCardOwnerIndex", "_toolCardOccurrence") + """
const assert=require('assert');
const cards=[
  {tid:'call_0',name:'terminal',group:{assistant_msg_idx:1},payload:{occurrence:0},args:{command:'echo FIRST'}},
  {tid:'call_0',name:'terminal',group:{assistant_msg_idx:1},payload:{occurrence:1},args:{command:'exit 3'}},
];
const keys=cards.map(_toolIdentity);
assert.notStrictEqual(keys[0],keys[1],
  'two occurrences of one id in one worklog step must be distinct, got '+JSON.stringify(keys));
assert.strictEqual(keys[0],'id:call_0@1#0',keys[0]);
assert.strictEqual(keys[1],'id:call_0@1#1',keys[1]);
console.log('OK');
"""
    run_node(script)


def test_worklog_disclosure_identity_combines_owner_and_occurrence():
    """Toggling one occurrence's disclosure must not open the other's."""
    script = js(
        "_toolDisclosureIdentity",
        "_toolCardOwnerIndex",
        "_toolCardOccurrence",
        "_worklogDetailHashKey",
    ) + """
const assert=require('assert');
const cards=[
  {tid:'call_0',name:'terminal',group:{assistant_msg_idx:1},payload:{occurrence:0}},
  {tid:'call_0',name:'terminal',group:{assistant_msg_idx:1},payload:{occurrence:1}},
];
const keys=cards.map(_toolDisclosureIdentity);
assert.notStrictEqual(keys[0],keys[1],
  'two occurrences of one id must not share a disclosure key, got '+JSON.stringify(keys));
console.log('OK');
"""
    run_node(script)


def test_worklog_identity_owner_only_row_is_unchanged():
    """Non-goal control: an owner-only card keeps the round-15 key."""
    script = js("_toolIdentity", "_toolCardOwnerIndex", "_toolCardOccurrence") + """
const assert=require('assert');
assert.strictEqual(_toolIdentity({tid:'call_0',group:{assistant_msg_idx:4}}),'id:call_0@4');
assert.strictEqual(_toolIdentity({tid:'call_0',payload:{occurrence:2}}),'id:call_0#2');
assert.strictEqual(_toolIdentity({tid:'call_0'}),'id:call_0');
console.log('OK');
"""
    run_node(script)


# ---------------------------------------------------------------------------
# Settle path: _completeSettledAnchorSceneForTurn + _anchorSceneExistingRowKey
# ---------------------------------------------------------------------------

MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

_SETTLE_EXTRACT_JS = """
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
"""

_SETTLE_BOOT = _SETTLE_EXTRACT_JS + """
const fs = require('fs');
const src = fs.readFileSync(SOURCE_PATH, 'utf8');
global.window = { chatActivityMode(){ return 'compact_worklog'; }, _chatActivityDisplayMode: 'compact_worklog', };
global.S = { session: {} };
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
function _anchorSceneFinalAnswerText(message){ return message && (message.final_answer || message.content || ''); }
function _anchorSceneRowsByMessageIndex(){ return new Map(); }
function _anchorSceneMessageRef(message){ return String(message && message.id || ''); }
function _anchorSceneTurnDurationForSettlement(_lastAsst, base){ return (base && base.turn_duration) ? base.turn_duration : 0; }
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
""".replace(
    "SOURCE_PATH",
    json.dumps(str(ROOT / "static" / "messages.js")),
)


def _run_settle(script):
    assert NODE, "node not on PATH"
    # The settle harness evals real messages.js functions; run it from a file
    # so the argv budget can never bite (see tests/_node_script.py).
    out = _node_script.run_node_script(script, node=str(NODE), cwd=ROOT, timeout=30)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    return json.loads(out.stdout)


def _assert_settle(script):
    """Run an assertion-style harness (it prints OK) under node from a file."""
    assert NODE, "node not on PATH"
    out = _node_script.run_node_script(script, node=str(NODE), cwd=ROOT, timeout=30)
    assert out.returncode == 0, f"node harness failed:\n{out.stderr}\n{out.stdout}"
    assert "OK" in out.stdout, out.stdout


def test_existing_row_key_combines_owner_and_occurrence():
    """The settle-path key must not collapse two occurrences of one owner."""
    script = js_settle("_anchorSceneExistingRowKey") + """
const assert=require('assert');
const mk=(occ,cmd,err)=>({role:'tool',kind:'tool_completed',status:err?'error':'completed',
  tool_call_id:'call_0',name:'terminal',group:{assistant_msg_idx:1},
  payload:{assistant_msg_idx:1,tid:'call_0',occurrence:occ},
  tool:{id:'call_0',name:'terminal',args:{command:cmd},snippet:cmd,done:true,is_error:err}});
const k0=_anchorSceneExistingRowKey(mk(0,'echo FIRST',false));
const k1=_anchorSceneExistingRowKey(mk(1,'exit 3',true));
assert.notStrictEqual(k0,k1,'two occurrences of one owner must be distinct, got '+k0+' / '+k1);
assert.strictEqual(k0,'tool:call_0@1#0',k0);
assert.strictEqual(k1,'tool:call_0@1#1',k1);
// unchanged shapes
assert.strictEqual(_anchorSceneExistingRowKey({role:'tool',tool_call_id:'call_0',group:{assistant_msg_idx:4}}),'tool:call_0@4');
assert.strictEqual(_anchorSceneExistingRowKey({role:'tool',tool_call_id:'call_0',payload:{occurrence:2}}),'tool:call_0#2');
assert.strictEqual(_anchorSceneExistingRowKey({role:'tool',tool_call_id:'call_0'}),'tool:call_0');
console.log('OK');
"""
    _assert_settle(script)


def js_settle(*names):
    """Real source of the named messages.js functions — never a re-implementation."""
    return "\n".join(extract_function(MESSAGES_JS, name) for name in names)


def test_settle_keeps_two_occurrences_of_one_owner_apart():
    """The reviewer's repro through the real settle path: one assistant
    message emitting ``call_0`` twice must stay two cards with their own
    verdicts."""
    script = _SETTLE_BOOT + """
const messages = [
  {role:'user', content:'go', id:'user-1'},
  {role:'assistant', content:'both', id:'asst-1'},
  {role:'assistant', content:'Final answer', id:'asst-final'},
];
function _anchorSceneRowsByMessageIndex(){
  return new Map([
    [1, [{role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'completed',
          tool_call_id:'call_0', row_id:'settled:1', group:{assistant_msg_idx:1},
          payload:{assistant_msg_idx:1, tid:'call_0', occurrence:0, snippet:'FIRST'},
          tool:{id:'call_0', name:'terminal', args:{command:'echo FIRST'}, snippet:'FIRST', done:true, is_error:false}},
         {role:'tool', kind:'tool_completed', source_event_type:'tool_complete', status:'error',
          tool_call_id:'call_0', row_id:'settled:1b', group:{assistant_msg_idx:1},
          payload:{assistant_msg_idx:1, tid:'call_0', occurrence:1, snippet:'3'},
          tool:{id:'call_0', name:'terminal', args:{command:'exit 3'}, snippet:'3', done:true, is_error:true}}]],
  ]);
}
const projectedScene = {
  mode:'compact_worklog', final_answer:'Final answer',
  identity:{source_message_refs:['asst-final']}, lifecycle:{},
  activity_rows:[]
};
const scene = _completeSettledAnchorSceneForTurn(messages, 2, projectedScene);
const tools = (scene && scene.activity_rows || []).filter(r => r.role === 'tool');
process.stdout.write(JSON.stringify(tools.map(r => ({
  key: _anchorSceneExistingRowKey(r),
  cmd: (r.tool && r.tool.args && r.tool.args.command) || (r.payload && r.payload.args && r.payload.args.command),
  occ: (r.payload && r.payload.occurrence!==undefined) ? r.payload.occurrence : null,
  err: !!((r.tool && r.tool.is_error) || (r.payload && r.payload.is_error) || r.status === 'error'),
}))));
"""
    tools = _run_settle(script)
    assert len(tools) == 2, f"expected two tool cards, got {tools}"
    by_cmd = {t["cmd"]: t for t in tools}
    assert set(by_cmd) == {"echo FIRST", "exit 3"}, f"got {tools}"
    assert by_cmd["echo FIRST"]["err"] is False, (
        f"echo FIRST must stay successful: {tools}"
    )
    assert by_cmd["exit 3"]["err"] is True, f"exit 3 must stay failed: {tools}"
    assert by_cmd["echo FIRST"]["key"] != by_cmd["exit 3"]["key"], (
        f"the two occurrences must not share a key: {tools}"
    )
