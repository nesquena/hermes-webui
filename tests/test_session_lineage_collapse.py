"""Regression tests for sidebar lineage collapse helpers."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    # Pass source via stdin rather than `-e <source>` argv — the latter is
    # capped at MAX_ARG_STRLEN (131072 bytes on Linux) and tests that embed
    # the entire sessions.js file can exceed that. stdin has no such limit.
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


def test_sidebar_lineage_collapse_keeps_latest_tip_and_counts_segments():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = f"""
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
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
const sessions = [
  {{session_id:'root', title:'Hermes WebUI', message_count:10, updated_at:10, last_message_at:10, _lineage_root_id:'root', _lineage_tip_id:'root'}},
  {{session_id:'tip', title:'Hermes WebUI', message_count:20, updated_at:20, last_message_at:20, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
  {{session_id:'solo', title:'Other', message_count:5, updated_at:15, last_message_at:15}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = json.loads(_run_node(source))
    by_sid = {row["session_id"]: row for row in collapsed}
    assert set(by_sid) == {"tip", "solo"}
    assert by_sid["tip"]["_lineage_collapsed_count"] == 2
    assert [seg["session_id"] for seg in by_sid["tip"]["_lineage_segments"]] == ["tip", "root"]


def test_sidebar_active_state_can_fall_back_to_url_session_during_boot():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = f"""
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
global.S = {{ session: null }};
global.window = {{ location: {{ pathname: '/session/url-active', search: '', hash: '' }} }};
eval(extractFunc('_sessionIdFromLocation'));
eval(extractFunc('_activeSessionIdForSidebar'));
console.log(_activeSessionIdForSidebar());
"""
    assert _run_node(source) == "url-active"


def test_collapsed_lineage_contains_active_hidden_segment():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = f"""
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
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_sessionLineageContainsSession'));
const sessions = [
  {{session_id:'root', title:'Hermes WebUI', message_count:10, updated_at:10, last_message_at:10, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
  {{session_id:'tip', title:'Hermes WebUI', message_count:20, updated_at:20, last_message_at:20, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify({{sid: collapsed[0].session_id, containsRoot: _sessionLineageContainsSession(collapsed[0], 'root')}}));
"""
    result = _run_node(source)
    assert '"sid":"tip"' in result
    assert '"containsRoot":true' in result


def test_sidebar_attaches_child_sessions_to_collapsed_hidden_parent_lineage():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = f"""
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
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const raw = [
  {{session_id:'root', title:'Root', updated_at:10, last_message_at:10, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
  {{session_id:'tip', title:'Tip', updated_at:20, last_message_at:20, _lineage_root_id:'root', _lineage_tip_id:'tip'}},
  {{session_id:'child', title:'Subtask', parent_session_id:'tip', relationship_type:'child_session', _parent_lineage_root_id:'root', updated_at:30, last_message_at:30}},
];
const collapsed = _collapseSessionLineageForSidebar(raw);
const attached = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(attached));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["tip"]
    assert rows[0]["_child_session_count"] == 1
    assert rows[0]["_child_sessions"][0]["session_id"] == "child"
