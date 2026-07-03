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
global.document = {{ createElement:(tag)=>new FakeElement(tag) }};
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
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
eval(extractFunc('_transparentLiveRowKey'));
eval(extractFunc('_transparentLiveRowsCompatible'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
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

const secondRender = _renderLiveAnchorActivitySceneTransparent('stream-1', secondScene, {{ sessionId:'session-1' }});
const keptAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-kept\"]');
const staleAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-stale\"]');
const newAfterSecond = turn.querySelector('.transparent-event-row[data-anchor-row-id=\"row-new\"]');
const rows = turn.children.filter((child) => child.classList.contains('transparent-event-row'));
const idxs = {{
  keptDirect: turn.children.indexOf(keptAfterSecond),
  freshDirect: turn.children.indexOf(newAfterSecond),
  footerDirect: turn.children.indexOf(firstFooter),
  staleInVisibleRows: rows.findIndex((child) => child.getAttribute('data-anchor-row-id') === 'row-stale'),
  rowKeeps: rows.indexOf(keptAfterSecond),
  rowNew: rows.indexOf(newAfterSecond),
  stale: rows.findIndex((child) => child.getAttribute('data-anchor-row-id') === 'row-stale'),
}};

_renderLiveAnchorActivitySceneTransparent('stream-1', thirdScene, {{ sessionId:'session-1' }});
const keylessRowsAfterThird = turn.querySelectorAll('.transparent-event-row[data-anchor-row-id=\"\"]');
_renderLiveAnchorActivitySceneTransparent('stream-1', fourthScene, {{ sessionId:'session-1' }});
const keylessRowsAfterFourth = turn.querySelectorAll('.transparent-event-row[data-anchor-row-id=\"\"]');

process.stdout.write(JSON.stringify({{
  firstRender,
  secondRender,
  sameNode: keptAfterFirst === keptAfterSecond,
  keptId: keptAfterSecond && keptAfterSecond.getAttribute('data-anchor-row-id'),
  keptSource: keptAfterSecond && keptAfterSecond.getAttribute('data-anchor-source-event-type'),
  keptText: keptAfterSecond && keptAfterSecond.textContent,
  staleGone: staleAfterSecond === null,
  staleAfterFirst: staleAfterFirst !== null,
  idxs,
  hasNewRow: !!newAfterSecond,
  newRowSession: newAfterSecond && newAfterSecond.getAttribute('data-session-id'),
  keylessAfterThird: keylessRowsAfterThird.length,
  keylessAfterFourth: keylessRowsAfterFourth.length,
  keylessTextsAfterFourth: keylessRowsAfterFourth.map((child) => child.textContent),
  totalRowsAfterFourth: turn.children.filter((child) => child.classList.contains('transparent-event-row')).length,
}}));
"""
    script = script.replace("{{", "{").replace("}}", "}")
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["firstRender"] is True
    assert data["secondRender"] is True
    assert data["sameNode"] is True
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
    assert data["hasNewRow"] is True
    assert data["newRowSession"] == "session-1"
    assert data["keylessAfterThird"] == 1
    assert data["keylessAfterFourth"] == 1
    assert data["keylessTextsAfterFourth"] == ["keyless row second"]
    assert data["totalRowsAfterFourth"] == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_live_scene_rehydrates_copy_button_and_tcdata_after_reconcile():
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
