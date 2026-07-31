"""Production-composed regressions for the scoped sidebar lineage index."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(body: str):
    result = subprocess.run(
        [NODE],
        input=body,
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def _harness(body: str) -> str:
    js = (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const start = src.search(new RegExp('function\\\\s+' + name + '\\\\s*\\\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start), depth = 1; i++;
  while (depth && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
eval(extractFunc('_sessionProfileScope'));
eval(extractFunc('_sidebarLineageSourceBucket'));
eval(extractFunc('_isReadOnlySession'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_authoritativeLineageTipId'));
eval(extractFunc('_buildSidebarLineageIndex'));
eval(extractFunc('_sidebarActiveSessionIdentityKey'));
eval(extractFunc('_sidebarIdentityMatchesActiveSession'));
eval(extractFunc('_sidebarSessionMatchesActiveSession'));
eval(extractFunc('_sessionLineageContainsSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
{body}
"""


def test_reported_fork_and_delegate_rows_stay_stable_across_rebuilds():
    result = _run_node(_harness("""
const root = {session_id:'root', title:'Renamed root', profile_scope:'work', project_id:'projA'};
const delegated = {session_id:'delegate', title:'Delegated child', profile_scope:'work',
  relationship_type:'child_session', read_only:true, parent_session_id:'root'};
const fork = {session_id:'fork', title:'Writable fork', profile_scope:'work',
  session_source:'fork', relationship_type:'child_session', parent_session_id:'root', project_id:null};
function render(rows, refs) {
  const index = _buildSidebarLineageIndex(rows, refs);
  return rows.map(row => ({id:row.session_id, project:index.projectFor(row),
    scope:index.scopeKey(row), linkable:index.isLinkable(row)}));
}
const narrow = render([root, delegated, fork], []);
const restored = render([{...root, title:'Renamed root'}, delegated, fork], [root]);
console.log(JSON.stringify({narrow, restored}));
"""))
    assert result["narrow"] == result["restored"]
    assert result["narrow"][1]["project"] == "projA"
    assert result["narrow"][2]["project"] is None
    assert result["narrow"][1]["linkable"]


def test_four_gate_defects_are_closed_by_production_index_and_collapse():
    result = _run_node(_harness("""
const parent = {session_id:'same', profile_scope:'p', project_id:'A'};
const fork = {session_id:'fork', profile_scope:'p', session_source:'fork',
  relationship_type:'child_session', parent_session_id:'same', project_id:null};
const delegate = {session_id:'delegate', profile_scope:'p', relationship_type:'child_session',
  read_only:true, parent_session_id:'same'};
const profileB = {session_id:'same', profile_scope:'other', project_id:'B'};
const tipA = {session_id:'tip-a', profile_scope:'p', project_id:'A', _lineage_root_id:'same'};
const tipB = {session_id:'tip-b', profile_scope:'other', project_id:'B', _lineage_root_id:'same'};
const index = _buildSidebarLineageIndex([parent, fork, delegate, profileB, tipA, tipB], []);
const collapsed = _collapseSessionLineageForSidebar([tipA, tipB], index);
console.log(JSON.stringify({fork:index.ownership(fork), delegate:index.ownership(delegate),
  collapsed:collapsed.map(row=>row.session_id), scopes:[index.scopeKey(tipA), index.scopeKey(tipB)]}));
"""))
    assert result["fork"]["status"] == "resolved_null"
    assert result["delegate"]["project"] == "A"
    assert result["collapsed"] == ["tip-a", "tip-b"]
    assert result["scopes"][0] != result["scopes"][1]


@pytest.mark.parametrize("size", [100, 500, 1000, 2000])
def test_nested_lineage_resolution_is_linear(size):
    result = _run_node(_harness(f"""
const rows = [{{session_id:'root', profile_scope:'p', project_id:'proj'}}];
for(let i=1;i<{size};i++) rows.push({{session_id:'n'+i, profile_scope:'p',
  relationship_type:'child_session', read_only:true, parent_session_id:i===1?'root':'n'+(i-1)}});
const index = _buildSidebarLineageIndex(rows, []);
const last = rows[rows.length-1];
console.log(JSON.stringify({{project:index.projectFor(last), visits:index.stats.nodeVisits,
  edges:index.stats.edgeVisits, linkable:index.isLinkable(last)}}));
"""))
    assert result["project"] == "proj"
    assert result["linkable"]
    assert result["visits"] <= size
    assert result["edges"] <= size


def test_snapshot_lineage_index_contract_and_invalid_identity_are_distinct():
    result = _run_node(_harness("""
const root = {session_id:'root', profile_scope:'p', project_id:null};
const missing = {session_id:'missing', profile_scope:'p', relationship_type:'child_session',
  read_only:true, parent_session_id:'unknown'};
const index = _buildSidebarLineageIndex([root, missing], []);
console.log(JSON.stringify({root:index.ownership(root), missing:index.ownership(missing),
  rootKey:index.identityKey(root,'same'), missingKey:index.identityKey(missing,'same')}));
"""))
    assert result["root"]["status"] == "resolved_null"
    assert result["missing"]["status"] == "missing"
    assert result["rootKey"] != result["missingKey"]


def test_same_profile_project_duplicates_fail_closed_before_attachment():
    result = _run_node(_harness("""
const parentA = {session_id:'parent', profile_scope:'work', project_id:'projA'};
const parentB = {session_id:'parent', profile_scope:'work', project_id:'projB'};
const delegate = {session_id:'delegate', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'parent'};
const tipA = {session_id:'tipA', profile_scope:'work', project_id:'projA', _lineage_root_id:'same'};
const tipB = {session_id:'tipB', profile_scope:'work', project_id:'projB', _lineage_root_id:'same'};
const index = _buildSidebarLineageIndex([parentA, parentB, delegate, tipA, tipB], []);
const collapsed = _collapseSessionLineageForSidebar([tipA, tipB], index);
console.log(JSON.stringify({delegate:index.ownership(delegate),
  project:index.projectFor(delegate)===undefined?'invalid':index.projectFor(delegate),
  collapsed:collapsed.map(row=>row.session_id)}));
"""))
    assert result["delegate"]["status"] == "ambiguous"
    assert result["project"] == "invalid"
    assert result["collapsed"] == ["tipA", "tipB"]


def test_active_session_matching_uses_scoped_lineage_identity():
    result = _run_node(_harness("""
global.S = {session:{session_id:'same', profile_scope:'work', project_id:'projA'}};
const rowA = {session_id:'same', profile_scope:'work', project_id:'projA'};
const rowB = {session_id:'same', profile_scope:'work', project_id:'projB'};
const index = _buildSidebarLineageIndex([rowA, rowB], []);
console.log(JSON.stringify({same:_sessionLineageContainsSession(rowA, 'same', index), other:_sessionLineageContainsSession(rowB, 'same', index)}));
"""))
    assert result == {"same": True, "other": False}


def test_compression_parent_lookup_stays_within_profile_scope():
    result = _run_node(_harness("""
const parentA = {session_id:'parent', profile_scope:'A', project_id:'projA',
  pre_compression_snapshot:true, updated_at:10};
const tipA = {session_id:'tipA', profile_scope:'A', parent_session_id:'parent',
  project_id:'projA', updated_at:20};
const parentB = {session_id:'parent', profile_scope:'B', project_id:'projB',
  pre_compression_snapshot:true, updated_at:30};
const rows = [parentA, tipA, parentB];
const index = _buildSidebarLineageIndex(rows, []);
const collapsed = _collapseSessionLineageForSidebar(rows, index);
console.log(JSON.stringify(collapsed.map(row => ({
  id: row.session_id, profile: row.profile_scope, count: row._lineage_collapsed_count || 1
}))));
"""))
    assert result == [
        {"id": "tipA", "profile": "A", "count": 2},
        {"id": "parent", "profile": "B", "count": 1},
    ]


def test_attachment_accepts_the_collapsed_index_identity_without_rescoping():
    result = _run_node(_harness("""
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const root = {session_id:'root', profile_scope:'work', project_id:'projA',
  _lineage_root_id:'root', _lineage_tip_id:'tip'};
const tip = {session_id:'tip', profile_scope:'work', project_id:'projA',
  _lineage_root_id:'root', _lineage_tip_id:'tip'};
const child = {session_id:'child', profile_scope:'work', relationship_type:'child_session',
  read_only:true, parent_session_id:'tip', _parent_lineage_root_id:'root'};
const raw = [root, tip, child];
const index = _buildSidebarLineageIndex(raw, []);
const collapsed = _collapseSessionLineageForSidebar(raw, index);
const attached = _attachChildSessionsToSidebarRows(collapsed, raw, [], undefined, index);
console.log(JSON.stringify(attached.map(row => ({
  id: row.session_id, children: (row._child_sessions || []).map(item => item.session_id)
}))));
"""))
    assert result == [{"id": "tip", "children": ["child"]}]
