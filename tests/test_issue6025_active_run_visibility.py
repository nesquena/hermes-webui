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


def test_active_run_visibility_uses_canonical_session_matrix():
    from api import config, route_session_list_cache as cache

    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
        config.ACTIVE_RUNS.update({
            "direct": {"session_id": "direct", "started_at": 10},
            "archived": {"session_id": "archived", "started_at": 11},
        })
    try:
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
    source = f"""
let _renderSessionListGen=7; let cleared=0; let _pendingSessionListPayload=null; let _renderSessionListQueuedRequest=null;
let _sessionListLoadError=null;
globalThis.window={{_renderActiveRunProjection:rows=>{{if(!rows.length)cleared++;}}}};
{invalidate}
{current}
const stale=_renderSessionListGen;
_invalidateSessionListRenders();
console.log(JSON.stringify({{staleRejected:!_sessionListGenerationIsCurrent(stale),currentAccepted:_sessionListGenerationIsCurrent(_renderSessionListGen),cleared}}));
"""
    assert _run_node_script(source) == {"staleRejected": True, "currentAccepted": True, "cleared": 1}


def test_active_run_locale_keys_cover_exact_supported_locale_set():
    parser = _extract_function(I18N_JS, "_activeRunLocaleNames")
    source = f"""
{parser}
const expected=['en','it','ja','ru','es','de','zh','zh-Hant','pt','ko','fr','cs','tr','pl','vi'];
const names=_activeRunLocaleNames({json.dumps(I18N_JS)});
const allKeys=expected.every(name=>{{const start={json.dumps(I18N_JS)}.indexOf(name);return start>=0;}});
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
