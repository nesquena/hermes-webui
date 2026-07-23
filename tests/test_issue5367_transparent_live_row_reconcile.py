"""Browserless regression for transparent stream live row reconciliation."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _run_node_script(script, ui_js_path=None):
    assert NODE, "node is required for DOM-executed anchor render tests"
    env = os.environ.copy()
    if ui_js_path is not None:
        env["UI_JS_PATH"] = ui_js_path
    result = subprocess.run([NODE, "-e", script], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_transparent_thinking_scroll_container_reserves_gutter():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    # Find the transparent thinking scroll container block and verify scrollbar-gutter:stable is present.
    # There are multiple blocks with this selector — check that at least one has the property.
    import re
    selector = ".transparent-event-row .thinking-card.open .thinking-card-body{"
    pattern = re.escape(selector) + r'([\s\S]*?)\}'
    for match in re.finditer(pattern, css):
        if "scrollbar-gutter:stable" in match.group(1):
            return  # Test passes — found the property in a matching block
    assert False, f"scrollbar-gutter:stable not found in any {selector} block"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_scene_reuses_matching_rows_and_removes_stale_rows():
    script = """
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){{
  const marker = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){{
    if(src[i] === '{{') depth += 1;
    else if(src[i] === '}}') depth -= 1;
    i += 1;
  }}
  return src.slice(start, i);
}}
class FakeElement {{
  static moves = 0;
  constructor(tag='div'){{
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.hidden = false;
    this.id = '';
    this._textContent = '';
    this._innerHTML = '';
    this._classes = new Set();
    const self = this;
    this.classList = {{
      add(...names){{ names.forEach(name=>self._classes.add(name)); }},
      remove(...names){{ names.forEach(name=>self._classes.delete(name)); }},
      contains(name){{ return self._classes.has(name); }},
      toggle(name, force){{
        if(force === true){{ self._classes.add(name); return true; }}
        if(force === false){{ self._classes.delete(name); return false; }}
        if(self._classes.has(name)){{ self._classes.delete(name); return false; }}
        self._classes.add(name);
        return true;
      }},
    }};
  }}
  get parentElement(){{ return this.parentNode; }}
  get firstChild(){{ return this.children[0]||null; }}
  get nextSibling(){{
    if(!this.parentNode) return null;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    return idx >= 0 ? (siblings[idx + 1] || null) : null;
  }}
  get className(){{
    return Array.from(this._classes).join(' ');
  }}
  set className(value){{
    this._classes = new Set(String(value).trim().split(/\\s+/).filter(Boolean));
  }}
  get textContent(){{
    return this._textContent;
  }}
  set textContent(value){{
    this._textContent = String(value ?? '');
    this._innerHTML = this._textContent;
    this.children = [];
  }}
  get innerHTML(){{
    return this._innerHTML;
  }}
  set innerHTML(value){{
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
  }}
  setAttribute(name, value){{
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){{
      const dataKey = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[dataKey] = val;
    }}
    if(key === 'class'){
      this.className = val;
    }}
  }}
  getAttribute(name){{
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }}
  getAttributeNames(){{
    return Object.keys(this.attributes);
  }}
  removeAttribute(name){{
    delete this.attributes[name];
    if(name === 'id') this.id = '';
    if(name.startsWith('data-')){{
      const dataKey = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      delete this.dataset[dataKey];
    }}
    if(name === 'class'){
      this._classes = new Set();
    }}
  }}
  appendChild(child){{
    this.appendChildCount = (this.appendChildCount || 0) + 1;
    if(child && child.tagName === '#FRAGMENT'){{
      child.children.slice().forEach(grandchild=>this.appendChild(grandchild));
      child.children = [];
      return child;
    }}
    if(child && child.parentNode){{
      FakeElement.moves += 1;
      child.remove();
    }}
    if(!child) return null;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }}
  insertBefore(child, refNode){{
    this.insertBeforeCount = (this.insertBeforeCount || 0) + 1;
    if(child && child.parentNode){{
      FakeElement.moves += 1;
      child.remove();
    }}
    if(!child) return null;
    const idx = this.children.indexOf(refNode);
    child.parentNode = this;
    if(idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }}
  remove(){{
    if(!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if(idx >= 0) siblings.splice(idx, 1);
    this.parentNode = null;
  }}
  matches(selector){{
    return matchesSelector(this, selector);
  }}
  querySelector(selector){{
    return this.querySelectorAll(selector)[0] || null;
  }}
  querySelectorAll(selector){{
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }}
  closest(selector){{
    let node = this;
    while(node){
      if(matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }}
    return null;
  }}
}}
function matchesSelector(el, selector){{
  if(!selector) return false;
  const options = selector.split(',').map(part=>part.trim()).filter(Boolean);
  return options.some(part=>matchesSimple(el, part));
}}
function matchesSimple(el, selector){{
  selector = selector.replace(/^:scope\\s*>\\s*/, '').trim();
  if(!selector) return false;
  const idMatch = selector.match(/#([^.\\[#]+)/);
  if(idMatch && el.id !== idMatch[1]) return false;
  const clsMatches = selector.match(/\\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){{
    const name = cls.slice(1);
    if(!el.classList.contains(name)) return false;
  }}
  const attrMatches = selector.match(/\\[([^=\\]]+)(?:=\\"([^\\"]*)\\")?\\]/g) || [];
  for(const attrMatch of attrMatches){{
    const [, name, expected] = attrMatch.match(/\\[([^=\\]]+)(?:=\\"([^\\"]*)\\")?\\]/);
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }}
  return !!(idMatch || clsMatches.length || attrMatches.length);
}}

global.window = {{}};
global.document = {{
  createElement:(tag)=>new FakeElement(tag),
  createTextNode:(text)=>{{
    const node = new FakeElement('#text');
    node.textContent = text;
    return node;
  }},
  createDocumentFragment:()=>new FakeElement('#fragment'),
}};
global.CSS = {{ escape:(value)=>String(value) }};
global.requestAnimationFrame = (fn)=>fn();

global.S = {{ session:{{ session_id: 'session-1', pending_started_at: 123 }}, activeStreamId:'stream-1' }};
global._captureMessageScrollSnapshot = () => ({{ scrollHeight: 1000 }});
global._prepareLiveAnchorScrollRebuildGuard = () => ({{ readerAwayFromBottom:false, release:null }});
global._restoreMessageScrollSnapshotSameFrame = () => {{}};
global.scrollIfPinned = () => {{}};
global._moveLiveRunStatusToTurnEnd = () => {{}};
global._messageUserUnpinned = false;
global._syncTransparentEventControls = () => {{}};
global._anchorSceneRowsForRendering = (scene) => scene && scene.activity_rows || [];
global._anchorSceneNodeForRow = (row) => {{
  const node = new FakeElement('div');
  node.classList.add('assistant-segment');
  node.textContent = String(row && (row.text || row.thinking&&row.thinking.text || '') || '');
  return node;
}};
global._decorateTransparentEventRow = (node, opts) => {{
  node.classList.add('transparent-event-row');
  node.setAttribute('data-transparent-event-row','1');
  if(opts && Object.prototype.hasOwnProperty.call(opts,'type')) node.setAttribute('data-event-type', opts.type);
  if(opts && Object.prototype.hasOwnProperty.call(opts,'text')) node.setAttribute('data-text', opts.text);
  if(opts && Object.prototype.hasOwnProperty.call(opts,'status')) node.setAttribute('data-event-status', opts.status);
  return node;
}};
global._thinkingActivityNode = (text)=>{{
  const node = new FakeElement('div');
  node.classList.add('agent-activity-thinking');
  node.textContent = text || '';
  return node;
}};
global._anchorSceneToolCallFromRow = (row) => ({{
  name:(row.tool && row.tool.name) || row.tool_name || 'tool',
  done:true
}});
global._autoCompressionWorklogNode = () => new FakeElement('div');
global._autoCompressionPreviewText = () => 'preview';
global._transparentToolStatus = () => 'done';
global.buildToolCard = () => {{
  const node = new FakeElement('div');
  node.classList.add('tool-card-row');
  return node;
}};

const emptyState = new FakeElement('div');
const msgInner = new FakeElement('div');
const messages = new FakeElement('div');
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const liveRunStatus = new FakeElement('div');
liveRunStatus.id = 'liveRunStatus';
msgInner.appendChild(turn);
turn.appendChild(liveRunStatus);
global.document._findById = (id) => id === 'emptyState' ? emptyState : id === 'msgInner' ? msgInner : id === 'messages' ? messages : id === 'liveAssistantTurn' ? turn : null;
global.$ = (id)=>global.document._findById(id);
global._createAssistantTurn = () => turn;
global._assistantTurnBlocks = () => turn;

global._anchorSceneTransparentNodeForRow = (row) => null;
eval(extractFunc('_anchorSceneLiveTokenFinalPrefix'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
eval(extractFunc('_transparentLiveRowKey'));
eval(extractFunc('_transparentLiveRowsCompatible'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_bindTransparentFadeCleanup'));
eval(extractFunc('_appendTransparentFadeText'));
eval(extractFunc('_refreshTransparentFadeProseRow'));
eval(extractFunc('_refreshTransparentLiveRow'));
eval(extractFunc('_renderLiveAnchorActivitySceneTransparent'));

const firstScene = {{
  version:'activity_scene_v1',
  activity_rows:[
    {{ row_id:'row-kept', role:'prose', source_event_type:'process_prose', text:'first progress line' }},
    {{ row_id:'row-stale', role:'prose', source_event_type:'process_prose', text:'will be removed' }},
  ],
}};
const secondScene = {{
  version:'activity_scene_v1',
  activity_rows:[
    {{ row_id:'row-kept', role:'prose', source_event_type:'process_prose', text:'updated progress line' }},
    {{ row_id:'row-new', role:'prose', source_event_type:'process_prose', text:'new row appears' }},
  ],
}};
const thirdScene = {{
  version:'activity_scene_v1',
  activity_rows:[
    {{ role:'prose', source_event_type:'process_prose', text:'keyless row first' }},
  ],
}};
const fourthScene = {{
  version:'activity_scene_v1',
  activity_rows:[
    {{ role:'prose', source_event_type:'process_prose', text:'keyless row second' }},
  ],
}};

const firstRender = _renderLiveAnchorActivitySceneTransparent('stream-1', firstScene, {{ sessionId:'session-1' }});
const keptAfterFirst = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-kept\"]');
const staleAfterFirst = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-stale\"]');
const firstFooter = turn.querySelector('#liveRunStatus');

turn.insertBeforeCount = 0;
turn.appendChildCount = 0;
const secondRender = _renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, {{ sessionId:'session-1' }});
const secondInsertBeforeCount = turn.insertBeforeCount || 0;
const secondAppendChildCount = turn.appendChildCount || 0;
const keptAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-kept\"]');
const staleAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-stale\"]');
const newAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-new\"]');
const rows = turn.children.filter((child) => child.classList.contains('transparent-event-row'));
const movesBeforeStableRender = FakeElement.moves;
const stableOrderRender = _renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, {{ sessionId:'session-1' }});
const movesAfterStableRender = FakeElement.moves;
const idxs = {{
  keptDirect: turn.children.indexOf(keptAfterSecond),
  freshDirect: turn.children.indexOf(newAfterSecond),
  footerDirect: turn.children.indexOf(firstFooter),
  staleInVisibleRows: rows.findIndex((child) => child.getAttribute('data-anchor-row-id') === 'row-stale'),
  rowKeeps: rows.indexOf(keptAfterSecond),
  rowNew: rows.indexOf(newAfterSecond),
  stale: rows.findIndex((child) => child.getAttribute('data-anchor-row-id') === 'row-stale'),
}};

turn.insertBeforeCount = 0;
turn.appendChildCount = 0;
_renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, {{ sessionId:'session-1' }});
const stableInsertBeforeCount = turn.insertBeforeCount || 0;
const stableAppendChildCount = turn.appendChildCount || 0;

const reorderedScene = {{
  version:'activity_scene_v1',
  activity_rows:[
    {{ row_id:'row-new', role:'prose', source_event_type:'process_prose', text:'new row appears' }},
    {{ row_id:'row-kept', role:'prose', source_event_type:'process_prose', text:'updated progress line' }},
  ],
}};
turn.insertBeforeCount = 0;
turn.appendChildCount = 0;
_renderLiveAnchorActivitySceneTransparent('stream-1', reorderedScene, {{ sessionId:'session-1' }});
const reorderInsertBeforeCount = turn.insertBeforeCount || 0;
const reorderedIds = turn.children
  .filter((child) => child.classList.contains('transparent-event-row'))
  .map((child) => child.getAttribute('data-anchor-row-id'));

firstFooter.remove();
turn.insertBefore(firstFooter, turn.children[0] || null);
turn.insertBeforeCount = 0;
turn.appendChildCount = 0;
_renderLiveAnchorActivitySceneTransparent('stream-1', reorderedScene, {{ sessionId:'session-1' }});
const footerRepairInsertBeforeCount = turn.insertBeforeCount || 0;
const footerRepairIdxs = {{
  newRow: turn.children.indexOf(turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-new\"]')),
  keptRow: turn.children.indexOf(turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-kept\"]')),
  footer: turn.children.indexOf(firstFooter),
}};

_renderLiveAnchorActivitySceneTransparent('stream-1', thirdScene, {{ sessionId:'session-1' }});
const keylessRowsAfterThird = turn.querySelectorAll('.transparent-event-row[data-anchor-row-id=\"\"]');
_renderLiveAnchorActivitySceneTransparent('stream-1', fourthScene, {{ sessionId:'session-1' }});
const keylessRowsAfterFourth = turn.querySelectorAll('.transparent-event-row[data-anchor-row-id=\"\"]');

const fadeParent = new FakeElement('div');
const fadeExisting = new FakeElement('div');
fadeExisting.className = 'assistant-segment transparent-event-row';
fadeExisting.setAttribute('data-anchor-row-role', 'prose');
fadeExisting.setAttribute('data-anchor-row-id', 'fade-row');
fadeExisting.setAttribute('data-anchor-source-event-type', 'process_prose');
const staleBody = new FakeElement('div');
staleBody.className = 'msg-body stream-fade-active';
const oldSpan = new FakeElement('span');
oldSpan.className = 'stream-fade-word is-new';
oldSpan.textContent = 'old';
staleBody.appendChild(oldSpan);
staleBody.appendChild((()=>{{ const n = new FakeElement('#text'); n.textContent = ' '; return n; }})());
fadeExisting.appendChild(staleBody);
fadeExisting.setAttribute('data-stream-fade-text', 'old ');
fadeParent.appendChild(fadeExisting);

const fadeCandidate = new FakeElement('div');
fadeCandidate.className = 'assistant-segment transparent-event-row';
fadeCandidate.setAttribute('data-anchor-row-role', 'prose');
fadeCandidate.setAttribute('data-anchor-row-id', 'fade-row');
fadeCandidate.setAttribute('data-anchor-source-event-type', 'process_prose');
fadeCandidate.dataset.rawText = 'old new';
const fadeBody = new FakeElement('div');
fadeBody.className = 'msg-body stream-fade-active';
const fadeSpan = new FakeElement('span');
fadeSpan.className = 'stream-fade-word is-new';
fadeSpan.textContent = 'old';
fadeBody.appendChild(fadeSpan);
fadeBody.appendChild((()=>{{ const n = new FakeElement('#text'); n.textContent = ' new'; return n; }})());
fadeCandidate.appendChild(fadeBody);
const fadeRefresh = _refreshTransparentLiveRow(fadeExisting, fadeCandidate);
const fadeSpans = fadeParent.querySelectorAll('.stream-fade-word.is-new');

process.stdout.write(JSON.stringify({{
  firstRender,
  secondRender,
  stableOrderRender,
  sameNode: keptAfterFirst === keptAfterSecond,
  stableOrderMovedRows: movesAfterStableRender - movesBeforeStableRender,
  keptId: keptAfterSecond && keptAfterSecond.getAttribute('data-anchor-row-id'),
  keptSource: keptAfterSecond && keptAfterSecond.getAttribute('data-anchor-source-event-type'),
  keptText: keptAfterSecond && keptAfterSecond.textContent,
  staleGone: staleAfterSecond === null,
  staleAfterFirst: staleAfterFirst !== null,
  idxs,
  secondInsertBeforeCount,
  secondAppendChildCount,
  stableInsertBeforeCount,
  stableAppendChildCount,
  reorderInsertBeforeCount,
  reorderedIds,
  footerRepairInsertBeforeCount,
  footerRepairIdxs,
  hasNewRow: !!newAfterSecond,
  newRowSession: newAfterSecond && newAfterSecond.getAttribute('data-session-id'),
  keylessAfterThird: keylessRowsAfterThird.length,
  keylessAfterFourth: keylessRowsAfterFourth.length,
  keylessTextsAfterFourth: keylessRowsAfterFourth.map((child) => child.textContent),
  totalRowsAfterFourth: turn.children.filter((child) => child.classList.contains('transparent-event-row')).length,
  fadeKeptExisting: fadeRefresh === fadeExisting,
  fadeExistingStillParented: fadeExisting.parentNode === fadeParent,
  fadeCandidateDetached: fadeCandidate.parentNode === null,
  fadeOldSpanPreserved: fadeSpans[0] === oldSpan,
  fadeSpanTexts: fadeSpans.map(span=>span.textContent),
  fadeText: fadeExisting.getAttribute('data-stream-fade-text'),
  fadeSpanPreserved: !!fadeParent.querySelector('.stream-fade-word.is-new'),
  fadeBodyActive: !!fadeParent.querySelector('.msg-body.stream-fade-active'),
}}));
"""
    script = script.replace("{{", "{").replace("}}", "}")
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["firstRender"] is True
    assert data["secondRender"] is True
    assert data["stableOrderRender"] is True
    assert data["sameNode"] is True
    assert data["stableOrderMovedRows"] == 0
    assert data["keptId"] == "row-kept"
    assert data["keptSource"] == "process_prose"
    assert data["keptText"] == "updated progress line"
    assert data["staleGone"] is True
    assert data["staleAfterFirst"] is True
    assert data["idxs"]["keptDirect"] == 0
    assert data["idxs"]["freshDirect"] == 1
    assert data["idxs"]["footerDirect"] > data["idxs"]["freshDirect"]
    assert data["idxs"]["freshDirect"] < data["idxs"]["footerDirect"]
    assert data["idxs"]["rowKeeps"] == 0
    assert data["idxs"]["rowNew"] == 1
    assert data["idxs"]["stale"] == -1
    assert data["idxs"]["staleInVisibleRows"] == -1
    assert data["secondInsertBeforeCount"] >= 1
    assert data["secondAppendChildCount"] == 0
    assert data["stableInsertBeforeCount"] == 0
    assert data["stableAppendChildCount"] == 0
    assert data["reorderInsertBeforeCount"] >= 1
    assert data["reorderedIds"] == ["row-new", "row-kept"]
    assert data["footerRepairInsertBeforeCount"] >= 1
    assert data["footerRepairIdxs"]["newRow"] == 0
    assert data["footerRepairIdxs"]["keptRow"] == 1
    assert data["footerRepairIdxs"]["footer"] > data["footerRepairIdxs"]["keptRow"]
    assert data["hasNewRow"] is True
    assert data["newRowSession"] == "session-1"
    assert data["keylessAfterThird"] == 1
    assert data["keylessAfterFourth"] == 1
    assert data["keylessTextsAfterFourth"] == ["keyless row second"]
    assert data["totalRowsAfterFourth"] == 1
    assert data["fadeKeptExisting"] is True
    assert data["fadeExistingStillParented"] is True
    assert data["fadeCandidateDetached"] is True
    assert data["fadeOldSpanPreserved"] is True
    assert data["fadeSpanTexts"] == ["old", "new"]
    assert data["fadeText"] == "old new"
    assert data["fadeSpanPreserved"] is True
    assert data["fadeBodyActive"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_row_refresh_rebinds_controls_and_carries_tool_data():
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
class FakeElement {
  constructor(tag='div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.hidden = false;
    this.id = '';
    this.onclick = null;
    this.onkeydown = null;
    this.title = '';
    this._textContent = '';
    this._innerHTML = '';
    this._classes = new Set();
    const self = this;
    this.classList = {
      add(...names){ names.forEach(name=>self._classes.add(name)); },
      remove(...names){ names.forEach(name=>self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        const next = force === undefined ? !self._classes.has(name) : !!force;
        if(next) self._classes.add(name);
        else self._classes.delete(name);
        return next;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get firstChild(){ return this.children[0]||null; }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes = new Set(String(value).trim().split(/\s+/).filter(Boolean)); }
  get textContent(){
    if(this.children.length) return this.children.map(child=>child.textContent).join('');
    return this._textContent;
  }
  set textContent(value){
    this._textContent = String(value ?? '');
    this._innerHTML = this._textContent;
    this.children = [];
  }
  get innerHTML(){ return this._innerHTML; }
  set innerHTML(value){
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
    if(this._innerHTML.indexOf('tool-template') >= 0) buildToolTemplate(this);
  }
  setAttribute(name, value){
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){
      const dataKey = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[dataKey] = val;
    }
    if(key === 'class') this.className = val;
  }
  getAttribute(name){
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  getAttributeNames(){ return Object.keys(this.attributes); }
  removeAttribute(name){
    delete this.attributes[name];
    if(name === 'id') this.id = '';
    if(name.startsWith('data-')){
      const dataKey = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      delete this.dataset[dataKey];
    }
    if(name === 'class') this._classes = new Set();
  }
  appendChild(child){
    if(child && child.parentNode) child.remove();
    if(!child) return null;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, refNode){
    if(child && child.parentNode) child.remove();
    if(!child) return null;
    const idx = this.children.indexOf(refNode);
    child.parentNode = this;
    if(idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  remove(){
    if(!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if(idx >= 0) siblings.splice(idx, 1);
    this.parentNode = null;
  }
  matches(selector){ return matchesSelector(this, selector); }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  closest(selector){
    let node = this;
    while(node){
      if(matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}
function matchesSelector(el, selector){
  if(!selector) return false;
  const options = selector.split(',').map(part=>part.trim()).filter(Boolean);
  return options.some(part=>matchesSimple(el, part));
}
function matchesSimple(el, selector){
  selector = selector.replace(/^:scope\s*>\s*/, '').trim();
  if(!selector) return false;
  const idMatch = selector.match(/#([^\.\[#\s]+)/);
  if(idMatch && el.id !== idMatch[1]) return false;
  const clsMatches = selector.match(/\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){
    if(!el.classList.contains(cls.slice(1))) return false;
  }
  const attrMatches = selector.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/g) || [];
  for(const attrMatch of attrMatches){
    const [, name, expected] = attrMatch.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/);
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }
  return !!(idMatch || clsMatches.length || attrMatches.length);
}
function el(tag, classes){
  const node = new FakeElement(tag);
  String(classes||'').split(/\s+/).filter(Boolean).forEach(cls=>node.classList.add(cls));
  return node;
}
function buildToolTemplate(row){
  const card = el('div', 'tool-card transparent-event-card');
  const header = el('div', 'tool-card-header');
  const name = el('span', 'tool-card-name');
  name.textContent = 'shell';
  const copy = el('span', 'transparent-event-copy');
  const toggle = el('span', 'tool-card-toggle');
  const detail = el('div', 'tool-card-detail');
  detail.setAttribute('data-transparent-detail-mode', 'full');
  const tabs = el('div', 'transparent-detail-modes');
  const full = el('span', 'transparent-detail-mode active');
  full.setAttribute('data-mode', 'full');
  const output = el('span', 'transparent-detail-mode');
  output.setAttribute('data-mode', 'output');
  const args = el('div', 'tool-card-args');
  const result = el('div', 'tool-card-result');
  const pre = el('pre', '');
  pre.textContent = 'fresh output';
  result.appendChild(pre);
  tabs.appendChild(full);
  tabs.appendChild(output);
  detail.appendChild(tabs);
  detail.appendChild(args);
  detail.appendChild(result);
  header.appendChild(name);
  header.appendChild(copy);
  header.appendChild(toggle);
  card.appendChild(header);
  card.appendChild(detail);
  row.appendChild(card);
}

const copied = [];
Object.defineProperty(global, 'navigator', {
  configurable: true,
  value: { clipboard: { writeText: (text)=>{ copied.push(text); return { then(fn){ fn(); return { catch(){} }; } }; } } },
});
global.document = { createElement:(tag)=>new FakeElement(tag), body:new FakeElement('body'), execCommand:()=>true };
global.t = (key)=>key === 'copy' ? 'Copy' : key;
global.showToast = ()=>{};

eval(extractFunc('_copyEventToClipboard'));
eval(extractFunc('_attachCopyButton'));
eval(extractFunc('_setTransparentCardOpen'));
eval(extractFunc('_wireTransparentHeaderToggle'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));

const existing = new FakeElement('div');
existing.classList.add('transparent-event-row');
existing.setAttribute('data-anchor-row-id', 'row-tool');
existing.setAttribute('data-expanded', '1');
existing.innerHTML = 'tool-template-old';
existing._tcData = { name:'old_tool', args:{ stale:true }, snippet:'old output' };
const oldCard = existing.querySelector('.tool-card');
oldCard.classList.add('open');
const oldDetail = existing.querySelector('.tool-card-detail');
oldDetail.setAttribute('data-transparent-detail-mode', 'output');

const candidate = new FakeElement('div');
candidate.classList.add('transparent-event-row');
candidate.setAttribute('data-anchor-row-id', 'row-tool');
candidate.setAttribute('data-event-type', 'tool');
candidate.innerHTML = 'tool-template-new';
candidate._tcData = { name:'fresh_tool', args:{ cmd:'ls' }, snippet:'fresh output' };

const refreshed = _refreshTransparentLiveRow(existing, candidate);
const header = refreshed.querySelector('.tool-card-header');
const copy = refreshed.querySelector('.transparent-event-copy');
const card = refreshed.querySelector('.tool-card');
const detail = refreshed.querySelector('.tool-card-detail');
const outputTab = refreshed.querySelector('.transparent-detail-mode[data-mode="output"]');
const fullTab = refreshed.querySelector('.transparent-detail-mode[data-mode="full"]');
const initiallyOpenAfterRefresh = card.classList.contains('open');
const ariaExpandedAfterRefresh = header.getAttribute('aria-expanded');
const detailMode = detail.getAttribute('data-transparent-detail-mode');
const outputActive = outputTab.classList.contains('active');
const fullActive = fullTab.classList.contains('active');

copy.onclick({
  stopPropagation(){},
  preventDefault(){},
  target: copy,
});
header.onclick({
  target: header,
  preventDefault(){},
});
const afterClickOpen = card.classList.contains('open');
const afterClickExpanded = refreshed.getAttribute('data-expanded');
header.onkeydown({
  key: 'Enter',
  target: header,
  preventDefault(){},
});

process.stdout.write(JSON.stringify({
  sameNode: refreshed === existing,
  copiedText: copied[0],
  existingToolName: refreshed._tcData && refreshed._tcData.name,
  candidateHasToolData: Object.prototype.hasOwnProperty.call(candidate, '_tcData'),
  copyBound: typeof copy.onclick === 'function' && typeof copy.onkeydown === 'function',
  headerBound: typeof header.onclick === 'function' && typeof header.onkeydown === 'function',
  initiallyOpenAfterRefresh,
  ariaExpandedAfterRefresh,
  detailMode,
  outputActive,
  fullActive,
  afterClickOpen,
  afterClickExpanded,
  afterKeyOpen: card.classList.contains('open'),
  afterKeyExpanded: refreshed.getAttribute('data-expanded'),
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["sameNode"] is True
    assert data["copyBound"] is True
    assert data["headerBound"] is True
    assert data["existingToolName"] == "fresh_tool"
    assert data["candidateHasToolData"] is False
    assert "tool: fresh_tool" in data["copiedText"]
    assert '"cmd": "ls"' in data["copiedText"]
    assert "fresh output" in data["copiedText"]
    assert data["initiallyOpenAfterRefresh"] is True
    assert data["ariaExpandedAfterRefresh"] == "true"
    assert data["detailMode"] == "output"
    assert data["outputActive"] is True
    assert data["fullActive"] is False
    assert data["afterClickOpen"] is False
    assert data["afterClickExpanded"] == "0"
    assert data["afterKeyOpen"] is True
    assert data["afterKeyExpanded"] == "1"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_row_refresh_skips_unchanged_child_dom_and_carries_tool_data():
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
class FakeElement {
  constructor(tag='div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.id = '';
    this.onclick = null;
    this.onkeydown = null;
    this._textContent = '';
    this._innerHTML = '';
    this.innerHTMLSetCount = 0;
    this._classes = new Set();
    const self = this;
    this.classList = {
      add(...names){ names.forEach(name=>self._classes.add(name)); },
      remove(...names){ names.forEach(name=>self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        const next = force === undefined ? !self._classes.has(name) : !!force;
        if(next) self._classes.add(name);
        else self._classes.delete(name);
        return next;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get firstChild(){ return this.children[0]||null; }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes = new Set(String(value).trim().split(/\s+/).filter(Boolean)); }
  get textContent(){ return this.children.length ? this.children.map(child=>child.textContent).join('') : this._textContent; }
  set textContent(value){ this._textContent = String(value ?? ''); this._innerHTML = this._textContent; this.children = []; }
  get innerHTML(){ return this._innerHTML; }
  set innerHTML(value){
    this.innerHTMLSetCount += 1;
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
    if(this._innerHTML.indexOf('tool-template') >= 0) buildToolTemplate(this);
  }
  setAttribute(name, value){
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){
      const dataKey = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[dataKey] = val;
    }
    if(key === 'class') this.className = val;
  }
  getAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  removeAttribute(name){
    delete this.attributes[name];
    if(name === 'id') this.id = '';
    if(name.startsWith('data-')){
      const dataKey = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      delete this.dataset[dataKey];
    }
  }
  appendChild(child){ child.parentNode = this; this.children.push(child); return child; }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  closest(selector){
    let node = this;
    while(node){
      if(matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}
function matchesSelector(el, selector){
  return String(selector||'').split(',').map(part=>part.trim()).filter(Boolean).some(part=>matchesSimple(el, part));
}
function matchesSimple(el, selector){
  const clsMatches = selector.match(/\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){
    if(!el.classList.contains(cls.slice(1))) return false;
  }
  const attrMatches = selector.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/g) || [];
  for(const attrMatch of attrMatches){
    const [, name, expected] = attrMatch.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/);
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }
  return !!(clsMatches.length || attrMatches.length);
}
function el(tag, classes){
  const node = new FakeElement(tag);
  String(classes||'').split(/\s+/).filter(Boolean).forEach(cls=>node.classList.add(cls));
  return node;
}
function buildToolTemplate(row){
  const card = el('div', 'tool-card transparent-event-card');
  const header = el('div', 'tool-card-header');
  const copy = el('span', 'transparent-event-copy');
  const detail = el('div', 'tool-card-detail');
  detail.setAttribute('data-transparent-detail-mode', 'full');
  const full = el('span', 'transparent-detail-mode active');
  full.setAttribute('data-mode', 'full');
  const output = el('span', 'transparent-detail-mode');
  output.setAttribute('data-mode', 'output');
  detail.appendChild(full);
  detail.appendChild(output);
  header.appendChild(copy);
  card.appendChild(header);
  card.appendChild(detail);
  row.appendChild(card);
}
global.document = { createElement:(tag)=>new FakeElement(tag) };
global.t = (key)=>key === 'copy' ? 'Copy' : key;
eval(extractFunc('_setTransparentCardOpen'));
eval(extractFunc('_attachCopyButton'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));

const existing = new FakeElement('div');
existing.classList.add('transparent-event-row');
existing.setAttribute('data-anchor-row-id', 'row-tool');
existing.setAttribute('data-expanded', '1');
existing.innerHTML = 'tool-template-same';
existing.innerHTMLSetCount = 0;
existing._tcData = { name:'old_tool' };
const header = existing.querySelector('.tool-card-header');
const copy = existing.querySelector('.transparent-event-copy');
header.children = header.children.filter((child)=>child !== copy);
copy.parentNode = null;
const headerClick = function headerClick(){};
const headerKeydown = function headerKeydown(){};
const copyClick = function copyClick(){};
header.onclick = headerClick;
header.onkeydown = headerKeydown;
copy.onclick = copyClick;
const card = existing.querySelector('.tool-card');
card.classList.add('open');
const detail = existing.querySelector('.tool-card-detail');
detail.setAttribute('data-transparent-detail-mode', 'output');
const outputTab = existing.querySelector('.transparent-detail-mode[data-mode="output"]');
const fullTab = existing.querySelector('.transparent-detail-mode[data-mode="full"]');
outputTab.classList.add('active');
fullTab.classList.remove('active');

const candidate = new FakeElement('div');
candidate.classList.add('transparent-event-row');
candidate.setAttribute('data-anchor-row-id', 'row-tool');
candidate.setAttribute('data-event-type', 'tool');
candidate.innerHTML = 'tool-template-same';
candidate._tcData = { name:'fresh_tool', args:{ cmd:'pwd' }, snippet:'fresh output' };

const refreshed = _refreshTransparentLiveRow(existing, candidate);
const countAfterCarry = existing.innerHTMLSetCount;
const candidateHasToolDataAfterCarry = Object.prototype.hasOwnProperty.call(candidate, '_tcData');
const ownToolNameAfterCarry = existing._tcData && existing._tcData.name;
const repairedCopiesAfterCarry = existing.querySelectorAll('.transparent-event-copy');
const repairedCopy = repairedCopiesAfterCarry[0] || null;

const candidateWithoutData = new FakeElement('div');
candidateWithoutData.classList.add('transparent-event-row');
candidateWithoutData.setAttribute('data-anchor-row-id', 'row-tool');
candidateWithoutData.setAttribute('data-event-type', 'tool');
candidateWithoutData.innerHTML = 'tool-template-same';
_refreshTransparentLiveRow(existing, candidateWithoutData);
const repairedCopiesAfterSecondRefresh = existing.querySelectorAll('.transparent-event-copy');

process.stdout.write(JSON.stringify({
  sameNode: refreshed === existing,
  innerHTMLSetCount: existing.innerHTMLSetCount,
  countAfterCarry,
  ownToolNameAfterCarry,
  candidateHasToolDataAfterCarry,
  hasToolDataAfterClear: Object.prototype.hasOwnProperty.call(existing, '_tcData'),
  cardStillOpen: card.classList.contains('open'),
  rowExpanded: existing.getAttribute('data-expanded'),
  headerAria: header.getAttribute('aria-expanded'),
  detailMode: detail.getAttribute('data-transparent-detail-mode'),
  outputActive: outputTab.classList.contains('active'),
  fullActive: fullTab.classList.contains('active'),
  headerClickSurvived: header.onclick === headerClick,
  headerKeydownSurvived: header.onkeydown === headerKeydown,
  repairedCopyCountAfterCarry: repairedCopiesAfterCarry.length,
  repairedCopyCountAfterSecondRefresh: repairedCopiesAfterSecondRefresh.length,
  repairedCopyBound: !!(repairedCopy && typeof repairedCopy.onclick === 'function' && typeof repairedCopy.onkeydown === 'function'),
  oldMissingCopyDetached: copy.parentNode === null,
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["sameNode"] is True
    assert data["innerHTMLSetCount"] == 0
    assert data["countAfterCarry"] == 0
    assert data["ownToolNameAfterCarry"] == "fresh_tool"
    assert data["candidateHasToolDataAfterCarry"] is False
    assert data["hasToolDataAfterClear"] is False
    assert data["cardStillOpen"] is True
    assert data["rowExpanded"] == "1"
    assert data["headerAria"] == "true"
    assert data["detailMode"] == "output"
    assert data["outputActive"] is True
    assert data["fullActive"] is False
    assert data["headerClickSurvived"] is True
    assert data["headerKeydownSurvived"] is True
    assert data["repairedCopyCountAfterCarry"] == 1
    assert data["repairedCopyCountAfterSecondRefresh"] == 1
    assert data["repairedCopyBound"] is True
    assert data["oldMissingCopyDetached"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_scene_inline_fallback_skips_unchanged_child_dom():
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
class FakeElement {
  constructor(tag='div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.hidden = false;
    this.id = '';
    this.onclick = null;
    this._textContent = '';
    this._innerHTML = '';
    this.innerHTMLSetCount = 0;
    this._classes = new Set();
    const self = this;
    this.classList = {
      add(...names){ names.forEach(name=>self._classes.add(name)); },
      remove(...names){ names.forEach(name=>self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        const next = force === undefined ? !self._classes.has(name) : !!force;
        if(next) self._classes.add(name);
        else self._classes.delete(name);
        return next;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get firstChild(){ return this.children[0]||null; }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes = new Set(String(value).trim().split(/\s+/).filter(Boolean)); }
  get textContent(){ return this.children.length ? this.children.map(child=>child.textContent).join('') : this._textContent; }
  set textContent(value){ this._textContent = String(value ?? ''); this._innerHTML = this._textContent; this.children = []; }
  get innerHTML(){ return this._innerHTML; }
  set innerHTML(value){
    this.innerHTMLSetCount += 1;
    this._innerHTML = String(value ?? '');
    this._textContent = this._innerHTML;
    this.children = [];
    if(this._innerHTML.indexOf('tool-template') >= 0) buildToolTemplate(this);
  }
  setAttribute(name, value){
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){
      const dataKey = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[dataKey] = val;
    }
  }
  getAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  removeAttribute(name){ delete this.attributes[name]; }
  appendChild(child){ if(child.parentNode) child.remove(); child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, refNode){
    if(child.parentNode) child.remove();
    const idx = this.children.indexOf(refNode);
    child.parentNode = this;
    if(idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  remove(){
    if(!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if(idx >= 0) siblings.splice(idx, 1);
    this.parentNode = null;
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  closest(selector){
    let node = this;
    while(node){
      if(matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}
function matchesSelector(el, selector){
  return String(selector||'').split(',').map(part=>part.trim()).filter(Boolean).some(part=>matchesSimple(el, part));
}
function matchesSimple(el, selector){
  selector = selector.replace(/^:scope\s*>\s*/, '').trim();
  const idMatch = selector.match(/#([^\.\[#\s]+)/);
  if(idMatch && el.id !== idMatch[1]) return false;
  const clsMatches = selector.match(/\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){
    if(!el.classList.contains(cls.slice(1))) return false;
  }
  const attrMatches = selector.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/g) || [];
  for(const attrMatch of attrMatches){
    const [, name, expected] = attrMatch.match(/\[([^=\]]+)(?:=\"([^\"]*)\")?\]/);
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }
  return !!(idMatch || clsMatches.length || attrMatches.length);
}
function el(tag, classes){
  const node = new FakeElement(tag);
  String(classes||'').split(/\s+/).filter(Boolean).forEach(cls=>node.classList.add(cls));
  return node;
}
function buildToolTemplate(row){
  const card = el('div', 'tool-card transparent-event-card');
  const header = el('div', 'tool-card-header');
  const copy = el('span', 'transparent-event-copy');
  const detail = el('div', 'tool-card-detail');
  detail.setAttribute('data-transparent-detail-mode', 'full');
  header.appendChild(copy);
  card.appendChild(header);
  card.appendChild(detail);
  row.appendChild(card);
}
const emptyState = new FakeElement('div');
const msgInner = new FakeElement('div');
const messages = new FakeElement('div');
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const liveRunStatus = new FakeElement('div');
liveRunStatus.id = 'liveRunStatus';
turn.appendChild(liveRunStatus);
msgInner.appendChild(turn);
global.window = {};
global.document = { createElement:(tag)=>new FakeElement(tag) };
global.requestAnimationFrame = (fn)=>fn();
global.S = { session:{ session_id:'session-1' }, activeStreamId:'stream-1' };
global.$ = (id)=>id === 'emptyState' ? emptyState : id === 'msgInner' ? msgInner : id === 'messages' ? messages : id === 'liveAssistantTurn' ? turn : null;
global._createAssistantTurn = () => turn;
global._assistantTurnBlocks = () => turn;
global._captureMessageScrollSnapshot = () => ({ scrollHeight:1000 });
global._prepareLiveAnchorScrollRebuildGuard = () => ({ readerAwayFromBottom:false, release:null });
global._restoreMessageScrollSnapshotSameFrame = () => {};
global.scrollIfPinned = () => {};
global._moveLiveRunStatusToTurnEnd = () => {};
global._messageUserUnpinned = false;
global._anchorSceneRowsForRendering = (scene) => scene.activity_rows || [];
global._syncTransparentEventControls = () => {};
global._setTransparentCardOpen = (card, open) => {
  card.classList.toggle('open', !!open);
  const row = card.closest('.transparent-event-row');
  if(row) row.setAttribute('data-expanded', open ? '1' : '0');
  const header = card.querySelector('.tool-card-header');
  if(header) header.setAttribute('aria-expanded', open ? 'true' : 'false');
};
global._attachCopyButton = (header) => header && header.querySelector('.transparent-event-copy');
global._anchorSceneTransparentNodeForRow = (row, opts) => {
  const node = new FakeElement('div');
  node.classList.add('transparent-event-row');
  node.setAttribute('data-anchor-scene-row','1');
  node.setAttribute('data-anchor-live-scene-row','1');
  node.setAttribute('data-live-stream-owned','1');
  node.setAttribute('data-anchor-row-id', row.row_id);
  node.setAttribute('data-anchor-row-role', row.role);
  node.setAttribute('data-anchor-source-event-type', row.source_event_type);
  node.setAttribute('data-anchor-stream-id', opts.streamId);
  node.setAttribute('data-session-id', opts.sessionId);
  node.innerHTML = 'tool-template-same';
  node._tcData = { name: row.toolName };
  return node;
};
eval(extractFunc('_transparentLiveRowKey'));
eval(extractFunc('_transparentLiveRowsCompatible'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));
eval(extractFunc('_renderLiveAnchorActivitySceneTransparent'));

const firstScene = { version:'activity_scene_v1', activity_rows:[{ row_id:'row-tool', role:'tool', source_event_type:'tool_delta', toolName:'old_tool' }] };
const secondScene = { version:'activity_scene_v1', activity_rows:[{ row_id:'row-tool', role:'tool', source_event_type:'tool_delta', toolName:'fresh_tool' }] };
_renderLiveAnchorActivitySceneTransparent('stream-1', firstScene, { sessionId:'session-1' });
const row = turn.querySelector('.transparent-event-row[data-anchor-row-id="row-tool"]');
row.innerHTMLSetCount = 0;
row.setAttribute('data-expanded','1');
row.querySelector('.tool-card').classList.add('open');
const header = row.querySelector('.tool-card-header');
const copy = row.querySelector('.transparent-event-copy');
const headerClick = function headerClick(){};
const copyClick = function copyClick(){};
header.onclick = headerClick;
copy.onclick = copyClick;
_renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, { sessionId:'session-1' });

process.stdout.write(JSON.stringify({
  innerHTMLSetCount: row.innerHTMLSetCount,
  ownToolName: row._tcData && row._tcData.name,
  headerClickSurvived: header.onclick === headerClick,
  copyClickSurvived: copy.onclick === copyClick,
  cardStillOpen: row.querySelector('.tool-card').classList.contains('open'),
  rowExpanded: row.getAttribute('data-expanded'),
  headerAria: header.getAttribute('aria-expanded'),
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["innerHTMLSetCount"] == 0
    assert data["ownToolName"] == "fresh_tool"
    assert data["headerClickSurvived"] is True
    assert data["copyClickSurvived"] is True
    assert data["cardStillOpen"] is True
    assert data["rowExpanded"] == "1"
    assert data["headerAria"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_fade_helper_clears_settled_rows_without_assigning_fade():
    script = """
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){{
  const marker = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){{
    if(src[i] === '{{') depth += 1;
    else if(src[i] === '}}') depth -= 1;
    i += 1;
  }}
  return src.slice(start, i);
}}
const htmlRegistry = new Map();
class FakeElement {{
  constructor(tag='div'){{
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.style = Object.create(null);
    this.hidden = false;
    this.id = '';
    this._textContent = '';
    this._innerHTML = '';
    this._classes = new Set();
    this.onclick = null;
    this.onkeydown = null;
    this.title = '';
    const self = this;
    this.classList = {{
      add(...names){{ names.forEach(name=>self._classes.add(name)); }},
      remove(...names){{ names.forEach(name=>self._classes.delete(name)); }},
      contains(name){{ return self._classes.has(name); }},
      toggle(name, force){{
        if(force === true){{ self._classes.add(name); return true; }}
        if(force === false){{ self._classes.delete(name); return false; }}
        if(self._classes.has(name)){{ self._classes.delete(name); return false; }}
        self._classes.add(name);
        return true;
      }},
    }};
  }}
  cloneNode(deep){{
    const clone = new FakeElement(this.tagName);
    clone.className = this.className;
    clone._textContent = this._textContent;
    clone._innerHTML = this._innerHTML;
    clone.hidden = this.hidden;
    clone.id = this.id;
    clone.title = this.title;
    Object.entries(this.attributes).forEach(([name, value])=>clone.setAttribute(name, value));
    if(Object.prototype.hasOwnProperty.call(this, '_tcData')) clone._tcData = this._tcData;
    if(deep){{
      this.children.forEach(child=>clone.appendChild(child.cloneNode(true)));
    }}
    return clone;
  }}
  get parentElement(){{ return this.parentNode; }}
  get firstChild(){{ return this.children[0]||null; }}
  get className(){{
    return Array.from(this._classes).join(' ');
  }}
  set className(value){{
    this._classes = new Set(String(value).trim().split(/\\s+/).filter(Boolean));
  }}
  get textContent(){{
    if(this.children.length) return this.children.map(child=>child.textContent).join('');
    return this._textContent;
  }}
  set textContent(value){{
    this._textContent = String(value ?? '');
    this._innerHTML = this._textContent;
    this.children = [];
  }}
  get innerHTML(){{
    return this._innerHTML;
  }}
  set innerHTML(value){{
    this._innerHTML = String(value ?? '');
    this._textContent = '';
    this.children = [];
    const factory = htmlRegistry.get(this._innerHTML);
    if(factory){{
      factory().forEach(child=>this.appendChild(child.cloneNode(true)));
      return;
    }}
    this._textContent = this._innerHTML;
  }}
  setAttribute(name, value){{
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){{
      const dataKey = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[dataKey] = val;
    }}
    if(key === 'class') this.className = val;
  }}
  getAttribute(name){{
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }}
  getAttributeNames(){{
    return Object.keys(this.attributes);
  }}
  removeAttribute(name){{
    delete this.attributes[name];
    if(name === 'id') this.id = '';
    if(name.startsWith('data-')){{
      const dataKey = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      delete this.dataset[dataKey];
    }}
    if(name === 'class') this._classes = new Set();
  }}
  appendChild(child){{
    if(child && child.parentNode) child.remove();
    if(!child) return null;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }}
  insertBefore(child, refNode){{
    if(child && child.parentNode) child.remove();
    if(!child) return null;
    const idx = this.children.indexOf(refNode);
    child.parentNode = this;
    if(idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }}
  remove(){{
    if(!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if(idx >= 0) siblings.splice(idx, 1);
    this.parentNode = null;
  }}
  matches(selector){{
    return matchesSelector(this, selector);
  }}
  querySelector(selector){{
    return this.querySelectorAll(selector)[0] || null;
  }}
  querySelectorAll(selector){{
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){{
        if(matchesSelector(child, selector)) out.push(child);
        walk(child);
      }}
    };
    walk(this);
    return out;
  }}
  closest(selector){{
    let node = this;
    while(node){{
      if(matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }}
    return null;
  }}
}}
function matchesSelector(el, selector){{
  if(!selector) return false;
  const options = selector.split(',').map(part=>part.trim()).filter(Boolean);
  return options.some(part=>matchesSimple(el, part));
}}
function matchesSimple(el, selector){{
  selector = selector.replace(/^:scope\\s*>\\s*/, '').trim();
  if(!selector) return false;
  const idMatch = selector.match(/#([^.\\[#]+)/);
  if(idMatch && el.id !== idMatch[1]) return false;
  const clsMatches = selector.match(/\\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){{
    if(!el.classList.contains(cls.slice(1))) return false;
  }}
  const attrMatches = selector.match(/\\[([^=\\]]+)(?:=\\"([^\\"]*)\\")?\\]/g) || [];
  for(const attrMatch of attrMatches){{
    const [, name, expected] = attrMatch.match(/\\[([^=\\]]+)(?:=\\"([^\\"]*)\\")?\\]/);
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }}
  return !!(idMatch || clsMatches.length || attrMatches.length);
}}
function buildToolChildren(version){{
  const card = new FakeElement('div');
  card.className = 'tool-card open';
  const header = new FakeElement('div');
  header.className = 'tool-card-header';
  const name = new FakeElement('span');
  name.className = 'tool-card-name';
  name.textContent = version === 'first' ? 'Shell old' : 'Shell new';
  const toggle = new FakeElement('span');
  toggle.className = 'tool-card-toggle';
  toggle.textContent = '>';
  header.appendChild(name);
  header.appendChild(toggle);
  const detail = new FakeElement('div');
  detail.className = 'tool-card-detail';
  detail.setAttribute('data-transparent-detail-mode', 'output');
  const full = new FakeElement('span');
  full.className = 'transparent-detail-mode';
  full.setAttribute('data-mode', 'full');
  full.textContent = 'Full';
  const output = new FakeElement('span');
  output.className = 'transparent-detail-mode active';
  output.setAttribute('data-mode', 'output');
  output.textContent = 'Output';
  detail.appendChild(full);
  detail.appendChild(output);
  card.appendChild(header);
  card.appendChild(detail);
  return [card];
}}
function toolPayload(version){{
  return version === 'first'
    ? {{ name:'shell', args:{{ cmd:'echo old' }}, snippet:'old payload', done:false }}
    : {{ name:'shell', args:{{ cmd:'echo new' }}, snippet:'new payload', done:false }};
}}
function makeToolRow(version){{
  const token = version === 'first' ? '__tool_row_first__' : '__tool_row_second__';
  htmlRegistry.set(token, ()=>buildToolChildren(version));
  const row = new FakeElement('div');
  row.className = 'transparent-event-row';
  row.setAttribute('data-transparent-event-row', '1');
  row.setAttribute('data-event-type', 'tool');
  row.setAttribute('data-anchor-scene-row', '1');
  row.setAttribute('data-anchor-live-scene-row', '1');
  row.setAttribute('data-anchor-row-id', 'row-tool');
  row.setAttribute('data-anchor-row-role', 'tool');
  row.setAttribute('data-anchor-source-event-type', 'process_tool');
  row.setAttribute('data-anchor-stream-id', 'stream-1');
  row.setAttribute('data-session-id', 'session-1');
  row.setAttribute('data-live-stream-owned', '1');
  row.setAttribute('data-expanded', '1');
  row.innerHTML = token;
  row._tcData = toolPayload(version);
  const header = row.querySelector('.tool-card-header');
  const card = row.querySelector('.tool-card');
  const detail = row.querySelector('.tool-card-detail');
  _wireTransparentHeaderToggle(header);
  _attachCopyButton(header);
  _setTransparentCardOpen(card, true);
  detail.setAttribute('data-transparent-detail-mode', 'output');
  detail.querySelectorAll('.transparent-detail-mode').forEach(el=>el.classList.toggle('active', el.getAttribute('data-mode') === 'output'));
  return row;
}}

global.window = {{}};
global.document = {{
  body:new FakeElement('body'),
  createElement:(tag)=>new FakeElement(tag),
  execCommand:()=>true,
}};
global.requestAnimationFrame = (fn)=>fn();
global.CSS = {{ escape:(value)=>String(value) }};
global.t = (key)=>key;
global.showToast = () => {{}};
global.S = {{ session:{{ session_id:'session-1' }}, activeStreamId:'stream-1' }};
global._captureMessageScrollSnapshot = () => ({{ scrollHeight: 1000 }});
global._prepareLiveAnchorScrollRebuildGuard = () => ({{ readerAwayFromBottom:false, release:null }});
global._restoreMessageScrollSnapshotSameFrame = () => {{}};
global.scrollIfPinned = () => {{}};
global._moveLiveRunStatusToTurnEnd = () => {{}};
global._messageUserUnpinned = false;
global._syncTransparentEventControls = () => {{}};
global._anchorSceneRowsForRendering = (scene) => scene && scene.activity_rows || [];
const emptyState = new FakeElement('div');
const msgInner = new FakeElement('div');
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const liveRunStatus = new FakeElement('div');
liveRunStatus.id = 'liveRunStatus';
msgInner.appendChild(turn);
turn.appendChild(liveRunStatus);
global.document._findById = (id) => id === 'emptyState' ? emptyState : id === 'msgInner' ? msgInner : id === 'liveAssistantTurn' ? turn : null;
global.$ = (id)=>global.document._findById(id);
global._createAssistantTurn = () => turn;
global._assistantTurnBlocks = () => turn;
global._anchorSceneTransparentNodeForRow = () => null;

eval(extractFunc('_attachCopyButton'));
eval(extractFunc('_setTransparentCardOpen'));
eval(extractFunc('_wireTransparentHeaderToggle'));
eval(extractFunc('_transparentLiveRowKey'));
eval(extractFunc('_transparentLiveRowsCompatible'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));
eval(extractFunc('_renderLiveAnchorActivitySceneTransparent'));
global._copyEventToClipboard = (row) => {{
  const tc = row && row._tcData || {{}};
  global.__copied = JSON.stringify({{
    name: tc.name || '',
    cmd: tc.args && tc.args.cmd || '',
    snippet: tc.snippet || '',
  }});
}};

global._anchorSceneTransparentNodeForRow = (row) => makeToolRow(row.version);

(async () => {{
  const firstScene = {{
    version:'activity_scene_v1',
    activity_rows:[{{ row_id:'row-tool', role:'tool', source_event_type:'process_tool', version:'first' }}],
  }};
  const secondScene = {{
    version:'activity_scene_v1',
    activity_rows:[{{ row_id:'row-tool', role:'tool', source_event_type:'process_tool', version:'second' }}],
  }};
  _renderLiveAnchorActivitySceneTransparent('stream-1', firstScene, {{ sessionId:'session-1' }});
  const firstRow = turn.querySelector('.transparent-event-row[data-anchor-row-id="row-tool"]');
  const firstCard = firstRow.querySelector('.tool-card');
  const firstDetail = firstRow.querySelector('.tool-card-detail');
  _setTransparentCardOpen(firstCard, true);
  firstDetail.setAttribute('data-transparent-detail-mode', 'output');
  _renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, {{ sessionId:'session-1' }});
  const keptRow = turn.querySelector('.transparent-event-row[data-anchor-row-id="row-tool"]');
  const header = keptRow.querySelector('.tool-card-header');
  const copy = keptRow.querySelector('.transparent-event-copy');
  const card = keptRow.querySelector('.tool-card');
  const detail = keptRow.querySelector('.tool-card-detail');
  copy.onclick({{ stopPropagation(){{}}, preventDefault(){{}} }});
  await Promise.resolve();
  process.stdout.write(JSON.stringify({{
    sameNode: firstRow === keptRow,
    tcSnippet: keptRow._tcData && keptRow._tcData.snippet,
    tcCommand: keptRow._tcData && keptRow._tcData.args && keptRow._tcData.args.cmd,
    hasCopyHandler: !!(copy && typeof copy.onclick === 'function'),
    hasHeaderHandler: !!(header && typeof header.onclick === 'function'),
    copiedText: global.__copied || '',
    cardOpen: !!(card && card.classList.contains('open')),
    detailMode: detail && detail.getAttribute('data-transparent-detail-mode'),
    headerName: (header.querySelector('.tool-card-name') || {{ textContent:'' }}).textContent,
  }}));
}})().catch(err => {{
  console.error(err && err.stack || String(err));
  process.exit(1);
}});
"""
    script = script.replace("{{", "{").replace("}}", "}")
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["sameNode"] is True
    assert data["tcSnippet"] == "new payload"
    assert data["tcCommand"] == "echo new"
    assert data["hasCopyHandler"] is True
    assert data["hasHeaderHandler"] is True
    assert data["copiedText"] == '{"name":"shell","cmd":"echo new","snippet":"new payload"}'
    assert data["cardOpen"] is True
    assert data["detailMode"] == "output"
    assert data["headerName"] == "Shell new"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_thinking_refresh_preserves_scroll_container():
    script = """
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){
  const marker = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
class FakeElement {
  constructor(tag='div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this._textContent = '';
    this._innerHTML = '';
    this.innerHTMLSetCount = 0;
    this._classes = new Set();
    const self = this;
    this.classList = {
      add(...names){ names.forEach(name=>self._classes.add(name)); },
      remove(...names){ names.forEach(name=>self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        const enabled = force === undefined ? !self._classes.has(name) : !!force;
        if(enabled) self._classes.add(name);
        else self._classes.delete(name);
        return enabled;
      },
    };
  }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes = new Set(String(value).trim().split(/\\s+/).filter(Boolean)); }
  get textContent(){ return this.children.length ? this.children.map(child=>child.textContent).join('') : this._textContent; }
  set textContent(value){ this._textContent = String(value ?? ''); this.children = []; }
  get innerHTML(){ return this._innerHTML || this.children.map(child=>child.textContent).join(''); }
  set innerHTML(value){ this.innerHTMLSetCount += 1; this._innerHTML = String(value ?? ''); this._textContent = this._innerHTML; this.children = []; }
  setAttribute(name, value){ this.attributes[String(name)] = String(value); if(name === 'class') this.className = value; }
  getAttribute(name){ return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  getAttributeNames(){ return Object.keys(this.attributes); }
  removeAttribute(name){ delete this.attributes[name]; if(name === 'class') this._classes = new Set(); }
  appendChild(child){ if(child.parentNode) child.remove(); child.parentNode = this; this.children.push(child); return child; }
  remove(){ if(!this.parentNode) return; const idx = this.parentNode.children.indexOf(this); if(idx >= 0) this.parentNode.children.splice(idx, 1); this.parentNode = null; }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){
    const parts = String(selector).split(',').map(s=>s.trim()).filter(Boolean);
    const out = [];
    const walk = (node)=>{
      for(const child of node.children){
        if(parts.some(part=>matchesSelectorPath(child, part))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}
function matchesSelectorPath(el, selector){
  const tokens = String(selector).split(/\\s+/).filter(Boolean);
  if(!tokens.length) return false;
  if(!matchesSimple(el, tokens[tokens.length - 1])) return false;
  let ancestor = el.parentNode;
  for(let i=tokens.length - 2;i>=0;i--){
    while(ancestor && !matchesSimple(ancestor, tokens[i])) ancestor = ancestor.parentNode;
    if(!ancestor) return false;
    ancestor = ancestor.parentNode;
  }
  return true;
}
function matchesSimple(el, selector){
  const tag = String(selector).match(/^[A-Za-z][A-Za-z0-9-]*/);
  if(tag && el.tagName !== tag[0].toUpperCase()) return false;
  const classes = selector.match(/\\.([A-Za-z0-9_-]+)/g) || [];
  if(!classes.length) return !!tag;
  return classes.every(cls=>el.classList.contains(cls.slice(1)));
}
function thinkingRow(text, preview){
  const row = new FakeElement('div');
  row.className = 'agent-activity-thinking transparent-event-row transparent-thinking-event';
  row.setAttribute('data-event-type', 'thinking');
  row.setAttribute('data-anchor-row-id', 'think-1');
  row.setAttribute('data-anchor-row-role', 'thinking');
  row.setAttribute('data-anchor-source-event-type', 'reasoning');
  const card = new FakeElement('div');
  card.className = 'thinking-card open';
  const header = new FakeElement('div');
  header.className = 'thinking-card-header';
  const label = new FakeElement('span');
  label.className = 'thinking-card-label';
  label.textContent = 'Thinking';
  const previewEl = new FakeElement('span');
  previewEl.className = 'transparent-event-thinking-preview';
  previewEl.textContent = preview;
  const body = new FakeElement('div');
  body.className = 'thinking-card-body';
  body.scrollTop = 37;
  const pre = new FakeElement('pre');
  pre.textContent = text;
  header.appendChild(label);
  header.appendChild(previewEl);
  body.appendChild(pre);
  card.appendChild(header);
  card.appendChild(body);
  row.appendChild(card);
  return row;
}
global._decorateTransparentEventRow = (row, opts)=>{
  const preview = row.querySelector('.transparent-event-thinking-preview');
  if(preview) preview.textContent = opts.preview || opts.text || '';
  row.setAttribute('data-event-type', opts.type || 'thinking');
  return row;
};
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));

const existing = thinkingRow('short thought', 'short thought');
const originalBody = existing.querySelector('.thinking-card-body');
const originalPre = existing.querySelector('.thinking-card-body pre');
const candidate = thinkingRow('long thought\\n'.repeat(80), 'long thought');
const refreshed = _refreshTransparentLiveRow(existing, candidate);
const refreshedBody = refreshed.querySelector('.thinking-card-body');
const refreshedPre = refreshed.querySelector('.thinking-card-body pre');
const refreshedPreview = refreshed.querySelector('.transparent-event-thinking-preview');
process.stdout.write(JSON.stringify({
  sameRow: refreshed === existing,
  sameBody: refreshedBody === originalBody,
  samePre: refreshedPre === originalPre,
  textUpdated: refreshedPre.textContent === 'long thought\\n'.repeat(80),
  previewUpdated: refreshedPreview.textContent === 'long thought',
  scrollTopPreserved: refreshedBody.scrollTop === 37,
  innerHTMLSetCount: refreshed.innerHTMLSetCount,
  cardOpen: !!refreshed.querySelector('.thinking-card').classList.contains('open'),
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["sameRow"] is True
    assert data["sameBody"] is True
    assert data["samePre"] is True
    assert data["textUpdated"] is True
    assert data["previewUpdated"] is True
    assert data["scrollTopPreserved"] is True
    assert data["innerHTMLSetCount"] == 0
    assert data["cardOpen"] is True
