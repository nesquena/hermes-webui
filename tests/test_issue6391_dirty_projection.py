"""Deterministic contracts for bounded live-scene projection and Worklog reuse."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _run_node(body: str) -> None:
    assert NODE, "node is required for live-scene projection contracts"
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const anchors = path.join(path.dirname(process.argv[1]), 'assistant_turn_anchors.js');
require(anchors);
const api = globalThis.HermesAssistantTurnAnchors;
const uiSource = fs.readFileSync(process.argv[1], 'utf8');
function extract(source, name) {
  const start = source.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, name + ' must exist');
  let cursor = source.indexOf('{', start), depth = 1;
  for (cursor += 1; depth && cursor < source.length; cursor += 1) {
    if (source[cursor] === '{') depth += 1;
    if (source[cursor] === '}') depth -= 1;
  }
  return source.slice(start, cursor);
}
''' + body
    result = subprocess.run(
        [NODE, "-e", script, str(ROOT / "static" / "ui.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_live_projection_reuses_clean_prefix_and_scans_only_dirty_tail():
    _run_node(
        r'''
const registry = api.createAssistantTurnAnchorRegistry({session_id: 's', stream_id: 'stream'});
for (let i = 0; i < 200; i += 1) {
  const result = api.applyAssistantTurnAnchorSourceEvent(registry, {
    source_event_type: 'tool',
    event_id: 'event-' + i,
    seq: i + 1,
    payload: {id: 'tool-' + i, name: 'terminal', args: {i}},
  }, {session_id: 's', stream_id: 'stream'});
  assert.equal(result.applied, true);
}
const first = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
assert.equal(first.activity_rows.length, 200);
assert.equal(first.projection_stats.events_scanned, 200);
assert.equal(first.projection_stats.full_rebuild, true);
assert.equal(Object.hasOwn(JSON.parse(JSON.stringify(registry)),'projection_stats'),false);
assert.equal(Object.hasOwn(JSON.parse(JSON.stringify(first)),'projection'),false);
assert.equal(Object.hasOwn(JSON.parse(JSON.stringify(first)),'projection_stats'),false);
const firstRow = first.activity_rows[0];
const firstLast = first.activity_rows[199];
const clean=api.projectAssistantTurnAnchorActivityScene(registry, {mode:'compact_worklog'});
assert.equal(clean.projection.full_rebuild,false);
assert.equal(clean.projection_stats.events_scanned,0);
assert.strictEqual(clean.activity_rows,first.activity_rows);
const unchanged = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
assert.strictEqual(unchanged, clean, 'repeated clean projection must be returned by identity');
assert.equal(registry.projection_stats.events_scanned, 0);
const next = api.applyAssistantTurnAnchorSourceEvent(registry, {
  source_event_type: 'tool_complete',
  event_id: 'event-201',
  seq: 201,
  payload: {id: 'tool-200', result: 'ok'},
}, {session_id: 's', stream_id: 'stream'});
assert.equal(next.applied, true);
const second = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
assert.equal(second.activity_rows.length, 201);
assert.strictEqual(second.activity_rows[0], firstRow);
assert.strictEqual(second.activity_rows[199], firstLast);
assert.equal(second.projection_stats.events_scanned, 1);
assert.equal(second.projection_stats.rows_rebuilt, 1);
assert.equal(second.projection_stats.full_rebuild, false);
for(let i=202;i<=203;i++) api.applyAssistantTurnAnchorSourceEvent(registry,{
 source_event_type:'tool',event_id:'event-'+i,seq:i,payload:{id:'tool-'+i,name:'terminal'},
},{session_id:'s',stream_id:'stream'});
const burst=api.projectAssistantTurnAnchorActivityScene(registry,{mode:'compact_worklog'});
assert.equal(burst.projection_stats.events_scanned,2,'a two-event frame must not force full projection');
assert.equal(burst.projection_stats.full_rebuild,false);
'''
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_projection_fails_closed_to_full_rebuild_on_uncertain_order_or_identity():
    _run_node(
        r'''
const registry = api.createAssistantTurnAnchorRegistry({session_id: 's', stream_id: 'stream'});
api.applyAssistantTurnAnchorSourceEvent(registry, {
  source_event_type: 'tool', event_id: 'known', seq: 1,
  payload: {id: 'known-tool', name: 'terminal'},
}, {session_id: 's', stream_id: 'stream'});
api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
const uncertain = api.applyAssistantTurnAnchorSourceEvent(registry, {
  source_event_type: 'tool',
  payload: {name: 'terminal'},
}, {session_id: 's', stream_id: 'stream'});
assert.equal(uncertain.applied, true);
const scene = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
assert.equal(scene.projection_stats.full_rebuild, true);
assert.equal(scene.projection_stats.fallback_reason, 'uncertain_identity_or_order');
assert.equal(scene.activity_rows.length, 2);
'''
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_external_activity_replacement_requires_explicit_projection_invalidation_hook():
    _run_node(
        r'''
const registry = api.createAssistantTurnAnchorRegistry({session_id: 's', stream_id: 'stream'});
api.applyAssistantTurnAnchorSourceEvent(registry, {
  source_event_type: 'tool', event_id: 'event-1', seq: 1,
  payload: {id: 'tool-1', name: 'terminal', preview: 'before'},
}, {session_id: 's', stream_id: 'stream'});
const first = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
registry.anchor.activity_events[0] = {
  ...registry.anchor.activity_events[0],
  payload: {...registry.anchor.activity_events[0].payload, preview: 'after'},
};
api.invalidateAssistantTurnAnchorActivityProjection(registry, {indices: [0]});
const second = api.projectAssistantTurnAnchorActivityScene(registry, {mode: 'compact_worklog'});
assert.equal(second.activity_rows[0].payload.preview, 'after');
assert.strictEqual(second.activity_rows[0].row_id, first.activity_rows[0].row_id);
assert.equal(second.projection_stats.events_scanned, 1);
assert.equal(second.projection_stats.rows_rebuilt, 1);
assert.equal(second.projection_stats.full_rebuild, false);
'''
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_compact_worklog_reuses_group_and_clean_nodes_without_clearing_list():
    _run_node(
        r'''
const source = fs.readFileSync(process.argv[1], 'utf8');
const render = extract(source, '_renderAnchorSceneRowsIntoWorklog');
const list = {
  children: [],
  appendChild(node) { this.children.push(node); node.parentElement = this; return node; },
  querySelector() { return null; },
};
const group = {
  _testList: list,
  querySelector(selector) { return selector === '.tool-worklog-list' ? this._testList : null; },
};
let created = 0;
function nodeForRow(row) {
  created += 1;
  return {
    dataset: {},
    parentElement: null,
    setAttribute(name, value) { this[name] = String(value); },
    getAttribute(name) { return this[name] || ''; },
    classList: {contains() { return false;}},
    rowKey: row.row_id,
  };
}
global._toolWorklogListEl = (value) => value && value._testList;
global._anchorSceneNodeForRow = nodeForRow;
global._syncToolCallGroupSummary = () => {};
global.document = {createElement() { return {
  children: [],
  className: '',
  appendChild(node) { this.children.push(node); node.parentElement = this; return node; },
  setAttribute() {},
}; }};
global._anchorSceneWorklogProjectionStats = null;
eval(extract(source, '_anchorSceneToolRowLogicalKey'));
eval(extract(source, '_anchorSceneWorklogRowKey'));
eval(extract(source, '_anchorSceneWorklogState'));
eval(extract(source, '_anchorSceneWorklogAppendRow'));
eval(render);
const rows = Array.from({length: 100}, (_, i) => ({role: 'prose', row_id: 'row-' + i}));
assert.equal(_renderAnchorSceneRowsIntoWorklog(group, rows, {
  live: true, settled: false,
  projection: {revision: 1, full_rebuild: true, changed_row_keys: rows.map(row => row.row_id)},
}), true);
const firstNodes = list.children.slice();
const firstCreated = created;
assert.equal(firstCreated, 100);
assert.equal(_renderAnchorSceneRowsIntoWorklog(group, rows, {
  live: true, settled: false,
  projection: {revision: 1, full_rebuild: false, changed_row_keys: []},
}), true);
assert.equal(created, firstCreated, 'a clean frame must reuse every existing node');
assert.deepEqual(list.children, firstNodes, 'a clean frame must not clear or reorder the list');
assert.equal(_renderAnchorSceneRowsIntoWorklog(group, rows.concat({role: 'tool', row_id: 'row-100'}), {
  live: true, settled: false,
  projection: {revision: 2, full_rebuild: false, changed_row_keys: ['row-100']},
}), true);
assert.equal(created, firstCreated + 1, 'append must only create the dirty tail node');
assert.strictEqual(list.children[0], firstNodes[0]);
assert.equal(list.children.length, 101);
const updatedRows = rows.concat({role: 'tool', row_id: 'row-100'});
updatedRows[0] = {role: 'prose', row_id: 'row-0', text: 'updated'};
assert.equal(_renderAnchorSceneRowsIntoWorklog(group, updatedRows, {
  live: true, settled: false,
  projection: {
    revision: 3,
    full_rebuild: false,
    changed_row_keys: ['prose:row-0'],
    changed_output_indices: [0],
  },
}), true);
assert.equal(created, firstCreated + 2, 'an updated dirty row rebuilds only itself');
assert.notStrictEqual(list.children[0], firstNodes[0]);
assert.equal(list.children.length, 101);
const committedNodes=list.children.slice(),committedCreated=created;
_renderAnchorSceneRowsIntoWorklog(group,updatedRows,{
 live:true,settled:false,
 projection:{revision:3,full_rebuild:false,changed_row_keys:['prose:row-0'],changed_output_indices:[0]},
});
assert.equal(created,committedCreated,'a second reader of the same revision must not repaint');
assert.deepEqual(list.children,committedNodes);
assert.deepEqual(group._anchorSceneProjectionStats, {
  rows_scanned: 102,
  rows_rebuilt: 102,
  groups_scanned: 5,
  groups_rebuilt: 1,
  groups_created: 0,
  groups_reused: 5,
});
'''
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_ui_scene_projection_cache_skips_clean_history_and_reads_only_dirty_tail():
    _run_node(
        r'''
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('const _anchorSceneRenderProjectionCaches');
const end = source.indexOf('function _anchorSceneToolCallFromRow', start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);
eval(source.slice(start, end));
const rows = Array.from({length: 200}, (_, i) => ({
  role: 'prose', row_id: 'row-' + i, text: 'text-' + i,
}));
const identity = {session_id: 's', turn_id: 't', stream_id: 'stream'};
const first = _anchorSceneRowsForRendering({
  identity, mode: 'compact_worklog', activity_rows: rows,
  projection: {
    revision: 1, full_rebuild: true,
    changed_row_keys: rows.map(row => 'prose:' + row.row_id),
    changed_row_indices: rows.map((_, i) => i),
  },
}, {settled: false});
assert.equal(first.length, 200);
const clean = _anchorSceneRowsForRendering({
  identity, mode: 'compact_worklog', activity_rows: rows,
  projection: {revision: 1, full_rebuild: false, changed_row_keys: []},
}, {settled: false});
assert.strictEqual(clean, first, 'clean scene projection must reuse its cached rows');
assert.deepEqual(clean._anchorSceneProjection.changed_row_keys, []);
const nextRows = rows.concat({role: 'prose', row_id: 'row-200', text: 'tail'});
let indexedReads = 0;
const boundedRows = new Proxy(nextRows, {
  get(target, property, receiver) {
    if (/^[0-9]+$/.test(String(property))) indexedReads += 1;
    return Reflect.get(target, property, receiver);
  },
});
const dirty = _anchorSceneRowsForRendering({
  identity, mode: 'compact_worklog', activity_rows: boundedRows,
  projection: {
    revision: 2, full_rebuild: false,
    changed_row_keys: ['prose:row-200'], changed_row_indices: [200],
  },
}, {settled: false});
assert.equal(indexedReads, 1, 'dirty tail projection must read only the changed source row');
assert.equal(dirty.length, 201);
assert.strictEqual(dirty[0], first[0]);
assert.strictEqual(dirty[199], first[199]);
assert.equal(dirty[200].row_id, 'row-200');
const cleanAgain = _anchorSceneRowsForRendering({
  identity, mode: 'compact_worklog', activity_rows: boundedRows,
  projection: {
    revision: 2, full_rebuild: false,
    changed_row_keys: ['prose:row-200'], changed_row_indices: [200],
  },
}, {settled: false});
assert.strictEqual(cleanAgain, dirty);
// Reading a projection must not consume another renderer's pending dirty set.
assert.deepEqual(cleanAgain._anchorSceneProjection.changed_row_keys, ['prose:row-200']);
assert.equal(indexedReads, 1, 'clean projection must not rescan the history');
nextRows[0]={role:'prose',row_id:'row-0',text:'updated prefix'};
indexedReads=0;
const prefix = _anchorSceneRowsForRendering({
 identity,mode:'compact_worklog',activity_rows:boundedRows,
 projection:{revision:3,full_rebuild:false,changed_row_keys:['prose:row-0'],changed_row_indices:[0]},
},{settled:false});
assert.equal(prefix[0].text,'updated prefix');
assert.equal(indexedReads,1,'known same-key prefix update must not scan historical rows');
const replacement=_anchorSceneRowsForRendering({
 identity,mode:'compact_worklog',activity_rows:[{role:'prose',row_id:'replacement',text:'new registry'}],
 projection:{revision:3,full_rebuild:true,changed_row_keys:['prose:replacement'],changed_row_indices:[0]},
},{settled:false});
assert.equal(replacement.length,1,'same stream new registry must invalidate old projection');
assert.equal(replacement[0].text,'new registry');

'''
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_transparent_mounted_projection_only_touches_dirty_nodes_and_fade_tail():
    _run_node(r'''
const source=fs.readFileSync(process.argv[1],'utf8');
eval(extract(source,'_tryIncrementalTransparentAnchorPaint'));
globalThis.window={_showThinking:true,_fadeTextEffect:false};
let captures=0,restores=0,created=0,reads=0;
function _captureMessageScrollSnapshot(){captures++;return {};}
function _restoreMessageScrollSnapshotSameFrame(){restores++;}
function _anchorSceneRenderOutputKey(row){return 'prose:'+row.row_id;}
function _transparentLiveRowsCompatible(){return true;}
function _refreshTransparentLiveRow(old,node){old.text=node.text;return old;}
function _anchorSceneTransparentNodeForRow(row){created++;return node(row.text);}
function _transparentEventCountLabel(count){return String(count);}
function node(text){return {text,parentElement:null,setAttribute(){},removeAttribute(){}};}
const label=node(''),bar={querySelector(){return label;},setAttribute(){}};
const blocks={querySelector(){return bar;},querySelectorAll(){throw Error('history DOM scan');}};
const nodes=Array.from({length:200},(_,i)=>node('text-'+i));nodes.forEach(n=>n.parentElement=blocks);
const turn={};
blocks._anchorTransparentProjectionState={streamId:'s',sessionId:'a',flags:'[true,false]',revision:1,keys:Array.from({length:200},(_,i)=>'prose:row-'+i),nodes,toolCount:0};
const values=Array.from({length:200},(_,i)=>({role:'prose',row_id:'row-'+i,text:'text-'+i}));
values[0]={...values[0],text:'new text'};
const rows=new Proxy(values,{get(target,key){if(/^\d+$/.test(String(key)))reads++;return target[key];}});
rows._anchorSceneProjection={revision:2,full_rebuild:false,changed_output_indices:[0]};
const old=nodes.slice();
assert.equal(_tryIncrementalTransparentAnchorPaint(turn,blocks,rows,{streamId:'s',sessionId:'a'}),true);
assert.equal(reads,1);assert.equal(created,1);assert.equal(captures,1);assert.equal(restores,1);
assert.equal(nodes[0].text,'new text');assert.deepEqual(nodes,old);
assert.equal(_tryIncrementalTransparentAnchorPaint(turn,blocks,rows,{streamId:'s',sessionId:'a'}),true);
assert.equal(reads,1);assert.equal(created,1);assert.equal(captures,1);assert.equal(restores,1);
''')
