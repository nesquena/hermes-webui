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


def test_stale_optimistic_compression_tips_collapse_even_when_parents_are_visible():
    """Active compression can leave old streaming tips in browser memory.

    The server/index already expose only the latest tip, but client-side
    optimistic rows from previous tips may still include parent_session_id links.
    Those rows carry explicit lineage metadata and must collapse as one sidebar
    conversation instead of rendering 7/8/9/10 segment duplicates.
    """
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
  {{session_id:'seg7', title:'Graphify', parent_session_id:'seg6', message_count:1141, updated_at:70, last_message_at:70, _lineage_root_id:'root', _compression_segment_count:7}},
  {{session_id:'seg8', title:'Graphify', parent_session_id:'seg7', message_count:1254, updated_at:80, last_message_at:80, _lineage_root_id:'root', _compression_segment_count:8, pending_user_message:'old'}},
  {{session_id:'seg9', title:'Graphify', parent_session_id:'seg8', message_count:1404, updated_at:90, last_message_at:90, _lineage_root_id:'root', _compression_segment_count:9, active_stream_id:'old-stream'}},
  {{session_id:'seg10', title:'Graphify', parent_session_id:'seg9', message_count:1490, updated_at:100, last_message_at:100, _lineage_root_id:'root', _compression_segment_count:10, active_stream_id:'current-stream'}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = json.loads(_run_node(source))
    assert [row["session_id"] for row in collapsed] == ["seg10"]
    assert collapsed[0]["_lineage_collapsed_count"] == 4
    assert collapsed[0]["_compression_segment_count"] == 10
    assert [seg["session_id"] for seg in collapsed[0]["_lineage_segments"]] == ["seg10", "seg9", "seg8", "seg7"]


def test_sidebar_lineage_collapse_prefers_highest_compression_segment_over_touched_parent():
    """A touched parent segment must not hide the newer compressed tip.

    Opening or polling an older segment can refresh its updated_at without adding
    messages. The collapsed sidebar row must still pick the highest compression
    segment, otherwise the visible chat jumps back to a parent that lacks the
    completed assistant answer.
    """
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
  {{session_id:'seg13', title:'Schaue dir die Release (fork)', message_count:2490, updated_at:200, last_message_at:200, _lineage_root_id:'root', _compression_segment_count:13}},
  {{session_id:'seg14', title:'Schaue dir die Release (fork)', message_count:2532, updated_at:150, last_message_at:150, _lineage_root_id:'root', _compression_segment_count:14}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = json.loads(_run_node(source))
    assert [row["session_id"] for row in collapsed] == ["seg14"]
    assert collapsed[0]["_lineage_collapsed_count"] == 2
    assert [seg["session_id"] for seg in collapsed[0]["_lineage_segments"]] == ["seg14", "seg13"]



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


def test_cross_surface_webui_child_session_remains_top_level_when_parent_is_messaging():
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
eval(extractFunc('_isChildSession'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const collapsed = [{{session_id:'telegram_parent', title:'Telegram parent', source_label:'Telegram'}}];
const raw = [
  collapsed[0],
  {{
    session_id:'webui_tip',
    title:'Current WebUI continuation',
    parent_session_id:'telegram_parent',
    relationship_type:'child_session',
    parent_source:'telegram',
    source_label:'Telegram',
    session_source:'messaging',
    raw_source:'telegram',
    _cross_surface_child_session:true,
  }},
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["telegram_parent", "webui_tip"]
    assert rows[1].get("_orphan_child_session") is True
    assert "_child_sessions" not in rows[0]


def test_session_segment_count_prefers_visible_collapsed_backend_and_materialized_counts():
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
eval(extractFunc('_sessionSegmentCount'));
const cases = [
  _sessionSegmentCount({{_lineage_collapsed_count:3, _compression_segment_count:2, _lineage_segments:[{{session_id:'a'}}, {{session_id:'b'}}]}}),
  _sessionSegmentCount({{_compression_segment_count:25}}),
  _sessionSegmentCount({{_lineage_segments:[{{session_id:'tip'}}, {{session_id:'root'}}, {{session_id:'older'}}]}}),
  _sessionSegmentCount({{_lineage_collapsed_count:1, _compression_segment_count:1}}),
  _sessionSegmentCount(null),
];
console.log(JSON.stringify(cases));
"""
    assert json.loads(_run_node(source)) == [3, 25, 3, 0, 0]


def test_sidebar_lineage_segment_badge_is_localized():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "session-lineage-count" in js
    assert "const segmentCount=_sessionSegmentCount(s);" in js
    assert "t('session_meta_segments', segmentCount)" in js
    assert "titleRow.appendChild(segmentCountEl);" in js
    assert ".session-lineage-count{" in css


def test_lineage_segment_expansion_static_contract():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "const _expandedLineageKeys = new Set();" in js
    assert "session-lineage-count,.session-lineage-segments,.session-lineage-segment" in js
    assert "segmentCountEl.setAttribute('aria-expanded'" in js
    assert "_expandedLineageKeys.has(lineageKey)" in js
    assert "_expandedLineageKeys.add(lineageKey)" in js
    assert "_expandedLineageKeys.delete(lineageKey)" in js
    assert "className='session-lineage-segments'" in js
    assert "className='session-lineage-segment'" in js
    assert "const segTitle=seg.title||t('session_lineage_segment_untitled');" in js
    assert "row.title=t('session_lineage_segment_open');" in js
    assert "await loadSession(seg.session_id);" in js
    assert ".session-lineage-count.expandable{" in css
    assert ".session-lineage-count.expandable:hover" in css
    assert ".session-lineage-segments{" in css
    assert ".session-lineage-segment{" in css


def test_active_hidden_lineage_segment_auto_expands_parent():
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
const _expandedChildSessionKeys = new Set();
const _expandedLineageKeys = new Set();
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_syncSidebarExpansionForActiveSession'));
const rows = [{{
  session_id:'seg10',
  _lineage_key:'root',
  _lineage_segments:[
    {{session_id:'seg10', updated_at:100}},
    {{session_id:'seg9', updated_at:90}},
    {{session_id:'seg8', updated_at:80}},
  ],
}}];
_syncSidebarExpansionForActiveSession(rows, 'seg8');
console.log(JSON.stringify({{lineage:[..._expandedLineageKeys], child:[..._expandedChildSessionKeys]}}));
"""
    assert json.loads(_run_node(source)) == {"lineage": ["root"], "child": []}


def test_lineage_segment_locale_keys_are_defined_for_sidebar_locales():
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    required = [
        "session_meta_segments:",
        "session_lineage_segment_untitled:",
        "session_lineage_segment_open:",
    ]
    locale_count = i18n.count("session_meta_messages:")
    for key in required:
        assert i18n.count(key) >= locale_count, f"{key} missing from one or more locale blocks"
