"""Regression test for issue #4658: collapsed tool-card rows in the
transparent-stream view lost their inline summary/preview.

Symptom: collapsed transparent tool rows showed only the bare tool name
(`read_file`, `terminal`, `search_files`) with no hint of what the call did;
expanding still worked. Caused by the interaction of two changes:
  * 69072ac34 moved buildToolCard's collapsed preview to an arg summary and
    #4411 (6951d10a) then blanked that preview for the common arg/shell case
    (it assumes the row NAME carries the target — true for the worklog view's
    action-label name), and
  * the transparent view (_decorateTransparentEventRow) overrides the row name
    back to the BARE tool name (_toolShortName), so it had neither the
    target-carrying label nor a preview.

Fix: _decorateTransparentEventRow now populates the `.tool-card-preview` span
from a quiet, TARGET-based summary (_transparentToolSummary) — path/command/
query/skill, never the raw result JSON — so collapsed rows are self-describing
again while honoring the "keep collapsed previews quiet" intent
(test_tool_card_preview_summary.py).

This drives the ACTUAL functions from static/ui.js via node + jsdom-free DOM
shims, and runs the same render path live streaming and persisted reload share
(both go through _decorateTransparentEventRow(buildToolCard(tc))).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ── Minimal DOM shims (enough for buildToolCard + _decorateTransparentEventRow)
function makeEl(tag){
  const el = {
    tagName: (tag||'div').toUpperCase(),
    _attrs: {}, _classes: new Set(), children: [], style: {},
    dataset: {}, _html: '', textContent: '', parentNode: null,
    firstChild: null,
    classList: {
      add(c){el._classes.add(c)}, remove(c){el._classes.delete(c)},
      toggle(c,on){const w=on===undefined?!el._classes.has(c):!!on; if(w)el._classes.add(c); else el._classes.delete(c);},
      contains(c){return el._classes.has(c)},
    },
    setAttribute(k,v){el._attrs[k]=String(v);}, getAttribute(k){return k in el._attrs?el._attrs[k]:null;},
    removeAttribute(k){delete el._attrs[k];}, hasAttribute(k){return k in el._attrs;},
    appendChild(c){c.parentNode=el; el.children.push(c); el.firstChild=el.children[0]; return c;},
    insertBefore(c,ref){c.parentNode=el; const i=el.children.indexOf(ref); if(i<0)el.children.push(c); else el.children.splice(i,0,c); el.firstChild=el.children[0]; return c;},
    insertAdjacentHTML(){/* detail body not needed for collapsed-preview assertions */},
    closest(){return null}, focus(){},
    get innerHTML(){return el._html;},
    set innerHTML(v){
      el._html=String(v);
      // Parse just the spans we assert on out of the template buildToolCard emits.
      el.children=[];
      const mk=(cls,text)=>{const c=makeEl('span'); c._classes=new Set(cls.split(' ').filter(Boolean)); c.textContent=text; c.parentNode=el; el.children.push(c);};
      const grab=(cls)=>{const re=new RegExp('<span class="'+cls.replace(/[-]/g,'\\-')+'">([\\s\\S]*?)<\\/span>'); const m=el._html.match(re); return m?m[1]:null;};
      const nameLabel=grab('tool-card-name-label');
      if(nameLabel!==null){ const nameSpan=makeEl('span'); nameSpan._classes=new Set(['tool-card-name']); nameSpan.textContent=nameLabel; nameSpan.parentNode=el; el.children.push(nameSpan); }
      const preview=grab('tool-card-preview');
      if(preview!==null) mk('tool-card-preview', preview);
      const header=makeEl('div'); header._classes=new Set(['tool-card-header']);
      // re-home the parsed spans under a header element so querySelector('.tool-card-header') works
      header.children=el.children.slice(); header.children.forEach(c=>c.parentNode=header);
      const card=makeEl('div'); card._classes=new Set(['tool-card']); card.appendChild(header);
      el.children=[card]; el.firstChild=card;
    },
    querySelector(sel){ return el._find(sel, false)[0]||null; },
    querySelectorAll(sel){ return el._find(sel, true); },
    _find(sel, all){
      const want=sel.split(',').map(s=>s.trim().replace(/^\./,''));
      const out=[];
      const walk=(node)=>{ for(const c of (node.children||[])){ if(want.some(w=>c._classes&&c._classes.has(w))) out.push(c); walk(c);} };
      walk(el); return out;
    },
  };
  return el;
}

global.document = { createElement: (t)=>makeEl(t), querySelectorAll:()=>[], querySelector:()=>null };
global.window = {};
global.CSS = { escape: s=>s };
global.t = undefined;
global.li = () => '<svg></svg>';
global.esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.toolIcon = () => '<svg></svg>';
global._snippetLooksLikeDiff = () => false;
global._colorDiffLines = s => s;
global._attachCopyButton = () => {};
global._attachProgressBar = () => {};
global._wireTransparentHeaderToggle = () => {};
global._transparentToolDetailHtml = () => '<div class="tool-card-detail"></div>';
// _toolDisclosureIdentity pulls in worklog hashing helpers irrelevant to the
// collapsed-preview assertion; stub it (only feeds a data- attribute).
global._toolDisclosureIdentity = () => '';

function extractFunc(name){
  const re=new RegExp('function\\s+'+name+'\\s*\\(');
  const start=src.search(re); if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{',start); let depth=1; i++;
  while(depth>0&&i<src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start,i);
}
// helpers buildToolCard / decorate depend on — eval'd at TOP LEVEL (a function
// declaration eval'd inside a callback would be scoped to that callback and
// lost, so collect all sources and eval them once here).
var _FN_NAMES=[
 '_toolDisplayName','_toolActionKind','_toolKindIcon','_toolPathBasename',
 '_decodeToolLabelEntities','_redactToolTargetLabel','_shortToolLabel','_toolI18n',
 '_toolTargetLabel','_toolVisibleTargetLabel','_toolCommandTitle','_toolQueryTitle',
 '_toolActionLabelText','_toolArgPreviewValue','_toolArgPreviewKeyIsHidden',
 '_formatToolArgPreview','_toolResultOneLiner','_toolCardPreviewText','_toolCardAllowsDetail',
 '_toolDetailLeadLabel','_toolDetailLeadText','_toolShortName','_transparentEventPreview',
 '_transparentToolStatus','_transparentToolSummary',
 '_isMemorySave','_isSkillUpdate','_tcAction',
 'buildToolCard','_decorateTransparentEventRow',
];
var _FN_SRC='';
for(var _i=0;_i<_FN_NAMES.length;_i++){
  try{ _FN_SRC+=extractFunc(_FN_NAMES[_i])+'\n'; }catch(e){ /* optional helper absent */ }
}
eval(_FN_SRC);

function previewFor(tc){
  const row=_decorateTransparentEventRow(buildToolCard(tc),{
    type:'tool', name:tc.name, status:_transparentToolStatus(tc,true), toolCall:tc,
  });
  const card=row.querySelector('.tool-card');
  const header=card?card.querySelector('.tool-card-header'):null;
  const preview=header?header.querySelector('.tool-card-preview'):null;
  const name=header?header.querySelector('.tool-card-name'):null;
  return { preview: preview?preview.textContent:null, name: name?name.textContent:null };
}

const cases = JSON.parse(process.argv[3]);
const out = {};
for(const [key,tc] of Object.entries(cases)) out[key]=previewFor(tc);
process.stdout.write(JSON.stringify(out));
"""


CASES = {
    "read_file": {"name": "read_file", "args": {"path": "/home/x/api/config.py"}, "snippet": '{"content":"1|import os"}', "done": True},
    "terminal": {"name": "terminal", "args": {"command": "git fetch origin --quiet"}, "snippet": "275 /opt/...", "done": True},
    "search_files": {"name": "search_files", "args": {"pattern": "buildToolCard", "path": "/tmp"}, "snippet": '{"total_count": 0}', "done": True},
    "skill_view": {"name": "skill_view", "args": {"name": "opencode-review-agents"}, "snippet": '{"success": true}', "done": True},
    "no_args": {"name": "terminal", "args": {}, "done": True},
    "args_no_target": {"name": "terminal", "args": {"workdir": "/tmp"}, "done": True},
    "unknown_tool_args": {"name": "frobnicate", "args": {"mode": "dry-run"}, "done": True},
}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    driver = tmp_path_factory.mktemp("t4658") / "driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(CASES)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node driver failed: {proc.stderr}")
    return json.loads(proc.stdout)


def test_read_file_row_shows_file_target(results):
    r = results["read_file"]
    assert r["name"] == "read_file", f"transparent row keeps the bare tool name: {r}"
    assert "config.py" in (r["preview"] or ""), (
        f"collapsed read_file row must show the file target inline, not be blank: {r}"
    )


def test_terminal_row_shows_command(results):
    r = results["terminal"]
    assert r["preview"], f"collapsed terminal row must not be blank (the #4658 regression): {r}"
    assert "git fetch" in r["preview"], f"terminal row should summarize the command: {r}"


def test_search_row_shows_pattern_not_result_json(results):
    r = results["search_files"]
    assert r["preview"], f"collapsed search row must not be blank: {r}"
    assert "buildToolCard" in r["preview"], f"search row should show the pattern/query: {r}"
    # Honor the "keep collapsed previews quiet" contract — no raw result JSON.
    assert "total_count" not in r["preview"], f"raw result JSON must stay in the detail body: {r}"


def test_skill_row_shows_skill_name(results):
    r = results["skill_view"]
    assert "opencode-review-agents" in (r["preview"] or ""), f"skill row should name the skill: {r}"


def test_no_args_row_does_not_invent_noise(results):
    # With no target/args there is nothing useful to summarize; the preview
    # should be empty rather than echoing a status word as a fake summary.
    r = results["no_args"]
    assert r["preview"] in ("", None), f"no-target row should have an empty preview, got: {r}"


def test_args_but_no_target_row_has_empty_preview(results):
    # A call with args but NO real target (e.g. terminal with only {workdir})
    # must NOT dump a raw arg snippet into the collapsed preview — that would
    # contradict the quiet/target-based intent and the no-args case (#4658 review).
    r = results["args_no_target"]
    assert r["preview"] in ("", None), (
        f"args-but-no-target row must have an empty collapsed preview (no raw arg dump), got: {r}"
    )


def test_unknown_tool_with_args_has_empty_preview(results):
    # An unknown tool whose args yield no target must also stay empty rather than
    # echoing {mode:"dry-run"} as an invented summary.
    r = results["unknown_tool_args"]
    assert r["preview"] in ("", None), (
        f"unknown-tool args-only row must have an empty collapsed preview, got: {r}"
    )
