"""Visible-order contract for the first anchor-backed Compact Worklog handoff."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_body(src, name):
    start = src.find(f"function {name}")
    assert start != -1, f"{name} not found"
    params = src.find("(", start)
    assert params != -1, f"{name} params not found"
    depth = 0
    close = -1
    for idx in range(params, len(src)):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} params did not close"
    brace = src.find("{", close)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:idx]
    raise AssertionError(f"{name} body did not close")


def _event_listener_body(src, event_name):
    marker = f"source.addEventListener('{event_name}',e=>{{"
    start = src.find(marker)
    if start == -1:
        marker = f"es.addEventListener('{event_name}', e => {{"
        start = src.find(marker)
    assert start != -1, f"{event_name} listener not found"
    brace = src.find("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1:idx]
    raise AssertionError(f"{event_name} listener did not close")


def _run_node_script(script):
    assert NODE, "node is required for DOM-executed anchor render tests"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_EXTRACT_FUNC_JS = """
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
""".strip()


def _run_complete_anchor_settlement_case(active_mode):
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "messages.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
const activeMode = {json.dumps(active_mode)};
global.window = {{
  chatActivityMode(){{ return activeMode; }},
  _chatActivityDisplayMode: activeMode === 'transparent_stream' ? 'compact_worklog' : 'transparent_stream',
  _transparentStream: activeMode === 'transparent_stream' ? false : true,
}};
global.S = {{ session: {{}} }};
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
if(src.indexOf('function _anchorSceneActiveMode') !== -1){{
  eval(extractFunc('_anchorSceneActiveMode'));
}}else{{
  eval("function _anchorSceneActiveMode(){{ return activeMode; }}");
}}
if(src.indexOf('function _anchorSceneRowDisplayHintForMode') !== -1){{
  eval(extractFunc('_anchorSceneRowDisplayHintForMode'));
}}else{{
  eval("function _anchorSceneRowDisplayHintForMode(row, sceneMode){{ const hints=row&&typeof row==='object'&&row.display_hints&&typeof row.display_hints==='object'?row.display_hints:null; if(sceneMode==='transparent_stream') return (hints&&hints.transparent_stream)||'chronological_activity'; if(sceneMode==='compact_worklog') return (hints&&hints.compact_worklog)||row.display_hint||'activity_row'; return row&&row.display_hint||'activity_row'; }}");
}}
function _anchorSceneFinalAnswerText(message){{ return message && (message.final_answer || message.content || ''); }}
function _anchorSceneRowsByMessageIndex(){{ return new Map(); }}
function _anchorSceneMessageRef(message){{ return String(message && message.id || ''); }}
function _anchorSceneTurnDurationForSettlement(_lastAsst, base){{ return base && base.turn_duration ? base.turn_duration : 0; }}
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
const messages = [
  {{role:'user', content:'Prompt', id:'user-1'}},
  {{role:'assistant', content:'Final answer text', id:'assistant-1'}},
];
const projectedScene = {{
  mode:'compact_worklog',
  final_answer:'Final answer text',
  identity:{{source_message_refs:['legacy']}},
  lifecycle:{{terminal_state:'done'}},
  turn_duration:42,
  activity_rows:[
    {{
      role:'prose',
      text:'Final answer text',
      status:'running',
      row_id:'live-prose-final',
      local_id:'live-prose-final',
      display_hint:'main_prose',
      display_hints:{{compact_worklog:'main_prose', transparent_stream:'chronological_activity'}},
    }},
    {{
      role:'thinking',
      text:'Working through the result',
      status:'running',
      row_id:'live-thinking-1',
      local_id:'live-thinking-1',
      display_hint:'collapsed_thinking',
      display_hints:{{compact_worklog:'collapsed_thinking', transparent_stream:'chronological_activity'}},
    }},
    {{
      role:'tool',
      text:'Fetched docs',
      status:'running',
      row_id:'live-tool-1',
      local_id:'live-tool-1',
      tool_call_id:'tool-1',
      display_hint:'tool_row',
      display_hints:{{compact_worklog:'tool_row', transparent_stream:'chronological_activity'}},
    }},
    {{
      role:'terminal',
      text:'done',
      status:'completed',
      row_id:'terminal-1',
      local_id:'terminal-1',
      display_hint:'terminal_status_row',
      display_hints:{{compact_worklog:'terminal_status_row', transparent_stream:'chronological_activity'}},
    }},
  ],
}};
const scene = _completeSettledAnchorSceneForTurn(messages, 1, projectedScene);
process.stdout.write(JSON.stringify({{
  mode: scene && scene.mode,
  final_answer: scene && scene.final_answer,
  final_message_ref: scene && scene.final_message_ref,
  identity: scene && scene.identity,
  activity_rows: scene && scene.activity_rows ? scene.activity_rows.map(row => ({{
    role: row.role,
    text: row.text,
    status: row.status,
    display_hint: row.display_hint,
    compact_hint: row.display_hints && row.display_hints.compact_worklog,
    transparent_hint: row.display_hints && row.display_hints.transparent_stream,
  }})) : null,
}}));
"""
    return _run_node_script(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_dom_render_live_compression_row_transitions_to_settled_scene():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
class FakeElement {{
  constructor(tag){{
    this.tagName = String(tag || 'div').toUpperCase();
    this.attributes = Object.create(null);
    this.dataset = {{}};
    this.children = [];
    this.parentNode = null;
    this.style = {{}};
    this.className = '';
    this._innerHTML = '';
  }}
  setAttribute(name, value){{
    this.attributes[name] = String(value);
    if(name === 'class') this.className = String(value);
    if(name.startsWith('data-')){{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }}
  }}
  getAttribute(name){{ return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }}
  removeAttribute(name){{ delete this.attributes[name]; }}
  appendChild(child){{ child.parentNode = this; this.children.push(child); return child; }}
  querySelector(){{ return null; }}
  querySelectorAll(){{ return []; }}
  set innerHTML(value){{ this._innerHTML = String(value); }}
  get innerHTML(){{ return this._innerHTML; }}
  set textContent(value){{ this._textContent = String(value); }}
  get textContent(){{ return this._textContent || ''; }}
}}
global.window = {{}};
global.document = {{ createElement(tag){{ return new FakeElement(tag); }} }};
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
const li = name => `<i data-icon="${{name}}"></i>`;
const renderMd = text => esc(text);
function buildToolCard(){{ throw new Error('tool branch should not execute'); }}
function _thinkingActivityNode(){{ throw new Error('thinking branch should not execute'); }}
function _activityStatusNode(){{ throw new Error('status branch should not execute'); }}
function _anchorSceneToolRowLogicalKey(){{ return ''; }}
function _anchorSceneMergeToolRows(_prev, row){{ return row; }}
eval(extractFunc('_anchorSceneIsSettledSuccessfulCompression'));
eval(extractFunc('_anchorSceneRowsForRendering'));
eval(extractFunc('_autoCompressionPreviewText'));
eval(extractFunc('_autoCompressionWorklogNode'));
eval(extractFunc('_anchorSceneNodeForRow'));
const compressing = {{
  row_id:'compressing-1',
  role:'lifecycle',
  kind:'lifecycle_status',
  source_event_type:'compressing',
  status:'running',
  text:'Compressing context',
}};
const compressed = {{
  row_id:'compressed-1',
  role:'lifecycle',
  kind:'lifecycle_status',
  source_event_type:'compressed',
  status:'completed',
  text:'Context auto-compressed',
}};
const runningRows = _anchorSceneRowsForRendering({{activity_rows:[compressing]}}, {{settled:false}});
const completedRows = _anchorSceneRowsForRendering({{activity_rows:[compressing, compressed]}}, {{settled:false}});
const settledRows = _anchorSceneRowsForRendering({{activity_rows:[compressing, compressed]}}, {{settled:true}});
const runningNode = _anchorSceneNodeForRow(runningRows[0], {{live:true, settled:false}});
const completedNode = _anchorSceneNodeForRow(completedRows[0], {{live:true, settled:false}});
process.stdout.write(JSON.stringify({{
  runningCount: runningRows.length,
  runningSource: runningRows[0] && runningRows[0].source_event_type,
  runningRole: runningNode && runningNode.getAttribute('data-anchor-row-role'),
  runningHtml: runningNode && runningNode.innerHTML,
  completedCount: completedRows.length,
  completedSource: completedRows[0] && completedRows[0].source_event_type,
  completedRole: completedNode && completedNode.getAttribute('data-anchor-row-role'),
  completedHtml: completedNode && completedNode.innerHTML,
  settledCount: settledRows.length,
}}));
"""
    data = _run_node_script(script)

    assert data["runningCount"] == 1
    assert data["runningSource"] == "compressing"
    assert data["runningRole"] == "lifecycle"
    assert "auto-compression-divider-done" not in data["runningHtml"]
    assert data["completedCount"] == 1
    assert data["completedSource"] == "compressed"
    assert data["completedRole"] == "lifecycle"
    assert "auto-compression-divider-done" in data["completedHtml"]
    assert data["settledCount"] == 0


def test_process_prose_is_an_anchor_scene_row_not_a_dom_mirror():
    schedule = _function_body(MESSAGES_JS, "_scheduleRender")
    flush = _function_body(MESSAGES_JS, "_flushPendingSegmentRender")

    assert "_upsertAnchorProcessProse(displayText,{sealed:force})" in flush
    assert "function _upsertAnchorProcessProse" in MESSAGES_JS
    assert "source_event_type:sourceEventType" in _function_body(MESSAGES_JS, "_applyToAnchor")
    assert "_upsertAnchorProcessProse(displayText)" in schedule
    assert "function _replaceAnchorActivityEventByLocalId" in MESSAGES_JS
    assert "events[i]=next" in MESSAGES_JS
    assert "_renderAnchorLiveScene();" in _function_body(MESSAGES_JS, "_upsertAnchorProcessProse")


def test_already_streamed_interim_does_not_duplicate_token_prose_in_anchor():
    interim = _event_listener_body(MESSAGES_JS, "interim_assistant")

    already_idx = interim.index("if(alreadyStreamed)")
    return_idx = interim.index("return;", already_idx)
    apply_idx = interim.index("_applyToAnchor('interim_assistant'", return_idx)
    assert already_idx < return_idx < apply_idx


def test_tool_boundaries_seal_prose_before_tool_rows_enter_anchor_scene():
    tool = _event_listener_body(MESSAGES_JS, "tool")
    complete = _event_listener_body(MESSAGES_JS, "tool_complete")

    assert tool.index("_upsertAnchorProcessProse(pendingDisplayTextBeforeTool") < tool.index(
        "_applyToAnchor('tool'"
    )
    assert complete.index("_upsertAnchorProcessProse(pendingDisplayTextBeforeComplete") < complete.index(
        "_applyToAnchor('tool_complete'"
    )


def test_live_ui_legacy_paths_exit_when_anchor_scene_owns_the_turn():
    for fn_name in [
        "appendLiveToolCard",
        "appendLiveCompressionCard",
        "appendThinking",
        "ensureLiveWorklogShell",
        "_syncLiveWorklogReasonsForAnchor",
    ]:
        body = _function_body(UI_JS, fn_name)
        assert "isLiveAnchorActivitySceneOwner" in body
        assert "_renderLiveAnchorActivitySceneForStream" in body or fn_name == "_syncLiveWorklogReasonsForAnchor"

    remove = _function_body(UI_JS, "removeThinking")
    assert '.agent-activity-thinking:not([data-anchor-scene-row="1"])' in remove
    assert ':not([data-anchor-scene-owner="1"])' in remove


@pytest.mark.skipif("_anchorSceneActiveMode()" not in MESSAGES_JS, reason="base branch lacks the active-mode helper")
def test_anchor_scene_projection_tracks_active_mode():
    render_live = _function_body(MESSAGES_JS, "_renderAnchorLiveScene")
    project_live = _function_body(MESSAGES_JS, "_projectLiveAnchorActivityScene")

    assert "_anchorSceneActiveMode()" in render_live
    assert "_anchorSceneActiveMode()" in project_live
    assert "mode:'compact_worklog'" not in render_live
    assert "mode:'compact_worklog'" not in project_live


@pytest.mark.skipif("_anchorSceneActiveMode()" not in MESSAGES_JS, reason="base branch lacks the active-mode helper")
def test_anchor_scene_active_mode_falls_back_when_primary_accessor_throws():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "messages.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
eval(extractFunc('_anchorSceneActiveMode'));
global.window = {{
  chatActivityMode(){{ throw new Error('mode unavailable'); }},
  _chatActivityDisplayMode:'transparent_stream',
  _transparentStream:false,
}};
const displayModeFallback = _anchorSceneActiveMode();
global.window = {{
  chatActivityMode(){{ throw new Error('mode unavailable'); }},
  _chatActivityDisplayMode:'broken',
  _transparentStream:true,
}};
const booleanFallback = _anchorSceneActiveMode();
process.stdout.write(JSON.stringify({{displayModeFallback, booleanFallback}}));
"""
    result = _run_node_script(script)

    assert result["displayModeFallback"] == "transparent_stream"
    assert result["booleanFallback"] == "transparent_stream"


def test_complete_settled_anchor_scene_tracks_transparent_stream_and_filters_duplicate_final_answer():
    result = _run_complete_anchor_settlement_case("transparent_stream")

    assert result["mode"] == "transparent_stream"
    assert result["final_answer"] == "Final answer text"
    assert result["final_message_ref"] == "assistant-1"
    assert result["identity"]["source_message_refs"] == ["assistant-1"]
    assert [row["role"] for row in result["activity_rows"]] == ["thinking", "tool", "terminal"]
    assert [row["status"] for row in result["activity_rows"]] == ["completed", "completed", "completed"]
    assert [row["display_hint"] for row in result["activity_rows"]] == [
        "chronological_activity",
        "chronological_activity",
        "chronological_activity",
    ]
    assert [row["compact_hint"] for row in result["activity_rows"]] == [
        "collapsed_thinking",
        "tool_row",
        "terminal_status_row",
    ]
    assert [row["transparent_hint"] for row in result["activity_rows"]] == [
        "chronological_activity",
        "chronological_activity",
        "chronological_activity",
    ]


def test_live_processed_anchor_renders_before_first_activity_row():
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    shell = _function_body(UI_JS, "ensureLiveWorklogShell")
    send = _function_body(MESSAGES_JS, "send")

    assert "if(!rows.length) return false;" not in live
    assert "if(!ok){" in live
    assert "_syncToolCallGroupSummary(group);" in live
    assert "_startActivityElapsedTimer(group)" in live
    assert "turnStartedAt:S.session&&S.session.pending_started_at" in live
    assert "if(!S.session) return null;" in shell
    assert "const activeStreamId=S.activeStreamId||'';" in shell
    assert "_renderLiveAnchorActivitySceneForStream(activeStreamId, S.session.session_id, {mode:'compact_worklog'})" in shell
    assert "if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();" in send


def test_live_processed_anchor_starts_before_chat_start_returns_stream_id():
    send = _function_body(MESSAGES_JS, "send")

    optimistic_idx = send.index("S.messages.push(userMsg);renderMessages();setBusy(true);")
    started_idx = send.index("if(S.session&&!S.session.pending_started_at) S.session.pending_started_at=Date.now()/1000;", optimistic_idx)
    ensure_idx = send.index("if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();", optimistic_idx)
    fallback_idx = send.index("else appendThinking('',{pending:true});", ensure_idx)
    chat_start_idx = send.index("const startData=await api('/api/chat/start'")
    assert optimistic_idx < started_idx < ensure_idx < fallback_idx < chat_start_idx


def test_live_processed_anchor_rekeys_when_stream_id_is_known():
    send = _function_body(MESSAGES_JS, "send")
    shell = _function_body(UI_JS, "ensureLiveWorklogShell")

    stream_idx = send.index("S.activeStreamId = streamId;")
    pending_idx = send.index("S.session.pending_started_at=startData.pending_started_at;")
    ensure_idx = send.index("if(typeof ensureLiveWorklogShell==='function') ensureLiveWorklogShell();", pending_idx)
    stop_idx = send.index("if(typeof updateSendBtn==='function') updateSendBtn();", ensure_idx)
    assert stream_idx < pending_idx < ensure_idx < stop_idx

    compact_idx = shell.index("const compactWorklog=typeof isCompactWorklogMode")
    legacy_idx = shell.index("if(!compactWorklog&&!isSimplifiedToolCalling())")
    timer_idx = shell.index("if(typeof _startActivityElapsedTimer==='function') _startActivityElapsedTimer(group);")
    assert compact_idx < legacy_idx < timer_idx


def test_live_processed_anchor_timer_falls_back_after_real_started_at():
    timer = _function_body(UI_JS, "_startActivityElapsedTimer")

    set_idx = timer.index("_setActivityElapsedStartedAt(group);")
    fallback_idx = timer.index("if(!group.getAttribute('data-turn-started-at'))")
    update_idx = timer.index("_updateActiveActivityElapsedTimer();")
    assert set_idx < fallback_idx < update_idx
    assert "String(_activityNowSeconds())" in timer


def test_server_started_turn_also_creates_processed_anchor_before_stop_button_refresh():
    listener = _event_listener_body(MESSAGES_JS, "server_turn_started")

    pending_idx = listener.index("S.session.pending_started_at = d.pending_started_at")
    ensure_idx = listener.index("if (typeof ensureLiveWorklogShell === 'function') ensureLiveWorklogShell();")
    stop_idx = listener.index("if (typeof updateSendBtn === 'function') updateSendBtn();")
    assert pending_idx < ensure_idx < stop_idx

    assert "else if (!S.session.pending_started_at) S.session.pending_started_at = Date.now()/1000;" in listener
    assert "if (typeof appendThinking === 'function') appendThinking();" in listener


def test_server_started_turn_payload_carries_pending_started_at():
    recovery = ROUTES_PY.split("source\": \"subscribe_recovery\"", 1)[0].rsplit("try:", 1)[-1]
    assert "recover_session = get_session(sid, metadata_only=True)" in recovery
    assert "pending_started_at = getattr(recover_session, \"pending_started_at\", None)" in recovery
    assert '"pending_started_at": pending_started_at' in ROUTES_PY
    assert '"pending_started_at": getattr(session, "pending_started_at", None)' not in ROUTES_PY
    assert '"pending_started_at": (resp or {}).get("pending_started_at")' in ROUTES_PY


def test_live_processed_anchor_is_deduped_across_restore_paths():
    dedupe = _function_body(UI_JS, "_dedupeLiveProcessedWorklogAnchors")
    score = _function_body(UI_JS, "_liveProcessedWorklogAnchorScore")
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    restore = _function_body(UI_JS, "restoreLiveTurnHtmlForSession")
    shell = _function_body(UI_JS, "ensureLiveWorklogShell")

    assert ".tool-worklog-group[data-tool-worklog-group=\"1\"]" in dedupe
    assert ".live-worklog[data-live-worklog-shell=\"1\"]" in dedupe
    assert "if(groups.length<=1)" in dedupe
    assert "group.remove()" in dedupe
    assert "hasElapsed" in score and "hasRows" in score
    assert "_dedupeLiveProcessedWorklogAnchors(turn)" in live
    assert "_dedupeLiveProcessedWorklogAnchors(restored)" in restore
    assert "_dedupeLiveProcessedWorklogAnchors($('liveAssistantTurn'))" in shell


def test_complete_settled_anchor_scene_keeps_compact_worklog_rows_in_compact_mode():
    result = _run_complete_anchor_settlement_case("compact_worklog")

    assert result["mode"] == "compact_worklog"
    assert result["final_answer"] == "Final answer text"
    assert [row["role"] for row in result["activity_rows"]] == ["thinking", "tool", "terminal"]
    assert [row["display_hint"] for row in result["activity_rows"]] == [
        "collapsed_thinking",
        "tool_row",
        "terminal_status_row",
    ]
    assert [row["compact_hint"] for row in result["activity_rows"]] == [
        "collapsed_thinking",
        "tool_row",
        "terminal_status_row",
    ]
    assert [row["transparent_hint"] for row in result["activity_rows"]] == [
        "chronological_activity",
        "chronological_activity",
        "chronological_activity",
    ]


def test_anchor_scene_has_worklog_worthy_rows_rejects_prose_only_and_terminal_only_scenes():
    body = _function_body(MESSAGES_JS, "_anchorSceneHasWorklogWorthyRows")

    assert "role==='tool'||role==='thinking'" in body
    assert "source==='compressing'||source==='compressed'" in body

    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "messages.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
eval(extractFunc('_anchorSceneHasWorklogWorthyRows'));
const proseOnly = {{activity_rows:[{{role:'prose'}}, {{role:'terminal', source_event_type:'done'}}]}};
const terminalOnly = {{activity_rows:[{{role:'terminal', source_event_type:'done'}}]}};
process.stdout.write(JSON.stringify({{
  proseOnly: _anchorSceneHasWorklogWorthyRows(proseOnly),
  terminalOnly: _anchorSceneHasWorklogWorthyRows(terminalOnly),
}}));
"""
    result = _run_node_script(script)

    assert result["proseOnly"] is False
    assert result["terminalOnly"] is False


def test_scene_renderer_coalesces_row_updates_and_renders_in_scene_order():
    rows = _function_body(UI_JS, "_anchorSceneRowsForRendering")
    render = _function_body(UI_JS, "_renderAnchorSceneRowsIntoWorklog")
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")

    assert "const live=!settled" in rows
    assert "const liveProseTextKeys=new Map()" in rows
    assert "if(textKey&&liveProseTextKeys.has(textKey)) continue;" in rows
    assert "byKey.set(key,out.length)" in rows
    assert "out[index]=row.role==='tool'?_anchorSceneMergeToolRows(out[index],row):row" in rows
    assert "for(const row of rows)" in render
    assert "_anchorSceneNodeForRow(row,opts)" in render
    assert "blocks.querySelectorAll('[data-live-assistant=\"1\"]')" in live
    assert "assistant-segment-worklog-source" in live
    assert "el.hidden=true" in live


@pytest.mark.skipif(NODE is None, reason="node is required for anchor row normalization tests")
def test_live_anchor_scene_dedupes_exact_duplicate_process_prose_only_live():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
function _anchorSceneToolRowLogicalKey(){{ return ''; }}
function _anchorSceneMergeToolRows(a,b){{ return b; }}
function _anchorSceneIsSettledSuccessfulCompression(){{ return false; }}
eval(extractFunc('_anchorSceneRowsForRendering'));
const scene = {{
  activity_rows: [
    {{role:'prose', local_id:'reasoning:291', text:'same process prose'}},
    {{role:'prose', local_id:'interim:293', text:' same\\nprocess prose '}},
    {{role:'thinking', local_id:'thinking:1', text:'same process prose'}},
    {{role:'prose', local_id:'process:294', text:'new process prose'}}
  ]
}};
const liveRows = _anchorSceneRowsForRendering(scene, {{settled:false}});
const settledRows = _anchorSceneRowsForRendering(scene, {{settled:true}});
console.log(JSON.stringify({{
  live: liveRows.map(row => row.role + ':' + row.text.replace(/\\s+/g, ' ').trim()),
  settled: settledRows.map(row => row.role + ':' + row.text.replace(/\\s+/g, ' ').trim())
}}));
"""
    result = _run_node_script(script)

    assert result["live"] == [
        "prose:same process prose",
        "thinking:same process prose",
        "prose:new process prose",
    ]
    assert result["settled"] == [
        "prose:same process prose",
        "prose:same process prose",
        "thinking:same process prose",
        "prose:new process prose",
    ]


def test_live_anchor_scene_removes_legacy_interim_collapse_toggle():
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    interim = _event_listener_body(MESSAGES_JS, "interim_assistant")

    cleanup_idx = live.index(".interim-collapse-toggle")
    hide_idx = live.index("blocks.querySelectorAll('[data-live-assistant=\"1\"]')")
    group_idx = live.index("const group=_anchorSceneWorklogGroup")
    assert cleanup_idx < hide_idx < group_idx

    guard_idx = interim.index("data-anchor-scene-live-owner")
    remove_idx = interim.index("blocks.querySelectorAll('.interim-collapse-toggle').forEach(el=>el.remove())")
    legacy_create_idx = interim.index("let toggle=blocks.querySelector('.interim-collapse-toggle')")
    assert guard_idx < remove_idx < legacy_create_idx


def test_recycled_assistant_turn_clears_live_anchor_attrs_before_role_refresh():
    reset_attrs = UI_JS[UI_JS.index("const _recycleResetAttrs="):UI_JS.index("let _scrollbarDragActive=false;")]
    assert "data-anchor-scene-live-owner" in reset_attrs
    assert "data-anchor-stream-id" in reset_attrs
    assert "data-live-assistant-turn" in reset_attrs

    recycle = _function_body(UI_JS, "renderMessages")
    recycle = recycle[recycle.index("if(!currentAssistantTurn){"):]

    loop_idx = recycle.index("for(const attr of _recycleResetAttrs) recycled.removeAttribute(attr);")
    refresh_idx = recycle.index("if(role) role.outerHTML=_assistantRoleHtml(tsTitle, isTpsDisplayEnabled()?_formatTurnTps(m._turnTps):'');")
    assert loop_idx < refresh_idx


def test_tool_scene_rows_coalesce_by_logical_tool_call_identity():
    rows = _function_body(UI_JS, "_anchorSceneRowsForRendering")
    key = _function_body(UI_JS, "_anchorSceneToolRowLogicalKey")
    merge = _function_body(UI_JS, "_anchorSceneMergeToolRows")

    assert "function _anchorSceneToolRowLogicalKey" in UI_JS
    assert "if(row.role==='tool') return `tool:${_anchorSceneToolRowLogicalKey(row)||row.row_id||row.event_id||row.local_id||out.length}`" in rows
    assert "row.tool_call_id||tool.id||tool.tid||tool.tool_call_id||tool.tool_use_id||tool.call_id" in key
    assert "payload.tid||payload.id||payload.tool_call_id||payload.tool_use_id||payload.call_id" in key
    assert "mergedTool.args=prevArgs" in merge
    assert "mergedTool.preview=prevTool.preview||prevPayload.preview||prevPreview" in merge
    assert "row_id:prev.row_id||row.row_id" in merge


def test_anchor_tool_result_text_stays_out_of_header_preview():
    tool = _function_body(UI_JS, "_anchorSceneToolCallFromRow")
    row_builder = _function_body(MESSAGES_JS, "_anchorSceneToolRowFromCall")

    assert "command:tool.command||payload.command||payload.cmd||''," in tool
    assert "const command=" in row_builder
    assert "args&&(args.cmd||args.command)" in row_builder
    assert "preview:tool.preview||payload.preview||''," in tool
    assert "preview:tool.preview||payload.preview||row.text||''" not in tool
    assert "row&&row.status!=='running'&&row.status!=='pending'?row.text:''" in tool


def test_scene_renderer_allows_prose_tool_prose_tool_interleaving():
    render = _function_body(UI_JS, "_renderAnchorSceneRowsIntoWorklog")

    assert "currentTools=null;" in render
    assert render.index("if(row.role==='tool')") < render.index("}else{")
    assert render.index("currentTools=null;") < render.index("list.appendChild(node);")


def test_anchor_tool_preview_slot_stays_empty_for_result_text():
    assert ".tool-worklog-list .tool-card-preview" in STYLE_CSS
    preview_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-list .tool-card-title"):
        STYLE_CSS.index(".tool-worklog-list .tool-card-header:hover", STYLE_CSS.index(".tool-worklog-list .tool-card-title"))
    ]
    assert ".tool-worklog-list .tool-card-preview" in preview_rule
    assert "display:block" in preview_rule
    preview = _function_body(UI_JS, "_toolCardPreviewText")
    assert "const explicit=String(tc&&tc.preview||'').trim();" not in preview
    assert "if(explicit) return explicit;" not in preview


def test_anchor_tool_rows_are_action_labeled_iconed_and_single_line():
    build = _function_body(UI_JS, "buildToolCard")
    sync = _function_body(UI_JS, "_syncToolRowsContainer")
    icon = _function_body(UI_JS, "toolIcon")
    summary = _function_body(UI_JS, "_syncToolCallGroupSummary")

    assert "_toolActionLabelText(tc,{limit:112})" in build
    assert "const hasRawDetail=!!(tc.snippet)" in build
    assert "_toolCardAllowsDetail(toolKind,tc)" in build
    assert "previewText===argPreview" in build
    assert "previewText==='Completed'||previewText==='Running'||previewText==='Failed'" in build
    assert "skill_view" in icon and "book-open" in icon
    assert "tool-worklog-tool-group-icon" in sync
    assert "_toolGroupIcon(rows)" in sync
    assert "wasOpen||_worklogDetailsExpandedDefault()" in sync
    assert "data-anchor-scene-owner')!=='1'" not in summary
    assert "if(group.getAttribute('data-tool-worklog-group')==='1') _syncToolWorklogToolGroup(group);" in summary

    assert ".tool-worklog-list .tool-card-icon" in STYLE_CSS
    icon_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-list .tool-card-icon"):
        STYLE_CSS.index(".tool-worklog-list .tool-card-name", STYLE_CSS.index(".tool-worklog-list .tool-card-icon"))
    ]
    assert "display:inline-flex" in icon_rule
    name_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-list .tool-card-name"):
        STYLE_CSS.index(".tool-worklog-list .tool-card-title", STYLE_CSS.index(".tool-worklog-list .tool-card-name"))
    ]
    assert "text-overflow:ellipsis" in name_rule
    assert "white-space:nowrap" in name_rule
    group_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-tool-group-head"):
        STYLE_CSS.index(".tool-worklog-tool-group-head:hover", STYLE_CSS.index(".tool-worklog-tool-group-head"))
    ]
    assert "overflow:hidden" in group_rule and "white-space:nowrap" in group_rule


def test_tool_worklog_action_summaries_are_i18n_backed():
    assert "tool_action_label" in UI_JS
    assert "tool_worklog_summary" in UI_JS
    assert "tool_summary_join" in UI_JS
    assert "tool_target_skill_suffix" in UI_JS

    assert "tool_action_label" in I18N_JS
    assert "tool_worklog_summary" in I18N_JS
    assert "tool_summary_join" in I18N_JS
    assert "worklog_thinking" in I18N_JS
    assert "已读取" in I18N_JS and "已搜索代码" in I18N_JS
    assert "已讀取" in I18N_JS and "已搜尋程式碼" in I18N_JS
    assert "正在思考" in I18N_JS


def test_live_anchor_scene_rerender_preserves_inner_tool_detail_state():
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")

    capture_idx = live.index("const liveDisclosureState=")
    remove_idx = live.index("blocks.querySelectorAll('[data-anchor-scene-owner=\"1\"],[data-anchor-scene-row=\"1\"]')")
    render_idx = live.index("const ok=_renderAnchorSceneRowsIntoWorklog")
    restore_idx = live.index("_restoreWorklogDetailDisclosureState(blocks, liveDisclosureState)")
    assert capture_idx < remove_idx < render_idx < restore_idx


def test_settled_anchor_scene_carries_live_disclosure_state_by_stream():
    settled = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    group = _function_body(UI_JS, "_anchorSceneWorklogGroup")
    key = _function_body(UI_JS, "_worklogDetailBaseKey")

    assert "const streamId=String(message._anchor_stream_id||scene.stream_id||scene.identity&&scene.identity.stream_id||'');" in settled
    assert "_copyActivityDisclosureState(`live:${streamId}`, activityKey)" in settled
    assert "streamId," in settled
    assert "data-anchor-stream-id" in group
    assert "stream:${activity.getAttribute('data-anchor-stream-id')}" in key


def test_live_footer_owner_guard_blocks_stale_session_updates():
    update = _function_body(UI_JS, "updateLiveRunStatus")
    hide = _function_body(UI_JS, "hideLiveRunStatus")

    assert "opts&&opts.sessionId&&_liveRunStatusSessionId&&opts.sessionId!==_liveRunStatusSessionId" in update
    assert "sid&&_liveRunStatusSessionId&&sid!==_liveRunStatusSessionId" in hide


def test_settled_scene_keeps_user_visible_lifecycle_and_control_rows():
    rows = _function_body(UI_JS, "_anchorSceneRowsForRendering")
    node = _function_body(UI_JS, "_anchorSceneNodeForRow")

    assert "return 'lifecycle:compression';" in rows
    assert 'return "lifecycle:compression"' in ROUTES_PY
    assert 'if key == "lifecycle:compression":' in ROUTES_PY
    assert "return out.slice().sort" not in rows
    assert "source==='compressing'||source==='compressed'" in rows
    assert "(weight(a)-weight(b))" not in rows
    assert "_anchorSceneIsSettledSuccessfulCompression(row,settled)" in rows
    assert "}else if(row.role==='control'){" in node
    assert "}else if(!settled&&row.role==='control'){" not in node
    assert "phase:settled||row.source_event_type==='compressed'?'done':'running'" in node
    assert "kind:settled?'done':'waiting'" in node
    assert "status:settled?'done':'running'" in node
    assert "status:!settled&&row.status==='running'?'running':'done'" in node


def test_settled_successful_auto_compression_stays_live_only():
    helper = _function_body(UI_JS, "_anchorSceneIsSettledSuccessfulCompression")

    assert "source!=='compressing'&&source!=='compressed'" in helper
    assert "compression_exhausted" in helper
    assert "degraded" in helper
    assert "connection_lost" in helper


def test_settled_scene_does_not_render_tool_start_as_still_running():
    tool = _function_body(UI_JS, "_anchorSceneToolCallFromRow")
    node = _function_body(UI_JS, "_anchorSceneNodeForRow")

    assert "const settled=!!(opts&&opts.settled);" in tool
    assert "done:settled?true:" in tool
    assert "buildToolCard(_anchorSceneToolCallFromRow(row,opts))" in node


def test_stream_end_restore_attaches_projected_anchor_scene_before_render():
    restore = _function_body(MESSAGES_JS, "_restoreSettledSession")

    assert "function _attachProjectedAnchorSceneToLastAssistant" in MESSAGES_JS
    carry_idx = restore.index("const _stagedMessages=_carryForwardEphemeralTurnFields(_currentMessages, _nextMsgs3018);")
    filter_idx = restore.index("S.messages=_filterRecoveryControlMessages(_resolvedMessages || []);")
    attach_idx = restore.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);")
    render_idx = restore.index("syncTopbar();renderMessages({preserveScroll:true})")
    assert carry_idx < filter_idx < attach_idx < render_idx


def test_cancel_settlement_attaches_projected_anchor_scene_before_render():
    cancel = _event_listener_body(MESSAGES_JS, "cancel")

    fetch_idx = cancel.index("const _nextMsgs3018=(sessionPayload.messages||[]).filter(m=>m&&m.role);")
    attach_idx = cancel.index("_attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);")
    carry_idx = cancel.index("S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);")
    render_idx = cancel.index("renderMessages({preserveScroll:true});")
    assert fetch_idx < attach_idx < carry_idx < render_idx

    embedded_idx = cancel.index("if(_applyCancelSessionPayload(_cancelSessionPayload)) return;")
    fallback_get_idx = cancel.index("const data=await api(`/api/session?session_id=${encodeURIComponent(activeSid)}`);")
    fallback_apply_idx = cancel.index("if(data&&data.session) _applyCancelSessionPayload(data.session);")
    assert embedded_idx < fallback_get_idx < fallback_apply_idx

    fallback_push_idx = cancel.index("S.messages.push({role:'assistant',content:`**Task cancelled:**")
    fallback_attach_idx = cancel.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);", fallback_push_idx)
    fallback_render_idx = cancel.index("renderMessages({preserveScroll:true});", fallback_attach_idx)
    assert fallback_push_idx < fallback_attach_idx < fallback_render_idx


def test_application_error_settlement_attaches_projected_anchor_scene_before_render():
    apperror = _event_listener_body(MESSAGES_JS, "apperror")

    assert "_applyToAnchor('apperror'" in apperror
    session_idx = apperror.index("const _nextMsgs3018=(d.session.messages||[]).filter(m=>m&&m.role);")
    attach_idx = apperror.index("_attachProjectedAnchorSceneToLastAssistant(_nextMsgs3018);")
    carry_idx = apperror.index("S.messages=_carryForwardEphemeralTurnFields(S.messages||[], _nextMsgs3018);")
    render_idx = apperror.index("renderMessages({preserveScroll:true});")
    assert session_idx < attach_idx < carry_idx < render_idx

    synthetic_push_idx = apperror.index("S.messages.push({role:'assistant',content:`**${label}:**")
    synthetic_attach_idx = apperror.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);", synthetic_push_idx)
    assert synthetic_push_idx < synthetic_attach_idx < render_idx


def test_connection_error_terminal_message_attaches_projected_anchor_scene_before_render():
    error = _function_body(MESSAGES_JS, "_handleStreamError")

    assert "_applyToAnchor('error'" in error
    push_idx = error.index("_ensureSingleTerminalStreamErrorMarker(S.messages);")
    attach_idx = error.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);")
    render_idx = error.index("renderMessages({preserveScroll:true});")
    assert push_idx < attach_idx < render_idx


def test_settled_final_answer_gets_anchor_activity_above_it():
    done = _event_listener_body(MESSAGES_JS, "done")
    settled = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    group = _function_body(UI_JS, "_anchorSceneWorklogGroup")
    attach = _function_body(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant")

    assert "_attachProjectedAnchorSceneToLastAssistant(S.messages);" in done
    assert "lastAsst._anchor_activity_scene=scene" in attach
    assert "beforeAnchor:true" in settled
    assert "collapsed:true" in settled or "live:false" in settled
    assert "syncAnchorReason:false" in group
    assert "_renderSettledAnchorSceneForMessage(msg, seg, rawIdx)" in UI_JS


def test_done_sets_turn_duration_before_persisting_anchor_scene():
    done = _event_listener_body(MESSAGES_JS, "done")

    duration_idx = done.index("lastAsst._turnDuration=d.usage.duration_seconds;")
    attach_idx = done.index("_attachProjectedAnchorSceneToLastAssistant(S.messages);")
    assert duration_idx < attach_idx


def test_settled_anchor_scene_is_persisted_as_ui_metadata():
    attach = _function_body(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant")
    persist = _function_body(MESSAGES_JS, "_persistSettledAnchorScene")
    msg_ref = _function_body(MESSAGES_JS, "_anchorSceneMessageRef")

    assert "_persistSettledAnchorScene(lastAsst, scene, lastAsstIndex);" in attach
    assert "api('/api/session/anchor-scene'" in persist
    assert "session_id:activeSid" in persist
    assert "stream_id:streamId" in persist
    assert "const messageOffset=_anchorSceneMessageOffsetForPersist();" in persist
    assert "message_index:_anchorSceneAbsoluteMessageIndexForPersist(messageIndex,messageOffset)" in persist
    assert "message_window_index:messageIndex" in persist
    assert "message_offset:messageOffset" in persist
    assert "message_ref:_anchorSceneMessageRef(message)" in persist
    assert "timeoutToast:false" in persist
    assert "console.warn('anchor activity scene persistence failed',err)" in persist
    assert "content:String(content||'').replace(/\\s+/g,' ').trim()" in msg_ref


def test_settled_anchor_scene_persists_the_full_assistant_turn_not_only_tail():
    attach = _function_body(MESSAGES_JS, "_attachProjectedAnchorSceneToLastAssistant")
    complete = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    rows_by_message = _function_body(MESSAGES_JS, "_anchorSceneRowsByMessageIndex")
    reasoning_text = _function_body(MESSAGES_JS, "_anchorSceneMessageReasoningText")

    assert "const scene=_completeSettledAnchorSceneForTurn(messages,lastAsstIndex,projectedScene);" in attach
    assert "for(let idx=lastAsstIndex-1;idx>=0;idx-=1)" in complete
    assert "messages.slice(turnStart+1,lastAsstIndex+1)" in complete
    assert "message.reasoning||message._reasoning||message.reasoning_content||message.thinking" in reasoning_text
    assert "const reasoning=_anchorSceneMessageReasoningText(message);" in rows_by_message
    assert "const toolsByIdx=new Map();" in rows_by_message
    assert "if(S.toolCalls) for(const tc of S.toolCalls){" in rows_by_message
    assert "for(const tool of (toolsByIdx.get(idx)||[]))" in rows_by_message
    assert "_anchorSceneToolRowFromCall(tool,0,idx)" in rows_by_message


def test_settled_anchor_scene_preserves_live_projected_order_before_backfill():
    complete = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    overlap = _function_body(MESSAGES_JS, "_anchorSceneRowTextOverlapsExisting")

    projected_idx = complete.index("const projectedRows=Array.isArray(base.activity_rows)?base.activity_rows:[];")
    projected_push_idx = complete.index("for(const row of projectedRows){")
    backfill_idx = complete.index("for(let idx=turnStart+1;idx<=lastAsstIndex;idx+=1)")
    terminal_idx = complete.index("if(row&&row.role==='terminal') pushRow(row);", backfill_idx)
    assert projected_idx < projected_push_idx < backfill_idx < terminal_idx

    assert "const seenTextKeys=[];" in complete
    assert "_anchorSceneRowTextOverlapsExisting(textKey,seenTextKeys)" in complete
    assert "if(isTextual&&textKey) seenTextKeys.push(textKey);" in complete
    assert "rowTextKey.includes(existing)||existing.includes(rowTextKey)" in overlap


def test_settled_anchor_scene_does_not_persist_running_live_activity_rows():
    complete = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    live_identity = _function_body(MESSAGES_JS, "_anchorSceneRowHasLiveIdentity")
    settle_live = _function_body(MESSAGES_JS, "_anchorSceneSettleLiveRunningRow")

    assert "const hasSettledThinking=_anchorSceneMessageRowsHaveThinking(messageRows);" in complete
    assert "row=_anchorSceneSettleLiveRunningRow(row,hasSettledThinking);" in complete
    assert "String(value||'').startsWith('live-')" in live_identity
    assert "String(row.status||'').toLowerCase()!=='running'" in settle_live
    assert "if(row.role==='thinking'&&hasSettledThinking) return null;" in settle_live
    assert "return {...row,status:'completed'};" in settle_live


def test_settled_anchor_scene_separates_final_answer_from_activity_rows():
    complete = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    final_filter = _function_body(MESSAGES_JS, "_anchorSceneRowLooksLikeFinalAnswer")
    duration = _function_body(MESSAGES_JS, "_anchorSceneTurnDurationForSettlement")

    assert "final_answer:_anchorSceneCleanText(finalAnswer)?finalAnswer" in complete
    assert "final_message_ref:_anchorSceneMessageRef(lastAsst)" in complete
    assert "turn_duration:_anchorSceneTurnDurationForSettlement(lastAsst,base)" in complete
    assert "_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)" in complete
    assert "finalKey.startsWith(rowTextKey)||rowTextKey.startsWith(finalKey)" in final_filter
    assert "session&&session.pending_started_at" in duration
    assert "Date.now()/1000" in duration


def test_anchor_owned_settled_turn_skips_legacy_worklog_rebuild():
    render = _function_body(UI_JS, "renderMessages")

    assert "const anchorOwnedAssistantRawIdxs=new Set();" in render
    assert "msg._anchor_activity_scene" in render
    assert "anchorOwnedAssistantRawIdxs.add(idx)" in render
    assert "if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;" in render
    assert "if(anchorOwnedAssistantRawIdxs.has(rawIdx)) return;" in render
    assert "S.messages.indexOf(m)" not in render
    assert "S.messages.some((m,rawIdx)=>" in render
    assert (
        "!anchorOwnedAssistantRawIdxs.has(rawIdx)"
        "&&_legacySettledFallbackHasToolMetadata(m)"
    ) in render


def test_transparent_stream_renders_persisted_anchor_scene_after_reload():
    settled = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    transparent = _function_body(UI_JS, "_renderSettledAnchorSceneTransparentForMessage")
    row = _function_body(UI_JS, "_anchorSceneTransparentNodeForRow")
    render = _function_body(UI_JS, "renderMessages")

    assert "if(typeof isTransparentStream==='function'&&isTransparentStream())" in settled
    assert "return _renderSettledAnchorSceneTransparentForMessage(message,segment,rawIdx);" in settled
    assert "_anchorSceneRowsForRendering(scene,{settled:true})" in transparent
    assert 'blocks.querySelectorAll(\'[data-anchor-settled-scene-row="1"],.transparent-event-row[data-anchor-scene-row="1"]\')' in transparent
    # combined fix: the final answer text is computed and threaded into the row
    # renderer so intermediate prose survives while the final-answer duplicate is dropped.
    assert "_anchorSceneTransparentNodeForRow(row,{settled:true,finalAnswer})" in transparent
    assert "finalAnswer" in transparent
    assert "blocks.insertBefore(node,segment)" in transparent
    assert "_syncTransparentEventControls(turn)" in transparent
    # tool + thinking rows are rendered as transparent event rows
    assert "_decorateTransparentEventRow(_thinkingActivityNode" in row
    assert "_decorateTransparentEventRow(buildToolCard(toolCall)" in row
    assert "_transparentToolStatus(toolCall,settled)" in row
    assert 'data-anchor-settled-scene-row' in row
    assert "if(anchorOwnedAssistantRawIdxs.has(aIdx)) continue;" in render


def test_live_anchor_scene_snapshot_renders_transparent_rows_before_compact_gate():
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")
    transparent = _function_body(UI_JS, "_renderLiveAnchorActivitySceneTransparent")
    stream = _function_body(UI_JS, "_renderLiveAnchorActivitySceneForStream")
    row = _function_body(UI_JS, "_anchorSceneTransparentNodeForRow")

    transparent_gate = "return _renderLiveAnchorActivitySceneTransparent(streamId,scene,opts);"
    compact_gate = "if(typeof isCompactWorklogMode==='function'&&!isCompactWorklogMode()) return false;"
    assert transparent_gate in live
    assert compact_gate in live
    assert live.index(transparent_gate) < live.index(compact_gate), (
        "transparent live snapshots must render before the compact-only guard"
    )

    assert "_anchorSceneRowsForRendering(scene,{settled:false})" in transparent
    assert "turn.id='liveAssistantTurn'" in transparent
    assert "turn.dataset.sessionId=S.session.session_id" in transparent
    assert "turn.setAttribute('data-anchor-scene-live-owner','1')" in transparent
    assert "turn.setAttribute('data-anchor-stream-id',String(streamId||''))" in transparent
    assert "turn.setAttribute('data-live-assistant-turn','1')" in transparent
    assert "_anchorSceneTransparentNodeForRow(row,{" in transparent
    assert "live:true" in transparent
    assert "streamId:streamId||S.activeStreamId||''" in transparent
    assert "sessionId:S.session&&S.session.session_id" in transparent
    assert "_syncTransparentEventControls(turn)" in transparent
    assert "blocks.querySelectorAll('[data-live-assistant=\"1\"]').forEach" in transparent
    assert ".transparent-event-row[data-live-tid]" in transparent
    assert "[data-live-stream-owned=\"1\"]" in transparent

    assert "'transparent_stream'" in stream
    assert "_projectLiveAnchorActivitySceneForStream(streamId,mode)" in stream
    assert "data-anchor-live-scene-row" in row
    assert "data-live-stream-owned" in row
    assert "if(settled) node.setAttribute('data-anchor-settled-scene-row','1')" in row


@pytest.mark.skipif(NODE is None, reason="node is required for DOM-executed anchor render tests")
def test_live_anchor_scene_transparent_snapshot_render_is_idempotent_and_hides_legacy_rows():
    script = f"""
const assert = require('assert');
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}

class FakeElement {{
  constructor(tag='div'){{
    this.tagName=tag.toUpperCase();
    this.children=[];
    this.parentNode=null;
    this.attributes={{}};
    this.dataset={{}};
    this.style={{}};
    this.hidden=false;
    this.id='';
    this.textContent='';
    this._classes=new Set();
    const self=this;
    this.classList={{
      add(...names){{ names.forEach(name=>self._classes.add(name)); }},
      remove(...names){{ names.forEach(name=>self._classes.delete(name)); }},
      contains(name){{ return self._classes.has(name); }},
      toggle(name, force){{
        const on=force===undefined?!self._classes.has(name):!!force;
        if(on) self._classes.add(name); else self._classes.delete(name);
        return on;
      }},
    }};
  }}
  get parentElement(){{ return this.parentNode; }}
  get firstChild(){{ return this.children[0]||null; }}
  setAttribute(name,value){{
    const str=String(value);
    this.attributes[name]=str;
    if(name==='id') this.id=str;
    if(name.startsWith('data-')){{
      const key=name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      this.dataset[key]=str;
    }}
  }}
  getAttribute(name){{ return Object.prototype.hasOwnProperty.call(this.attributes,name)?this.attributes[name]:null; }}
  hasAttribute(name){{ return Object.prototype.hasOwnProperty.call(this.attributes,name); }}
  removeAttribute(name){{
    delete this.attributes[name];
    if(name==='id') this.id='';
    if(name.startsWith('data-')){{
      const key=name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase());
      delete this.dataset[key];
    }}
  }}
  appendChild(child){{
    if(child.parentNode) child.remove();
    child.parentNode=this;
    this.children.push(child);
    return child;
  }}
  insertBefore(child, ref){{
    if(child.parentNode) child.remove();
    child.parentNode=this;
    const idx=this.children.indexOf(ref);
    if(idx<0) this.children.push(child);
    else this.children.splice(idx,0,child);
    return child;
  }}
  remove(){{
    if(!this.parentNode) return;
    const siblings=this.parentNode.children;
    const idx=siblings.indexOf(this);
    if(idx>=0) siblings.splice(idx,1);
    this.parentNode=null;
  }}
  matches(selector){{ return matchesSelector(this, selector); }}
  querySelector(selector){{ return this.querySelectorAll(selector)[0]||null; }}
  querySelectorAll(selector){{
    const out=[];
    const walk=(node)=>{{
      for(const child of node.children){{
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }}
    }};
    walk(this);
    return out;
  }}
  closest(selector){{
    let node=this;
    while(node){{
      if(matchesSelector(node, selector)) return node;
      node=node.parentNode;
    }}
    return null;
  }}
}}
function matchesSelector(el, selector){{
  return String(selector||'').split(',').some(part=>matchesSimple(el, part.trim()));
}}
function matchesSimple(el, selector){{
  if(!selector) return false;
  selector=selector.replace(/^:scope\\s*>\\s*/, '').trim();
  if(selector.includes(' ')) selector=selector.split(/\\s+/).pop();
  const idMatch=selector.match(/#([A-Za-z0-9_-]+)/);
  if(idMatch && el.id!==idMatch[1]) return false;
  for(const match of selector.matchAll(/\\.([A-Za-z0-9_-]+)/g)){{
    if(!el._classes.has(match[1])) return false;
  }}
  for(const match of selector.matchAll(/\\[([^=\\]]+)(?:="([^"]*)")?\\]/g)){{
    const attr=match[1];
    const expected=match[2];
    if(!el.hasAttribute(attr)) return false;
    if(expected!==undefined && el.getAttribute(attr)!==expected) return false;
  }}
  return !!(idMatch || selector.includes('.') || selector.includes('['));
}}
function findById(root, id){{
  if(root.id===id) return root;
  for(const child of root.children){{
    const found=findById(child,id);
    if(found) return found;
  }}
  return null;
}}

const emptyState=new FakeElement('div');
const msgInner=new FakeElement('div');
const messages=new FakeElement('div');
messages.scrollHeight=1000;
messages.scrollTop=1000;
messages.clientHeight=500;
global.document={{createElement:(tag)=>new FakeElement(tag)}};
global.window={{}};
global.CSS={{escape:(value)=>String(value)}};
global.requestAnimationFrame=(fn)=>fn();
global.S={{session:{{session_id:'sid-1', pending_started_at:123}}, activeStreamId:'stream-1'}};
global.$=(id)=>{{
  if(id==='emptyState') return emptyState;
  if(id==='msgInner') return msgInner;
  if(id==='messages') return messages;
  return findById(msgInner,id);
}};
let transparentMode=true;
global.isTransparentStream=()=>transparentMode;
global.isCompactWorklogMode=()=>!transparentMode;
global._anchorSceneRowsForRendering=(scene)=>scene.activity_rows||[];
global._createAssistantTurn=()=>{{ const el=new FakeElement('div'); el.classList.add('assistant-turn'); return el; }};
global._assistantTurnBlocks=(turn)=>turn;
global._captureMessageScrollSnapshot=()=>({{scrollHeight:1000}});
global._prepareLiveAnchorScrollRebuildGuard=()=>({{readerAwayFromBottom:false, release:null}});
global._restoreMessageScrollSnapshotSameFrame=()=>{{}};
global.scrollIfPinned=()=>{{}};
global._moveLiveRunStatusToTurnEnd=()=>{{}};
global._messageUserUnpinned=false;
global._anchorSceneNodeForRow=(row)=>{{ const node=new FakeElement('div'); node.classList.add('assistant-segment'); node.textContent=row.text||''; return node; }};
global._thinkingActivityNode=(text)=>{{ const node=new FakeElement('div'); node.classList.add('agent-activity-thinking'); node.textContent=text||''; return node; }};
global._anchorSceneToolCallFromRow=(row)=>({{name:row.tool&&row.tool.name||row.tool_name||'tool', done:true}});
global.buildToolCard=(toolCall)=>{{ const node=new FakeElement('div'); node.classList.add('tool-card-row'); node.setAttribute('data-tool-name',toolCall.name); return node; }};
global._decorateTransparentEventRow=(node,opts)=>{{
  node.classList.add('transparent-event-row');
  node.setAttribute('data-transparent-event-row','1');
  node.setAttribute('data-event-type',opts.type);
  if(opts.text) node.setAttribute('data-text',opts.text);
  if(opts.name) node.setAttribute('data-tool-name',opts.name);
  if(opts.status) node.setAttribute('data-event-status',opts.status);
  return node;
}};
global._transparentToolStatus=()=>'Completed';
let syncCalls=0;
global._syncTransparentEventControls=(turn)=>{{
  syncCalls++;
  turn.setAttribute('data-sync-count',String(syncCalls));
}};
let compactGroups=0;
let compactRenders=0;
global._captureWorklogDetailDisclosureState=()=>null;
global._restoreWorklogDetailDisclosureState=()=>{{}};
global._startActivityElapsedTimer=()=>{{}};
global._dedupeLiveProcessedWorklogAnchors=()=>{{}};
global._anchorSceneWorklogGroup=(blocks, opts)=>{{
  compactGroups++;
  const group=new FakeElement('div');
  group.classList.add('tool-worklog-group');
  group.setAttribute('data-anchor-scene-owner','1');
  group.setAttribute('data-anchor-stream-id',opts.streamId||'');
  blocks.appendChild(group);
  return group;
}};
global._renderAnchorSceneRowsIntoWorklog=(group, rows)=>{{
  compactRenders++;
  group.setAttribute('data-rendered-count',String(rows.length));
  return true;
}};
global._syncToolCallGroupSummary=()=>{{}};

    eval(extractFunc('_anchorSceneTransparentNodeForRow'));
    eval(extractFunc('renderLiveAnchorActivityScene'));
    eval(extractFunc('_transparentLiveRowKey'));
    eval(extractFunc('_transparentLiveRowsCompatible'));
    eval(extractFunc('_transparentLiveRowAttributePairs'));
    eval(extractFunc('_transparentLiveRowInteractiveState'));
    eval(extractFunc('_rehydrateTransparentLiveRow'));
    eval(extractFunc('_refreshTransparentThinkingLiveRow'));
    eval(extractFunc('_refreshTransparentLiveRow'));
    eval(extractFunc('_renderLiveAnchorActivitySceneTransparent'));

const existingTurn=global._createAssistantTurn();
existingTurn.id='liveAssistantTurn';
existingTurn.dataset.sessionId='sid-1';
msgInner.appendChild(existingTurn);
const legacyOne=new FakeElement('div');
legacyOne.setAttribute('data-live-assistant','1');
legacyOne.textContent='legacy process one';
existingTurn.appendChild(legacyOne);
const legacyTwo=new FakeElement('div');
legacyTwo.setAttribute('data-live-assistant','1');
legacyTwo.textContent='legacy process two';
existingTurn.appendChild(legacyTwo);
const staleTool=new FakeElement('div');
staleTool.classList.add('transparent-event-row');
staleTool.classList.add('tool-card-row');
staleTool.setAttribute('data-live-tid','legacy-tool');
existingTurn.appendChild(staleTool);

const scene={{
  version:'activity_scene_v1',
  activity_rows:[
    {{row_id:'p1', role:'prose', source_event_type:'process_prose', text:'progress one'}},
    {{row_id:'t1', role:'tool', source_event_type:'tool_completed', tool:{{name:'read_file'}}, tool_call_id:'call-1'}},
    {{row_id:'r1', role:'thinking', source_event_type:'reasoning', text:'thinking'}},
    {{row_id:'p2', role:'prose', source_event_type:'process_prose', text:'progress two'}},
  ],
}};

assert.strictEqual(renderLiveAnchorActivityScene('stream-1', scene, {{sessionId:'sid-1'}}), true);
assert.strictEqual(renderLiveAnchorActivityScene('stream-1', scene, {{sessionId:'sid-1'}}), true);
const liveTurn=$('liveAssistantTurn');
const rows=liveTurn.querySelectorAll('[data-anchor-live-scene-row="1"]');
const staleRows=liveTurn.querySelectorAll('[data-live-tid]');
const legacySegments=liveTurn.querySelectorAll('[data-live-assistant="1"]');
const roles=rows.map(row=>row.getAttribute('data-anchor-row-role'));
const eventTypes=rows.map(row=>row.getAttribute('data-anchor-source-event-type'));
const hiddenLegacy=legacySegments.map(row=>({{
  hidden:row.hidden,
  ariaHidden:row.getAttribute('aria-hidden'),
  source:row.classList.contains('assistant-segment-worklog-source'),
}}));
const settledProbe=_anchorSceneTransparentNodeForRow(
  {{row_id:'settled-p1', role:'prose', source_event_type:'process_prose', text:'settled progress'}},
  {{settled:true, finalAnswer:''}}
);

transparentMode=false;
global.S.activeStreamId='stream-compact';
delete liveTurn.dataset.sessionId;
assert.strictEqual(renderLiveAnchorActivityScene('stream-compact', scene, {{sessionId:'sid-1'}}), true);

process.stdout.write(JSON.stringify({{
  rowCount:rows.length,
  roles,
  eventTypes,
  staleRowCount:staleRows.length,
  hiddenLegacy,
  settledMarker:settledProbe&&settledProbe.getAttribute('data-anchor-settled-scene-row'),
  settledLiveMarker:settledProbe&&settledProbe.getAttribute('data-anchor-live-scene-row'),
  syncCalls,
  compactGroups,
  compactRenders,
  compactSessionId:liveTurn.dataset.sessionId,
  compactRenderedCount:liveTurn.querySelector('.tool-worklog-group').getAttribute('data-rendered-count'),
}}));
"""
    result = _run_node_script(script)

    assert result["rowCount"] == 4
    assert result["roles"] == ["prose", "tool", "thinking", "prose"]
    assert result["eventTypes"] == ["process_prose", "tool_completed", "reasoning", "process_prose"]
    assert result["staleRowCount"] == 0
    assert result["hiddenLegacy"] == [
        {"hidden": True, "ariaHidden": "true", "source": True},
        {"hidden": True, "ariaHidden": "true", "source": True},
    ]
    assert result["settledMarker"] == "1"
    assert result["settledLiveMarker"] is None
    assert result["syncCalls"] == 2
    assert result["compactGroups"] == 1
    assert result["compactRenders"] == 1
    assert result["compactSessionId"] == "sid-1"
    assert result["compactRenderedCount"] == "4"


def test_transparent_anchor_intermediate_prose_preserved_only_final_answer_suppressed():
    """#4568 combined fix: intermediate between-tool progress prose must render in
    Transparent Stream reload (not be blanket-dropped like the first pass did);
    ONLY the prose row that duplicates the final answer is suppressed."""
    row = _function_body(UI_JS, "_anchorSceneTransparentNodeForRow")
    match = _function_body(UI_JS, "_anchorSceneProseMatchesFinalAnswer")
    # the prose branch must RENDER intermediate prose via the shared node builder,
    # gated only on the final-answer match — NOT an unconditional `return null`.
    assert "row.role==='prose'" in row
    assert "_anchorSceneProseMatchesFinalAnswer(text,finalAnswer)" in row
    assert "_anchorSceneNodeForRow(row,{settled})" in row, (
        "intermediate prose must be rendered as an inline assistant-segment node, "
        "not dropped"
    )
    assert "type:'prose'" in row
    # the matcher exists and compares whitespace-insensitively; the prefix
    # tolerance is length-ratio-guarded so a short intermediate sentence that
    # merely prefixes a long final answer is NOT suppressed (Codex #4568).
    assert "replace(/\\s+/g,' ')" in match
    assert "startsWith" in match
    assert "0.9" in match and ">=80" in match.replace(" ", ""), (
        "prefix tolerance must be guarded by a near-equal length ratio, not a bare >=40 floor"
    )
    # guard against the regression where ALL prose rows were dropped:
    assert "// avoid duplicating the answer" in row or "duplicates the final answer" in row



def test_settled_anchor_scene_final_answer_does_not_fold_into_worklog_source():
    belongs = _function_body(UI_JS, "_assistantMessageBelongsInWorklog")
    render = _function_body(UI_JS, "renderMessages")

    assert "if(hasVisibleText&&m._anchor_activity_scene) return false;" in belongs
    assert belongs.index("if(m._live) return true;") < belongs.index(
        "if(hasVisibleText&&m._anchor_activity_scene) return false;"
    )
    assert belongs.index("if(hasVisibleText&&m._anchor_activity_scene) return false;") < belongs.index(
        "if(m._activityBurstId!==undefined||m._liveSegmentSeq!==undefined) return true;"
    )
    assert "seg.classList.add('assistant-segment-worklog-source')" in render
    assert "seg.hidden=true" in render
    assert "_renderSettledAnchorSceneForMessage(msg, seg, rawIdx)" in render


def test_settled_anchor_scene_promotes_final_content_array_to_ordered_activity_rows():
    complete = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    rows_by_message = _function_body(MESSAGES_JS, "_anchorSceneRowsByMessageIndex")
    content_rows = _function_body(MESSAGES_JS, "_anchorSceneRowsFromContentParts")
    final_answer = _function_body(MESSAGES_JS, "_anchorSceneFinalAnswerText")
    content_text = _function_body(MESSAGES_JS, "_anchorSceneContentText")
    visible_text = _function_body(MESSAGES_JS, "_anchorSceneContentVisibleText")

    assert "const messageFinalAnswer=_anchorSceneFinalAnswerText(lastAsst);" in complete
    assert "const finalAnswer=_anchorSceneCleanText(messageFinalAnswer)" in complete
    assert "_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex,{includeFinal:true})" in complete
    assert "for(let idx=turnStart+1;idx<=lastAsstIndex;idx+=1)" in complete
    assert "options=(options&&typeof options==='object')?options:{};" in rows_by_message
    assert "const endIndex=options&&options.includeFinal?lastAsstIndex+1:lastAsstIndex;" in rows_by_message
    assert "const contentRows=_anchorSceneRowsFromContentParts(message,idx,{isFinalMessage:idx===lastAsstIndex});" in rows_by_message
    assert "part.type==='tool_use'" in content_rows
    assert "const isFinalMessage=!!options.isFinalMessage;" in content_rows
    assert "if(!part||typeof part!=='object'){" in content_rows
    assert "if(isFinalMessage&&i>lastToolIndex) continue;" in content_rows
    assert "if(isFinalMessage&&i>lastToolIndex&&_anchorSceneContentVisibleText(part)) continue;" in content_rows
    assert "_anchorSceneProseRow(text,rows.length,messageIndex)" in content_rows
    assert "_anchorSceneToolRowFromCall(_anchorSceneContentTool(part),rows.length,messageIndex)" in content_rows
    assert "lastToolIndex+1" in final_answer
    assert "_anchorSceneContentVisibleText(part)" in final_answer
    assert "if(typeof part==='string') return part;" in content_text
    assert "part.thinking||part.reasoning||part.summary" in content_text
    assert "partType==='thinking'||partType==='reasoning'" in visible_text
    assert "part.text||part.input_text||part.output_text" in visible_text
    assert "_fromContent:true" in rows_by_message
    assert "const useStartedAt=!hasOrderedContentRows;" in rows_by_message
    assert "if(useStartedAt){" in rows_by_message
    assert "const {_phase,_encounter,_fromContent,...clean}=row;" in rows_by_message
    content_tool = _function_body(MESSAGES_JS, "_anchorSceneContentTool")
    assert "part.id||part.tid||part.tool_call_id||part.tool_use_id||part.call_id" in content_tool
    assert "part.name||part.tool_name||fn.name||'tool'" in content_tool
    assert "args:part.args" in content_tool
    assert "input:part.input" in content_tool


def test_settled_anchor_scene_hides_prior_process_segments_not_final_answer():
    settled = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    group = _function_body(UI_JS, "_anchorSceneWorklogGroup")

    assert "blocks.querySelectorAll('.assistant-segment[data-msg-idx]').forEach" in settled
    assert "idx<rawIdx" in settled
    assert "node.classList.add('assistant-segment-worklog-source')" in settled
    assert "node.setAttribute('aria-hidden','true')" in settled
    assert "node.hidden=true" in settled
    assert "turnDuration:message._turnDuration!==undefined&&message._turnDuration!==null?message._turnDuration:scene.turn_duration" in settled
    assert "turnDuration:opts&&opts.turnDuration" in group
    assert "data-turn-duration" in group


def test_anchor_scene_worklog_summary_is_processed_time_anchor():
    summary = _function_body(UI_JS, "_syncToolCallGroupSummary")
    live = _function_body(UI_JS, "renderLiveAnchorActivityScene")

    assert "_activityProcessedElapsedLabel(group)" in summary
    assert "_activitySettledProcessedLabel(group)" in summary
    assert "label.textContent=processedLabel||t('processed_elapsed','')" in summary
    assert "durationEl.textContent='';" in summary
    assert "else if(isWorklogGroup)" in summary
    assert "collapsed:false" in live
    assert ".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-summary" in STYLE_CSS


def test_processed_time_anchor_uses_lightweight_summary_style():
    summary_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-summary{"):
        STYLE_CSS.index(".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-label{")
    ]
    label_rule = STYLE_CSS[
        STYLE_CSS.index(".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-label{"):
        STYLE_CSS.index(".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-summary:hover{")
    ]

    assert "border-bottom:1px solid color-mix(in srgb,var(--border-subtle) 62%,transparent)" in summary_rule
    assert "font-size:calc(var(--message-body-font-size) * .9)" in label_rule
    assert "font-weight:400" in label_rule
    assert "color:color-mix(in srgb,var(--muted) 74%,var(--bg))" in label_rule
    assert "opacity:.82" in label_rule


def test_legacy_settled_worklog_summary_uses_processed_anchor_too():
    summary = _function_body(UI_JS, "_syncToolCallGroupSummary")

    assert "const processedLabel=isLiveWorklog" in summary
    assert ": _activitySettledProcessedLabel(group)" in summary
    assert "_toolWorklogSummary(cards,{live:isLiveWorklog, toolCount, labelOnly:!toolCount&&isLiveWorklog})" not in summary
    assert ".tool-worklog-group[data-tool-worklog-group=\"1\"]:not([data-run-activity-group=\"1\"]) .tool-worklog-summary" in STYLE_CSS


def test_live_processed_anchor_is_clickable_while_streaming():
    toggle = _function_body(UI_JS, "_toggleActivityGroup")
    ensure = _function_body(UI_JS, "ensureActivityGroup")
    finalize = _function_body(UI_JS, "_finalizeLiveActivityDisclosureGroup")
    close = _function_body(UI_JS, "closeCurrentLiveActivityGroup")

    assert "data-live-activity-current')==='1'" not in toggle
    assert "_writeActivityDisclosureState(group.getAttribute('data-activity-disclosure-key'), !collapsed);" in toggle
    assert "_onLiveActivityToggle(group)" in toggle
    assert "summary.setAttribute('data-live-summary-static','1')" not in ensure
    assert "summary.setAttribute('aria-disabled','true')" not in ensure
    assert "summary.disabled=true" not in ensure
    assert "summary.removeAttribute('data-live-summary-static')" in ensure
    assert "summary.removeAttribute('aria-disabled')" in ensure
    assert "summary.disabled=false" in ensure
    assert "group.removeAttribute('data-live-tool-call-group')" in finalize
    assert "group.removeAttribute('data-live-tool-worklog-group')" in finalize
    assert ".tool-card.open,.thinking-card.open,.tool-group.open,.tool-worklog-tool-group.open" in finalize
    assert "group.classList.toggle('tool-call-group-collapsed', !keepOpen)" in finalize
    assert "if(keepOpen&&disclosureKey) _writeActivityDisclosureState(disclosureKey, true);" in finalize
    assert "summary.removeAttribute('data-live-summary-static')" in finalize
    assert "summary.disabled=false" in finalize
    assert "summary.setAttribute('aria-expanded',keepOpen?'true':'false')" in finalize
    assert "_finalizeLiveActivityDisclosureGroup(group)" in close
    assert ".tool-worklog-summary[data-live-summary-static=\"1\"]" not in STYLE_CSS
    assert ".tool-worklog-group[data-live-tool-call-group=\"1\"][data-live-activity-current=\"1\"] .tool-call-group-chevron" not in STYLE_CSS


@pytest.mark.skipif(NODE is None, reason="node is required for live disclosure behavior tests")
def test_live_processed_anchor_toggle_collapses_current_worklog_group():
    script = f"""
const assert = require('assert');
let collapsed = false;
let open = true;
let wrote = null;
let liveExpanded = null;
function _writeActivityDisclosureState(key, value) {{ wrote = [key, value]; }}
function _onLiveActivityToggle(group) {{ liveExpanded = !group.classList.contains('tool-call-group-collapsed'); }}
const group = {{
  attrs: {{
    'data-live-tool-call-group': '1',
    'data-live-activity-current': '1',
    'data-activity-disclosure-key': 'live:stream-1'
  }},
  getAttribute(name) {{ return this.attrs[name] || ''; }},
  classList: {{
    toggle(name, force) {{
      if (name === 'tool-call-group-collapsed') {{
        collapsed = force === undefined ? !collapsed : !!force;
        return collapsed;
      }}
      if (name === 'open') {{
        open = force === undefined ? !open : !!force;
        return open;
      }}
      throw new Error('unexpected class ' + name);
    }},
    contains(name) {{
      if (name === 'tool-call-group-collapsed') return collapsed;
      if (name === 'open') return open;
      return false;
    }}
  }}
}};
const summary = {{
  attrs: {{}},
  closest(selector) {{ return group; }},
  setAttribute(name, value) {{ this.attrs[name] = String(value); }}
}};
function _toggleActivityGroup(summary) {{
{_function_body(UI_JS, "_toggleActivityGroup")}
}}
_toggleActivityGroup(summary);
assert.strictEqual(collapsed, true);
assert.strictEqual(open, false);
assert.deepStrictEqual(wrote, ['live:stream-1', false]);
assert.strictEqual(liveExpanded, false);
assert.strictEqual(summary.attrs['aria-expanded'], 'false');
    console.log(JSON.stringify({{ok:true}}));
"""
    _run_node_script(script)


def test_pre_start_worklog_shell_shows_thinking_placeholder_immediately():
    ensure = _function_body(UI_JS, "ensureLiveWorklogShell")
    placeholder = _function_body(UI_JS, "_setLiveWorklogThinkingPlaceholder")

    assert "ensureActivityGroup(blocks" in ensure
    assert "ensureLiveWorklogContainer(blocks" not in ensure
    assert "if(activeStreamId)" in ensure
    assert "_setLiveWorklogThinkingPlaceholder(group)" in ensure
    assert "group.removeAttribute('data-prestart-thinking')" in ensure
    assert "group.setAttribute('data-prestart-thinking','1')" in placeholder
    assert "t('worklog_thinking')" in placeholder
    assert "durationEl.textContent='';" in placeholder


def test_done_time_empty_message_uses_anchor_scene_final_answer():
    helper = _function_body(UI_JS, "_assistantAnchorSceneFinalAnswerText")
    projection = _function_body(UI_JS, "_assistantTurnAnchorSettledFinalAnswer")
    visible = _function_body(UI_JS, "_assistantMessageHasVisibleContent")
    reasoning_visible = _function_body(UI_JS, "_assistantVisibleContentForReasoningCompare")

    assert "m._anchor_activity_scene" in helper
    assert "scene.final_answer" in helper
    assert "const effectiveContent=String(content||'').trim()?content:sceneFinal;" in projection
    assert "content:effectiveContent" in projection
    assert "return String(sceneFinal||'').trim()?sceneFinal:null;" in projection
    assert "if(_assistantAnchorSceneFinalAnswerText(m)) return true;" in visible
    assert "const anchorFinal=_assistantAnchorSceneFinalAnswerText(m);" in reasoning_visible
    assert "if(anchorFinal) return anchorFinal;" in reasoning_visible


def test_done_follow_scroll_uses_pre_settle_follow_state():
    done = _event_listener_body(MESSAGES_JS, "done")

    capture_idx = done.index("const shouldFollowOnDone=")
    render_idx = done.index("syncTopbar();renderMessages({preserveScroll:true});")
    follow_idx = done.index("if(shouldFollowOnDone&&typeof scrollToBottom==='function') scrollToBottom();")
    assert capture_idx < render_idx < follow_idx
    after_render = done[render_idx:follow_idx]
    assert "_isMessagePaneNearBottom(250)" not in after_render


def test_session_switch_prefers_live_anchor_scene_before_snapshot_fallback():
    assert "window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid" in SESSIONS_JS
    first = SESSIONS_JS.index("window._renderLiveAnchorActivitySceneForStream(activeStreamId, sid")
    assert "_renderRuntimeJournalAnchorActivityScene(activeStreamId, sid)" in SESSIONS_JS[first:first + 500]
    fallback = SESSIONS_JS.index("restoreLiveTurnHtmlForSession", first)
    assert first < fallback
    assert "let restoredLiveTurn=!!restoredAnchorScene;" in SESSIONS_JS
    assert "{mode:'compact_worklog'}" in SESSIONS_JS


def test_session_reload_can_render_runtime_journal_anchor_scene_snapshot():
    helper = _function_body(SESSIONS_JS, "_serverLiveSnapshotInflight")
    renderer = _function_body(SESSIONS_JS, "_renderRuntimeJournalAnchorActivityScene")
    ui_export = UI_JS[UI_JS.index("function _renderLiveAnchorActivitySceneSnapshotForStream") : UI_JS.index("function _renderSettledAnchorSceneForMessage")]

    assert "snapshot.anchor_activity_scene" in helper
    assert "anchorActivityScene" in helper
    assert "hasAnchorActivityScene" in helper
    assert "window._renderLiveAnchorActivitySceneSnapshotForStream" in renderer
    assert "scene.version!=='activity_scene_v1'" in ui_export
    # The export MUST be a direct assignment of the top-level function, NOT a
    # same-name wrapper (window.X = function(){ return X() }) — in a classic
    # script the wrapper reassigns the global to itself → infinite recursion
    # (#2715/#2771 brick class). Assert the direct-assignment form and that the
    # recursive wrapper form is absent.
    assert "window._renderLiveAnchorActivitySceneSnapshotForStream=_renderLiveAnchorActivitySceneSnapshotForStream" in ui_export
    assert "window._renderLiveAnchorActivitySceneSnapshotForStream=function(" not in ui_export


def test_runtime_journal_anchor_scene_seeds_live_registry_before_new_events():
    persist = _function_body(MESSAGES_JS, "persistInflightState")
    hydrate = _function_body(MESSAGES_JS, "_hydrateAnchorRegistryFromActivityScene")

    assert "anchorActivityScene:inflight.anchorActivityScene||null" in persist
    assert "applyAssistantTurnAnchorSourceEvent" in hydrate
    assert "_sourceEventTypeForSnapshotAnchorRow" in hydrate
    assert "_hydrateAnchorRegistryFromActivityScene(INFLIGHT[activeSid]&&INFLIGHT[activeSid].anchorActivityScene);" in MESSAGES_JS
