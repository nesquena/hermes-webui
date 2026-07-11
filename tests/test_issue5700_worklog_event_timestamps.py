"""Browserless regression for Transparent Stream event timestamps."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(body):
    env = os.environ.copy()
    env["UI_JS_PATH"] = str(UI_JS)
    script = NODE_PREFIX + body
    result = subprocess.run([NODE, "-e", script], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


NODE_PREFIX = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.UI_JS_PATH, 'utf8');
function extractFunc(name){
  const marker = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(marker);
  if(start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1;
  let depth = 1;
  while(depth > 0 && i < src.length){
    const ch = src[i];
    if(ch === '{') depth += 1;
    else if(ch === '}') depth -= 1;
    i += 1;
  }
  return src.slice(start, i);
}
class FakeElement {
  constructor(tag = 'div'){
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
    this.classList = {
      add(...names){ names.forEach(name => self._classes.add(name)); },
      remove(...names){ names.forEach(name => self._classes.delete(name)); },
      contains(name){ return self._classes.has(name); },
      toggle(name, force){
        if(force === true){ self._classes.add(name); return true; }
        if(force === false){ self._classes.delete(name); return false; }
        if(self._classes.has(name)){ self._classes.delete(name); return false; }
        self._classes.add(name);
        return true;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get firstChild(){ return this.children[0] || null; }
  get nextSibling(){
    if(!this.parentNode) return null;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    return idx >= 0 ? (siblings[idx + 1] || null) : null;
  }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){
    this._classes = new Set(String(value).trim().split(/\s+/).filter(Boolean));
  }
  get textContent(){ return this._textContent; }
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
  getAttributeNames(){
    return Object.keys(this.attributes);
  }
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
    if(child && child.parentNode){
      child.remove();
    }
    if(!child) return null;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, refNode){
    if(child && child.parentNode){
      child.remove();
    }
    if(!child) return null;
    const idx = this.children.indexOf(refNode);
    child.parentNode = this;
    if(idx < 0) this.children.push(child);
    else this.children.splice(idx, 0, child);
    return child;
  }
  replaceWith(...nodes){
    if(!this.parentNode) return;
    const parent = this.parentNode;
    const siblings = parent.children;
    const idx = siblings.indexOf(this);
    if(idx < 0) return;
    siblings.splice(idx, 1);
    this.parentNode = null;
    let insertAt = idx;
    for(const node of nodes){
      if(!node) continue;
      if(node.parentNode) node.remove();
      node.parentNode = parent;
      siblings.splice(insertAt, 0, node);
      insertAt += 1;
    }
  }
  remove(){
    if(!this.parentNode) return;
    const siblings = this.parentNode.children;
    const idx = siblings.indexOf(this);
    if(idx >= 0) siblings.splice(idx, 1);
    this.parentNode = null;
  }
  matches(selector){
    return matchesSelector(this, selector);
  }
  querySelector(selector){
    return this.querySelectorAll(selector)[0] || null;
  }
  querySelectorAll(selector){
    const out = [];
    const walk = (node) => {
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
  return selector.split(',').map(part => part.trim()).filter(Boolean).some(part => matchesSimple(el, part));
}
function matchesSimple(el, selector){
  selector = selector.trim();
  if(!selector || selector.includes(' ') || selector.includes('>')) return false;
  const idMatch = selector.match(/#([A-Za-z0-9_-]+)/);
  if(idMatch && el.id !== idMatch[1]) return false;
  const clsMatches = selector.match(/\.([A-Za-z0-9_-]+)/g) || [];
  for(const cls of clsMatches){
    if(!el.classList.contains(cls.slice(1))) return false;
  }
  const attrMatches = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/g) || [];
  for(const attrMatch of attrMatches){
    const parsed = attrMatch.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
    const name = parsed[1];
    const expected = parsed[2];
    const value = el.getAttribute(name);
    if(value === null) return false;
    if(expected !== undefined && String(value) !== String(expected)) return false;
  }
  const stripped = selector.replace(/#[A-Za-z0-9_-]+/g, '').replace(/\.[A-Za-z0-9_-]+/g, '').replace(/\[[^\]]+\]/g, '').trim();
  if(stripped && stripped !== '*' && stripped.toLowerCase() !== String(el.tagName || '').toLowerCase()) return false;
  return !!(idMatch || clsMatches.length || attrMatches.length || stripped);
}
global.window = {};
global.document = {
  createElement:(tag)=>new FakeElement(tag),
  createTextNode:(text)=>{
    const node = new FakeElement('#text');
    node.textContent = text;
    return node;
  },
  createDocumentFragment:()=>new FakeElement('#fragment'),
};
global.CSS = { escape:(value)=>String(value) };
global.requestAnimationFrame = (fn)=>fn();
global._toolShortName = (name)=>String(name || '');
global._transparentToolSummary = ()=>'summary';
global._transparentEventPreview = (text)=>String(text || '');
global._wireTransparentHeaderToggle = () => {};
global._attachCopyButton = () => {};
global._attachProgressBar = () => {};
global._transparentToolStatus = () => 'Running';
global._activityNowSeconds = () => 1700000000;
global._syncToolCallGroupSummary = () => {};
global._syncTransparentEventControls = () => {};
global._moveLiveRunStatusToTurnEnd = () => {};
global._removeEmptyLiveWorklogShells = () => {};
global.scrollIfPinned = () => {};
global.isLiveAnchorActivitySceneOwner = () => false;
global._findLiveAssistantAnchorForSegment = () => null;
global._findLatestVisibleLiveAssistantByBurst = () => null;
global._findLatestVisibleLiveAssistant = () => null;
global._toolWorklogListEl = () => null;
global._activityKeyForLiveTurn = () => 'activity-key';
global._assistantTurnBlocks = () => null;
eval(extractFunc('_activityClockLabel'));
eval(extractFunc('_activityFullClockLabel'));
eval(extractFunc('_timestampSeconds'));
eval(extractFunc('_firstValidTimestampSeconds'));
eval(extractFunc('_transparentEventTimestampSeconds'));
eval(extractFunc('_syncTransparentEventTimestamp'));
eval(extractFunc('_decorateTransparentEventRow'));
eval(extractFunc('_transparentStreamOrderedParts'));
eval(extractFunc('_transparentOrderedToolCall'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));
eval(extractFunc('_anchorSceneRowTimestampSeconds'));
eval(extractFunc('_anchorSceneToolCallFromRow'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
eval(extractFunc('appendLiveToolCard'));
function makeToolRow(ts = null, live = false){
  const row = new FakeElement('div');
  row.className = 'tool-card-row';
  if(live) row.setAttribute('data-live-tid', '1');
  row._tcData = { name: 'search' };
  if(ts !== null && ts !== undefined) row._tcData.ts = ts;
  const card = new FakeElement('div');
  card.className = 'tool-card';
  const header = new FakeElement('div');
  header.className = 'tool-card-header';
  const icon = new FakeElement('span');
  icon.className = 'tool-card-icon';
  const name = new FakeElement('span');
  name.className = 'tool-card-name';
  name.textContent = 'search';
  const preview = new FakeElement('span');
  preview.className = 'tool-card-preview';
  preview.textContent = 'preview';
  const status = new FakeElement('span');
  status.className = 'transparent-event-status';
  status.textContent = 'Running';
  const toggle = new FakeElement('span');
  toggle.className = 'tool-card-toggle';
  toggle.textContent = '>';
  const detail = new FakeElement('div');
  detail.className = 'tool-card-detail';
  header.appendChild(icon);
  header.appendChild(name);
  header.appendChild(preview);
  header.appendChild(status);
  header.appendChild(toggle);
  card.appendChild(header);
  card.appendChild(detail);
  row.appendChild(card);
  return row;
}
function makeThinkingRow(live = false){
  const row = new FakeElement('div');
  row.className = 'agent-activity-thinking';
  if(live){
    row.setAttribute('data-live-thinking', '1');
    row.setAttribute('data-live-thinking-key', 'turn');
  }
  const card = new FakeElement('div');
  card.className = 'thinking-card';
  const header = new FakeElement('div');
  header.className = 'thinking-card-header';
  const icon = new FakeElement('span');
  icon.className = 'thinking-card-icon';
  const label = new FakeElement('span');
  label.className = 'thinking-card-label';
  label.textContent = 'Thinking';
  const preview = new FakeElement('span');
  preview.className = 'transparent-event-preview transparent-event-thinking-preview';
  preview.textContent = 'quiet trace';
  const btnRow = new FakeElement('span');
  btnRow.className = 'thinking-card-btn-row';
  const copy = new FakeElement('span');
  copy.className = 'transparent-event-copy';
  const toggle = new FakeElement('span');
  toggle.className = 'thinking-card-toggle';
  btnRow.appendChild(copy);
  btnRow.appendChild(toggle);
  header.appendChild(icon);
  header.appendChild(label);
  header.appendChild(preview);
  header.appendChild(btnRow);
  card.appendChild(header);
  row.appendChild(card);
  return row;
}
"""


def test_settled_tool_row_with_timestamp_sets_one_timestamp_node_and_updates_in_place():
    data = _run_node(
        """
const ts1 = 1700000000;
const ts2 = 1700000300;
const row = makeToolRow(null, false);
_decorateTransparentEventRow(row, { type: 'tool', toolCall: row._tcData, ts: ts1, status: 'Running' });
const header = row.querySelector('.tool-card-header');
const timeBefore = header.querySelector('.transparent-event-time');
_decorateTransparentEventRow(row, { type: 'tool', toolCall: row._tcData, ts: ts2, status: 'Completed' });
const timeAfter = header.querySelector('.transparent-event-time');
const statusNode = header.querySelector('.transparent-event-status');
const toggleNode = header.querySelector('.tool-card-toggle');
process.stdout.write(JSON.stringify({
  count: header.querySelectorAll('.transparent-event-time').length,
  timeBeforeSameAsAfter: timeBefore === timeAfter,
  timeText: timeAfter && timeAfter.textContent,
  rowEventAt: row.getAttribute('data-event-at'),
  timeEventAt: timeAfter && timeAfter.getAttribute('data-event-at'),
  timeBeforeIndex: header.children.indexOf(timeAfter),
  statusIndex: header.children.indexOf(statusNode),
  toggleIndex: header.children.indexOf(toggleNode),
}));
"""
    )

    assert data["count"] == 1
    assert data["timeBeforeSameAsAfter"] is True
    assert data["timeText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000300)}));")["_clock"]
    assert data["rowEventAt"] == "1700000300"
    assert data["timeEventAt"] == "1700000300"
    assert data["timeBeforeIndex"] < data["statusIndex"] < data["toggleIndex"]


def test_live_anchor_scene_thinking_row_uses_live_timestamp_before_stream_owner_flag():
    data = _run_node(
        """
const liveStamp = 1700000777;
global._activityNowSeconds = () => liveStamp;
global._thinkingActivityNode = (text) => makeThinkingRow(false);
const node = _anchorSceneTransparentNodeForRow({ role: 'thinking', row_id: 'live-think', text: 'quiet trace' }, {
  live: true,
  settled: false,
  streamId: 'stream-1',
  sessionId: 'session-1',
});
const header = node && node.querySelector('.thinking-card-header');
const timeNode = header && header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  hasNode: !!node,
  timeText: timeNode && timeNode.textContent,
  rowEventAt: node && node.getAttribute('data-event-at'),
  liveOwned: node && node.getAttribute('data-live-stream-owned'),
}));
"""
    )

    assert data["hasNode"] is True
    assert data["timeText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000777)}));")["_clock"]
    assert data["rowEventAt"] == "1700000777"
    assert data["liveOwned"] == "1"


def test_anchor_scene_row_timestamp_seconds_accepts_seconds_milliseconds_and_iso_strings():
    data = _run_node(
        """
const iso = '2026-06-11T00:00:01Z';
const isoStamp = Date.parse(iso) / 1000;
process.stdout.write(JSON.stringify({
  seconds: _anchorSceneRowTimestampSeconds({ created_at: 1700000901 }),
  millis: _anchorSceneRowTimestampSeconds({ created_at: 1700000901000 }),
  iso: _anchorSceneRowTimestampSeconds({ created_at: iso }),
  invalid: _anchorSceneRowTimestampSeconds({ created_at: 'not-a-date' }),
  zero: _anchorSceneRowTimestampSeconds({ created_at: 0 }),
  negative: _anchorSceneRowTimestampSeconds({ created_at: -1 }),
  nil: _anchorSceneRowTimestampSeconds({}),
  isoStamp,
}));
"""
    )

    assert data["seconds"] == 1700000901
    assert data["millis"] == 1700000901
    assert data["iso"] == data["isoStamp"]
    assert data["invalid"] is None
    assert data["zero"] is None
    assert data["negative"] is None
    assert data["nil"] is None


def test_timestamp_seconds_rejects_out_of_range_epochs_so_invalid_date_never_renders():
    # A garbage huge numeric timestamp (e.g. 1e20) passes the finite/>0 checks but
    # is far past JavaScript's max representable Date (±8.64e15 ms), so without a
    # range guard new Date(stamp*1000) yields "Invalid Date" and would render
    # literally in the worklog time label. (#5739 gate finding.)
    data = _run_node(
        """
process.stdout.write(JSON.stringify({
  huge: _timestampSeconds(1e20),
  hugeMs: _timestampSeconds(1e20 * 1000),
  normal: _timestampSeconds(1700000901),
  normalMs: _timestampSeconds(1700000901000),
  hugeLabel: _activityClockLabel(_timestampSeconds(1e20) || undefined),
  normalLabel: _activityClockLabel(_timestampSeconds(1700000901)),
}));
"""
    )

    # garbage huge epochs (whether given as seconds or ms) resolve to null
    assert data["huge"] is None
    assert data["hugeMs"] is None
    # a normal recent epoch still resolves, in both seconds and ms forms
    assert data["normal"] == 1700000901
    assert data["normalMs"] == 1700000901
    # the clock label for a rejected timestamp must never be the literal "Invalid Date";
    # a valid one produces a real time string.
    assert "Invalid Date" not in (data["hugeLabel"] or "")
    assert "Invalid Date" not in (data["normalLabel"] or "")
    assert data["normalLabel"]


def test_full_clock_label_tooltip_includes_date_and_rejects_bad_epochs():
    # The worklog time label carries a full date+time tooltip (title attr) so a
    # settled session reviewed days later (or a run crossing midnight) is not
    # date-ambiguous. Bad epochs must yield '' (never 'Invalid Date'). (#5739 Fable.)
    data = _run_node(
        """
process.stdout.write(JSON.stringify({
  normal: _activityFullClockLabel(1700000901),
  huge: _activityFullClockLabel(1e20),
  zero: _activityFullClockLabel(0),
  negative: _activityFullClockLabel(-1),
}));
"""
    )

    # a normal epoch yields a non-empty full label with the year present, no "Invalid Date"
    assert data["normal"]
    assert "Invalid Date" not in data["normal"]
    assert "2023" in data["normal"] or "2024" in data["normal"] or "2025" in data["normal"] or "2026" in data["normal"]
    # out-of-range / non-positive epochs yield empty string (no tooltip)
    assert data["huge"] == ""
    assert data["zero"] == ""
    assert data["negative"] == ""


def test_settled_anchor_scene_rows_use_row_created_at_timestamps():
    data = _run_node(
        """
const toolTs = 1700000901;
const thinkTs = 1700000902;
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  const tid = tc && (tc.tid || tc.id || tc.tool_call_id || tc.tool_use_id || tc.call_id || '');
  if(tid) row.setAttribute('data-live-tid', tid);
  return row;
};
global._thinkingActivityNode = (text) => makeThinkingRow(false);
const toolNode = _anchorSceneTransparentNodeForRow({
  role: 'tool',
  row_id: 'scene-tool',
  created_at: toolTs,
  tool: { name: 'lookup' },
}, { settled: true, finalAnswer: '' });
const toolHeader = toolNode && toolNode.querySelector('.tool-card-header');
const toolTime = toolHeader && toolHeader.querySelector('.transparent-event-time');
const thinkNode = _anchorSceneTransparentNodeForRow({
  role: 'thinking',
  row_id: 'scene-think',
  created_at: thinkTs,
  text: 'quiet trace',
}, { settled: true, finalAnswer: '' });
const thinkHeader = thinkNode && thinkNode.querySelector('.thinking-card-header');
const thinkTime = thinkHeader && thinkHeader.querySelector('.transparent-event-time');
const missingNode = _anchorSceneTransparentNodeForRow({
  role: 'thinking',
  row_id: 'scene-missing',
  text: 'quiet trace',
}, { settled: true, finalAnswer: '' });
const missingHeader = missingNode && missingNode.querySelector('.thinking-card-header');
process.stdout.write(JSON.stringify({
  toolCount: toolHeader && toolHeader.querySelectorAll('.transparent-event-time').length,
  toolText: toolTime && toolTime.textContent,
  toolEventAt: toolNode && toolNode.getAttribute('data-event-at'),
  thinkCount: thinkHeader && thinkHeader.querySelectorAll('.transparent-event-time').length,
  thinkText: thinkTime && thinkTime.textContent,
  thinkEventAt: thinkNode && thinkNode.getAttribute('data-event-at'),
  missingCount: missingHeader && missingHeader.querySelectorAll('.transparent-event-time').length,
  missingEventAt: missingNode && missingNode.getAttribute('data-event-at'),
}));
"""
    )

    assert data["toolCount"] == 1
    assert data["toolText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000901)}));")["_clock"]
    assert data["toolEventAt"] == "1700000901"
    assert data["thinkCount"] == 1
    assert data["thinkText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000902)}));")["_clock"]
    assert data["thinkEventAt"] == "1700000902"
    assert data["missingCount"] == 0
    assert data["missingEventAt"] is None


def test_settled_anchor_scene_rows_use_iso_created_at_timestamps():
    data = _run_node(
        """
const toolIso = '2026-06-11T00:00:01Z';
const thinkIso = '2026-06-11T00:00:02Z';
const toolStamp = Date.parse(toolIso) / 1000;
const thinkStamp = Date.parse(thinkIso) / 1000;
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  const tid = tc && (tc.tid || tc.id || tc.tool_call_id || tc.tool_use_id || tc.call_id || '');
  if(tid) row.setAttribute('data-live-tid', tid);
  return row;
};
global._thinkingActivityNode = (text) => makeThinkingRow(false);
const toolNode = _anchorSceneTransparentNodeForRow({
  role: 'tool',
  row_id: 'scene-tool',
  created_at: toolIso,
  tool: { name: 'lookup' },
}, { settled: true, finalAnswer: '' });
const toolHeader = toolNode && toolNode.querySelector('.tool-card-header');
const toolTime = toolHeader && toolHeader.querySelector('.transparent-event-time');
const thinkNode = _anchorSceneTransparentNodeForRow({
  role: 'thinking',
  row_id: 'scene-think',
  created_at: thinkIso,
  text: 'quiet trace',
}, { settled: true, finalAnswer: '' });
const thinkHeader = thinkNode && thinkNode.querySelector('.thinking-card-header');
const thinkTime = thinkHeader && thinkHeader.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  toolCount: toolHeader && toolHeader.querySelectorAll('.transparent-event-time').length,
  toolText: toolTime && toolTime.textContent,
  toolEventAt: toolNode && toolNode.getAttribute('data-event-at'),
  toolStamp,
  thinkCount: thinkHeader && thinkHeader.querySelectorAll('.transparent-event-time').length,
  thinkText: thinkTime && thinkTime.textContent,
  thinkEventAt: thinkNode && thinkNode.getAttribute('data-event-at'),
  thinkStamp,
}));
"""
    )

    assert data["toolCount"] == 1
    assert data["toolText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(Date.parse('2026-06-11T00:00:01Z')/1000)}));")["_clock"]
    assert data["toolEventAt"] == str(int(data["toolStamp"]))
    assert data["thinkCount"] == 1
    assert data["thinkText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(Date.parse('2026-06-11T00:00:02Z')/1000)}));")["_clock"]
    assert data["thinkEventAt"] == str(int(data["thinkStamp"]))


def test_settled_thinking_row_without_timestamp_omits_timestamp_node():
    data = _run_node(
        """
const row = makeThinkingRow();
_decorateTransparentEventRow(row, { type: 'thinking', text: 'quiet trace', preview: 'quiet trace' });
const header = row.querySelector('.thinking-card-header');
process.stdout.write(JSON.stringify({
  count: header.querySelectorAll('.transparent-event-time').length,
  rowEventAt: row.getAttribute('data-event-at'),
  hasPreview: !!header.querySelector('.transparent-event-thinking-preview'),
}));
"""
    )

    assert data["count"] == 0
    assert data["rowEventAt"] is None
    assert data["hasPreview"] is True


def test_settled_thinking_row_with_timestamp_sets_one_timestamp_node():
    data = _run_node(
        """
const ts = 1700000066;
const row = makeThinkingRow();
_decorateTransparentEventRow(row, { type: 'thinking', text: 'quiet trace', preview: 'quiet trace', ts });
const header = row.querySelector('.thinking-card-header');
const timeNode = header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  count: header.querySelectorAll('.transparent-event-time').length,
  timeText: timeNode && timeNode.textContent,
  rowEventAt: row.getAttribute('data-event-at'),
  timeEventAt: timeNode && timeNode.getAttribute('data-event-at'),
}));
"""
    )

    assert data["count"] == 1
    assert data["timeText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000066)}));")["_clock"]
    assert data["rowEventAt"] == "1700000066"
    assert data["timeEventAt"] == "1700000066"


def test_live_tool_row_without_timestamp_uses_observed_live_time():
    data = _run_node(
        """
const liveStamp = 1700000123;
global._activityNowSeconds = () => liveStamp;
const row = makeToolRow(null, true);
_decorateTransparentEventRow(row, { type: 'tool', toolCall: row._tcData, status: 'Running', live: true });
const header = row.querySelector('.tool-card-header');
const timeBefore = header.querySelector('.transparent-event-time');
const expected = _activityClockLabel(liveStamp);
global._activityNowSeconds = () => 1700000999;
_decorateTransparentEventRow(row, { type: 'tool', toolCall: row._tcData, status: 'Running', live: true });
const timeAfter = header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  count: header.querySelectorAll('.transparent-event-time').length,
  timeSameNode: timeBefore === timeAfter,
  timeText: timeAfter && timeAfter.textContent,
  expected,
  rowEventAt: row.getAttribute('data-event-at'),
  timeEventAt: timeAfter && timeAfter.getAttribute('data-event-at'),
}));
"""
    )

    assert data["count"] == 1
    assert data["timeSameNode"] is True
    assert data["timeText"] == data["expected"]
    assert data["rowEventAt"] == "1700000123"
    assert data["timeEventAt"] == "1700000123"


def test_live_tool_row_replacement_preserves_original_fallback_timestamp():
    data = _run_node(
        """
const firstLiveStamp = 1700000123;
const replacementLiveStamp = 1700000999;
global._activityNowSeconds = () => firstLiveStamp;
global.isTransparentStream = () => true;
global.S = { session: { session_id: 'session-1' }, activeStreamId: 'stream-1' };
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  const tid = tc && (tc.tid || tc.id || tc.tool_call_id || tc.tool_use_id || tc.call_id || '');
  if(tid) row.setAttribute('data-live-tid', tid);
  return row;
};
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const inner = new FakeElement('div');
turn.appendChild(inner);
global.$ = (id) => id === 'liveAssistantTurn' ? turn : null;
global._assistantTurnBlocks = () => inner;
global._createAssistantTurn = () => turn;
global._syncTransparentEventControls = () => {};
global._toolWorklogListEl = () => null;
appendLiveToolCard({ name: 'lookup', tid: 'tool-1', done: false });
const initialRow = inner.querySelector('.transparent-event-row[data-live-tid="tool-1"]');
const initialTime = initialRow && initialRow.querySelector('.transparent-event-time');
global._activityNowSeconds = () => replacementLiveStamp;
appendLiveToolCard({ name: 'lookup', tid: 'tool-1', done: true });
const replacementRow = inner.querySelector('.transparent-event-row[data-live-tid="tool-1"]');
const replacementTime = replacementRow && replacementRow.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  rowCount: inner.querySelectorAll('.transparent-event-row').length,
  initialEventAt: initialRow && initialRow.getAttribute('data-event-at'),
  replacementEventAt: replacementRow && replacementRow.getAttribute('data-event-at'),
  initialTimeText: initialTime && initialTime.textContent,
  replacementTimeText: replacementTime && replacementTime.textContent,
  sameText: initialTime && replacementTime && initialTime.textContent === replacementTime.textContent,
}));
"""
    )

    expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000123)}));")["_clock"]
    assert data["rowCount"] == 1
    assert data["initialEventAt"] == "1700000123"
    assert data["replacementEventAt"] == "1700000123"
    assert data["initialTimeText"] == expected
    assert data["replacementTimeText"] == expected
    assert data["sameText"] is True


def test_ordered_tool_use_row_without_s_toolcalls_uses_assistant_timestamp():
    data = _run_node(
        """
const messageStamp = 1700000801;
const partStamp = 1700000802;
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  return row;
};
global._toolArgsSnapshot = (args) => args;
global._cliPatchSnippetFromArgs = () => '';
global._cliToolCardSnippet = () => '';
global._cliToolCardHasDiffSnippet = () => false;
const ordered = _transparentStreamOrderedParts({
  role: 'assistant',
  content: [
    { type: 'text', text: 'working' },
    { type: 'tool_use', id: 'tool-a', name: 'lookup', input: { q: 'x' } },
    { type: 'tool_use', id: 'tool-b', name: 'lookup', input: { q: 'y' }, ts: partStamp },
    { type: 'tool_use', id: 'tool-c', name: 'lookup', input: { q: 'z' } },
  ],
});
const normalizedWithMessageTs = ordered.find(part => part.toolUseId === 'tool-a');
const normalizedWithPartTs = ordered.find(part => part.toolUseId === 'tool-b');
const normalizedWithoutTs = ordered.find(part => part.toolUseId === 'tool-c');
const fromMessage = _transparentOrderedToolCall(normalizedWithMessageTs, 7, new Map(), {}, {}, messageStamp);
const messageRow = _decorateTransparentEventRow(buildToolCard(fromMessage), {
  type: 'tool',
  toolCall: fromMessage,
  status: 'Completed',
});
const messageHeader = messageRow.querySelector('.tool-card-header');
const messageTime = messageHeader && messageHeader.querySelector('.transparent-event-time');
const fromPart = _transparentOrderedToolCall(normalizedWithPartTs, 8, new Map(), {}, {}, messageStamp);
const partRow = _decorateTransparentEventRow(buildToolCard(fromPart), {
  type: 'tool',
  toolCall: fromPart,
  status: 'Completed',
});
const partHeader = partRow.querySelector('.tool-card-header');
const partTime = partHeader && partHeader.querySelector('.transparent-event-time');
const omitted = _transparentOrderedToolCall(normalizedWithoutTs, 9, new Map(), {}, {}, null);
const omittedRow = _decorateTransparentEventRow(buildToolCard(omitted), {
  type: 'tool',
  toolCall: omitted,
  status: 'Completed',
});
const omittedHeader = omittedRow.querySelector('.tool-card-header');
process.stdout.write(JSON.stringify({
  messageCount: messageHeader && messageHeader.querySelectorAll('.transparent-event-time').length,
  messageEventAt: messageRow && messageRow.getAttribute('data-event-at'),
  messageText: messageTime && messageTime.textContent,
  partCount: partHeader && partHeader.querySelectorAll('.transparent-event-time').length,
  partEventAt: partRow && partRow.getAttribute('data-event-at'),
  partText: partTime && partTime.textContent,
  omittedCount: omittedHeader && omittedHeader.querySelectorAll('.transparent-event-time').length,
  omittedEventAt: omittedRow && omittedRow.getAttribute('data-event-at'),
}));
"""
    )

    message_expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000801)}));")["_clock"]
    part_expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000802)}));")["_clock"]
    assert data["messageCount"] == 1
    assert data["messageEventAt"] == "1700000801"
    assert data["messageText"] == message_expected
    assert data["partCount"] == 1
    assert data["partEventAt"] == "1700000802"
    assert data["partText"] == part_expected
    assert data["omittedCount"] == 0
    assert data["omittedEventAt"] is None


def test_ordered_tool_use_row_prefers_later_valid_timestamp_field():
    data = _run_node(
        """
const ordered = _transparentStreamOrderedParts({
  role: 'assistant',
  content: [
    { type: 'text', text: 'working' },
    { type: 'tool_use', id: 'tool-a', name: 'lookup', input: { q: 'x' }, ts: 'bad', timestamp: 1700000802 },
    { type: 'tool_use', id: 'tool-b', name: 'lookup', input: { q: 'y' }, ts: 'bad', timestamp: 'still-bad', created_at: 1700000803 },
  ],
});
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  return row;
};
global._toolArgsSnapshot = (args) => args;
global._cliPatchSnippetFromArgs = () => '';
global._cliToolCardSnippet = () => '';
global._cliToolCardHasDiffSnippet = () => false;
const partA = ordered.find(part => part.toolUseId === 'tool-a');
const partB = ordered.find(part => part.toolUseId === 'tool-b');
const callA = _transparentOrderedToolCall(partA, 7, new Map(), {}, {}, 1700000801);
const callB = _transparentOrderedToolCall(partB, 8, new Map(), {}, {}, 1700000801);
const rowA = _decorateTransparentEventRow(buildToolCard(callA), { type: 'tool', toolCall: callA, status: 'Completed' });
const rowB = _decorateTransparentEventRow(buildToolCard(callB), { type: 'tool', toolCall: callB, status: 'Completed' });
const headerA = rowA.querySelector('.tool-card-header');
const headerB = rowB.querySelector('.tool-card-header');
const timeA = headerA && headerA.querySelector('.transparent-event-time');
const timeB = headerB && headerB.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  eventAtA: rowA.getAttribute('data-event-at'),
  timeTextA: timeA && timeA.textContent,
  eventAtB: rowB.getAttribute('data-event-at'),
  timeTextB: timeB && timeB.textContent,
  countA: headerA && headerA.querySelectorAll('.transparent-event-time').length,
  countB: headerB && headerB.querySelectorAll('.transparent-event-time').length,
}));
"""
    )

    assert data["eventAtA"] == "1700000802"
    assert data["timeTextA"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000802)}));")["_clock"]
    assert data["eventAtB"] == "1700000803"
    assert data["timeTextB"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000803)}));")["_clock"]
    assert data["countA"] == 1
    assert data["countB"] == 1


def test_settled_anchor_scene_tool_row_prefers_valid_payload_timestamp_over_invalid_tool_timestamp():
    data = _run_node(
        """
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  return row;
};
const row = {
  role: 'tool',
  row_id: 'anchor-tool',
  tool: { name: 'lookup', ts: 'bad' },
  payload: { name: 'lookup', timestamp: 1700000904, created_at: 1700000904 },
};
const toolCall = _anchorSceneToolCallFromRow(row, { settled: true });
const node = _anchorSceneTransparentNodeForRow(row, { settled: true, finalAnswer: '' });
const header = node && node.querySelector('.tool-card-header');
const timeNode = header && header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  toolTs: toolCall.ts,
  toolTimestamp: toolCall.timestamp,
  toolCreatedAt: toolCall.created_at,
  eventAt: node && node.getAttribute('data-event-at'),
  timeText: timeNode && timeNode.textContent,
  count: header && header.querySelectorAll('.transparent-event-time').length,
}));
"""
    )

    expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000904)}));")["_clock"]
    assert data["toolTs"] == 1700000904
    assert data["toolTimestamp"] == 1700000904
    assert data["toolCreatedAt"] == 1700000904
    assert data["eventAt"] == "1700000904"
    assert data["timeText"] == expected
    assert data["count"] == 1


def test_live_anchor_scene_refresh_preserves_existing_fallback_timestamp():
    data = _run_node(
        """
const firstLiveStamp = 1700000100;
const refreshLiveStamp = 1700000999;
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  return row;
};
global._activityNowSeconds = () => firstLiveStamp;
const existing = _anchorSceneTransparentNodeForRow({
  role: 'tool',
  row_id: 'scene-live-tool',
  tool: { name: 'lookup' },
}, { live: true, settled: false, streamId: 'stream-1', sessionId: 'session-1' });
const existingHeader = existing.querySelector('.tool-card-header');
const before = existingHeader && existingHeader.querySelector('.transparent-event-time');
global._activityNowSeconds = () => refreshLiveStamp;
const candidate = _anchorSceneTransparentNodeForRow({
  role: 'tool',
  row_id: 'scene-live-tool',
  tool: { name: 'lookup' },
}, { live: true, settled: false, streamId: 'stream-1', sessionId: 'session-1' });
const refreshed = _refreshTransparentLiveRow(existing, candidate, {
  preserveEventAt: existing.getAttribute('data-event-at'),
});
const refreshedHeader = refreshed.querySelector('.tool-card-header');
const after = refreshedHeader && refreshedHeader.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  beforeText: before && before.textContent,
  afterText: after && after.textContent,
  beforeEventAt: existing.getAttribute('data-event-at'),
  afterEventAt: refreshed.getAttribute('data-event-at'),
  count: refreshedHeader && refreshedHeader.querySelectorAll('.transparent-event-time').length,
}));
"""
    )

    expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000100)}));")["_clock"]
    assert data["beforeText"] == expected
    assert data["afterText"] == expected
    assert data["beforeEventAt"] == "1700000100"
    assert data["afterEventAt"] == "1700000100"
    assert data["count"] == 1


def test_live_anchor_scene_thinking_refresh_preserves_existing_fallback_timestamp():
    data = _run_node(
        """
const firstLiveStamp = 1700000111;
const refreshLiveStamp = 1700000998;
global._activityNowSeconds = () => firstLiveStamp;
global._thinkingActivityNode = (text) => makeThinkingRow(true);
const existing = _anchorSceneTransparentNodeForRow({
  role: 'thinking',
  row_id: 'scene-live-thinking',
  text: 'quiet trace',
}, { live: true, settled: false, streamId: 'stream-1', sessionId: 'session-1' });
const existingHeader = existing.querySelector('.thinking-card-header');
const before = existingHeader && existingHeader.querySelector('.transparent-event-time');
global._activityNowSeconds = () => refreshLiveStamp;
const candidate = _anchorSceneTransparentNodeForRow({
  role: 'thinking',
  row_id: 'scene-live-thinking',
  text: 'quiet trace',
}, { live: true, settled: false, streamId: 'stream-1', sessionId: 'session-1' });
const refreshed = _refreshTransparentLiveRow(existing, candidate, {
  preserveEventAt: existing.getAttribute('data-event-at'),
});
const refreshedHeader = refreshed.querySelector('.thinking-card-header');
const after = refreshedHeader && refreshedHeader.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  beforeText: before && before.textContent,
  afterText: after && after.textContent,
  beforeEventAt: existing.getAttribute('data-event-at'),
  afterEventAt: refreshed.getAttribute('data-event-at'),
  count: refreshedHeader && refreshedHeader.querySelectorAll('.transparent-event-time').length,
}));
"""
    )

    expected = _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000111)}));")["_clock"]
    assert data["beforeText"] == expected
    assert data["afterText"] == expected
    assert data["beforeEventAt"] == "1700000111"
    assert data["afterEventAt"] == "1700000111"
    assert data["count"] == 1


def test_live_tool_row_prefers_later_valid_timestamp_field():
    data = _run_node(
        """
const liveStamp = 1700000999;
const liveTool = { name: 'lookup', tid: 'tool-live', ts: 'bad', timestamp: 1700000124, created_at: 'also-bad' };
global.buildToolCard = (tc) => {
  const row = makeToolRow(null, false);
  row._tcData = tc;
  return row;
};
global._toolArgsSnapshot = (args) => args;
global._cliPatchSnippetFromArgs = () => '';
global._cliToolCardSnippet = () => '';
global._cliToolCardHasDiffSnippet = () => false;
const call = _transparentOrderedToolCall(
  { kind: 'tool', toolUseId: 'tool-live', name: 'lookup', input: { q: 'x' } },
  11,
  new Map([['tool-live', liveTool]]),
  {},
  {},
  1700000100,
);
const row = _decorateTransparentEventRow(buildToolCard(call), {
  type: 'tool',
  toolCall: call,
  status: 'Running',
});
const header = row.querySelector('.tool-card-header');
const timeNode = header && header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  eventAt: row.getAttribute('data-event-at'),
  timeText: timeNode && timeNode.textContent,
  count: header && header.querySelectorAll('.transparent-event-time').length,
  liveStamp,
}));
"""
    )

    assert data["eventAt"] == "1700000124"
    assert data["timeText"] == _run_node("process.stdout.write(JSON.stringify({_clock:_activityClockLabel(1700000124)}));")["_clock"]
    assert data["count"] == 1


def test_live_thinking_row_without_timestamp_uses_observed_live_time():
    data = _run_node(
        """
const liveStamp = 1700000456;
global._activityNowSeconds = () => liveStamp;
const row = makeThinkingRow(true);
_decorateTransparentEventRow(row, { type: 'thinking', text: 'quiet trace', preview: 'quiet trace', live: true });
const header = row.querySelector('.thinking-card-header');
const timeBefore = header.querySelector('.transparent-event-time');
const expected = _activityClockLabel(liveStamp);
global._activityNowSeconds = () => 1700000999;
_decorateTransparentEventRow(row, { type: 'thinking', text: 'quiet trace', preview: 'quiet trace', live: true });
const timeAfter = header.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  count: header.querySelectorAll('.transparent-event-time').length,
  timeSameNode: timeBefore === timeAfter,
  timeText: timeAfter && timeAfter.textContent,
  expected,
  rowEventAt: row.getAttribute('data-event-at'),
  timeEventAt: timeAfter && timeAfter.getAttribute('data-event-at'),
}));
"""
    )

    assert data["count"] == 1
    assert data["timeSameNode"] is True
    assert data["timeText"] == data["expected"]
    assert data["rowEventAt"] == "1700000456"
    assert data["timeEventAt"] == "1700000456"


def test_live_thinking_row_existing_update_promotes_late_timestamp():
    data = _run_node(
        """
eval(extractFunc('appendThinking'));
global.isSimplifiedToolCalling = () => true;
global.isTransparentStream = () => true;
global.S = { session: { session_id: 'session-1' }, activeStreamId: 'stream-1' };
global._showThinking = true;
global._sanitizeThinkingDisplayText = (text) => String(text || '');
global._thinkingActivityNode = () => makeThinkingRow(true);
global._renderThinkingInto = () => {};
global._syncTransparentEventControls = () => {};
global._moveLiveRunStatusToTurnEnd = () => {};
global._removeEmptyLiveWorklogShells = () => {};
global._toolWorklogListEl = () => null;
global.ensureLiveWorklogContainer = () => null;
global._syncToolCallGroupSummary = () => {};
global._findLiveAssistantAnchorForSegment = () => null;
global._findLatestVisibleLiveAssistantByBurst = () => null;
global._findLatestVisibleLiveAssistant = () => null;
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const blocks = new FakeElement('div');
turn.appendChild(blocks);
global.$ = (id) => id === 'liveAssistantTurn' ? turn : null;
global._assistantTurnBlocks = () => blocks;
global._activityNowSeconds = () => 1700000111;
appendThinking('quiet trace', { thinkingKey: 'thinking-key', live: true });
const row = blocks.querySelector('.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="thinking-key"]');
const before = row && row.querySelector('.transparent-event-time');
const beforeEventAt = row && row.getAttribute('data-event-at');
const beforeText = before && before.textContent;
global._activityNowSeconds = () => 1700000998;
appendThinking('quiet trace', { thinkingKey: 'thinking-key', live: true, ts: 1700000998 });
const afterRow = blocks.querySelector('.agent-activity-thinking[data-live-thinking="1"][data-live-thinking-key="thinking-key"]');
const after = afterRow && afterRow.querySelector('.transparent-event-time');
process.stdout.write(JSON.stringify({
  beforeEventAt,
  afterEventAt: afterRow && afterRow.getAttribute('data-event-at'),
  beforeText,
  afterText: after && after.textContent,
  count: afterRow && afterRow.querySelectorAll('.transparent-event-time').length,
}));
"""
    )

    assert data["beforeEventAt"] == "1700000111"
    assert data["afterEventAt"] == "1700000998"
    assert data["beforeText"] == _run_node("process.stdout.write(JSON.stringify({clock:_activityClockLabel(1700000111)}));")["clock"]
    assert data["afterText"] == _run_node("process.stdout.write(JSON.stringify({clock:_activityClockLabel(1700000998)}));")["clock"]
    assert data["count"] == 1


def test_compact_worklog_path_stays_off_transparent_decorator():
    data = _run_node(
        """
let decorated = 0;
let built = 0;
const turn = new FakeElement('div');
turn.id = 'liveAssistantTurn';
const inner = new FakeElement('div');
const group = new FakeElement('div');
const list = new FakeElement('div');
global.S = { session: { session_id: 'session-1' }, activeStreamId: 'stream-1' };
global.$ = (id) => id === 'liveAssistantTurn' ? turn : null;
global._assistantTurnBlocks = () => inner;
global.isTransparentStream = () => false;
global._decorateTransparentEventRow = () => { decorated += 1; };
global.ensureLiveWorklogContainer = () => group;
global._liveToolStepEl = () => list;
global._toolWorklogListEl = () => list;
global.buildToolCard = (tc) => {
  built += 1;
  const row = new FakeElement('div');
  row.className = 'tool-card-row';
  row._tcData = tc;
  return row;
};
appendLiveToolCard({ name: 'lookup', done: false });
process.stdout.write(JSON.stringify({
  decorated,
  built,
  rows: list.children.length,
}));
"""
    )

    assert data["decorated"] == 0
    assert data["built"] == 1
    assert data["rows"] == 1
