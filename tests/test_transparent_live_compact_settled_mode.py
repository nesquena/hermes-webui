"""Guard tests for the `transparent_live_compact_settled` chat activity mode.

The fourth mode streams activity transparently while a turn runs, then removes
settled tools and reasoning while preserving interim assistant comments once the final answer is available. Resolution is
per render path: `chatActivityLiveMode()` maps it to `transparent_stream`,
`chatActivitySettledMode()` stays `transparent_stream`, with a prose-only settled
filter. The three existing
modes must keep their exact behavior on both paths.
"""

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
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

MODE = "transparent_live_compact_settled"

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


def _run_node_script(script):
    assert NODE, "node is required for chat activity display mode behavior tests"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---------------------------------------------------------------- backend ---

def test_tlcs_backend_persists_and_rejects_invalid(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["chat_activity_display_mode"] == "compact_worklog"

    saved = config.save_settings({"chat_activity_display_mode": MODE})
    assert saved["chat_activity_display_mode"] == MODE
    assert json.loads(settings_path.read_text(encoding="utf-8"))["chat_activity_display_mode"] == MODE

    saved = config.save_settings({"chat_activity_display_mode": "bogus_mode"})
    assert saved["chat_activity_display_mode"] == MODE
    assert json.loads(settings_path.read_text(encoding="utf-8"))["chat_activity_display_mode"] == MODE


# -------------------------------------------------- resolver matrix (Node) ---

def test_tlcs_resolver_matrix_per_render_path():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{
  _chatActivityDisplayMode: 'compact_worklog',
  _transparentStream: false,
}};
global.isSimplifiedToolCalling = () => true;
eval(extractFunc('chatActivityMode'));
eval(extractFunc('chatActivityLiveMode'));
eval(extractFunc('chatActivitySettledMode'));
eval(extractFunc('isTransparentLiveMode'));
eval(extractFunc('isTransparentStream'));
eval(extractFunc('isFinalAnswerOnlyMode'));
eval(extractFunc('isCompactWorklogMode'));
const snapshot = () => [
  chatActivityMode(),
  chatActivityLiveMode(),
  chatActivitySettledMode(),
  isTransparentStream(),
  isTransparentLiveMode(),
  isFinalAnswerOnlyMode(),
  isCompactWorklogMode(),
];
const matrix = {{}};
for(const mode of ['compact_worklog','transparent_stream','{MODE}','hide_all_activity']){{
  window._chatActivityDisplayMode = mode;
  matrix[mode] = snapshot();
}}
window._chatActivityDisplayMode = 'bogus';
window._transparentStream = true;
matrix['bogus_legacy_fallback'] = snapshot();
process.stdout.write(JSON.stringify(matrix));
"""
    result = _run_node_script(script)

    assert result["compact_worklog"] == [
        "compact_worklog", "compact_worklog", "compact_worklog",
        False, False, False, True,
    ]
    assert result["transparent_stream"] == [
        "transparent_stream", "transparent_stream", "transparent_stream",
        True, True, False, False,
    ]
    # The TLCS contract: raw mode is preserved, live resolves transparent,
    # settled remains transparent so interim prose survives; a dedicated
    # prose-only filter removes tools and reasoning after conclusion.
    assert result[MODE] == [
        MODE, "transparent_stream", "transparent_stream",
        True, True, False, False,
    ]
    assert result["hide_all_activity"] == [
        "hide_all_activity", "hide_all_activity", "hide_all_activity",
        False, False, True, False,
    ]
    # Legacy _transparentStream fallback keeps resolving on both paths.
    assert result["bogus_legacy_fallback"] == [
        "transparent_stream", "transparent_stream", "transparent_stream",
        True, True, False, False,
    ]


# ------------------------------------------------- live render path (Node) ---

def test_tlcs_live_renderer_takes_transparent_branch():
    """renderLiveAnchorActivityScene must paint TLCS turns with the transparent
    live renderer, even when the caller hints {mode:'compact_worklog'}."""
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{
  _chatActivityDisplayMode: '{MODE}',
  _transparentStream: false,
}};
global.S = {{ session: {{ session_id: 'sid-1' }}, activeStreamId: 'stream-1' }};
global.isSimplifiedToolCalling = () => true;
global.$ = () => null;
let captured = null;
global._renderLiveAnchorActivitySceneTransparent = (streamId, scene, opts) => {{
  captured = {{ streamId, sceneMode: scene.mode, optMode: opts.mode }};
  return true;
}};
global._renderLiveAnchorActivitySceneCompactWorklog = () => {{
  throw new Error('compact worklog branch must not be used for a live TLCS turn');
}};
eval(extractFunc('chatActivityMode'));
eval(extractFunc('chatActivityLiveMode'));
eval(extractFunc('renderLiveAnchorActivityScene'));
const result = renderLiveAnchorActivityScene(
  'stream-1',
  {{version:'activity_scene_v1', mode:'compact_worklog', activity_rows:[{{role:'tool'}}]}},
  {{sessionId:'sid-1', mode:'compact_worklog'}},
);
process.stdout.write(JSON.stringify({{result, captured}}));
"""
    result = _run_node_script(script)

    assert result["result"] is True
    assert result["captured"] == {
        "streamId": "stream-1",
        "sceneMode": "compact_worklog",
        "optMode": "compact_worklog",
    }


def test_tlcs_live_scene_projection_uses_transparent_stream():
    """_renderLiveAnchorActivitySceneForStream resolves the LIVE mode, so a TLCS
    turn is projected with 'transparent_stream'."""
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
let projectedModes = [];
global._projectLiveAnchorActivitySceneForStream = (streamId, mode) => {{
  projectedModes.push(mode);
  return {{version:'activity_scene_v1', mode, activity_rows:[]}};
}};
global.renderLiveAnchorActivityScene = () => true;
global.chatActivityMode = () => '{MODE}';
eval(extractFunc('chatActivityLiveMode'));
eval(extractFunc('_renderLiveAnchorActivitySceneForStream'));
const result = _renderLiveAnchorActivitySceneForStream('stream-1', 'sid-1', {{mode:'compact_worklog'}});
process.stdout.write(JSON.stringify({{result, projectedModes}}));
"""
    result = _run_node_script(script)

    assert result["result"] is True
    assert result["projectedModes"] == ["transparent_stream"]


# ------------------------------------------- anchor scene resolvers (Node) ---

def test_tlcs_anchor_scene_mode_resolvers_in_messages_js():
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "messages.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{
  chatActivityMode() {{ return '{MODE}'; }},
  _chatActivityDisplayMode: '{MODE}',
  _transparentStream: false,
}};
eval(extractFunc('_anchorSceneActiveMode'));
eval(extractFunc('_anchorSceneLiveMode'));
eval(extractFunc('_anchorSceneSettledMode'));
const matrix = {{}};
for(const mode of ['compact_worklog','transparent_stream','{MODE}','hide_all_activity']){{
  window.chatActivityMode = () => mode;
  window._chatActivityDisplayMode = mode;
  matrix[mode] = [_anchorSceneActiveMode(), _anchorSceneLiveMode(), _anchorSceneSettledMode()];
}}
process.stdout.write(JSON.stringify(matrix));
"""
    result = _run_node_script(script)

    assert result["compact_worklog"] == ["compact_worklog", "compact_worklog", "compact_worklog"]
    assert result["transparent_stream"] == ["transparent_stream", "transparent_stream", "transparent_stream"]
    assert result[MODE] == [MODE, "transparent_stream", "transparent_stream"]
    assert result["hide_all_activity"] == ["hide_all_activity", "hide_all_activity", "hide_all_activity"]


# ------------------------------------------------------- static wiring ---

def test_tlcs_anchor_scene_call_sites_use_contextual_resolvers():
    """Live scene render uses the live resolver; projection + settled
    completion use the settled resolver."""
    assert "mode:_anchorSceneLiveMode()," in MESSAGES_JS
    assert "{mode:_anchorSceneSettledMode()}" in MESSAGES_JS
    assert "base.mode : _anchorSceneSettledMode();" in MESSAGES_JS
    assert "value==='transparent_live_compact_settled'" in MESSAGES_JS


def test_tlcs_ui_js_call_sites_use_contextual_resolvers():
    """Live gates resolve via isTransparentLiveMode(); settled hydration via
    chatActivitySettledMode(); isTransparentStream() keeps settled semantics."""
    assert "const activeMode=chatActivityLiveMode();" in UI_JS
    assert "const activityMode=typeof chatActivitySettledMode==='function'?chatActivitySettledMode():'compact_worklog';" in UI_JS
    assert UI_JS.count("if(isTransparentLiveMode()){") >= 3
    assert "if(!turn||!isTransparentLiveMode()) return;" in UI_JS
    assert "if(!root||!isTransparentLiveMode()) return;" in UI_JS
    assert "if(!isTransparentLiveMode()) return;" in UI_JS
    # Settled semantics preserved: the settled renderer and the live->settled
    # transition helpers still gate on the settled resolver.
    assert UI_JS.count("if(isTransparentStream())") >= 3


def test_tlcs_settled_fallback_renders_neither_reasoning_nor_tools():
    """Legacy settled rows must follow the same prose-only contract.

    This pins the reported mobile failure: tools disappeared at settle, but one
    disclosure row per reasoning block remained and filled the transcript.
    """
    render_messages = UI_JS.split("function renderMessages", 1)[1]
    activity_branch = render_messages.split("const byActivity = new Map();", 1)[1].split(
        "for(const [rawIdx,seg] of assistantSegments)", 1
    )[0]

    assert "if(isCompactWorklogMode()){" in activity_branch
    assert "}else if(isTransparentStream()&&chatActivityMode()!=='transparent_live_compact_settled'){" in activity_branch
    assert "if(!isTransparentStream()){" not in activity_branch


def test_tlcs_settled_scene_filter_keeps_interim_prose_only():
    rows = UI_JS.split("function _anchorSceneRowsForRendering", 1)[1].split(
        "function _anchorSceneIsSettledSuccessfulCompression", 1
    )[0]

    assert "const proseOnly=settled&&typeof window!=='undefined'&&window._chatActivityDisplayMode==='transparent_live_compact_settled';" in rows
    assert "if(proseOnly&&(row.role==='tool'||row.role==='thinking')) continue;" in rows


def test_tlcs_boot_and_panels_accept_the_fourth_value():
    assert "s.chat_activity_display_mode==='transparent_live_compact_settled'" in BOOT_JS
    assert PANELS_JS.count("transparent_live_compact_settled") >= 4


def test_tlcs_settings_ui_exposes_the_fourth_choice():
    assert 'data-chat-activity-mode="transparent_live_compact_settled"' in INDEX_HTML
    assert "_pickChatActivityDisplayMode('transparent_live_compact_settled')" in INDEX_HTML
    assert 'value="transparent_live_compact_settled"' in INDEX_HTML
    assert 'data-i18n="settings_option_transparent_live_compact_settled"' in INDEX_HTML
    assert INDEX_HTML.count('class="chat-activity-mode-btn') == 4
    assert "repeat(4,minmax(0,1fr))" in STYLE_CSS


def test_tlcs_backend_validation_lists_the_fourth_value():
    assert '"compact_worklog", "transparent_stream", "transparent_live_compact_settled", "hide_all_activity"' in CONFIG_PY
    assert '"chat_activity_display_mode": "compact_worklog"' in CONFIG_PY
    assert "compact_worklog | transparent_stream | transparent_live_compact_settled | hide_all_activity" in CONFIG_PY


def test_tlcs_i18n_covers_every_language():
    assert I18N_JS.count("settings_option_transparent_live_compact_settled") == I18N_JS.count("settings_option_final_answer_only")
    # Descriptions mention the new mode in at least English and French.
    assert "Live → Compact streams activity while the turn runs" in I18N_JS
    assert "Direct → Compact diffuse" in I18N_JS
