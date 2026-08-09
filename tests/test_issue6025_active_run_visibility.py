"""Executable production-path proofs for issue #6025 canonical activity rows."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    brace = source.index("{", source.index(")", start))
    depth = 0
    quote = None
    regex = False
    line_comment = False
    block_comment = False
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        previous = source[index - 1] if index else ""
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if regex:
            if char == "\\":
                escaped = not escaped
                continue
            if char == "/" and not escaped:
                regex = False
            escaped = False
            continue
        if char == "/" and following == "/":
            line_comment = True
            continue
        if char == "/" and following == "*":
            block_comment = True
            continue
        if char in "'\"`":
            quote = char
        elif char == "/" and previous in "=(:,[!&|?;{":
            regex = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced production function: {name}")


def _run_node_script(source: str):
    with tempfile.TemporaryDirectory(prefix="hermes-6025-node-") as directory:
        script = Path(directory) / "proof.js"
        script.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def test_issue6025_active_run_stays_visible_after_switching_away():
    projection = _extract_function(SESSIONS_JS, "_activeRunRowsForProjection")
    partition = _extract_function(SESSIONS_JS, "_partitionSidebarSessionRows")
    cli = _extract_function(SESSIONS_JS, "_isCliSession")
    sidebar_visible = _extract_function(SESSIONS_JS, "_sidebarRowHasVisibleMessages")
    effective = _extract_function(SESSIONS_JS, "_isSessionEffectivelyStreaming")
    ring = _extract_function(SESSIONS_JS, "_isSessionRingStreaming")
    active_lineage_key = _extract_function(SESSIONS_JS, "_activeRunLineageKey")
    source = f"""
const NO_PROJECT_FILTER='__none__';
let _activeProject='project-a'; let _sessionSourceFilter='webui'; let _showArchived=false;
let _activeProjectForRows=null; let _archivedWebuiCount=0; let _archivedCliCount=0;
const window={{_showCliSessions:true}};
function _isMessagingSession(){{return false;}}
function _sessionAttentionState(){{return null;}}
function _hasPendingUserMessageSignal(row){{return !!(row.pending_user_message||row.has_pending_user_message);}}
function _isSessionLocallyStreaming(){{return false;}}
function _activeSessionIdForSidebar(){{return null;}}
function _sidebarLineageKeyForRow(row){{return row.session_id;}}
function _collapseSessionLineageForSidebar(rows){{return rows;}}
{cli}
{effective}
{ring}
{sidebar_visible}
{partition}
{active_lineage_key}
{projection}
const rows=[
  {{session_id:'webui-project',raw_source:'webui',project_id:'project-a',active_run:{{started_at:10,age_seconds:4}}}},
  {{session_id:'webui-hidden-project',raw_source:'webui',project_id:'project-a',default_hidden:true,active_run:{{started_at:11,age_seconds:3}}}},
  {{session_id:'webui-other-project',raw_source:'webui',project_id:'project-b',active_run:{{started_at:12,age_seconds:2}}}},
  {{session_id:'cli-project',raw_source:'cli',project_id:'project-a',active_run:{{started_at:13,age_seconds:1}}}},
];
const webui=_activeRunRowsForProjection(rows).map(row=>row.session_id);
_sessionSourceFilter='cli';
const cliRows=_activeRunRowsForProjection(rows).map(row=>row.session_id);
console.log(JSON.stringify({{webui,cliRows}}));
"""
    assert _run_node_script(source) == {
        "webui": ["webui-project", "webui-hidden-project"],
        "cliRows": ["cli-project"],
    }


def test_active_run_projection_uses_the_canonical_visible_lineage_tip():
    projection = _extract_function(SESSIONS_JS, "_activeRunRowsForProjection")
    collapse = _extract_function(SESSIONS_JS, "_collapseSessionLineageForSidebar")
    lineage_key = _extract_function(SESSIONS_JS, "_sessionLineageKey")
    timestamp = _extract_function(SESSIONS_JS, "_sessionTimestampMs")
    child = _extract_function(SESSIONS_JS, "_isChildSession")
    tip = _extract_function(SESSIONS_JS, "_authoritativeLineageTipId")
    active_lineage_key = _extract_function(SESSIONS_JS, "_activeRunLineageKey")
    source = f"""
const NO_PROJECT_FILTER='__none__';
let _activeProject=null; let _sessionSourceFilter='webui'; let _showArchived=false;
const window={{_showCliSessions:true}};
function _isMessagingSession(){{return false;}}
function _sessionAttentionState(){{return null;}}
function _hasPendingUserMessageSignal(row){{return !!(row.pending_user_message||row.has_pending_user_message);}}
function _isSessionLocallyStreaming(){{return false;}}
function _activeSessionIdForSidebar(){{return null;}}
function _isCliSession(){{return false;}}
function _isSessionEffectivelyStreaming(row){{return !!row.is_streaming;}}
function _isSessionRingStreaming(row){{return _isSessionEffectivelyStreaming(row)||!!row.active_run;}}
function _sidebarRowHasVisibleMessages(row){{return !!(row.message_count||row.active_run);}}
function _partitionSidebarSessionRows(rows){{return {{sessionsRaw:rows}};}}
{timestamp}
{child}
{lineage_key}
{tip}
{collapse}
function _sidebarLineageKeyForRow(row){{return row._lineage_key||row._lineage_root_id||row.session_id;}}
{active_lineage_key}
{projection}
const rows=[
  {{session_id:'root',message_count:10,updated_at:10,_lineage_root_id:'root',_lineage_tip_id:'tip',active_run:{{started_at:10,age_seconds:4}}}},
  {{session_id:'tip',message_count:20,updated_at:20,_lineage_root_id:'root',_lineage_tip_id:'tip'}}
];
const visible=_activeRunRowsForProjection(rows);
console.log(JSON.stringify(visible.map(row=>({{id:row.session_id,started:row.active_run&&row.active_run.started_at}}))));
"""
    assert _run_node_script(source) == [{"id": "tip", "started": 10}]


def test_active_run_renderer_paints_and_clears_the_user_visible_pill_and_tray():
    elapsed = _extract_function(UI_JS, "_activeRunElapsedSeconds")
    monotonic = _extract_function(UI_JS, "_activeRunMonotonicSeconds")
    duration = _extract_function(UI_JS, "_activeRunDurationLabel")
    renderer = _extract_function(UI_JS, "_renderActiveRunProjection")
    source = f"""
class Node {{
  constructor(tag) {{ this.tagName=tag; this.children=[]; this.dataset={{}}; this.hidden=false; this.attributes={{}}; this.parentNode=null; this.textContent=''; }}
  append(...nodes) {{ for(const node of nodes) {{ node.parentNode=this; this.children.push(node); }} }}
  appendChild(node) {{ this.append(node); }}
  querySelector(selector) {{ if(selector==='button') return this.children.find(node=>node.tagName==='button')||null; if(selector==='.active-run-age') return this.children.find(node=>node.className==='active-run-age')||null; return null; }}
  setAttribute(key,value) {{ this.attributes[key]=String(value); }}
  remove() {{ if(this.parentNode) this.parentNode.children=this.parentNode.children.filter(node=>node!==this); }}
}}
const host=new Node('div'), pill=new Node('button'), tray=new Node('div'); tray.hidden=true;
const nodes={{activeRunVisibility:host,activeRunPill:pill,activeRunTray:tray}};
globalThis.document={{createElement:tag=>new Node(tag)}};
function $(id){{return nodes[id]||null;}}
function t(key,count,duration){{return key==='active_run_visibility_label'?`${{count}} active · ${{duration}}`:key;}}
function _openSidebarSession(){{}}
let _activeRunElapsedTimer=null; const _activeRunElapsedAnchors=new Map();
let now=100; const performance={{now:()=>now*1000}};
function _scheduleActiveRunElapsedRefresh(){{}}
function _stopActiveRunElapsedRefresh(){{}}
{elapsed}
{monotonic}
{duration}
function _activeRunRowsForProjection(rows){{return rows.filter(row=>row&&row.active_run&&!row.archived);}}
globalThis.window={{_activeRunRowsForProjection}};
{renderer}
_renderActiveRunProjection([{{session_id:'s1',title:'Away session',active_run:{{started_at:10,age_seconds:4}}}}]);
const painted={{hostHidden:host.hidden,pill:pill.textContent,rowCount:tray.children.length,rowLabel:tray.children[0].querySelector('button').textContent}};
_renderActiveRunProjection([]);
console.log(JSON.stringify({{painted,cleared:{{hostHidden:host.hidden,rowCount:tray.children.length,expanded:pill.attributes['aria-expanded']}}}}));
"""
    assert _run_node_script(source) == {
        "painted": {"hostHidden": False, "pill": "1 active · 4s", "rowCount": 1, "rowLabel": "Away session"},
        "cleared": {"hostHidden": True, "rowCount": 0, "expanded": "false"},
    }


def test_active_run_visibility_uses_canonical_session_matrix():
    from api import config, route_session_list_cache as cache, routes

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "direct": {"session_id": "direct", "started_at": 10},
            "archived": {"session_id": "archived", "started_at": 11},
        })
    try:
        canonical_rows = [
            {"session_id": "direct", "title": "Untitled", "profile": "profile-a", "message_count": 0, "updated_at": 10, "archived": False},
            {"session_id": "profile-a", "title": "A", "profile": "profile-a", "raw_source": "telegram", "session_source": "messaging", "user_id": "u", "chat_id": "c", "message_count": 2, "updated_at": 10, "archived": False},
            {"session_id": "profile-b", "title": "B", "profile": "profile-b", "raw_source": "telegram", "session_source": "messaging", "user_id": "u", "chat_id": "c", "message_count": 2, "updated_at": 20, "archived": False},
        ]
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(routes, "all_sessions", lambda diag=None, include_lineage_metadata=False: list(canonical_rows))
        monkeypatch.setattr(routes, "get_cli_sessions", lambda source_filter=None, all_profiles=False: [])
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda rows: False)
        monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, diag_stage=None: rows)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda rows: None)
        observed_dedupe_inputs = []
        original_dedupe = routes._keep_latest_messaging_session_per_source
        def capture_dedupe(rows, **kwargs):
            observed_dedupe_inputs.append(list(rows))
            return original_dedupe(rows, **kwargs)
        monkeypatch.setattr(routes, "_keep_latest_messaging_session_per_source", capture_dedupe)
        payload = routes._build_session_list_cache_payload(
            active_profile="profile-a",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
        )
        assert "direct" in {row["session_id"] for row in payload["sessions"]}
        assert all({row["profile"] for row in rows} <= {"profile-a"} for rows in observed_dedupe_inputs)
        monkeypatch.undo()
        rows = cache._session_list_cache_overlay_runtime_rows([
            {"session_id": "direct", "raw_source": "webui", "archived": False},
            {"session_id": "archived", "raw_source": "webui", "archived": True},
            {"session_id": "idle", "raw_source": "webui", "archived": False},
        ])
        by_id = {row["session_id"]: row for row in rows}
        assert "active_run" in by_id["direct"]
        assert "active_run" not in by_id["archived"]
        assert "active_run" not in by_id["idle"]
    finally:
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()


def test_active_run_visibility_routes_only_through_canonical_sessions():
    # Split forbidden names so this source-level guard cannot match its own literals.
    forbidden = (
        "/api/activity/" + "active-runs",
        "_active_run_" + "visibility_snapshot",
        "_session_list_cache_" + "get_with_reason",
        "_sidebar_row_" + "matches_scope",
        "_activeRun" + "Snapshot",
        "_activeRun" + "SessionIds",
        "_activeRun" + "ScopeQuery",
        "refreshActiveRun" + "Visibility",
        "_invalidateActiveRun" + "VisibilityScope",
    )
    source = "\n".join((
        (ROOT / "api" / "routes.py").read_text(encoding="utf-8"),
        SESSIONS_JS,
        UI_JS,
    ))
    assert not any(token in source for token in forbidden)
    assert "active_run" in source


def test_active_run_annotation_only_changes_ring_and_global_projection():
    effective = _extract_function(SESSIONS_JS, "_isSessionEffectivelyStreaming")
    ring = _extract_function(SESSIONS_JS, "_isSessionRingStreaming")
    source = f"""
function _hasPendingUserMessageSignal(row){{return !!(row.pending_user_message||row.has_pending_user_message);}}
function _isSessionLocallyStreaming(){{return false;}}
{effective}
{ring}
const active={{active_run:{{started_at:10}},is_streaming:false,pending_user_message:false}};
console.log(JSON.stringify({{effective:_isSessionEffectivelyStreaming(active),ring:_isSessionRingStreaming(active)}}));
"""
    assert _run_node_script(source) == {"effective": False, "ring": True}


def test_active_run_elapsed_label_advances_when_pill_is_visible_with_monotonic_delta():
    monotonic = _extract_function(UI_JS, "_activeRunMonotonicSeconds")
    elapsed = _extract_function(UI_JS, "_activeRunElapsedSeconds")
    source = f"""
let now=100; const performance={{now:()=>now*1000}}; const _activeRunElapsedAnchors=new Map();
{monotonic}
{elapsed}
const row={{session_id:'s1',active_run:{{started_at:10,age_seconds:4}}}};
const first=_activeRunElapsedSeconds(row);
now=103;
const second=_activeRunElapsedSeconds(row);
row.active_run.age_seconds=100;
const skewedRefresh=_activeRunElapsedSeconds(row);
console.log(JSON.stringify({{first,second,skewedRefresh}}));
"""
    assert _run_node_script(source) == {"first": 4, "second": 7, "skewedRefresh": 7}


def test_delayed_profile_response_cannot_repaint_activity_after_real_switch():
    invalidate = _extract_function(SESSIONS_JS, "_invalidateSessionListRenders")
    current = _extract_function(SESSIONS_JS, "_sessionListGenerationIsCurrent")
    apply_if_current = _extract_function(SESSIONS_JS, "_applySessionListPayloadIfCurrent")
    switch_profile = _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad")
    source = f"""
let _renderSessionListGen=7; let cleared=0; let applied=0;
let _pendingSessionListPayload=null; let _renderSessionListQueuedRequest=null; let _sessionListLoadError=null;
let _profileSwitchListEmbargo=false; let S={{activeProfile:'profile-a'}};
globalThis.window={{_renderActiveRunProjection:rows=>{{if(!rows.length)cleared++;}}}};
function showSessionListSkeleton(){{}}
function _setProfileSwitchListEmbargo(value){{_profileSwitchListEmbargo=!!value;}}
function _resetCronUnreadForProfileSwitch(){{}}
function _clearPersistedModelState(){{}}
function startGatewaySSE(){{}}
function syncTopbar(){{}}
function renderSessionList(){{return Promise.resolve();}}
function api(){{return Promise.resolve({{active:'profile-b'}});}}
function _applySessionListPayload(){{applied++;}}
{invalidate}
{current}
{apply_if_current}
{switch_profile}
(async()=>{{
  const stale=_renderSessionListGen;
  await _switchProfileForSessionLoad('profile-b');
  const staleApplied=_applySessionListPayloadIfCurrent(stale,{{}},{{}},{{}});
  const currentApplied=_applySessionListPayloadIfCurrent(_renderSessionListGen,{{}},{{}},{{}});
  console.log(JSON.stringify({{staleRejected:!_sessionListGenerationIsCurrent(stale),currentAccepted:_sessionListGenerationIsCurrent(_renderSessionListGen),cleared,staleApplied,currentApplied,applied,profile:S.activeProfile}}));
}})();
"""
    assert _run_node_script(source) == {
        "staleRejected": True,
        "currentAccepted": True,
        "cleared": 1,
        "staleApplied": False,
        "currentApplied": True,
        "applied": 1,
        "profile": "profile-b",
    }


def test_active_run_locale_keys_cover_exact_supported_locale_set():
    parser = _extract_function(I18N_JS, "_activeRunLocaleNames")
    source = f"""
{parser}
const expected=['en','it','ja','ru','es','de','zh','zh-Hant','pt','ko','fr','cs','tr','pl','vi'];
const names=_activeRunLocaleNames({json.dumps(I18N_JS)});
const source={json.dumps(I18N_JS)};
const keys=['active_run_conversation_fallback','active_run_open_conversation','active_run_visibility_label'];
const starts=expected.map(name=>source.indexOf(`\\n  ${{name==='zh-Hant'?"'zh-Hant'":name}}: {{`));
const allKeys=starts.every((start,index)=>{{
  const block=source.slice(start,index+1<starts.length?starts[index+1]:source.length);
  return start>=0&&keys.every(key=>block.includes(key+':'));
}});
console.log(JSON.stringify({{names,exact:JSON.stringify(names)===JSON.stringify(expected),allKeys}}));
"""
    result = _run_node_script(source)
    assert result["exact"] is True
    assert result["allKeys"] is True


def test_document_escape_enters_close_path_only_for_open_tray():
    escape = _extract_function(UI_JS, "_handleActiveRunEscape")
    hide = _extract_function(UI_JS, "_hideActiveRunTray")
    source = f"""
const tray={{hidden:true}}; const pill={{focused:0,setAttribute(){{}},focus(){{this.focused++;}}}};
function $(id){{return id==='activeRunTray'?tray:pill;}}
{hide}
{escape}
let prevented=0;
_handleActiveRunEscape({{key:'Escape',preventDefault(){{prevented++;}}}});
const closed={{hidden:false}}; tray.hidden=false;
_handleActiveRunEscape({{key:'Escape',preventDefault(){{prevented++;}}}});
console.log(JSON.stringify({{closed:tray.hidden,prevented,focused:pill.focused}}));
"""
    assert _run_node_script(source) == {"closed": True, "prevented": 1, "focused": 1}


def test_active_empty_session_flows_through_canonical_sidebar_filters():
    from api import config

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    config.register_active_run("run", session_id="empty-parent", started_at=10)
    try:
        assert "empty-parent" in config.active_run_session_snapshot()
    finally:
        config.unregister_active_run("run")
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        assert "empty-parent" not in config.active_run_session_snapshot()
