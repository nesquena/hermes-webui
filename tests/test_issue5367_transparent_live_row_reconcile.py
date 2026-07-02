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
    this.textContent = '';
    this.innerHTML = '';
    this._classes = new Set();
    const self = this;
    this.classList = {{
      add(...names){{ names.forEach(name=>self._classes.add(name)); }},
      remove(...names){{ names.forEach(name=>self._classes.delete(name)); }},
      contains(name){{ return self._classes.has(name); }},
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
