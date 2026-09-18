"""Guard tests for the `transparent_live_compact_settled` chat activity mode.

The fourth mode streams activity transparently while a turn runs, then folds
settled history into the Compact Worklog ("Trace: N tools"). Resolution is
per render path: `chatActivityLiveMode()` maps it to `transparent_stream`,
`chatActivitySettledMode()` maps it to `compact_worklog`. The three existing
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
    # settled resolves compact.
    assert result[MODE] == [
        MODE, "transparent_stream", "compact_worklog",
        False, True, False, True,
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


def test_tlcs_live_thinking_finalize_and_remove_preserve_rows():
    """Terminal cleanup still runs against the live transparent DOM. Both the
    done/finalize path and the error/cancel removal path must strip live state
    without deleting the thinking row before settled projection takes over."""
    script = f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
global.window = {{ _chatActivityDisplayMode: '{MODE}', _transparentStream: false }};
global.S = {{ session: {{ session_id: 'sid-1' }} }};
global.isSimplifiedToolCalling = () => true;
eval(extractFunc('chatActivityMode'));
eval(extractFunc('chatActivityLiveMode'));
eval(extractFunc('chatActivitySettledMode'));
eval(extractFunc('isTransparentLiveMode'));
eval(extractFunc('isTransparentStream'));

const makeRow = () => {{
  const attrs = new Set(['id','data-thinking-active','data-live-thinking']);
  return {{
    removed: false,
    removeAttribute(name) {{ attrs.delete(name); }},
    remove() {{ this.removed = true; }},
    has(name) {{ return attrs.has(name); }},
  }};
}};
const snapshot = row => ({{
  removed: row.removed,
  id: row.has('id'),
  active: row.has('data-thinking-active'),
  live: row.has('data-live-thinking'),
}});

const finalizeRow = makeRow();
global.$ = id => id === 'thinkingRow' ? finalizeRow : null;
eval(extractFunc('finalizeThinkingCard'));
finalizeThinkingCard();

const removeRow = makeRow();
const blocks = {{
  children: [removeRow],
  querySelectorAll(selector) {{
    return selector.includes('.agent-activity-thinking') ? [removeRow] : [];
  }},
}};
const liveTurn = {{
  removed: false,
  remove() {{ this.removed = true; }},
}};
global.$ = id => id === 'liveAssistantTurn' ? liveTurn : null;
global._assistantTurnBlocks = () => blocks;
eval(extractFunc('removeThinking'));
removeThinking();

process.stdout.write(JSON.stringify({{
  finalize: snapshot(finalizeRow),
  remove: snapshot(removeRow),
  liveTurnRemoved: liveTurn.removed,
}}));
"""
    result = _run_node_script(script)

    expected = {"removed": False, "id": False, "active": False, "live": False}
    assert result["finalize"] == expected
    assert result["remove"] == expected
    assert result["liveTurnRemoved"] is False


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
    assert result[MODE] == [MODE, "transparent_stream", "compact_worklog"]
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
    assert UI_JS.count("if(isTransparentStream())") >= 1


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


def test_tlcs_mode_picker_has_two_column_step_at_intermediate_widths():
    """Four labels do not fit four columns at tablet widths. The picker steps
    4 → 2 columns below ~1100px, and the pre-existing 768px rule still stacks
    to a single column; the three rules must cascade in that order."""
    four = STYLE_CSS.index("#mainSettings .chat-activity-mode-toggle{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))")
    two_media = STYLE_CSS.index("@media (max-width: 1100px){")
    two_rule = STYLE_CSS.index("#mainSettings .chat-activity-mode-toggle{grid-template-columns:repeat(2,minmax(0,1fr));}", two_media)
    one_media = STYLE_CSS.index("@media (max-width: 768px){", two_rule)
    one_rule = STYLE_CSS.index("#mainSettings .chat-activity-mode-toggle{grid-template-columns:1fr;}", one_media)
    assert four < two_media < two_rule < one_media < one_rule
    # The two-column rule lives inside the 1100px block, which closes before
    # the 768px block opens (no nested / overlapping media queries).
    assert STYLE_CSS.index("\n}", two_rule) < one_media


# ------------------------------------------------- settled ownership (Node) ---

_FAKE_DOM_JS = r"""
class FakeElement {
  constructor(tag='div'){
    this.tagName=String(tag).toUpperCase();
    this.id='';
    this.children=[];
    this.parentNode=null;
    this.attributes=Object.create(null);
    this._classes=new Set();
    this._innerHTML='';
    this._textContent='';
    this.onclick=null;
    this.onkeydown=null;
    const self=this;
    this.classList={
      add(...n){ n.forEach(x=>self._classes.add(x)); },
      remove(...n){ n.forEach(x=>self._classes.delete(x)); },
      contains(n){ return self._classes.has(n); },
      toggle(n,force){ const on=force===undefined?!self._classes.has(n):!!force; if(on) self._classes.add(n); else self._classes.delete(n); return on; },
    };
  }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(v){ this._classes=new Set(String(v).trim().split(/\s+/).filter(Boolean)); }
  get firstChild(){ return this.children[0]||null; }
  get parentElement(){ return this.parentNode; }
  get innerHTML(){ return this._innerHTML; }
  set innerHTML(v){ this._innerHTML=String(v??''); this.children=[]; }
  get textContent(){ return this.children.length?this.children.map(c=>c.textContent).join(''):this._textContent; }
  set textContent(v){ this._textContent=String(v??''); this.children=[]; }
  setAttribute(k,v){ this.attributes[String(k)]=String(v); if(k==='id') this.id=String(v); }
  getAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attributes,k)?this.attributes[k]:null; }
  hasAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attributes,k); }
  removeAttribute(k){ delete this.attributes[k]; if(k==='id') this.id=''; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  appendChild(c){ if(c.parentNode) c.remove(); c.parentNode=this; this.children.push(c); return c; }
  insertBefore(c,ref){ if(c.parentNode) c.remove(); c.parentNode=this; const i=this.children.indexOf(ref); if(i<0) this.children.push(c); else this.children.splice(i,0,c); return c; }
  remove(){ if(!this.parentNode) return; const s=this.parentNode.children; const i=s.indexOf(this); if(i>=0) s.splice(i,1); this.parentNode=null; }
  matches(sel){ return matchesSelector(this,sel); }
  closest(sel){ let n=this; while(n){ if(matchesSelector(n,sel)) return n; n=n.parentNode; } return null; }
  querySelector(sel){ return this.querySelectorAll(sel)[0]||null; }
  querySelectorAll(sel){ const out=[]; const walk=n=>{ for(const c of n.children){ if(matchesSelector(c,sel)) out.push(c); walk(c); } }; walk(this); return out; }
}
function matchesSelector(el,selector){
  return String(selector||'').split(',').map(s=>s.trim()).filter(Boolean).some(part=>matchesSimple(el,part));
}
function matchesSimple(el,selector){
  selector=selector.replace(/^:scope\s*>\s*/,'').trim();
  if(!selector) return false;
  const idMatch=selector.match(/#([^.\[#]+)/);
  if(idMatch&&el.id!==idMatch[1]) return false;
  const cls=selector.match(/\.([A-Za-z0-9_-]+)/g)||[];
  for(const c of cls){ if(!el.classList.contains(c.slice(1))) return false; }
  const attrs=selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/g)||[];
  for(const a of attrs){
    const [,name,expected]=a.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
    const value=el.getAttribute(name);
    if(value===null) return false;
    if(expected!==undefined&&String(value)!==String(expected)) return false;
  }
  return !!(idMatch||cls.length||attrs.length);
}
function el(tag,className,attrs){
  const node=new FakeElement(tag);
  if(className) node.className=className;
  Object.entries(attrs||{}).forEach(([k,v])=>node.setAttribute(k,v));
  return node;
}
// A settled assistant turn as the Compact Worklog renders it: role label,
// one collapsed tool-call group, the answer segment. No transparent rows.
function makeSettledCompactTurn(msgIdx){
  const turn=el('div','assistant-turn');
  const role=el('div','msg-role assistant'); role.textContent='Hermes';
  const blocks=el('div','assistant-turn-blocks');
  const group=el('div','tool-call-group',{'data-deferred-worklog':'1','data-worklog-tool-count':'3'});
  const seg=el('div','assistant-segment',{'data-msg-idx':String(msgIdx)});
  blocks.appendChild(group); blocks.appendChild(seg);
  turn.appendChild(role); turn.appendChild(blocks);
  return turn;
}
function makeTransparentRow(){
  const row=el('div','transparent-event-row',{'data-transparent-event-row':'1','data-event-type':'tool','data-transparent-stream':'1'});
  const card=el('div','tool-card'); const header=el('div','tool-card-header');
  card.appendChild(header); row.appendChild(card);
  return row;
}
function makeTransparentTurn(msgIdx,rowCount){
  const turn=makeSettledCompactTurn(msgIdx);
  const blocks=turn.querySelector('.assistant-turn-blocks');
  blocks.querySelector('.tool-call-group').remove();
  const seg=blocks.querySelector('.assistant-segment');
  for(let i=0;i<rowCount;i++) blocks.insertBefore(makeTransparentRow(),seg);
  return turn;
}
function controlsSnapshot(turn){
  const role=turn.querySelector('.msg-role.assistant');
  return {
    chevrons: turn.querySelectorAll('.transparent-turn-chevron').length,
    role: role.getAttribute('role'),
    tabindex: role.getAttribute('tabindex'),
    ariaExpanded: role.getAttribute('aria-expanded'),
    toggleBound: turn.getAttribute('data-transparent-turn-toggle-bound'),
    roleOnclick: typeof role.onclick==='function',
    roleOnkeydown: typeof role.onkeydown==='function',
    controlBars: turn.querySelectorAll('.transparent-event-controls').length,
    buttons: turn.querySelectorAll('[role="button"]').length,
  };
}
"""

_OWNERSHIP_FUNCS = [
    "chatActivityMode", "chatActivityLiveMode", "chatActivitySettledMode",
    "isTransparentLiveMode", "isTransparentStream", "_assistantTurnBlocks",
    "_transparentEventCountLabel", "_transparentTurnOwnsControls",
    "_syncTransparentEventControls", "_rehydrateTransparentStreamDom",
    "_wireTransparentTurnToggle", "_applyTransparentRowFading",
]


def _ownership_script(body):
    evals = "\n".join(f"eval(extractFunc({json.dumps(name)}));" for name in _OWNERSHIP_FUNCS)
    return f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(ROOT / "static" / "ui.js"))}, 'utf8');
{_EXTRACT_FUNC_JS}
{_FAKE_DOM_JS}
global.window = {{ _chatActivityDisplayMode: '{MODE}', _transparentStream: false }};
global.document = {{ createElement: (tag)=>new FakeElement(tag) }};
global.S = {{ session: {{ session_id: 'sid-1' }}, messages: [] }};
global.t = (key)=>key;
global.li = ()=>'<svg></svg>';
global.isSimplifiedToolCalling = ()=>true;
global._wireTransparentHeaderToggle = ()=>{{}};
global._attachCopyButton = ()=>{{}};
global._setTransparentCardOpen = ()=>{{}};
global._setTransparentRowsExpanded = ()=>{{}};
{evals}
{body}
"""


_INERT = {
    "chevrons": 0, "role": None, "tabindex": None, "ariaExpanded": None,
    "toggleBound": None, "roleOnclick": False, "roleOnkeydown": False,
    "controlBars": 0, "buttons": 0,
}


def test_tlcs_cache_restore_leaves_settled_compact_turns_without_transparent_controls():
    """Gate blocker: the session HTML-cache fast path re-runs
    _rehydrateTransparentStreamDom on the restored transcript. In hybrid mode
    isTransparentLiveMode() is true, but a settled turn rendered as a Compact
    Worklog owns no transparent rows — it must not receive the name-tag chevron,
    role=button, tabindex, aria-expanded, click handlers, or a "Trace" bar."""
    script = _ownership_script("""
const inner=el('div','',{});
inner.id='msgInner';
const turnA=makeSettledCompactTurn(1);
const turnB=makeSettledCompactTurn(3);
inner.appendChild(turnA); inner.appendChild(turnB);
// First visit + switch-away/switch-back both serve the cached HTML and rehydrate.
_rehydrateTransparentStreamDom(inner);
const afterFirstRestore=[controlsSnapshot(turnA),controlsSnapshot(turnB)];
_rehydrateTransparentStreamDom(inner);
const afterSwitchBack=[controlsSnapshot(turnA),controlsSnapshot(turnB)];
// Direct calls on the settled turn must be equally inert.
_wireTransparentTurnToggle(turnA);
_syncTransparentEventControls(turnA);
const afterDirect=controlsSnapshot(turnA);
process.stdout.write(JSON.stringify({afterFirstRestore,afterSwitchBack,afterDirect,owns:_transparentTurnOwnsControls(turnA)}));
""")
    result = _run_node_script(script)

    assert result["owns"] is False
    assert result["afterFirstRestore"] == [_INERT, _INERT]
    assert result["afterSwitchBack"] == [_INERT, _INERT]
    assert result["afterDirect"] == _INERT


def test_tlcs_live_turn_and_transparent_rows_still_own_controls_after_restore():
    """The ownership guard must not regress the real owners: the live turn
    (mid-stream reload restore) and any turn that actually carries transparent
    event rows (pure Transparent Stream settled history) keep the chevron and
    the "Trace: N tools" bar."""
    script = _ownership_script("""
const inner=el('div','',{});
const settled=makeSettledCompactTurn(1);
const live=makeTransparentTurn(5,2);
live.id='liveAssistantTurn';
inner.appendChild(settled); inner.appendChild(live);
_rehydrateTransparentStreamDom(inner);
const hybrid={settled:controlsSnapshot(settled),live:controlsSnapshot(live),
  liveBarCount:(live.querySelector('.transparent-event-controls')||{getAttribute(){return null;}}).getAttribute('data-tool-count')};
// Switch the setting to pure Transparent Stream: a settled turn with rows owns
// its controls exactly as before this guard existed.
window._chatActivityDisplayMode='transparent_stream';
const transparentSettled=makeTransparentTurn(7,3);
const inner2=el('div','',{});
inner2.appendChild(transparentSettled);
_rehydrateTransparentStreamDom(inner2);
const pure={settled:controlsSnapshot(transparentSettled),owns:_transparentTurnOwnsControls(transparentSettled)};
// Switch back to hybrid and restore a compact transcript again: still inert,
// and a stale "Trace" bar left in cached HTML is dropped rather than rewired.
window._chatActivityDisplayMode='transparent_live_compact_settled';
const inner3=el('div','',{});
const compactAgain=makeSettledCompactTurn(9);
const staleBar=el('div','transparent-event-controls');
compactAgain.querySelector('.assistant-turn-blocks').insertBefore(staleBar,compactAgain.querySelector('.assistant-turn-blocks').firstChild);
inner3.appendChild(compactAgain);
_rehydrateTransparentStreamDom(inner3);
const back={settled:controlsSnapshot(compactAgain),staleBarDetached:staleBar.parentNode===null};
process.stdout.write(JSON.stringify({hybrid,pure,back}));
""")
    result = _run_node_script(script)

    assert result["hybrid"]["settled"] == _INERT
    live = result["hybrid"]["live"]
    assert live["chevrons"] == 1
    assert live["role"] == "button"
    assert live["tabindex"] == "0"
    assert live["ariaExpanded"] == "true"
    assert live["toggleBound"] == "1"
    assert live["roleOnclick"] is True and live["roleOnkeydown"] is True
    assert live["controlBars"] == 1
    assert result["hybrid"]["liveBarCount"] == "2"

    assert result["pure"]["owns"] is True
    pure = result["pure"]["settled"]
    assert pure["chevrons"] == 1 and pure["role"] == "button" and pure["controlBars"] == 1

    assert result["back"]["settled"] == _INERT
    assert result["back"]["staleBarDetached"] is True


# ------------------------------------------------- live prose fade (Node) ---

def test_tlcs_hybrid_live_prose_uses_transparent_fade_with_reduced_motion_parity():
    """_shouldUseTransparentStreamFade resolves the LIVE mode: hybrid live prose
    gets the same Transparent-Stream fade as pure transparent mode, pure compact
    never does, reduced motion disables it everywhere, and the settled
    predicate stays the fallback when the live resolver is absent."""
    ui = json.dumps(str(ROOT / "static" / "ui.js"))
    messages = json.dumps(str(ROOT / "static" / "messages.js"))
    script = f"""
const fs = require('fs');
let src = fs.readFileSync({ui}, 'utf8');
{_EXTRACT_FUNC_JS}
global.isSimplifiedToolCalling = () => true;
eval(extractFunc('chatActivityMode'));
eval(extractFunc('chatActivityLiveMode'));
eval(extractFunc('chatActivitySettledMode'));
eval(extractFunc('isTransparentLiveMode'));
eval(extractFunc('isTransparentStream'));
src = fs.readFileSync({messages}, 'utf8');
let _streamFadeReduceMotionMql=null;
let _streamFadeReduceMotion=false;
let _streamFadeReduceMotionOnChange=null;
let reduceMotion=false;
global.window = {{
  _chatActivityDisplayMode: 'compact_worklog',
  _transparentStream: false,
  _fadeTextEffect: false,
  matchMedia(){{ return {{ get matches(){{ return reduceMotion; }}, addEventListener(){{}}, removeEventListener(){{}} }}; }},
}};
eval(extractFunc('_shouldUseStreamFade'));
eval(extractFunc('_shouldUseTransparentStreamFade'));
eval(extractFunc('_streamFadeReduceMotionEnabled'));
eval(extractFunc('_shouldUseLiveProseFade'));
const out = {{}};
for(const mode of ['compact_worklog','transparent_stream','{MODE}','hide_all_activity']){{
  window._chatActivityDisplayMode = mode;
  reduceMotion=false; _streamFadeReduceMotionMql=null;
  const motion = [_shouldUseTransparentStreamFade(), _shouldUseLiveProseFade()];
  reduceMotion=true; _streamFadeReduceMotionMql=null;
  const reduced = [_shouldUseTransparentStreamFade(), _shouldUseLiveProseFade()];
  out[mode] = {{ motion, reduced }};
}}
// Fallback: runtimes exposing only the settled predicate keep the old behavior.
reduceMotion=false; _streamFadeReduceMotionMql=null;
const liveResolver = global.isTransparentLiveMode;
delete global.isTransparentLiveMode;
global.isTransparentStream = () => true;
out.fallbackSettledTrue = [_shouldUseTransparentStreamFade(), _shouldUseLiveProseFade()];
global.isTransparentStream = () => false;
out.fallbackSettledFalse = [_shouldUseTransparentStreamFade(), _shouldUseLiveProseFade()];
global.isTransparentLiveMode = liveResolver;
process.stdout.write(JSON.stringify(out));
"""
    result = _run_node_script(script)

    assert result["compact_worklog"] == {"motion": [False, False], "reduced": [False, False]}
    assert result["transparent_stream"] == {"motion": [True, True], "reduced": [True, False]}
    # Hybrid live prose: fade on while motion is allowed, off under reduced motion.
    assert result[MODE] == {"motion": [True, True], "reduced": [True, False]}
    assert result["hide_all_activity"] == {"motion": [False, False], "reduced": [False, False]}
    assert result["fallbackSettledTrue"] == [True, True]
    assert result["fallbackSettledFalse"] == [False, False]
