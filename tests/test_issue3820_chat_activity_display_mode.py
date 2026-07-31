"""Regression tests for issue #3820 chat activity display mode."""

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")

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


def _transparentEventCountLabelBlock(ui_js):
    """Return the body of `_transparentEventCountLabel` as a string slice."""
    start = ui_js.index("function _transparentEventCountLabel")
    end = ui_js.index("\nfunction ", start + 1)
    return ui_js[start:end]


def _run_node_script(script):
    assert NODE, "node is required for chat activity display mode behavior tests"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_chat_activity_display_mode_defaults_to_compact_worklog(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()

    assert loaded["chat_activity_display_mode"] == "compact_worklog"


def test_chat_activity_display_mode_persists_transparent_stream_hide_all_activity_and_rejects_invalid(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"chat_activity_display_mode": "transparent_stream"})
    assert saved["chat_activity_display_mode"] == "transparent_stream"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["chat_activity_display_mode"] == "transparent_stream"

    saved = config.save_settings({"chat_activity_display_mode": "hide_all_activity"})
    assert saved["chat_activity_display_mode"] == "hide_all_activity"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["chat_activity_display_mode"] == "hide_all_activity"

    saved = config.save_settings({"chat_activity_display_mode": "invalid_mode"})
    assert saved["chat_activity_display_mode"] == "hide_all_activity"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["chat_activity_display_mode"] == "hide_all_activity"


def test_transparent_stream_event_timestamps_default_true_and_persist_boolean(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["transparent_stream_event_timestamps"] is True

    saved = config.save_settings({"transparent_stream_event_timestamps": False})
    assert saved["transparent_stream_event_timestamps"] is False
    assert json.loads(settings_path.read_text(encoding="utf-8"))["transparent_stream_event_timestamps"] is False


def test_chat_activity_display_mode_supports_three_values():
    assert "function chatActivityMode()" in UI_JS
    assert "function isTransparentStream()" in UI_JS
    assert "function isFinalAnswerOnlyMode()" in UI_JS
    assert "function isCompactWorklogMode()" in UI_JS
    assert "chatActivityMode()==='transparent_stream'" in UI_JS
    assert "chatActivityMode()==='hide_all_activity'" in UI_JS
    assert "window._chatActivityDisplayMode" in BOOT_JS
    assert "window._chatActivityDisplayMode" in PANELS_JS
    assert "window._simplifiedToolCalling=true" in BOOT_JS
    assert "window._simplifiedToolCalling=true" in PANELS_JS


def test_chat_activity_display_mode_picker_uses_three_desktop_columns():
    assert INDEX_HTML.count('class="chat-activity-mode-btn') == 3
    assert "#mainSettings .chat-activity-mode-toggle{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));" in STYLE_CSS
    assert "#mainSettings .chat-activity-mode-toggle{grid-template-columns:1fr;}" in STYLE_CSS


def test_chat_activity_display_mode_resolver_and_live_early_out():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{
  _chatActivityDisplayMode: 'hide_all_activity',
  _transparentStream: false,
}};
global.isSimplifiedToolCalling = () => true;
eval(extractFunc('chatActivityMode'));
eval(extractFunc('isTransparentStream'));
eval(extractFunc('isFinalAnswerOnlyMode'));
eval(extractFunc('isCompactWorklogMode'));
const resolverResults = [
  chatActivityMode(),
  isTransparentStream(),
  isFinalAnswerOnlyMode(),
  isCompactWorklogMode(),
];
window._chatActivityDisplayMode = 'transparent_stream';
window._transparentStream = false;
resolverResults.push(
  chatActivityMode(),
  isTransparentStream(),
  isFinalAnswerOnlyMode(),
  isCompactWorklogMode(),
);
window._chatActivityDisplayMode = 'bogus';
window._transparentStream = true;
resolverResults.push(
  chatActivityMode(),
  isTransparentStream(),
  isFinalAnswerOnlyMode(),
  isCompactWorklogMode(),
);
process.stdout.write(JSON.stringify({{resolverResults}}));
"""
    result = _run_node_script(script)

    assert result["resolverResults"] == [
        "hide_all_activity",
        False,
        True,
        False,
        "transparent_stream",
        True,
        False,
        False,
        "transparent_stream",
        True,
        False,
        False,
    ]


def test_chat_activity_display_mode_explicit_modes_are_preserved_by_render_helpers():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
let captured = [];
function _projectLiveAnchorActivitySceneForStream(streamId, mode){{
  return {{version:'activity_scene_v1', mode, activity_rows:[]}};
}}
function renderLiveAnchorActivityScene(streamId, scene, opts){{
  captured.push({{streamId, sceneMode: scene.mode, optMode: opts.mode, sessionId: opts.sessionId}});
  return false;
}}
let activeMode = 'compact_worklog';
global.chatActivityMode = () => activeMode;
eval(extractFunc('_renderLiveAnchorActivitySceneForStream'));
eval(extractFunc('_renderLiveAnchorActivitySceneSnapshotForStream'));
const helperResult = _renderLiveAnchorActivitySceneForStream('stream-1', 'sid-1', {{mode:'hide_all_activity'}});
const snapshotResult = _renderLiveAnchorActivitySceneSnapshotForStream(
  'stream-2',
  {{version:'activity_scene_v1', activity_rows:[]}},
  'sid-2',
  {{mode:'transparent_stream'}},
);
activeMode = 'hide_all_activity';
const compactOverrideResult = _renderLiveAnchorActivitySceneForStream(
  'stream-3',
  'sid-3',
  {{mode:'compact_worklog'}},
);
process.stdout.write(JSON.stringify({{helperResult, snapshotResult, compactOverrideResult, captured}}));
"""
    result = _run_node_script(script)

    assert result["helperResult"] is False
    assert result["snapshotResult"] is False
    assert result["compactOverrideResult"] is False
    assert result["captured"] == [
        {"streamId": "stream-1", "sceneMode": "hide_all_activity", "optMode": "hide_all_activity", "sessionId": "sid-1"},
        {"streamId": "stream-2", "optMode": "transparent_stream", "sessionId": "sid-2"},
        {"streamId": "stream-3", "sceneMode": "hide_all_activity", "optMode": "compact_worklog", "sessionId": "sid-3"},
    ]


def test_chat_activity_display_mode_live_renderer_early_outs_in_hide_all_mode():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{
  _chatActivityDisplayMode: 'hide_all_activity',
  _transparentStream: false,
}};
global.S = {{ session: {{ session_id: 'sid-1' }}, activeStreamId: 'stream-1' }};
global.isSimplifiedToolCalling = () => true;
global.$ = () => {{ throw new Error('unexpected DOM access'); }};
eval(extractFunc('chatActivityMode'));
eval(extractFunc('isTransparentStream'));
eval(extractFunc('isFinalAnswerOnlyMode'));
eval(extractFunc('isCompactWorklogMode'));
eval(extractFunc('renderLiveAnchorActivityScene'));
const result = renderLiveAnchorActivityScene(
  'stream-1',
  {{version:'activity_scene_v1', activity_rows:[{{role:'tool'}}]}},
  {{sessionId:'sid-1', mode:'hide_all_activity'}},
);
process.stdout.write(JSON.stringify({{result, mode: chatActivityMode(), finalOnly: isFinalAnswerOnlyMode(), compact: isCompactWorklogMode()}}));
"""
    result = _run_node_script(script)

    assert result == {"result": False, "mode": "hide_all_activity", "finalOnly": True, "compact": False}


def test_chat_activity_display_mode_anchor_scene_preserves_hide_all_mode():
    script = f"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "assistant_turn_anchors.js"))}, 'utf8');
const sandbox = {{window:{{}}}};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, {{filename:'assistant_turn_anchors.js'}});
const api = sandbox.window.HermesAssistantTurnAnchors;
const empty = api.projectAssistantTurnAnchorActivityScene(null, {{mode:'hide_all_activity'}});
const registry = api.createAssistantTurnAnchorRegistry({{session_id:'sid-1', turn_id:'turn-1'}});
api.applyAssistantTurnAnchorSourceEvents(registry, [
  {{event:'tool', payload:{{tool_call_id:'tool-1', name:'terminal'}}, event_id:'run-1:1', seq:1}},
  {{source_type:'settled_message', payload:{{role:'assistant', id:'message-final', content:'final answer'}}}},
], {{run_id:'run-1', stream_id:'stream-1'}});
const scene = api.projectAssistantTurnAnchorActivityScene(registry, {{mode:'hide_all_activity'}});
const snapshot = api.createAssistantTurnAnchorRendererSnapshot({{mode:'hide_all_activity', rows:[]}});
process.stdout.write(JSON.stringify({{emptyMode:empty.mode, sceneMode:scene.mode, snapshotMode:snapshot.mode}}));
"""
    result = _run_node_script(script)

    assert result == {
        "emptyMode": "hide_all_activity",
        "sceneMode": "hide_all_activity",
        "snapshotMode": "hide_all_activity",
    }


def test_chat_activity_display_mode_legacy_live_fallbacks_do_not_render_activity():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.S = {{ session: {{ session_id: 'sid-1' }}, activeStreamId: 'stream-1' }};
global.isFinalAnswerOnlyMode = () => true;
global.$ = () => {{ throw new Error('unexpected DOM access'); }};
eval(extractFunc('appendLiveToolCard'));
eval(extractFunc('ensureLiveWorklogShell'));
const appendResult = appendLiveToolCard({{tid:'tool-1'}}, {{sessionId:'sid-1', streamId:'stream-1'}});
const shellResult = ensureLiveWorklogShell();
process.stdout.write(JSON.stringify({{appendResult: appendResult === undefined, shellResult}}));
"""
    result = _run_node_script(script)

    assert result == {"appendResult": True, "shellResult": None}


def test_chat_activity_display_mode_legacy_thinking_fallback_does_not_render_activity():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.isFinalAnswerOnlyMode = () => true;
global.$ = () => {{ throw new Error('unexpected DOM access'); }};
eval(extractFunc('appendThinking'));
eval(extractFunc('updateThinking'));
const appendResult = appendThinking('reasoning text', {{pending:true}});
const updateResult = updateThinking('reasoning text', {{pending:true}});
process.stdout.write(JSON.stringify({{appendResult: appendResult === undefined, updateResult: updateResult === undefined}}));
"""
    result = _run_node_script(script)

    assert result == {"appendResult": True, "updateResult": True}


def test_chat_activity_display_mode_settled_hide_all_scene_persists_without_worklog():
    start = MESSAGES_JS.index("function _attachProjectedAnchorSceneToLastAssistant(messages){")
    end = MESSAGES_JS.index("function _upsertAnchorProcessProse", start)
    block = MESSAGES_JS[start:end]
    persist_index = block.index("_persistSettledAnchorScene(lastAsst, scene, lastAsstIndex);")
    return_index = block.index("return hasWorklogRows;")

    assert "const hasWorklogRows=_anchorSceneHasWorklogWorthyRows(scene);" in block
    assert "const hasOwnedOutcomes=_anchorSceneHasOwnedOutcomes(scene);" in block
    assert (
        "const shouldPersistScene=hasWorklogRows||scene.mode==='hide_all_activity'||hasOwnedOutcomes;"
        in block
    )
    assert persist_index < return_index
    assert "return true;" not in block


def test_chat_activity_display_mode_switch_to_final_only_clears_existing_live_activity():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "panels.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
let cleanupCalls = 0;
const select = {{value:''}};
const buttons = [
  {{mode:'compact_worklog'}},
  {{mode:'transparent_stream'}},
  {{mode:'hide_all_activity'}},
].map(({{mode}}) => ({{
  getAttribute: (name) => name === 'data-chat-activity-mode' ? mode : null,
  classList: {{toggle(){{}}}},
  setAttribute(){{}},
}}));
global.window = {{_hideLiveActivityForFinalAnswerOnly(){{ cleanupCalls += 1; }}}};
global.document = {{querySelectorAll: () => buttons}};
global.$ = (id) => id === 'settingsChatActivityDisplayMode' ? select : null;
eval(extractFunc('_syncChatActivityDisplayModeControl'));
_syncChatActivityDisplayModeControl('transparent_stream');
const afterTransparent = {{mode: window._chatActivityDisplayMode, transparent: window._transparentStream, cleanupCalls}};
_syncChatActivityDisplayModeControl('hide_all_activity');
const afterHide = {{mode: window._chatActivityDisplayMode, transparent: window._transparentStream, cleanupCalls, selectValue: select.value}};
process.stdout.write(JSON.stringify({{afterTransparent, afterHide}}));
"""
    result = _run_node_script(script)

    assert result == {
        "afterTransparent": {"mode": "transparent_stream", "transparent": True, "cleanupCalls": 0},
        "afterHide": {"mode": "hide_all_activity", "transparent": False, "cleanupCalls": 1, "selectValue": "hide_all_activity"},
    }


def test_chat_activity_display_mode_live_cleanup_removes_existing_activity_rows():
    start = UI_JS.index("function _hideLiveActivityForFinalAnswerOnly(){")
    end = UI_JS.index("function _removeEmptyLiveWorklogShells", start)
    block = UI_JS[start:end]

    assert "clearLiveToolCards();" in block
    assert "removeThinking" in block
    assert ".transparent-event-row" in block
    assert "#liveRunStatus" in block
    assert "window._hideLiveActivityForFinalAnswerOnly=_hideLiveActivityForFinalAnswerOnly" in UI_JS


def test_transparent_stream_live_branch_uses_direct_rows():
    decorator_start = UI_JS.index("function _decorateTransparentEventRow(row, opts){")
    decorator_end = UI_JS.index("function _setTransparentRowsExpanded", decorator_start)
    decorator_block = UI_JS[decorator_start:decorator_end]
    assert "data-transparent-event-row" in decorator_block
    assert "transparent-event-status" in decorator_block
    # Title carries the simple tool name (short form for MCP); status text
    # is only shown for non-default states.
    assert "_toolShortName(name)" in decorator_block
    assert "if(status==='Completed'){" in decorator_block
    assert "function _syncTransparentEventControls" in UI_JS
    assert "function _transparentEventCountLabel" in UI_JS
    assert "data-transparent-tool-count" in UI_JS
    assert "data-tool-count" in UI_JS
    # Transparent mode labels the event controls as a trace, not a Worklog
    # Activity summary.
    assert "return toolCount?`Trace: ${toolCount} ${toolCount===1?'tool':'tools'}`:'Trace';" in UI_JS
    assert "return `Activity: ${toolCount} ${toolCount===1?'tool':'tools'}`" not in UI_JS
    assert "Activity: ${parts.join(' · ')}" not in UI_JS
    # The summary must not list thinking count (DESIGN.md:75 forbids a second
    # trailing count badge, and thinking rows render as their own rows).
    assert "thinkingCount" not in _transparentEventCountLabelBlock(UI_JS)
    assert "function _setTransparentRowsExpanded" in UI_JS
    assert "function _setTransparentCardOpen" in UI_JS
    assert "function _wireTransparentHeaderToggle" in UI_JS
    assert "function _transparentToolDetailHtml" in UI_JS
    assert "function _setTransparentDetailMode" in UI_JS
    assert "transparent-detail-modes" in UI_JS
    # Spec R4: key on own line, value on next line, no inline pair.
    assert 'class="tool-arg-pair"' in UI_JS
    assert 'class="tool-arg-key"' in UI_JS
    assert 'class="tool-arg-val"' in UI_JS
    # Spec R5: controls are span[role=button], not <button> (text-link style)
    assert 'setAttribute(\'role\',\'button\')' in UI_JS
    # Spec R4: detail-mode tabs are span[role=tab], not <button>
    assert 'role="tab"' in UI_JS
    assert "transparent-detail-modes" in UI_JS

    append_thinking_start = UI_JS.index("function appendThinking(text='', options){")
    append_thinking_end = UI_JS.index("function updateThinking", append_thinking_start)
    append_thinking_block = UI_JS[append_thinking_start:append_thinking_end]
    assert "if(isTransparentStream())" in append_thinking_block
    assert "turn=_createAssistantTurn()" in append_thinking_block
    assert "row.id='thinkingRow'" in append_thinking_block
    assert "_decorateTransparentEventRow(row,{" in append_thinking_block
    assert "type:'thinking'" in append_thinking_block
    assert "data-live-thinking-key" in append_thinking_block
    assert "_syncTransparentEventControls(turn)" in append_thinking_block

    append_tool_start = UI_JS.index("function appendLiveToolCard(tc){")
    append_tool_end = UI_JS.index("function _findLatestLiveAssistantByBurst", append_tool_start)
    append_tool_block = UI_JS[append_tool_start:append_tool_end]
    assert "if(isTransparentStream())" in append_tool_block
    assert "_decorateTransparentEventRow(buildToolCard(tc)" in append_tool_block
    assert "_syncTransparentEventControls(turn)" in append_tool_block
    assert "ensureLiveWorklogContainer(inner" in append_tool_block  # compact_worklog fallback remains intact


def test_settings_ui_exposes_chat_activity_display_mode_selector():
    assert 'id="settingsChatActivityDisplayMode"' in INDEX_HTML
    assert 'id="settingsTransparentEventTimestamps"' in INDEX_HTML
    assert 'data-chat-activity-mode="compact_worklog"' in INDEX_HTML
    assert 'data-chat-activity-mode="transparent_stream"' in INDEX_HTML
    assert 'data-chat-activity-mode="hide_all_activity"' in INDEX_HTML
    assert "_pickTransparentEventTimestamps(this.checked)" in INDEX_HTML
    assert 'value="compact_worklog"' in INDEX_HTML
    assert 'value="transparent_stream"' in INDEX_HTML
    assert 'value="hide_all_activity"' in INDEX_HTML
    assert 'data-i18n="settings_label_chat_activity_display_mode"' in INDEX_HTML
    assert 'data-i18n="settings_label_transparent_stream_event_timestamps"' in INDEX_HTML
    assert 'data-i18n="settings_desc_transparent_stream_event_timestamps"' in INDEX_HTML
    assert 'settings_option_final_answer_only' in INDEX_HTML
    assert "chat_activity_display_mode" in PANELS_JS
    assert "transparent_stream_event_timestamps" in PANELS_JS
    assert "settingsChatActivityDisplayMode" in PANELS_JS
    assert "function _syncChatActivityDisplayModeControl" in PANELS_JS
    assert "function _syncTransparentEventTimestampsControl" in PANELS_JS
    assert "function _pickChatActivityDisplayMode" in PANELS_JS
    assert "function _pickTransparentEventTimestamps" in PANELS_JS
    assert "body.chat_activity_display_mode" in PANELS_JS
    assert "renderMessages({preserveScroll:true})" in PANELS_JS
    assert "settings_label_chat_activity_display_mode" in I18N_JS
    assert "settings_desc_chat_activity_display_mode" in I18N_JS
    assert "settings_label_transparent_stream_event_timestamps" in I18N_JS
    assert "settings_desc_transparent_stream_event_timestamps" in I18N_JS
    assert I18N_JS.count("settings_option_final_answer_only") == I18N_JS.count("settings_option_transparent_stream")


def test_chat_activity_display_mode_plumbing_preserves_hide_all_activity():
    assert "s.chat_activity_display_mode==='transparent_stream'||s.chat_activity_display_mode==='hide_all_activity'" in BOOT_JS
    assert "window._transparentEventTimestamps=s.transparent_stream_event_timestamps!==false;" in BOOT_JS
    assert "window._transparentEventTimestamps=true;" in BOOT_JS
    assert "chatActivityModeSel&&(chatActivityModeSel.value==='transparent_stream'||chatActivityModeSel.value==='hide_all_activity')" in PANELS_JS
    assert "const next=mode==='transparent_stream'||mode==='hide_all_activity' ? mode : 'compact_worklog';" in PANELS_JS
    assert "if(typeof _syncTransparentEventTimestampsControl==='function') _syncTransparentEventTimestampsControl(window._transparentEventTimestamps,next);" in PANELS_JS
    assert "window._transparentEventTimestamps=next;" in PANELS_JS
    assert "hide_all_activity" in PANELS_JS


def test_chat_activity_display_mode_settled_worklog_suppression():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "messages.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
eval(extractFunc('_anchorSceneActiveMode'));
eval(extractFunc('_anchorSceneRowDisplayHintForMode'));
eval(extractFunc('_anchorSceneHasWorklogWorthyRows'));
global.window = {{
  chatActivityMode() {{ return 'hide_all_activity'; }},
  _chatActivityDisplayMode: 'compact_worklog',
  _transparentStream: false,
  isFinalAnswerOnlyMode() {{ return false; }},
}};
const activeMode = _anchorSceneActiveMode();
const hiddenHint = _anchorSceneRowDisplayHintForMode({{display_hints:{{}}}}, 'hide_all_activity');
const compactHint = _anchorSceneRowDisplayHintForMode({{display_hints:{{compact_worklog:'activity_row'}}}}, 'compact_worklog');
const hiddenScene = _anchorSceneHasWorklogWorthyRows({{mode:'hide_all_activity', activity_rows:[{{role:'tool'}},{{role:'thinking'}}]}});
window.isFinalAnswerOnlyMode = () => true;
const activeFinalOnly = _anchorSceneHasWorklogWorthyRows({{mode:'compact_worklog', activity_rows:[{{role:'tool'}}]}});
window.isFinalAnswerOnlyMode = () => false;
const compactScene = _anchorSceneHasWorklogWorthyRows({{mode:'compact_worklog', activity_rows:[{{role:'tool'}}]}});
process.stdout.write(JSON.stringify({{activeMode, hiddenHint, compactHint, hiddenScene, activeFinalOnly, compactScene}}));
"""
    result = _run_node_script(script)

    assert result == {
        "activeMode": "hide_all_activity",
        "hiddenHint": "hidden_activity",
        "compactHint": "activity_row",
        "hiddenScene": False,
        "activeFinalOnly": False,
        "compactScene": True,
    }


def test_appearance_autosave_rerenders_only_when_activity_mode_changes():
    """Appearance autosave receives the full settings object back from the
    server, so the presence of chat_activity_display_mode alone is not a
    reason to rebuild the message list. Only an effective mode change should
    clear the render cache and re-render messages."""
    start = PANELS_JS.index("async function _autosaveAppearanceSettings(payload)")
    end = PANELS_JS.index("window._sessionEndlessScrollEnabled=", start)
    autosave_block = PANELS_JS[start:end]

    assert "const beforeMode=window._chatActivityDisplayMode;" in autosave_block
    assert "const beforeTimestamps=window._transparentEventTimestamps!==false;" in autosave_block
    assert "_syncChatActivityDisplayModeControl(saved.chat_activity_display_mode);" in autosave_block
    assert "_syncTransparentEventTimestampsControl(saved.transparent_stream_event_timestamps, saved.chat_activity_display_mode);" in autosave_block
    changed_guard = "if(window._chatActivityDisplayMode!==beforeMode||((window._transparentEventTimestamps!==false)!==beforeTimestamps)){"
    assert changed_guard in autosave_block
    guarded = autosave_block[autosave_block.index(changed_guard):]
    assert "clearMessageRenderCache()" in guarded
    assert "renderMessages({preserveScroll:true})" in guarded
    assert PANELS_JS.count("_syncTransparentEventTimestampsControl(settings.transparent_stream_event_timestamps, settings.chat_activity_display_mode);") == 1


def test_attach_copy_button_declares_local_button():
    """_attachCopyButton must not leak an implicit window.btn global."""
    start = UI_JS.index("function _attachCopyButton(header)")
    end = UI_JS.index("\nfunction _transparentToolDetailHtml", start)
    copy_block = UI_JS[start:end]

    assert "const btn=document.createElement('span');" in copy_block
    assert "\n  btn=document.createElement('span');" not in copy_block


def test_transparent_settled_rows_preserve_same_anchor_order():
    """When multiple transparent event groups resolve to the same anchor, each
    inserted row must advance a per-anchor cursor instead of recomputing
    anchor.nextElementSibling and reversing chronological order."""
    transparent_branch = UI_JS[
        UI_JS.index("// ── transparent_stream path: individual expandable event rows ──"):
        UI_JS.index("// Render per-turn duration", UI_JS.index("// ── transparent_stream path: individual expandable event rows ──"))
    ]

    assert "const transparentInsertCursors=new Map();" in transparent_branch
    assert "const cursor=transparentInsertCursors.get(anchorRow)||anchorRow;" in transparent_branch
    assert "transparentInsertCursors.set(anchorRow,row);" in transparent_branch
    assert "insertAfterCursor(toolRow);" in transparent_branch
    assert "anchorRow.nextElementSibling" not in transparent_branch


def test_transparent_settled_reasoning_thinking_stays_before_final_answer():
    """Reasoning-only final assistant messages should keep their Thinking row
    above the final answer, matching the live order and Compact Worklog's
    beforeAnchor behavior."""
    transparent_branch = UI_JS[
        UI_JS.index("// ── transparent_stream path: individual expandable event rows ──"):
        UI_JS.index("// Render per-turn duration", UI_JS.index("// ── transparent_stream path: individual expandable event rows ──"))
    ]

    assert "const anchorIsWorklogSource=anchorRow.classList&&anchorRow.classList.contains('assistant-segment-worklog-source');" in transparent_branch
    assert "const insertBeforeAnchor=(row)=>{" in transparent_branch
    assert "if(!anchorIsWorklogSource) insertBeforeAnchor(thinkingRow);" in transparent_branch
    assert "else insertAfterCursor(thinkingRow);" in transparent_branch


def test_transparent_live_tool_rows_append_at_turn_end_before_status():
    """Live Transparent Stream rows arrive chronologically, so new rows should
    append at the end of the live turn before #liveRunStatus instead of jumping
    after the previous transparent row."""
    start = UI_JS.index("function appendLiveToolCard(tc){")
    end = UI_JS.index("function clearLiveToolCards()", start)
    live_block = UI_JS[start:end]
    transparent_start = live_block.index("if(isTransparentStream()){")
    transparent_end = live_block.index("if(anchor) _removeEmptyLiveWorklogShells(inner);", transparent_start)
    transparent_live = live_block[transparent_start:transparent_end]

    assert "const liveFooter=inner.querySelector('#liveRunStatus');" in transparent_live
    assert "inner.insertBefore(row,liveFooter);" in transparent_live
    assert "previousRows" not in transparent_live
    assert "previous.insertAdjacentElement('afterend',row)" not in transparent_live


def test_cached_transparent_html_is_rehydrated_after_restore():
    """Cached HTML restores DOM shape but not property-assigned handlers. The
    fast path must rehydrate Transparent Stream controls before returning."""
    cache_start = UI_JS.index("if(cached&&cached.msgCount===msgCount")
    cache_end = UI_JS.index("return;", cache_start)
    cache_block = UI_JS[cache_start:cache_end]

    assert "_rehydrateTransparentStreamDom(inner);" in cache_block
    assert "function _rehydrateTransparentStreamDom(root)" in UI_JS
    assert "_wireTransparentTurnToggle(turn);" in UI_JS
    assert "_syncTransparentEventControls(turn);" in UI_JS
    assert "_wireTransparentHeaderToggle(header);" in UI_JS
    assert "_attachCopyButton(header);" in UI_JS


def test_transparent_existing_copy_buttons_are_rebound_after_cache_restore():
    """The serialized cached DOM keeps copy button elements but loses onclick
    properties, so _attachCopyButton must bind existing buttons too."""
    start = UI_JS.index("function _attachCopyButton(header)")
    end = UI_JS.index("\nfunction _transparentToolDetailHtml", start)
    copy_block = UI_JS[start:end]

    assert "const bindCopyButton=(btn)=>{" in copy_block
    assert "return bindCopyButton(existing);" in copy_block
    assert "bindCopyButton(btn);" in copy_block
    assert "const fallbackName=row.getAttribute('data-event-name')||row.getAttribute('data-tool-name')||'tool';" in UI_JS


def test_transparent_event_row_quiet_metadata_visual_rhythm():
    """Transparent stream rows are quiet transcript metadata, not highlighted
    cards. Collapsed rows are transparent by default, with only neutral
    hover/focus/expanded treatment. Thinking rows share the same quiet family
    instead of using a special accent banner.

    Body itself stays flush with the card's border (no inner chrome), but
    contains a bordered code block for output. Status is rendered as a
    small text badge only when the tool isn't Completed.
    """
    # Collapsed rows are inline metadata: no gradient, no shadow, no accent rail.
    assert "border:1px solid transparent" in STYLE_CSS
    assert "border-left:1px solid transparent" in STYLE_CSS
    assert "border-radius:6px" in STYLE_CSS
    assert "background:transparent" in STYLE_CSS
    assert "box-shadow:none" in STYLE_CSS

    # Header: single flex line, low height, hover subtle.
    # Compact rhythm: 19px header for tighter inline trace rows.
    assert "min-height:19px" in STYLE_CSS
    assert "display:flex" in STYLE_CSS

    # Thinking rows no longer get a special accent gradient or bright border.
    thinking_start = STYLE_CSS.index('.transparent-event-row[data-event-type="thinking"]')
    thinking_end = STYLE_CSS.index('.transparent-event-row[data-expanded="1"]', thinking_start)
    thinking_block = STYLE_CSS[thinking_start:thinking_end]
    assert "background:transparent;" in thinking_block
    assert "border-color:transparent;" in thinking_block
    assert "border-left-color:transparent;" in thinking_block
    assert "var(--accent)" not in thinking_block

    # Expanded body uses visible max-height / opacity animation for smooth height.
    assert "max-height:0" in STYLE_CSS
    assert "max-height:520px" in STYLE_CSS
    assert "transition:max-height" in STYLE_CSS
    assert "data-expanded" in UI_JS

    # Output / JSON: flattened to a quiet left-rail (no second bordered card),
    # so args + output read as ONE expanded zone. (Trifecta V5/V7.)
    assert ".transparent-event-row .tool-card-result pre{" in STYLE_CSS
    _pre_start = STYLE_CSS.index(".transparent-event-row .tool-card-result pre{")
    _pre_block = STYLE_CSS[_pre_start:_pre_start + 220]
    assert "background:transparent;" in _pre_block
    assert "border-left:2px solid var(--border-subtle);" in _pre_block
    assert "max-height:none;" in _pre_block
    assert ".transparent-event-row .thinking-card-body pre{" in STYLE_CSS
    assert "border:0;" in STYLE_CSS
    assert "border-radius:0;" in STYLE_CSS
    assert "margin-top:0;" in STYLE_CSS
    assert "padding:0 8px 6px 27px;" in STYLE_CSS
    assert ".transparent-event-row .thinking-card.open .thinking-card-body{\n  border-top-color:transparent;\n  padding:0 0 3px;\n  scrollbar-gutter:stable;\n}" in STYLE_CSS

    # Tabs: text-link style with an active underline (no pill background).
    assert ".transparent-detail-mode.active{color:var(--text);opacity:1;font-weight:600;box-shadow:inset 0 -1px 0 var(--accent);}" in STYLE_CSS
    assert ".transparent-detail-mode.active{background:var(--hover-bg)" not in STYLE_CSS

    # Args layout: compact inline key beside value (not stacked). (Trifecta V4.)
    assert ".tool-arg-pair{display:flex;flex-direction:row" in STYLE_CSS
    # Compact size: key font 10px.
    assert ".tool-arg-key{flex:0 0 auto;min-width:54px;color:var(--muted);font-size:10px" in STYLE_CSS
    assert "font-family:var(--font-mono);" in STYLE_CSS

    # Tool rows must show a useful compact preview while collapsed.
    assert ".transparent-event-row .tool-card-preview" in STYLE_CSS
    assert ".transparent-event-row .tool-card:not(.open) .tool-card-preview" not in STYLE_CSS
    assert "display:inline" in STYLE_CSS
    assert "flex-direction:row;" in STYLE_CSS
    assert ".transparent-event-row .thinking-card-label{\n  display:inline;\n  flex:0 0 auto;\n  color:var(--muted);" in STYLE_CSS
    assert ".transparent-event-thinking-preview{\n  display:inline;\n  flex:1 1 auto;\n  min-width:0;" in STYLE_CSS
    thinking_label_block = STYLE_CSS[
        STYLE_CSS.index(".transparent-event-row .thinking-card-label{"):
        STYLE_CSS.index(".transparent-event-thinking-preview{")
    ]
    thinking_preview_block = STYLE_CSS[
        STYLE_CSS.index(".transparent-event-thinking-preview{"):
        STYLE_CSS.index(".transparent-event-status{")
    ]
    assert "order:" not in thinking_label_block
    assert "order:" not in thinking_preview_block
    assert "opacity:.6;" in STYLE_CSS
    assert "font-size:.85em;" in STYLE_CSS

    # Per-row copy button still attached for both tool and thinking rows.
    assert "function _attachCopyButton" in UI_JS
    assert "function _copyEventToClipboard" in UI_JS
    assert "navigator.clipboard.writeText" in UI_JS
    decorator_block = UI_JS[UI_JS.index("function _decorateTransparentEventRow"):UI_JS.index("function _setTransparentRowsExpanded")]
    assert decorator_block.count("_attachCopyButton(header)") >= 2
    assert "_transparentToolDetailHtml(tc,status)" in decorator_block
    assert "_wireTransparentHeaderToggle(header)" in decorator_block
    assert "const btnRow=header.querySelector('.thinking-card-btn-row')" in decorator_block
    assert "if(copy&&copy.parentNode!==header) header.appendChild(copy)" in decorator_block
    assert "if(toggle&&toggle.parentNode!==header) header.appendChild(toggle)" in decorator_block
    assert "preview.className='transparent-event-preview transparent-event-thinking-preview'" in decorator_block

    # Bug fix: thinking blocks persist after the stream stops. removeThinking()
    # and finalizeThinkingCard() must not delete the transparent thinking row;
    # they only strip live attributes so the row can survive the settled render.
    remove_start = UI_JS.index("function removeThinking()")
    remove_end = UI_JS.index("\nfunction ", remove_start + 1)
    remove_block = UI_JS[remove_start:remove_end]
    transparent_remove_block = remove_block[remove_block.index("if(isTransparentStream())"):remove_block.index("const turn=$('liveAssistantTurn');")]
    assert "row.removeAttribute('id')" in transparent_remove_block
    assert "row.removeAttribute('data-thinking-active')" in transparent_remove_block
    assert "row.removeAttribute('data-live-thinking')" in transparent_remove_block
    assert "row.remove()" not in transparent_remove_block

    finalize_start = UI_JS.index("function finalizeThinkingCard(){")
    finalize_end = UI_JS.index("function appendThinking", finalize_start)
    finalize_block = UI_JS[finalize_start:finalize_end]
    transparent_finalize_block = finalize_block[finalize_block.index("if(isTransparentStream())"):finalize_block.index("if(!isSimplifiedToolCalling())")]
    assert "row.removeAttribute('id')" in transparent_finalize_block
    assert "row.removeAttribute('data-thinking-active')" in transparent_finalize_block
    assert "row.removeAttribute('data-live-thinking')" in transparent_finalize_block
    assert "row.remove()" not in transparent_finalize_block


def test_transparent_stream_static_branch_bypasses_worklog_summary_and_adds_event_hooks():
    marker = "// ── transparent_stream path: individual expandable event rows ──"
    start = UI_JS.index(marker)
    end = UI_JS.index("// Render per-turn duration", start)
    transparent_branch = UI_JS[start:end]

    assert "_decorateTransparentEventRow(_thinkingActivityNode(event.thinkingText,false)" in transparent_branch
    assert "_decorateTransparentEventRow(buildToolCard(event.toolCall)" in transparent_branch
    assert "type:'thinking'" in transparent_branch
    assert "type:'tool'" in transparent_branch
    assert "_syncTransparentEventControls(turn)" in transparent_branch
    assert "_syncToolCallGroupSummary" not in transparent_branch
    assert "ensureActivityGroup" not in transparent_branch
    assert "_toolWorklogSummary" not in transparent_branch

    render_message_start = UI_JS.index("const messageBelongsInWorklog=")
    render_message_end = UI_JS.index("if(messageBelongsInWorklog)", render_message_start)
    assert "isCompactWorklogMode()" in UI_JS[render_message_start:render_message_end]
    assert "isSimplifiedToolCalling()" not in UI_JS[render_message_start:render_message_end]

    thinking_store_start = UI_JS.index("if(thinkingText&&window._showThinking!==false)")
    thinking_store_end = UI_JS.index("const hasVisibleBody=", thinking_store_start)
    thinking_store_block = UI_JS[thinking_store_start:thinking_store_end]
    assert "isTransparentStream()" in thinking_store_block
    assert "assistantThinking.set(rawIdx, thinkingText)" in thinking_store_block


def test_fade_text_effect_uses_dynamic_window_check():
    """The fade text effect must read window._fadeTextEffect dynamically
    on every call, and the Settings checkbox must update the live window flag
    immediately so the current session can start fading without a reload."""
    MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    # Locate the helper and confirm it reads the live value.
    helper_start = MESSAGES_JS.index("function _shouldUseStreamFade(")
    helper_end = MESSAGES_JS.index("\n  function ", helper_start + 1)
    helper_block = MESSAGES_JS[helper_start:helper_end]
    assert "window._fadeTextEffect" in helper_block
    # Should not return the captured const any more.
    assert "return _streamFadeEnabledForStream;" not in helper_block

    # The preferences listener must update the runtime flag immediately, not
    # only after autosave/save completes. Anchor on the listener occurrence —
    # the one immediately followed by the terminalAutoExpand field — rather than
    # a fragile byte offset (panels.js has two `fadeTextCb=` references: the
    # settings-body payload builder and this listener block).
    fade_cb_start = PANELS_JS.index(
        "const fadeTextCb=$('settingsFadeTextEffect');\n    if(fadeTextCb){"
    )
    fade_cb_end = PANELS_JS.index("const terminalAutoExpandCb", fade_cb_start)
    fade_cb_block = PANELS_JS[fade_cb_start:fade_cb_end]
    assert "window._fadeTextEffect=fadeTextCb.checked" in fade_cb_block
    assert "fadeTextCb.addEventListener('change',()=>{" in fade_cb_block
    assert "_schedulePreferencesAutosave();" in fade_cb_block
    # The change handler must not be the old direct autosave callback.
    assert "addEventListener('change',_schedulePreferencesAutosave" not in fade_cb_block


def test_thinking_blocks_persist_after_renderMessages():
    """renderMessages() rebuilds the settled DOM and used to wipe every
    .agent-activity-thinking row that lacked data-live-thinking="1" —
    including the promoted permanent event rows that removeThinking()
    now leaves behind. Guard the selector so promoted thinking rows
    survive the rebuild."""
    inner_sweep = ".agent-activity-thinking:not([data-live-thinking=\"1\"]):not([data-event-type=\"thinking\"])"
    assert inner_sweep in UI_JS
    # The promoted-row guard is the only thing that changed; the rest of
    # the selector must remain intact.
    head = ".tool-worklog-group:not([data-compression-card]),.tool-call-group:not([data-compression-card]),.tool-card-row:not([data-compression-card]):not([data-event-type=\"tool\"]),.agent-activity-thinking"
    tail = ".wl-reason[data-worklog-reason-source=\"reasoning\"]"
    assert head in UI_JS
    assert tail in UI_JS


def test_mcp_tool_names_appear_in_event_rows():
    """MCP (Model Context Protocol) tools are namespaced as
    mcp__<server>__<tool>. Their full name must be preserved on the row's
    data-tool-name attribute, the header must use a friendly short form
    (e.g. github/create_issue), and a plug-style icon must be used when
    no canonical Hermes icon matches."""
    # toolIcon: plug icon for mcp__ / mcp. prefixed names.
    assert "startsWith('mcp__')" in UI_JS
    assert "startsWith('mcp.')" in UI_JS
    assert "li('plug')" in UI_JS
    # _toolShortName reduces the namespace; the decorator must use it.
    assert "function _toolShortName" in UI_JS
    decorator_block = UI_JS[UI_JS.index("function _decorateTransparentEventRow"):UI_JS.index("function _setTransparentRowsExpanded")]
    assert "_toolShortName(name)" in decorator_block
    # buildToolCard preserves the full tool name on data-tool-name.
    assert 'dataset.toolName' in UI_JS or 'row.dataset.toolName' in UI_JS


def test_transparent_turn_header_is_collapsible():
    """The Hermes chat name tag (assistant role label) must be clickable
    in transparent mode to collapse/expand the entire event stack
    underneath. A chevron is appended to the role to telegraph the
    affordance; toggling flips data-transparent-turn-collapsed on the
    turn and the CSS collapses the blocks body via max-height."""
    assert "function _wireTransparentTurnToggle" in UI_JS
    assert "data-transparent-turn-collapsed" in UI_JS
    assert "transparent-turn-chevron" in UI_JS
    # The CSS must collapse the blocks when the data attribute is set.
    assert 'data-transparent-turn-collapsed="1"] .transparent-event-row' in STYLE_CSS
    # The chevron rotates on collapse.
    assert "transparent-turn-chevron" in STYLE_CSS
    assert "transform:rotate(-90deg)" in STYLE_CSS


def test_old_event_fading_medium_to_low():
    """Older transparent event rows fade (medium → low) so the eye lands on
    the most recent activity. The fade applies to the LIVE turn only (settled
    history stays full-opacity / readable) with a WCAG-respecting floor."""
    assert "function _applyTransparentRowFading" in UI_JS
    # Fading is gated to the live turn in JS (settled history is not dimmed).
    assert "id==='liveAssistantTurn'" in UI_JS
    # CSS steps from medium down to a readable floor.
    assert 'data-transparent-fade="1"' in STYLE_CSS
    assert 'data-transparent-fade="5"' in STYLE_CSS
    assert "opacity:.54" in STYLE_CSS
    # Hover restores full opacity.
    assert ".transparent-event-row[data-transparent-fade]:hover{opacity:1" in STYLE_CSS


def test_transparent_turn_footer_shows_elapsed_tokens_ttft_status():
    """The bottom-of-turn footer mirrors the live run-status line for
    settled turns: duration, first-token time, token usage, and final
    status. Only renders for turns that have transparent event rows."""
    assert "function _renderTransparentTurnFooter" in UI_JS
    assert "function _formatFirstToken" in UI_JS
    assert "transparent-turn-footer" in STYLE_CSS
    assert "lf-ttft" in STYLE_CSS
    assert "lf-time" in STYLE_CSS
    assert "lf-status" in STYLE_CSS
    # The footer must read duration, TTFT, and tokens from the message.
    assert "_firstTokenMs" in UI_JS
    assert "_formatFirstToken(msg._firstTokenMs)" in UI_JS
    assert "_formatTurnDuration(msg._turnDuration)" in UI_JS
    # i18n key for the TTFT tooltip.
    assert "first_token_time" in I18N_JS


def test_transparent_turn_wiring_runs_after_per_turn_duration_block():
    """The turn-level wiring (toggle + fading + footer) must run AFTER
    the per-turn duration block so the footer can reuse the computed
    duration / tokens / TTFT for each settled turn."""
    wiring_idx = UI_JS.index("// Transparent mode per-turn wiring:")
    duration_block_end = UI_JS.index("}", UI_JS.index("targetFoot.classList.add('msg-foot-with-usage')"))
    assert duration_block_end != -1
    assert wiring_idx > duration_block_end, (
        "transparent turn wiring must run after the per-turn duration block "
        "so the footer can reuse computed duration/tokens/TTFT"
    )


def test_muted_progress_bar_attached_to_each_event_row():
    """Each transparent event row keeps a very quiet progress affordance.
    The strip is neutral, 1px high, and only becomes visible while running so
    completed internal traces do not compete with assistant prose."""
    assert "function _attachProgressBar" in UI_JS
    # The decorator wires the progress bar at the end of the function.
    assert "_attachProgressBar(row, opts)" in UI_JS
    # CSS: bar is present and can shimmer while running, but has no accent glow.
    assert ".transparent-event-progress" in STYLE_CSS
    assert "transparent-progress-shimmer" in STYLE_CSS
    assert "@keyframes transparent-progress-shimmer" in STYLE_CSS
    assert "height:1px" in STYLE_CSS
    assert "border-radius:0 0 6px 6px" in STYLE_CSS
    assert "background:color-mix(in srgb,var(--muted) 42%,transparent)" in STYLE_CSS
    assert "box-shadow:none" in STYLE_CSS
    assert "opacity:0" in STYLE_CSS
    assert '.transparent-event-progress[data-progress-running="1"]::before{opacity:.55;' in STYLE_CSS
    # No 3D ridge or accent separator between adjacent rows.
    assert ".transparent-event-progress::after{\n  content:none;\n}" in STYLE_CSS
    assert ".transparent-event-row + .transparent-event-row::before" in STYLE_CSS
    assert ".transparent-event-row + .transparent-event-row::before{\n  content:none;\n}" in STYLE_CSS


def test_copy_button_position_is_stable_and_dedup_handles_legacy_template():
    """The copy button uses flex `order:9` so its position is stable
    regardless of which other elements (status, preview) are in the
    header. The toggle uses `order:10` so copy + toggle cluster at the
    right edge in a predictable order. _attachCopyButton also reuses
    the legacy .thinking-copy-btn baked into the thinking-card HTML
    template, preventing duplicate copy buttons in the thinking box."""
    # Flex order pinning.
    assert "order:9" in STYLE_CSS
    assert "order:10" in STYLE_CSS
    assert ".transparent-event-copy{\n  border:0;\n  background:transparent;\n  color:var(--muted);\n  opacity:.28;" in STYLE_CSS
    assert ".transparent-event-copy:hover,\n.transparent-event-copy:focus-visible{opacity:1;color:var(--text);background:transparent;}" in STYLE_CSS
    # Dedup: _attachCopyButton checks for both .transparent-event-copy
    # and .thinking-copy-btn.
    assert "'.transparent-event-copy,.thinking-copy-btn'" in UI_JS
    # The function normalises and rebinds existing buttons so cached HTML
    # restores keep copy behavior.
    assert "btn.classList.add('transparent-event-copy')" in UI_JS
    assert "return bindCopyButton(existing);" in UI_JS


def test_transparent_rows_remove_card_chrome_by_default():
    """Default transparent rows avoid card/banner styling. Neutral border and
    low-tint background are reserved for hover, focus, or expanded states."""
    assert "border:1px solid transparent" in STYLE_CSS
    assert "border-left:1px solid transparent" in STYLE_CSS
    assert "border-color:var(--border-subtle)" in STYLE_CSS
    assert "background:color-mix(in srgb,var(--surface-subtle) 62%,transparent)" in STYLE_CSS
    assert "background:color-mix(in srgb,var(--surface-subtle) 48%,transparent)" in STYLE_CSS
    assert "box-shadow:none" in STYLE_CSS
    assert "border-radius:6px" in STYLE_CSS


def test_smaller_tool_icons_and_reduced_font_sizes():
    """Tool icons are constrained to 11px (was 14px), and the tool name
    font is 11.5px (was 12.5px — reduced by two notches). The arg-key
    font is 10px (was 11px). Status badge gets a pill background and
    letter-spacing for visual distinction from the copy button."""
    # Icon wrapper size.
    assert "width:14px;height:14px" in STYLE_CSS
    # Inner SVG size.
    assert "width:11px;height:11px" in STYLE_CSS
    # Tool name font 11.5px (down from 12.5px).
    assert "font-size:11.5px" in STYLE_CSS
    # Status badge: pill background + letter-spacing.
    assert "border-radius:6px" in STYLE_CSS
    assert "letter-spacing:.02em" in STYLE_CSS


# ── Trifecta review fixes (round 2) ───────────────────────────────────────


def test_live_turn_restore_rehydrates_transparent_dom():
    """restoreLiveTurnHtmlForSession() restores liveTurnHtml via template.innerHTML,
    which drops the property-bound toggle/copy/expand handlers. It must re-run
    _rehydrateTransparentStreamDom so Transparent Stream controls keep working
    after an active-session live-turn restore. (Trifecta C1.)"""
    start = UI_JS.index("function restoreLiveTurnHtmlForSession")
    end = UI_JS.index("function markInflight", start)
    body = UI_JS[start:end]
    assert "_rehydrateTransparentStreamDom(restored)" in body


def test_transparent_settled_path_dedupes_echoed_thinking():
    """The transparent settled render must dedupe echoed thinking per turn
    (mirroring the compact path's seenReasons) so the same reasoning does not
    render twice / out of chronological order. (Trifecta O-Bug1.)"""
    assert "transparentSeenThinking" in UI_JS
    assert "_normalizeThinkingEchoCompare(event.thinkingText)" in UI_JS


def test_transparent_thinking_card_is_reset_to_flat_quiet_row():
    """Thinking inner cards must be reset to flat/transparent inside a transparent
    event row (they previously kept their accent-bg/border/radius/msg-rail chrome
    and were the heaviest object in the stream). (Trifecta V1.)"""
    assert ".transparent-event-row .thinking-card," in STYLE_CSS
    # The reset block carries background:transparent!important + border:0!important.
    reset_start = STYLE_CSS.index(".transparent-event-row .tool-card,")
    reset_block = STYLE_CSS[reset_start:reset_start + 700]
    assert ".transparent-event-row .thinking-card" in reset_block
    assert "background:transparent!important" in reset_block
    assert "border:0!important" in reset_block


def test_transparent_skin_reset_beats_per_skin_card_rules():
    """The reset must also win against the per-skin :root[data-skin] .tool-card
    rules that otherwise re-card the rows. (Trifecta V3.)"""
    assert ":root[data-skin] .transparent-event-row .tool-card" in STYLE_CSS


def test_transparent_failed_status_is_legible():
    """A failed tool must be visually legible (error color + left border), not an
    invisible muted badge. (Trifecta V2.)"""
    assert '.transparent-event-status[data-status="failed"]' in STYLE_CSS
    assert '.transparent-event-row[data-event-status="Failed"]' in STYLE_CSS


def test_transparent_interrupted_status_on_settled_tools():
    """A settled/reloaded tool left in done===false renders as Interrupted (not a
    permanent Running shimmer). (Trifecta O-Edge.)"""
    assert "function _transparentToolStatus(tc, settled)" in UI_JS
    assert "settled?'Interrupted':'Running'" in UI_JS
    start = UI_JS.index("function _anchorSceneTransparentNodeForRow(row, opts){")
    depth = 0
    end = None
    for idx in range(start, len(UI_JS)):
        char = UI_JS[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, "_anchorSceneTransparentNodeForRow body did not close"
    body = UI_JS[start:end]
    assert "else if(row.role==='tool')" in body
    assert "_transparentToolStatus(toolCall,settled)" in body
    assert "_transparentToolStatus(toolCall,true)" not in body
    assert "_transparentToolStatus(event.toolCall,true)" in UI_JS


def test_transparent_tool_completion_preserves_expand_state():
    """Tool completion rebuilds the row; it must carry over the user's open state
    and Full/Output detail tab. (Trifecta O-Bug2.)"""
    assert "_setTransparentCardOpen(_newCard,true)" in UI_JS


def test_transparent_entrance_animation_is_live_turn_only():
    """Transparent Stream must not keep the removed entrance animation rule."""
    assert "transparent-event-enter" not in STYLE_CSS


def test_live_worklog_reason_mirror_is_gated_to_compact_mode():
    """#4096: during a live multi-round turn in Transparent Stream mode, all
    assistant prose visually bunched at the top while every tool row clustered
    below, self-healing only when the turn settled.

    Root cause: _syncLiveWorklogReasonsForAnchor() runs on every live segment
    render (from _flushPendingSegmentRender + the RAF _doRender in messages.js).
    It builds the top-anchored `live-worklog` rail, mirrors each round's prose
    into a `wl-reason` row there, AND tags the real chronological inline
    `assistant-segment` as `assistant-segment-worklog-source` (-> display:none,
    style.css). That worklog-folding is the Compact Worklog presentation (#3401)
    and must NOT run in Transparent Stream mode, where prose stays as visible,
    chronologically-placed inline segments interleaved with tool rows.

    The fix gates the whole function on isCompactWorklogMode(). Assert the guard
    is the FIRST statement in the function body (before it touches
    ensureLiveWorklogContainer / _syncWorklogReasonFromAnchor) so it actually
    short-circuits in transparent mode rather than running the rail-build first.
    """
    start = UI_JS.index("function _syncLiveWorklogReasonsForAnchor(anchor, displayTextOverride){")
    end = UI_JS.index("\nfunction ", start + 1)
    body = UI_JS[start:end]

    # The compact-mode gate exists and short-circuits non-compact (transparent) mode.
    guard = "if(typeof isCompactWorklogMode==='function' && !isCompactWorklogMode()) return;"
    assert guard in body, "missing transparent-mode gate on _syncLiveWorklogReasonsForAnchor"

    # The guard must come BEFORE the rail is built / prose is mirrored, otherwise
    # it would not actually prevent the bunching.
    guard_idx = body.index(guard)
    assert guard_idx < body.index("ensureLiveWorklogContainer("), (
        "compact-mode gate must precede ensureLiveWorklogContainer() so transparent "
        "mode never builds the top worklog rail"
    )
    assert guard_idx < body.index("_syncWorklogReasonFromAnchor("), (
        "compact-mode gate must precede _syncWorklogReasonFromAnchor() so transparent "
        "mode never hides the inline assistant-segment or appends a wl-reason mirror"
    )

    # Both live-render call sites still invoke the (now-gated) helper — the gate
    # lives in the helper, not at the call sites, so live rendering is unchanged
    # in compact mode.
    MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    assert MESSAGES_JS.count("_syncLiveWorklogReasonsForAnchor(assistantRow") >= 2

    # The settled-render worklog-folding gate is also compact-only (regression
    # guard against the symmetric settled-path bug).
    render_message_start = UI_JS.index("const messageBelongsInWorklog=")
    render_message_end = UI_JS.index("if(messageBelongsInWorklog)", render_message_start)
    assert "isCompactWorklogMode()" in UI_JS[render_message_start:render_message_end]
