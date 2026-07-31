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
        [NODE], input=body, cwd=ROOT, capture_output=True, text=True, timeout=30
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
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_authoritativeLineageTipId'));
if(src.includes('function _buildSidebarLineageIndex(')) {{
  eval(extractFunc('_buildSidebarLineageIndex'));
}} else {{
  eval(extractFunc('_buildSidebarLineageProjectResolver'));
  eval(extractFunc('_sidebarLineageScopeKey'));
  eval(extractFunc('_sidebarScopedIdentityKey'));
}}
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
if(!src.includes('function _buildSidebarLineageIndex(')) {{
  const _legacyProjectResolver=_buildSidebarLineageProjectResolver;
    globalThis._buildSidebarLineageIndex = function(rows, refs) {{
    const resolver=_legacyProjectResolver(rows, refs);
    const projectFor=row=>resolver(row);
    const scopeKey=row=>_sidebarLineageScopeKey(row, undefined, resolver);
    const identityKey=(row,id)=>_sidebarScopedIdentityKey(row,id,undefined,resolver);
    return {{projectFor,scopeKey,identityKey,isLinkable:()=>true,
      ownership:row=>({{status:projectFor(row)===null?'resolved_null':'resolved_project',project:projectFor(row)}}),
      stats:{{nodeVisits:0,edgeVisits:0}}}};
    }};
}}
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
