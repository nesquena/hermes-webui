"""Regression tests for workspace subagent transparency UI."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"
STYLE_CSS_PATH = REPO_ROOT / "static" / "style.css"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def test_active_session_subagent_rows_collects_lineage_children_without_duplicates():
    js = WORKSPACE_JS_PATH.read_text(encoding="utf-8")
    source = f"""
const src = {js!r};
function extractConst(name) {{
  const re = new RegExp('const\\\\s+' + name + '\\\\s*=\\\\s*new Map\\\\(\\\\)\\\\s*;');
  const match = src.match(re);
  if (!match) throw new Error(name + ' const not found');
  return match[0];
}}
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
global.S = {{
  session: {{ session_id:'tip', _lineage_root_id:'root' }}
}};
global._allSessions = [
  {{session_id:'tip', title:'Tip', _child_sessions:[{{session_id:'child-a', title:'Child A', relationship_type:'child_session', parent_session_id:'tip', updated_at:15}}]}},
  {{session_id:'child-a', title:'Child A', relationship_type:'child_session', parent_session_id:'tip', _parent_lineage_root_id:'root', updated_at:15}},
  {{session_id:'child-b', title:'Child B', relationship_type:'child_session', parent_session_id:'older', _parent_lineage_root_id:'root', updated_at:20}},
  {{session_id:'other', title:'Other', relationship_type:'child_session', parent_session_id:'elsewhere', _parent_lineage_root_id:'elsewhere', updated_at:99}},
];
eval(extractConst('_subagentTransparencyDetailCache'));
eval(extractConst('_subagentTransparencyPending'));
eval(extractFunc('_activeSessionSubagentRows'));
console.log(JSON.stringify(_activeSessionSubagentRows()));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["child-b", "child-a"]


def test_workspace_js_contains_subagent_transparency_filters_and_detail_fetch():
    js = WORKSPACE_JS_PATH.read_text(encoding="utf-8")
    assert "Subagent transparency" in js
    assert "filterSubagentTransparencyLog" in js
    assert "data-subagent-log-filter=\"tool\"" in js
    assert "/api/session?session_id=" in js
    assert "Recent artifacts" in js


def test_workspace_subagent_transparency_styles_exist():
    css = STYLE_CSS_PATH.read_text(encoding="utf-8")
    assert ".workspace-subagent-card" in css
    assert ".workspace-subagent-log-filter.active" in css
    assert ".workspace-subagent-log-row" in css
    assert ".workspace-subagent-state-running" in css
