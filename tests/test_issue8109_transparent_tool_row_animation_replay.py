"""Regression for #8109: transparent-stream tool rows replay their entrance
animation mid-stream because a live row's wall-clock timestamp churns every
frame, forcing the whole tool card to be re-created.

The visible clock label is minute-resolution, but the stamp is stored with
sub-second precision on a `.transparent-event-time` span INSIDE the row, so the
row's serialized innerHTML differs on every render frame. That makes the
`existing.innerHTML !== newHtml` guard in `_refreshTransparentLiveRow` always
true, which destroys and rebuilds `.transparent-event-progress` — restarting the
`transparent-progress-shimmer` animation the reader has already seen.

These tests drive the real helpers through a fake DOM whose innerHTML is
SERIALIZED on read (descendant attributes included), the way a browser reports
it, so mutated descendant state is visible as innerHTML churn. That fidelity is
what lets these tests observe #8109 at all.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _run_node_script(script, ui_js_path=None):
    assert NODE, "node is required for DOM-executed transparent renderer tests"
    env = os.environ.copy()
    if ui_js_path is not None:
        env["UI_JS_PATH"] = ui_js_path
    result = subprocess.run([NODE, "-e", script], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# One animation frame apart, and both inside the same wall-clock minute so the
# visible clock label cannot legitimately differ between them.
FRAME_A = 1700000000
FRAME_B = 1700000016


_FAKE_DOM = r"""
class FakeElement {
  constructor(tag='div'){
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.id = '';
    this.onclick = null;
    this.onkeydown = null;
    this._textContent = '';
    this.innerHTMLSetCount = 0;
    this._classes = new Set();
    const self = this;
    this.style = {
      _props: Object.create(null),
      setProperty(name, value){ this._props[String(name)] = String(value); },
      removeProperty(name){ delete this._props[String(name)]; },
      getPropertyValue(name){ return Object.prototype.hasOwnProperty.call(this._props, name) ? this._props[name] : ''; },
    };
    this.classList = {
      add(...names){ names.forEach(n=>self._classes.add(n)); },
      remove(...names){ names.forEach(n=>self._classes.delete(n)); },
      contains(n){ return self._classes.has(n); },
      toggle(n, force){
        const next = force === undefined ? !self._classes.has(n) : !!force;
        if(next) self._classes.add(n); else self._classes.delete(n);
        return next;
      },
    };
  }
  get parentElement(){ return this.parentNode; }
  get firstChild(){ return this.children[0] || null; }
  get className(){ return Array.from(this._classes).join(' '); }
  set className(value){ this._classes = new Set(String(value).trim().split(/\s+/).filter(Boolean)); }
  get textContent(){
    if(this.children.length) return this.children.map(c=>c.textContent).join('');
    return this._textContent;
  }
  set textContent(value){ this._textContent = String(value ?? ''); this.children = []; }
  // Serialized on read, so descendant attribute churn is visible as innerHTML churn.
  get innerHTML(){
    const attrs = [];
    for(const [k,v] of Object.entries(this.attributes)) attrs.push(' ' + k + '="' + String(v).replace(/"/g,'&quot;') + '"');
    if(this._classes.size) attrs.unshift(' class="' + Array.from(this._classes).join(' ') + '"');
    for(const [k,v] of Object.entries(this.style._props)) attrs.push(' style="' + k + ':' + String(v) + '"');
    const tag = this.tagName.toLowerCase();
    const open = '<' + tag + attrs.join('') + '>';
    if(!this.children.length) return open + String(this._textContent||'').replace(/</g,'&lt;') + '</' + tag + '>';
    return open + this.children.map(c=>c.innerHTML).join('') + '</' + tag + '>';
  }
  set innerHTML(value){
    this.innerHTMLSetCount += 1;
    // A real innerHTML write drops every descendant and builds fresh nodes, so
    // node identity is NOT preserved across it.
    this.children.forEach(c => { c.parentNode = null; });
    this.children = [];
    this._textContent = '';
    if(String(value||'').indexOf('tool-card transparent-event-card') >= 0) buildToolCard(this);
  }
  setAttribute(name, value){
    const key = String(name);
    const val = String(value);
    this.attributes[key] = val;
    if(key === 'id') this.id = val;
    if(key.startsWith('data-')){
      this.dataset[key.slice(5).replace(/-([a-z])/g, (_,c)=>c.toUpperCase())] = val;
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
      delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_,c)=>c.toUpperCase())];
    }
  }
  appendChild(child){ child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, ref){
    const idx = this.children.indexOf(ref);
    child.parentNode = this;
    if(idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
    return child;
  }
  removeChild(child){
    const idx = this.children.indexOf(child);
    if(idx >= 0){ this.children.splice(idx, 1); child.parentNode = null; }
    return child;
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
    while(node){ if(matchesSelector(node, selector)) return node; node = node.parentNode; }
    return null;
  }
}
function matchesSelector(el, selector){
  return String(selector||'').split(',').map(p=>p.trim()).filter(Boolean).some(part=>matchesSimple(el, part));
}
function matchesSimple(el, selector){
  const cls = selector.match(/\.([A-Za-z0-9_-]+)/g) || [];
  for(const c of cls) if(!el.classList.contains(c.slice(1))) return false;
  const at = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/g) || [];
  for(const a of at){
    const mm = a.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
    const value = el.getAttribute(mm[1]);
    if(value === null) return false;
    if(mm[2] !== undefined && String(value) !== String(mm[2])) return false;
  }
  return !!(cls.length || at.length);
}
function el(tag, classes){
  const node = new FakeElement(tag);
  String(classes||'').split(/\s+/).filter(Boolean).forEach(c=>node.classList.add(c));
  return node;
}
function buildToolCard(row){
  const card = el('div', 'tool-card transparent-event-card');
  const header = el('div', 'tool-card-header');
  header.appendChild(el('span', 'tool-card-name'));
  const preview = el('span', 'tool-card-preview');
  preview.textContent = String(global.__PREVIEW_TEXT || '');
  header.appendChild(preview);
  const status = el('span', 'transparent-event-status');
  status.setAttribute('data-status', 'running');
  status.textContent = 'Running';
  header.appendChild(status);
  card.appendChild(header);
  const detail = el('div', 'tool-card-detail');
  detail.setAttribute('data-transparent-detail-mode', 'full');
  card.appendChild(detail);
  // The animated element: CSS gives [data-progress-running="1"]::before the
  // infinite `transparent-progress-shimmer` (#8109).
  const progress = el('div', 'transparent-event-progress');
  progress.setAttribute('data-progress-running', '1');
  progress.setAttribute('data-progress-percent', '60%');
  progress.style.setProperty('--transparent-progress-percent', '60%');
  card.appendChild(progress);
  row.appendChild(card);
}
global.document = { createElement:(tag)=>new FakeElement(tag) };

// Controllable wall clock: _syncTransparentEventTimestamp falls back to it for a
// live row that has no event timestamp of its own.
global.__CLOCK = 1700000000;
eval('function _activityNowSeconds(){ return global.__CLOCK; }');
eval('global._activityNowSeconds = _activityNowSeconds;');
"""

_PRELUDE = r"""
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
"""

_HELPERS = r"""
eval(extractFunc('_timestampSeconds'));
eval(extractFunc('_firstValidTimestampSeconds'));
eval(extractFunc('_activityClockLabel'));
eval(extractFunc('_activityFullClockLabel'));
eval(extractFunc('_syncTransparentEventTimestamp'));
eval(extractFunc('_transparentLiveRowAttributePairs'));
eval(extractFunc('_transparentLiveRowInteractiveState'));
eval(extractFunc('_rehydrateTransparentLiveRow'));
eval(extractFunc('_refreshTransparentThinkingLiveRow'));
eval(extractFunc('_refreshTransparentLiveRow'));

// A running live tool row shaped exactly as _anchorSceneTransparentNodeForRow
// builds it for the transparent live scene: no event timestamp yet, so the wall
// clock is the only available stamp.
function makeRunningToolRow(previewText){
  // The serialized card carries the preview text, the way a real buildToolCard
  // row does, so an innerHTML rewrite is observable in the harness.
  global.__PREVIEW_TEXT = String(previewText || '');
  const row = new FakeElement('div');
  row.className = 'tool-card-row transparent-event-row';
  row.setAttribute('data-transparent-event-row', '1');
  row.setAttribute('data-transparent-stream', '1');
  row.setAttribute('data-event-type', 'tool');
  row.setAttribute('data-tool-name', 'shell');
  row.setAttribute('data-event-status', 'Running');
  row.setAttribute('data-live-stream-owned', '1');
  row.setAttribute('data-anchor-row-id', 'row-1');
  row.setAttribute('data-anchor-row-role', 'tool');
  row.setAttribute('data-anchor-source-event-type', 'tool');
  row.innerHTML = '<div class="tool-card transparent-event-card"></div>';
  row._tcData = { name:'shell', args:{ cmd:'ls' }, done:false };
  const header = row.querySelector('.tool-card-header');
  _syncTransparentEventTimestamp(row, header, { toolCall: row._tcData, live: true });
  return row;
}
"""


def _run_refresh(existing_preview, candidate_preview, clock_a, clock_b):
    script = (
        _PRELUDE
        + _FAKE_DOM
        + _HELPERS
        + r"""
global.__CLOCK = global.__CLOCK_A;
const existing = makeRunningToolRow(global.__EXISTING_PREVIEW);
existing.innerHTMLSetCount = 0;
const progressBefore = existing.querySelector('.transparent-event-progress');
const htmlBefore = existing.innerHTML;
const labelBefore = existing.querySelector('.transparent-event-time').textContent;

global.__CLOCK = global.__CLOCK_B;
const candidate = makeRunningToolRow(global.__CANDIDATE_PREVIEW);
const labelCandidate = candidate.querySelector('.transparent-event-time').textContent;

const refreshed = _refreshTransparentLiveRow(existing, candidate, {});
const progressAfter = existing.querySelector('.transparent-event-progress');
const timeAfter = existing.querySelector('.transparent-event-time');

process.stdout.write(JSON.stringify({
  sameNode: refreshed === existing,
  innerHTMLSetCount: existing.innerHTMLSetCount,
  progressIdentityPreserved: !!progressBefore && progressBefore === progressAfter,
  htmlStable: htmlBefore === existing.innerHTML,
  stampAfter: timeAfter ? timeAfter.getAttribute('data-event-at') : null,
  labelStable: labelBefore === (timeAfter ? timeAfter.textContent : null),
  labelMatchesCandidate: labelBefore === labelCandidate,
  progressStillRunning: !!progressAfter && progressAfter.getAttribute('data-progress-running') === '1',
  previewAfter: (existing.querySelector('.tool-card-preview') || {textContent:null}).textContent,
}));
"""
    )
    script = script.replace("global.__CLOCK_A", str(clock_a))
    script = script.replace("global.__CLOCK_B", str(clock_b))
    script = script.replace("global.__EXISTING_PREVIEW", json.dumps(existing_preview))
    script = script.replace("global.__CANDIDATE_PREVIEW", json.dumps(candidate_preview))
    return _run_node_script(script, str(ROOT / "static" / "ui.js"))


def test_control_genuinely_changed_row_still_rewrites_and_rebuilds():
    """Control: the harness CAN see a rewrite, and a row whose content really
    changed is still re-rendered. This keeps the regression below honest — a
    blanket "never rewrite" would pass it while breaking live updates."""
    data = _run_refresh("ls -la", "rm -rf build", FRAME_A, FRAME_B)
    assert data["innerHTMLSetCount"] == 1
    assert data["progressIdentityPreserved"] is False
    assert data["htmlStable"] is False
    assert data["previewAfter"] == "rm -rf build"


def test_live_tool_row_refresh_is_byte_stable_across_frames():
    """#8109: two render frames of the SAME running tool row must not rewrite the
    card. Before the fix the wall-clock `data-event-at` inside the row changed
    every frame, so the row was rebuilt and its progress animation replayed."""
    data = _run_refresh("ls -la", "ls -la", FRAME_A, FRAME_B)

    assert data["sameNode"] is True
    assert data["innerHTMLSetCount"] == 0
    assert data["progressIdentityPreserved"] is True
    assert data["htmlStable"] is True
    # The minute-resolution label is unchanged across the two frames.
    assert data["labelStable"] is True
    assert data["labelMatchesCandidate"] is True
    # The running state survives, so the shimmer keeps running uninterrupted.
    assert data["progressStillRunning"] is True
    assert data["stampAfter"] is not None


def test_live_row_stamp_is_quantized_to_visible_minute_resolution():
    """The synthetic live stamp is stored at the resolution it is DISPLAYED at,
    so it cannot churn the row's serialized HTML frame over frame."""
    data = _run_refresh("ls -la", "ls -la", FRAME_A, FRAME_B)
    stamp = float(data["stampAfter"])
    assert stamp == float(int(stamp))
    assert stamp % 60 == 0


def test_explicit_event_timestamp_keeps_exact_precision():
    """Quantization applies ONLY to the synthetic live fallback. A row carrying
    a real event timestamp keeps it verbatim — #5700 depends on that."""
    script = _PRELUDE + _FAKE_DOM + _HELPERS + r"""
global.__CLOCK = 1700000016;
const row = makeRunningToolRow('ls -la');
const header = row.querySelector('.tool-card-header');
const stamp = _syncTransparentEventTimestamp(row, header, { ts: 1700000123.456, live: false });
process.stdout.write(JSON.stringify({
  stamp: stamp.getAttribute('data-event-at'),
  source: row.getAttribute('data-event-at-source'),
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["stamp"] == "1700000123.456"
    assert data["source"] == "event"


def test_live_stamp_advances_when_the_visible_minute_changes():
    """Quantization is to the displayed minute, not a frozen value: crossing a
    minute boundary still updates the row.

    Each candidate is a FRESH node (no `data-event-at` yet), which is what makes
    the live render fall through to the wall clock on every frame.
    """
    script = _PRELUDE + _FAKE_DOM + _HELPERS + r"""
function stampAt(clock){
  global.__CLOCK = clock;
  const row = makeRunningToolRow('ls -la');
  return row.querySelector('.transparent-event-time').getAttribute('data-event-at');
}
const inMinuteA = stampAt(1700000000);  // 13:33:20
const inMinuteB = stampAt(1700000016);  // 13:33:36, same minute
const nextMinute = stampAt(1700000060); // 13:34:20, next minute
process.stdout.write(JSON.stringify({
  inMinuteA, inMinuteB, nextMinute,
  sameMinuteStable: inMinuteA === inMinuteB,
  advancedOnMinuteChange: inMinuteA !== nextMinute,
}));
"""
    data = _run_node_script(script, str(ROOT / "static" / "ui.js"))
    assert data["sameMinuteStable"] is True
    assert data["advancedOnMinuteChange"] is True