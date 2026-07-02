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
        encoding="utf-8",
        text=True,
        timeout=30,
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


def test_parent_present_webui_compression_child_without_lineage_metadata_collapses():
    """WebUI-native compression continuations may only carry parent_session_id.

    When both the preserved parent snapshot and the new continuation are present
    in the sidebar payload, the continuation should still collapse with its
    parent instead of appearing as a separate branch-like conversation (#2489).
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
  {{session_id:'parent', title:'Long WebUI conversation', message_count:50, updated_at:10, last_message_at:10, pre_compression_snapshot:true}},
  {{session_id:'child', title:'Long WebUI conversation', parent_session_id:'parent', message_count:12, updated_at:20, last_message_at:20}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = json.loads(_run_node(source))
    assert [row["session_id"] for row in collapsed] == ["child"]
    assert collapsed[0]["_lineage_key"] == "parent"
    assert collapsed[0]["_lineage_collapsed_count"] == 2
    assert [seg["session_id"] for seg in collapsed[0]["_lineage_segments"]] == ["child", "parent"]


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



def test_sidebar_lineage_collapse_prefers_current_tip_over_same_segment_snapshot():
    """A preserved parent snapshot can share the child's backend segment count.

    Loading/polling the parent refreshes its timestamp, but the collapsed row
    must still open the non-snapshot continuation tip. Otherwise a reload after
    compression jumps back to the older parent transcript and looks like the
    active conversation disappeared.
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
  {{session_id:'parent', title:'Duplicate Assistant Text Blocks', message_count:64, updated_at:300, last_message_at:300, pre_compression_snapshot:true, _lineage_root_id:'parent', _compression_segment_count:2}},
  {{session_id:'child', title:'Duplicate Assistant Text Blocks', parent_session_id:'parent', message_count:86, updated_at:200, last_message_at:200, _lineage_root_id:'parent', _compression_segment_count:2}},
];
const collapsed = _collapseSessionLineageForSidebar(sessions);
console.log(JSON.stringify(collapsed));
"""
    collapsed = json.loads(_run_node(source))
    assert [row["session_id"] for row in collapsed] == ["child"]
    assert collapsed[0]["_lineage_collapsed_count"] == 2
    assert [seg["session_id"] for seg in collapsed[0]["_lineage_segments"]] == ["child", "parent"]



def test_direct_parent_restore_resolves_to_visible_compression_tip():
    """A stale /session/<parent> URL should reopen the visible continuation tip.

    The sidebar payload may omit the archived pre-compression parent but still
    include the latest continuation with lineage metadata pointing back to the
    parent. Boot restore should use that visible tip instead of loading the old
    parent transcript and making the continuation look lost.
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
var _allSessions = [
  {{session_id:'child', title:'Duplicate Assistant Text Blocks', parent_session_id:'parent', message_count:86, updated_at:200, last_message_at:200, _lineage_root_id:'parent', _compression_segment_count:2}},
  {{session_id:'other', title:'Other', message_count:4, updated_at:100, last_message_at:100}},
];
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sessionLineageContainsSession'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_resolveSessionIdFromSidebarLineage'));
console.log(JSON.stringify({{parent:_resolveSessionIdFromSidebarLineage('parent'), child:_resolveSessionIdFromSidebarLineage('child'), other:_resolveSessionIdFromSidebarLineage('other')}}));
"""
    result = json.loads(_run_node(source))
    assert result == {"parent": "child", "child": "child", "other": "other"}


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
eval(extractFunc('_isForkWithResolvableParent'));
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



def test_cross_surface_subagent_child_stacks_under_visible_webui_parent():
    """Delegate subagent rows are cross-source but still belong under their WebUI parent."""
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
function _isExternalSession(s) {{ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }}
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const collapsed = [{{
  session_id:'webui_parent',
  title:'Parent WebUI conversation',
  raw_source:'webui',
  source_tag:'webui',
  session_source:'webui',
  message_count:3,
}}];
const raw = [
  collapsed[0],
  {{
    session_id:'subagent_child',
    title:'Subagent Session',
    parent_session_id:'webui_parent',
    relationship_type:'child_session',
    raw_source:'subagent',
    source_tag:'subagent',
    session_source:'other',
    source_label:'Subagent',
    _parent_lineage_root_id:'webui_parent',
    _cross_surface_child_session:true,
  }},
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["webui_parent"]
    assert rows[0]["_child_session_count"] == 1
    assert rows[0]["_child_sessions"][0]["session_id"] == "subagent_child"
    assert "_orphan_child_session" not in rows[0]["_child_sessions"][0]



def test_cross_surface_subagent_child_stacks_under_visible_fork_parent():
    """Forked WebUI conversations can still own subagent child rows."""
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
function _isExternalSession(s) {{ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }}
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const collapsed = [{{
  session_id:'fork_parent',
  title:'Forked WebUI conversation',
  session_source:'fork',
  parent_session_id:'original_parent',
  message_count:3,
}}];
const raw = [
  collapsed[0],
  {{
    session_id:'subagent_child',
    title:'Subagent Session',
    parent_session_id:'fork_parent',
    relationship_type:'child_session',
    raw_source:'subagent',
    source_tag:'subagent',
    session_source:'other',
    source_label:'Subagent',
    _parent_lineage_root_id:'fork_parent',
    _cross_surface_child_session:true,
  }},
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["fork_parent"]
    assert rows[0]["_child_session_count"] == 1
    assert rows[0]["_child_sessions"][0]["session_id"] == "subagent_child"


def test_parent_source_label_display_text_does_not_force_external_orphaning():
    """Display-only source labels must not drive machine source classification."""
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
function _isExternalSession(s) {{ return !!(s && (s.is_cli_session || s.session_source === 'messaging')); }}
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const collapsed = [{{session_id:'parent', title:'Parent', source_label:'WebUI Continuation'}}];
const raw = [
  collapsed[0],
  {{session_id:'child', title:'Subagent', parent_session_id:'parent', relationship_type:'child_session', raw_source:'subagent', source_tag:'subagent', session_source:'other', _cross_surface_child_session:true}},
];
const rows = _attachChildSessionsToSidebarRows(collapsed, raw);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["parent"]
    assert rows[0]["_child_session_count"] == 1


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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const collapsed = [{{session_id:'telegram_parent', title:'Telegram parent', session_source:'messaging', raw_source:'telegram', source_label:'Telegram'}}];
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


def test_sidebar_reference_rows_suppress_source_filtered_archived_parent_child():
    """Source-filtered payloads keep archived parents as non-rendered references.

    The hidden archived parent reaches the render via the scoped reference helper
    (`_scopedSidebarReferenceRows`), mirroring how renderSessionListFromCache feeds
    project/source-scoped references into _renderSidebarRowsFromRawSessions.
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
var _showArchived = false;
var NO_PROJECT_FILTER = '__none__';
var _activeProject;
var _sidebarReferenceSessions = [{{session_id:'parent', title:'Archived parent', archived:true, updated_at:10, last_message_at:10, source:'webui'}}];
function _isMessagingSession(s) {{ return false; }}
function _isCliSession(s) {{ return !!(s && (s.source === 'cli' || s.raw_source === 'cli')); }}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_scopedSidebarReferenceRows'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
const child = {{session_id:'child', title:'Subagent', parent_session_id:'parent', relationship_type:'child_session', updated_at:20, last_message_at:20, source:'webui'}};
const refs = _scopedSidebarReferenceRows(false);
const rows = _renderSidebarRowsFromRawSessions([child], [child, ...refs]);
console.log(JSON.stringify(rows));
"""
    assert json.loads(_run_node(source)) == []


def test_sidebar_reference_rows_do_not_suppress_cross_project_child():
    """A cross-project archived parent must NOT hide a visible child in another project.

    Regression for the silent project-scope leak: sidebar_reference_sessions arrive
    project-unscoped from /api/sessions, so an archived parent in project B must not
    enter a project-A-filtered render's suppression context and erase a visible
    project-A fork. _scopedSidebarReferenceRows filters references to the active
    project/source before they feed the render.
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
var _showArchived = false;
var NO_PROJECT_FILTER = '__none__';
var _activeProject = 'projA';
var _sidebarReferenceSessions = [{{session_id:'parent', title:'Archived parent (projB)', archived:true, project_id:'projB', updated_at:10, last_message_at:10, source:'webui'}}];
function _isMessagingSession(s) {{ return false; }}
function _isCliSession(s) {{ return !!(s && (s.source === 'cli' || s.raw_source === 'cli')); }}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_scopedSidebarReferenceRows'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
const child = {{session_id:'child', title:'Fork in projA', parent_session_id:'parent', relationship_type:'child_session', project_id:'projA', message_count:3, updated_at:20, last_message_at:20, source:'webui'}};
const refs = _scopedSidebarReferenceRows(false);
const rows = _renderSidebarRowsFromRawSessions([child], [child, ...refs]);
console.log(JSON.stringify(rows.map(r => r.session_id)));
"""
    assert json.loads(_run_node(source)) == ["child"]



def test_archived_hidden_parent_suppresses_cross_surface_child_orphan():
    """A cross-surface child should not leak when its archived parent is hidden."""
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
var _showArchived = false;
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Archived parent', archived:true, updated_at:10, last_message_at:10}};
const child = {{
  session_id:'child',
  title:'Subagent',
  parent_session_id:'parent',
  relationship_type:'child_session',
  raw_source:'subagent',
  source_tag:'subagent',
  session_source:'other',
  _cross_surface_child_session:true,
  updated_at:20,
  last_message_at:20,
}};
const rows = _attachChildSessionsToSidebarRows([child], [child], [parent, child]);
console.log(JSON.stringify(rows));
"""
    assert json.loads(_run_node(source)) == []


def test_archived_hidden_parent_suppresses_child_and_fork_orphans():
    """Archived parents should not leave child/delegate clutter behind (#4293)."""
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
var _showArchived = false;
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Archived parent', archived:true, updated_at:10, last_message_at:10}};
const child = {{session_id:'child', title:'Child', parent_session_id:'parent', relationship_type:'child_session', updated_at:20, last_message_at:20}};
const fork = {{session_id:'fork', title:'Fork', session_source:'fork', parent_session_id:'parent', updated_at:30, last_message_at:30}};
const visibleRows = [child, fork];
const referenceRows = [parent, child, fork];
const rows = _attachChildSessionsToSidebarRows(visibleRows, visibleRows, referenceRows);
console.log(JSON.stringify(rows));
"""
    assert json.loads(_run_node(source)) == []


def test_archived_hidden_ancestor_suppresses_nested_child_and_fork_orphans():
    """Nested descendants of an archived hidden parent must not leak as orphans (#4293)."""
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
var _showArchived = false;
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Archived parent', archived:true, updated_at:10, last_message_at:10}};
const child = {{session_id:'child', title:'Child', parent_session_id:'parent', relationship_type:'child_session', updated_at:20, last_message_at:20}};
const grandchild = {{session_id:'grandchild', title:'Grandchild', parent_session_id:'child', relationship_type:'child_session', updated_at:30, last_message_at:30}};
const fork1 = {{session_id:'fork1', title:'Fork 1', session_source:'fork', parent_session_id:'parent', updated_at:40, last_message_at:40}};
const fork2 = {{session_id:'fork2', title:'Fork 2', session_source:'fork', parent_session_id:'fork1', updated_at:50, last_message_at:50}};
const visibleRows = [child, grandchild, fork1, fork2];
const referenceRows = [parent, child, grandchild, fork1, fork2];
const rows = _attachChildSessionsToSidebarRows(visibleRows, visibleRows, referenceRows);
console.log(JSON.stringify(rows));
"""
    assert json.loads(_run_node(source)) == []


def test_cross_project_archived_parent_does_not_hide_active_project_fork():
    """Archived parents outside the active project must not suppress an active-project fork (#4293)."""
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
var _showArchived = false;
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
const otherProjectParent = {{session_id:'parent', title:'Other project parent', archived:true, project_id:'other', updated_at:10, last_message_at:10}};
const activeProjectFork = {{session_id:'fork', title:'Active project fork', session_source:'fork', parent_session_id:'parent', project_id:'active', updated_at:20, last_message_at:20}};
const activeProjectRows = [activeProjectFork];
const activeProjectReferenceRows = [activeProjectFork];
const crossProjectReferenceRows = [otherProjectParent, activeProjectFork];
const correctRows = _renderSidebarRowsFromRawSessions(activeProjectRows, activeProjectReferenceRows);
const wrongRows = _renderSidebarRowsFromRawSessions(activeProjectRows, crossProjectReferenceRows);
console.log(JSON.stringify({{correctRows, wrongRows}}));
"""
    result = json.loads(_run_node(source))
    assert [row["session_id"] for row in result["correctRows"]] == ["fork"]
    assert "_orphan_child_session" not in result["correctRows"][0]
    # This documents why render references must be active-project scoped: a
    # cross-project archived parent would make the active-project fork vanish.
    assert result["wrongRows"] == []


def test_inactive_source_tab_count_uses_its_own_archived_parent_references():
    """Inactive source-count renders must not borrow another source's reference rows (#4293)."""
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
var _showArchived = false;
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
const webuiParent = {{session_id:'webui-parent', title:'Archived WebUI parent', archived:true, updated_at:10, last_message_at:10}};
const webuiChild = {{session_id:'webui-child', title:'WebUI child', parent_session_id:'webui-parent', relationship_type:'child_session', updated_at:20, last_message_at:20}};
const cliParent = {{session_id:'cli-parent', title:'Archived CLI parent', archived:true, is_cli_session:true, session_source:'cli', updated_at:30, last_message_at:30}};
const cliChild = {{session_id:'cli-child', title:'CLI child', parent_session_id:'cli-parent', relationship_type:'child_session', is_cli_session:true, session_source:'cli', updated_at:40, last_message_at:40}};
const webuiVisibleRows = [webuiChild];
const cliVisibleRows = [cliChild];
const webuiReferenceRows = [webuiParent, webuiChild];
const cliReferenceRows = [cliParent, cliChild];
const wrongWebuiCount = _renderSidebarRowsFromRawSessions(webuiVisibleRows, cliReferenceRows).length;
const rightWebuiCount = _renderSidebarRowsFromRawSessions(webuiVisibleRows, webuiReferenceRows).length;
const wrongCliCount = _renderSidebarRowsFromRawSessions(cliVisibleRows, webuiReferenceRows).length;
const rightCliCount = _renderSidebarRowsFromRawSessions(cliVisibleRows, cliReferenceRows).length;
console.log(JSON.stringify({{wrongWebuiCount,rightWebuiCount,wrongCliCount,rightCliCount}}));
"""
    counts = json.loads(_run_node(source))
    assert counts == {
        "wrongWebuiCount": 1,
        "rightWebuiCount": 0,
        "wrongCliCount": 1,
        "rightCliCount": 0,
    }


def test_child_archive_render_rebuild_drops_stale_decorated_children():
    """A previous decorated parent copy must not retain an archived child (#4293)."""
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const decoratedParent = {{
  session_id:'parent',
  title:'Parent',
  updated_at:10,
  last_message_at:10,
  _child_sessions:[{{session_id:'archived-child', title:'Archived child'}}],
  _child_session_count:1,
  _child_session_streaming:true,
  _child_session_has_unread:true,
  _child_session_attention:{{kind:'approval', count:1}},
}};
const rows = _attachChildSessionsToSidebarRows([decoratedParent], [decoratedParent]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["parent"]
    assert "_child_sessions" not in rows[0]
    assert "_child_session_count" not in rows[0]
    assert "_child_session_streaming" not in rows[0]
    assert "_child_session_has_unread" not in rows[0]
    assert "_child_session_attention" not in rows[0]


def test_fork_child_with_visible_parent_is_nested_once():
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Parent', updated_at:10, last_message_at:10}};
const fork = {{session_id:'fork1', title:'Fork', session_source:'fork', parent_session_id:'parent', updated_at:20, last_message_at:20}};
const rows = _attachChildSessionsToSidebarRows([parent, fork], [parent, fork]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["parent"]
    assert rows[0]["_child_session_count"] == 1
    assert [child["session_id"] for child in rows[0]["_child_sessions"]] == ["fork1"]


def test_fork_child_without_visible_parent_stays_top_level():
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const fork = {{session_id:'fork1', title:'Fork', session_source:'fork', parent_session_id:'missing', updated_at:20, last_message_at:20}};
const rows = _attachChildSessionsToSidebarRows([fork], [fork]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["fork1"]
    assert "_child_sessions" not in rows[0]


def test_pinned_fork_with_visible_parent_stays_top_level():
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Parent', updated_at:10, last_message_at:10}};
const fork = {{session_id:'fork1', title:'Fork', session_source:'fork', parent_session_id:'parent', pinned:true, updated_at:20, last_message_at:20}};
const rows = _attachChildSessionsToSidebarRows([parent, fork], [parent, fork]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["parent", "fork1"]
    assert "_child_sessions" not in rows[0]


def test_nested_fork_bubbles_latest_child_timestamp_for_sorting():
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Parent', updated_at:10, last_message_at:10}};
const fork = {{session_id:'fork1', title:'Fork', session_source:'fork', parent_session_id:'parent', updated_at:20, last_message_at:20}};
const rows = _attachChildSessionsToSidebarRows([parent, fork], [parent, fork]);
console.log(JSON.stringify({{row: rows[0], timestampMs: _sessionTimestampMs(rows[0])}}));
"""
    result = json.loads(_run_node(source))
    row = result["row"]
    assert row["session_id"] == "parent"
    # Keep the parent row's persisted timestamp intact, but use newest attached
    # child/subagent activity for sidebar sorting/date grouping.
    assert row["last_message_at"] == 10
    assert row["_child_session_latest_at"] == 20
    assert row["_sidebar_activity_at"] == 20
    assert result["timestampMs"] == 20_000


def test_hidden_archived_lineage_child_bubbles_running_state_and_activity_to_visible_parent():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && (session.is_streaming || session.active_stream_id));
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const visibleTip = {{
  session_id:'visible-tip',
  title:'Visible lineage row',
  parent_session_id:'parent-root',
  session_source:'webui',
  _lineage_root_id:'parent-root',
  updated_at:10,
  last_message_at:10,
}};
const archivedTip = {{
  session_id:'archived-tip',
  title:'Archived running tip',
  parent_session_id:'parent-root',
  session_source:'webui',
  archived:true,
  _lineage_root_id:'parent-root',
  is_streaming:true,
  active_stream_id:'stream-1',
  updated_at:30,
  last_message_at:30,
}};
const rows = _attachChildSessionsToSidebarRows([visibleTip], [visibleTip], [visibleTip, archivedTip]);
console.log(JSON.stringify({{row: rows[0], count: rows.length, timestampMs: _sessionTimestampMs(rows[0])}}));
"""
    result = json.loads(_run_node(source))
    row = result["row"]
    assert result["count"] == 1
    assert row["session_id"] == "visible-tip"
    assert "_child_sessions" not in row
    assert row["_child_session_streaming"] is True
    assert row["_child_session_latest_at"] == 30
    assert row["_sidebar_activity_at"] == 30
    assert result["timestampMs"] == 30_000


def test_archived_lineage_child_without_root_id_falls_back_to_parent_for_bubbling():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && (session.is_streaming || session.active_stream_id));
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const visibleTip = {{
  session_id:'visible-tip',
  title:'Visible lineage row',
  parent_session_id:'parent-root',
  session_source:'webui',
  _lineage_root_id:'parent-root',
  updated_at:10,
  last_message_at:10,
}};
const archivedTip = {{
  session_id:'archived-tip',
  title:'Archived running tip',
  parent_session_id:'parent-root',
  session_source:'webui',
  archived:true,
  is_streaming:true,
  active_stream_id:'stream-1',
  updated_at:30,
  last_message_at:30,
}};
const rows = _attachChildSessionsToSidebarRows([visibleTip], [visibleTip], [visibleTip, archivedTip]);
console.log(JSON.stringify({{row: rows[0], count: rows.length, timestampMs: _sessionTimestampMs(rows[0])}}));
"""
    result = json.loads(_run_node(source))
    row = result["row"]
    assert result["count"] == 1
    assert row["session_id"] == "visible-tip"
    assert "_child_sessions" not in row
    assert row["_child_session_streaming"] is True
    assert row["_child_session_latest_at"] == 30
    assert row["_sidebar_activity_at"] == 30
    assert result["timestampMs"] == 30_000


def test_non_archived_reference_only_lineage_row_does_not_bubble_to_visible_parent():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && (session.is_streaming || session.active_stream_id));
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const visibleTip = {{
  session_id:'visible-tip',
  title:'Visible lineage row',
  parent_session_id:'parent-root',
  session_source:'webui',
  _lineage_root_id:'parent-root',
  updated_at:10,
  last_message_at:10,
}};
const filteredReferenceOnly = {{
  session_id:'filtered-tip',
  title:'Filtered non-archived tip',
  parent_session_id:'parent-root',
  session_source:'webui',
  _lineage_root_id:'parent-root',
  archived:false,
  is_streaming:true,
  active_stream_id:'stream-1',
  updated_at:30,
  last_message_at:30,
}};
const rows = _attachChildSessionsToSidebarRows([visibleTip], [visibleTip], [visibleTip, filteredReferenceOnly]);
console.log(JSON.stringify({{row: rows[0], count: rows.length, timestampMs: _sessionTimestampMs(rows[0])}}));
"""
    result = json.loads(_run_node(source))
    row = result["row"]
    assert result["count"] == 1
    assert row["session_id"] == "visible-tip"
    assert "_child_sessions" not in row
    assert "_child_session_streaming" not in row
    assert "_child_session_latest_at" not in row
    assert "_sidebar_activity_at" not in row
    assert result["timestampMs"] == 10_000


def test_stale_active_stream_id_on_hidden_archived_child_does_not_bubble_streaming_state():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && (session.is_streaming || session.pending_user_message || session.has_pending_user_message));
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const visibleTip = {{
  session_id:'visible-tip',
  title:'Visible lineage row',
  parent_session_id:'parent-root',
  session_source:'webui',
  _lineage_root_id:'parent-root',
  updated_at:10,
  last_message_at:10,
}};
const archivedTip = {{
  session_id:'archived-tip',
  title:'Archived stale tip',
  parent_session_id:'parent-root',
  session_source:'webui',
  archived:true,
  _lineage_root_id:'parent-root',
  is_streaming:false,
  active_stream_id:'dead-stream',
  updated_at:30,
  last_message_at:30,
}};
const rows = _attachChildSessionsToSidebarRows([visibleTip], [visibleTip], [visibleTip, archivedTip]);
console.log(JSON.stringify({{row: rows[0], count: rows.length, timestampMs: _sessionTimestampMs(rows[0])}}));
"""
    result = json.loads(_run_node(source))
    row = result["row"]
    assert result["count"] == 1
    assert row["session_id"] == "visible-tip"
    assert "_child_sessions" not in row
    assert "_child_session_streaming" not in row
    assert row["_child_session_latest_at"] == 30
    assert row["_sidebar_activity_at"] == 30
    assert result["timestampMs"] == 30_000


def test_collapsed_lineage_segments_do_not_render_as_child_sessions():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && (session.is_streaming || session.active_stream_id));
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sessionLineageKey'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_collapseSessionLineageForSidebar'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
eval(extractFunc('_renderSidebarRowsFromRawSessions'));
const root = {{session_id:'root', title:'Root', updated_at:10, last_message_at:10, _lineage_root_id:'root', _lineage_tip_id:'root'}};
const mid = {{session_id:'mid', title:'Middle', parent_session_id:'root', updated_at:20, last_message_at:20, _lineage_root_id:'root', _lineage_tip_id:'mid'}};
const tip = {{session_id:'tip', title:'Tip', parent_session_id:'root', updated_at:30, last_message_at:30, _lineage_root_id:'root', _lineage_tip_id:'tip'}};
const rows = _renderSidebarRowsFromRawSessions([root, mid, tip], [root, mid, tip]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["tip"]
    assert rows[0]["_lineage_collapsed_count"] == 3
    assert [seg["session_id"] for seg in rows[0]["_lineage_segments"]] == ["tip", "mid", "root"]
    assert "_child_sessions" not in rows[0]
    assert "_child_session_count" not in rows[0]


def test_nested_fork_bubbles_parent_attention_state():
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
function _isSessionEffectivelyStreaming(session) {{
  return !!(session && session.active_stream_id);
}}
function _hasUnreadForSession(session) {{
  return !!(session && session.has_unread);
}}
eval(extractFunc('_sessionTimestampMs'));
eval(extractFunc('_isChildSession'));
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_sessionDisplayTitle'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parent = {{session_id:'parent', title:'Parent', updated_at:10, last_message_at:10}};
const fork = {{
  session_id:'fork1',
  title:'Fork',
  session_source:'fork',
  parent_session_id:'parent',
  updated_at:20,
  last_message_at:20,
  has_unread:true,
  attention:{{kind:'approval', count:2}},
  active_stream_id:'stream-1'
}};
const rows = _attachChildSessionsToSidebarRows([parent, fork], [parent, fork]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert rows[0]["_child_session_has_unread"] is True
    assert rows[0]["_child_session_streaming"] is True
    assert rows[0]["_child_session_attention"]["kind"] == "approval"


def test_fork_chain_stays_attached_under_visible_root():
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_sessionDisplayTitle'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const root = {{session_id:'root', title:'Root', updated_at:10, last_message_at:10}};
const fork1 = {{session_id:'fork1', title:'Fork 1', session_source:'fork', parent_session_id:'root', updated_at:20, last_message_at:20}};
const fork2 = {{session_id:'fork2', title:'Fork 2', session_source:'fork', parent_session_id:'fork1', updated_at:30, last_message_at:30}};
const rows = _attachChildSessionsToSidebarRows([fork2, fork1, root], [fork2, fork1, root]);
console.log(JSON.stringify(rows));
"""
    rows = json.loads(_run_node(source))
    assert [row["session_id"] for row in rows] == ["root"]
    assert [child["session_id"] for child in rows[0]["_child_sessions"]] == ["fork1", "fork2"]
    assert rows[0]["_child_sessions"][1]["_parent_segment_id"] == "fork1"


def test_sidebar_lineage_key_uses_session_id_for_fork_rows():
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
eval(extractFunc('_sidebarLineageKeyForRow'));
const root = {{session_id:'root', parent_session_id:null}};
const pinnedFork = {{session_id:'fork1', session_source:'fork', parent_session_id:'root', pinned:true}};
console.log(JSON.stringify({{
  rootKey:_sidebarLineageKeyForRow(root),
  forkKey:_sidebarLineageKeyForRow(pinnedFork),
}}));
"""
    result = json.loads(_run_node(source))
    assert result["rootKey"] == "root"
    assert result["forkKey"] == "fork1"


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


def test_sidebar_lineage_segment_badge_is_detailed_density_only_and_localized():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "session-lineage-count" in js
    assert "const density=(window._sidebarDensity==='detailed'?'detailed':'compact');" in js
    assert "const showLineageMetadata=density==='detailed';" in js
    assert "const segmentCount=showLineageMetadata?_sessionSegmentCount(s):0;" in js
    assert "const lineageSegments=showLineageMetadata?_lineageSegmentsForRender(s,lineageKey):[];" in js
    assert "const needsLineageReport=showLineageMetadata?_lineageReportNeedsFetch(s,lineageKey,segmentCount):false;" in js
    assert "const canExpandLineageSegments=showLineageMetadata&&Boolean(" in js
    assert "t('session_meta_segments', segmentCount)" in js
    assert "titleRow.appendChild(segmentCountEl);" in js
    assert ".session-lineage-count{" in css


def test_lineage_segment_expansion_static_contract():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "const _expandedLineageKeys = new Set();" in js
    assert "const _lineageReportCache = new Map();" in js
    assert "const _lineageReportInflight = new Map();" in js
    assert "_pruneLineageReportCacheToVisibleSessions(_allSessions);" in js
    assert "session-lineage-count,.session-lineage-segments,.session-lineage-segment" in js
    assert "segmentCountEl.setAttribute('aria-expanded'" in js
    assert "_expandedLineageKeys.has(lineageKey)" in js
    assert "_expandedLineageKeys.add(lineageKey)" in js
    assert "_expandedLineageKeys.delete(lineageKey)" in js
    assert "_fetchLineageReportForRow(s,lineageKey).then" in js
    assert js.count("_fetchLineageReportForRow(s,lineageKey).then(()=>renderSessionListFromCache());") == 2
    assert "'/api/session/lineage/report?session_id='" in js
    assert "encodeURIComponent(s.session_id)" in js
    assert "className='session-lineage-segments'" in js
    assert "className='session-lineage-segment'" in js
    assert "const segTitle=_sessionDisplayTitle(seg)||t('session_lineage_segment_untitled');" in js
    assert "row.title=t('session_lineage_segment_open');" in js
    assert "await loadSession(seg.session_id, {skipLineageResolve:true});" in js
    assert "const openChildSession=async(childSession)=>{" in js
    assert "await loadSession(childSession.session_id, {skipLineageResolve:true});" in js
    assert "if(!opts.skipLineageResolve && typeof _resolveSessionIdFromSidebarLineage==='function'){" in js
    assert ".session-lineage-count.expandable{" in css
    assert ".session-lineage-count.expandable:hover" in css
    assert ".session-lineage-segments{" in css
    assert ".session-lineage-segment{" in css


def test_lineage_report_fetch_is_needed_only_when_backend_count_exceeds_materialized_segments():
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
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
eval(extractFunc('_lineageReportCacheKey'));
eval(extractFunc('_lineageLocalSegmentCount'));
eval(extractFunc('_lineageReportNeedsFetch'));
const backendOnly = {{session_id:'tip', _lineage_key:'root', _compression_segment_count:25}};
const localFull = {{
  session_id:'tip',
  _lineage_key:'root',
  _compression_segment_count:2,
  _lineage_segments:[{{session_id:'tip'}}, {{session_id:'root'}}],
}};
const before = _lineageReportNeedsFetch(backendOnly, 'root', 25);
_lineageReportCache.set('root', {{segments:[{{session_id:'tip'}}, {{session_id:'root'}}]}});
const afterCache = _lineageReportNeedsFetch(backendOnly, 'root', 25);
const fullLocal = _lineageReportNeedsFetch(localFull, 'root', 2);
console.log(JSON.stringify({{before, afterCache, fullLocal}}));
"""
    assert json.loads(_run_node(source)) == {"before": True, "afterCache": False, "fullLocal": False}


def test_cached_lineage_report_segments_merge_with_materialized_segments_without_duplicates_or_children():
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
const _lineageReportCache = new Map();
eval(extractFunc('_lineageReportCacheKey'));
eval(extractFunc('_lineageSegmentsForRender'));
_lineageReportCache.set('root', {{
  segments:[
    {{session_id:'tip', title:'Tip', role:'tip', started_at:30}},
    {{session_id:'root', title:'Root', role:'hidden_segment', started_at:20}},
    {{session_id:'older', title:'Older', role:'hidden_segment', started_at:10}},
    {{session_id:'child', title:'Child', role:'child_session', started_at:40}},
  ],
  children:[{{session_id:'child', title:'Child', role:'child_session'}}],
}});
const row = {{
  session_id:'tip',
  _lineage_key:'root',
  _lineage_segments:[{{session_id:'tip', title:'Tip'}}, {{session_id:'root', title:'Root'}}],
}};
const segments = _lineageSegmentsForRender(row, 'root').map(seg => seg.session_id);
console.log(JSON.stringify(segments));
"""
    assert json.loads(_run_node(source)) == ["root", "older"]


def test_lineage_report_fetch_uses_endpoint_once_and_caches_result():
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
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
let _lineageReportCacheGeneration = 0;
const calls = [];
function api(path) {{
  calls.push(path);
  return Promise.resolve({{found:true, segments:[{{session_id:'tip'}}, {{session_id:'root'}}]}});
}}
eval(extractFunc('_lineageReportCacheKey'));
eval(extractFunc('_fetchLineageReportForRow'));
(async()=>{{
  const row = {{session_id:'tip', _lineage_key:'root'}};
  const [first, second] = await Promise.all([
    _fetchLineageReportForRow(row, 'root'),
    _fetchLineageReportForRow(row, 'root'),
  ]);
  await _fetchLineageReportForRow(row, 'root');
  console.log(JSON.stringify({{
    calls,
    cached:_lineageReportCache.has('root'),
    same:first===second,
  }}));
}})().catch(err=>{{console.error(err); process.exit(1);}});
"""
    result = json.loads(_run_node(source))
    assert result == {
        "calls": ["/api/session/lineage/report?session_id=tip"],
        "cached": True,
        "same": True,
    }


def test_lineage_refresh_cache_prune_keeps_visible_keys_and_drops_missing_ones():
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
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_pruneLineageReportCacheToVisibleSessions'));
const visibleRequest = Promise.resolve({{found:true}});
const staleRequest = Promise.resolve({{found:true}});
_lineageReportCache.set('root', {{segments:[{{session_id:'root'}}]}});
_lineageReportCache.set('stale', {{segments:[{{session_id:'stale'}}]}});
_lineageReportInflight.set('root', visibleRequest);
_lineageReportInflight.set('stale', staleRequest);
_pruneLineageReportCacheToVisibleSessions([
  {{session_id:'tip', _lineage_key:'root'}},
  {{session_id:'child', parent_session_id:'root'}},
]);
console.log(JSON.stringify({{
  cacheKeys:Array.from(_lineageReportCache.keys()),
  inflightKeys:Array.from(_lineageReportInflight.keys()),
}}));
"""
    assert json.loads(_run_node(source)) == {
        "cacheKeys": ["root"],
        "inflightKeys": ["root"],
    }


def test_pruned_lineage_inflight_request_cannot_repopulate_cache():
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
const _lineageReportCache = new Map();
const _lineageReportInflight = new Map();
let _lineageReportCacheGeneration = 0;
let resolveApi;
function api(path) {{
  return new Promise(resolve => {{
    resolveApi = () => resolve({{found:true, path, segments:[{{session_id:'stale'}}]}});
  }});
}}
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_lineageReportCacheKey'));
eval(extractFunc('_pruneLineageReportCacheToVisibleSessions'));
eval(extractFunc('_fetchLineageReportForRow'));
(async()=>{{
  const staleRow = {{session_id:'stale-tip', _lineage_key:'stale'}};
  const request = _fetchLineageReportForRow(staleRow, 'stale');
  _pruneLineageReportCacheToVisibleSessions([{{session_id:'tip', _lineage_key:'root'}}]);
  resolveApi();
  await request;
  console.log(JSON.stringify({{
    staleCached:_lineageReportCache.has('stale'),
    staleInflight:_lineageReportInflight.has('stale'),
    visibleCached:_lineageReportCache.has('root'),
  }}));
}})().catch(err=>{{console.error(err); process.exit(1);}});
"""
    assert json.loads(_run_node(source)) == {
        "staleCached": False,
        "staleInflight": False,
        "visibleCached": False,
    }


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

def test_session_meta_segments_softened_label_no_literal_segment_in_english():
    """Regression: the sidebar badge for compressed/lineage rows must not visibly
    say 'X segments' by default — the technical internal term should be replaced
    with softer user-facing copy (#2155).

    This verifies the English base locale's session_meta_segments key so that
    t() fallback for untranslated locales also produces softened copy.
    """
    import re
    i18n_text = (REPO_ROOT / 'static' / 'i18n.js').read_text(encoding='utf-8')
    # Locate the English base-locale block (first occurrence, before any _lang guard).
    first_lang = i18n_text.index('_lang: \'en\'')
    second_lang = i18n_text.index('_lang:', first_lang + 1)
    english_slice = i18n_text[first_lang:second_lang]
    assert 'session_meta_segments:' in english_slice, 'session_meta_segments missing from English locale'
    # Capture only the arrow-function value (not the key name which also contains 'segment').
    match = re.search(
        r"session_meta_segments:\s*(\(\w+\)\s*=>\s*[^,]+)",
        english_slice,
    )
    assert match, 'session_meta_segments value not found in English locale'
    rendered = match.group(1)
    assert 'segment' not in rendered, (
        f"session_meta_segments English value still contains the technical word 'segment': {rendered}. "
        "Expected softened copy like 'prior turn(s)' instead. See #2155."
    )


def test_sidebar_search_and_rows_use_read_only_display_title():
    """Stale persisted titles should not drive sidebar search/render when display_title exists."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    assert "function _sessionDisplayTitle" in js
    assert "function _sessionTitleTags" in js
    assert "function _sessionSearchDirectAndTitleMatches" in js
    assert "const titleMatches=(sessions||[]).filter(s=>_sessionDisplayTitle(s).toLowerCase().includes(q));" in js
    assert "const directAndTitleMatches=_sessionSearchDirectAndTitleMatches(_allSessions,currentQ);" in js
    assert "const rawTitle=_sessionDisplayTitle(s);" in js
    assert "const tags=_sessionTitleTags(rawTitle);" in js
    assert "const segTitle=_sessionDisplayTitle(seg)||t('session_lineage_segment_untitled');" in js
    assert "const childTitle=_sessionDisplayTitle(child)||'Untitled child session';" in js


def test_child_session_parent_segment_note_uses_display_title():
    """A child attached through a hidden parent segment should show the reconciled segment title."""
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
eval(extractFunc('_isForkWithResolvableParent'));
eval(extractFunc('_sidebarLineageKeyForRow'));
eval(extractFunc('_sessionDisplayTitle'));
eval(extractFunc('_attachChildSessionsToSidebarRows'));
const parentRow={{
  session_id:'tip',
  title:'Hermes WebUI #8',
  _lineage_root_id:'root',
  _lineage_segments:[
    {{session_id:'tip', title:'Hermes WebUI #8', display_title:'Hermes WebUI #177'}},
    {{session_id:'old-parent', title:'Hermes WebUI #8', display_title:'Hermes WebUI #176'}},
  ],
}};
const child={{
  session_id:'child',
  title:'Child Session',
  relationship_type:'child_session',
  parent_session_id:'old-parent',
}};
const rows = _attachChildSessionsToSidebarRows([parentRow], [parentRow, child]);
console.log(JSON.stringify(rows[0]._child_sessions[0]));
"""
    child = json.loads(_run_node(source))
    assert child["_parent_segment_id"] == "old-parent"
    assert child["_parent_segment_title"] == "Hermes WebUI #176"


def test_default_webui_numbered_titles_are_not_treated_as_hash_tags():
    """The reconciled title 'Hermes WebUI #177' must render with its number intact."""
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
eval(extractFunc('_sessionTitleIsDefaultWebUI'));
eval(extractFunc('_sessionTitleTags'));
console.log(JSON.stringify({{
  webui:_sessionTitleTags('Hermes WebUI #177'),
  custom:_sessionTitleTags('Deploy #prod'),
}}));
"""
    assert json.loads(_run_node(source)) == {"webui": [], "custom": ["#prod"]}


def test_streaming_state_recorded_from_own_state_not_bubbled_child():
    """_rememberRenderedStreamingState must receive the parent's own streaming
    state, not the composite own||child value.  Otherwise the parent gets
    marked unread/completed when a nested fork stops streaming."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    # The pattern we need: ownStreaming used for remember, isStreaming used
    # for rendering (includes child).
    assert "const ownStreaming=_isSessionEffectivelyStreaming(s);" in js
    assert "const isStreaming=ownStreaming||!!s._child_session_streaming;" in js
    assert "_rememberRenderedStreamingState(s, ownStreaming);" in js
    # The old buggy pattern must not exist.
    assert "_rememberRenderedStreamingState(s, isStreaming);" not in js


def test_nested_fork_rows_included_in_visible_sidebar_ids():
    """Expanded writable fork children must appear in _sessionVisibleSidebarIds
    so they participate in batch-select (select-all / shift-select)."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    assert "child.session_source==='fork'" in js
    # The _sessionVisibleSidebarIds builder must push fork children.
    assert "_sessionVisibleSidebarIds.push(child.session_id)" in js


def test_nested_fork_rows_render_select_checkbox():
    """The session-child-session-fork render path must include a batch-select
    checkbox when _sessionSelectMode is active and the child is writable."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    render_marker = "row.className='session-child-session session-child-session-fork'"
    fork_render_start = js.find(render_marker)
    assert fork_render_start > 0
    fork_render_block = js[fork_render_start:fork_render_start + 2000]
    assert "session-select-cb" in fork_render_block
    assert "_sessionSelectMode" in fork_render_block
