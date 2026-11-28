"""PR #8681 visual review: compact chips and route-owned effort choices.

The Node harness runs the production reasoning UI plus slash-command helpers.
Geometry/ellipsis are separately verified in Chromium; CSS checks here pin the
bounds rather than pretending a fake DOM measures browser layout.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const root = process.argv[2];
const scenes = JSON.parse(process.argv[3]);
function element(effort) {
  const classes = new Set();
  return {style:{}, dataset:{effort}, textContent:'', title:'', attrs:{},
    classList:{add(k){classes.add(k)}, remove(k){classes.delete(k)},
      contains(k){return classes.has(k)},
      toggle(k,on){(on===undefined?!classes.has(k):on)?classes.add(k):classes.delete(k)}},
    setAttribute(k,v){this.attrs[k]=String(v)}, getAttribute(k){return this.attrs[k]}};
}
const options = ['', 'none', 'low', 'high', 'xhigh', 'max', 'ultra'].map(element);
const els = Object.fromEntries(['composerReasoningWrap','composerReasoningLabel',
  'composerReasoningChip','composerMobileReasoningAction','composerMobileReasoningLabel']
  .map(id=>[id,element()]));
els.composerReasoningDropdown={querySelectorAll(){return options}};
const saved = [], toasts = [];
const context = vm.createContext({console, Promise, URLSearchParams, Set, Map,
  window:{}, document:{addEventListener(){}}, $:id=>els[id]||null,
  S:{activeProfile:'default',session:{session_id:'review',model:'gpt-5.6',model_provider:'openai-codex'}},
  showToast:s=>toasts.push(s)});
const ui = fs.readFileSync(root+'/static/ui.js','utf8');
vm.runInContext(ui.slice(ui.indexOf('// ── Reasoning effort chip'),
  ui.indexOf('// ── Session toolsets chip')),context);
const commands = fs.readFileSync(root+'/static/commands.js','utf8');
function extract(name) {
  const start = commands.search(new RegExp('function\\s+'+name+'\\s*\\('));
  if(start<0) throw new Error(name+' missing');
  let end = commands.indexOf('{',start)+1, depth=1;
  while(depth && end<commands.length) {
    if(commands[end]==='{') ++depth;
    else if(commands[end]==='}') --depth;
    ++end;
  }
  return commands.slice(start,end);
}
vm.runInContext(extract('_getSlashSubArgOptions'),context);
vm.runInContext(extract('cmdReasoning'),context);
const run=code=>vm.runInContext(code,context);
context.api=()=>Promise.resolve({});
context._saveReasoningEffort=effort=>{
  saved.push(effort);
  return Promise.resolve({reasoning_effort:effort,reasoning_effort_display:effort});
};
(async()=>{
  const snapshots=[];
  for(const scene of scenes) {
    if(scene.profile) context.S.activeProfile=scene.profile;
    if(scene.session) context.S.session={...context.S.session,...scene.session};
    if(scene.meta) {
      run('_lastReasoningFetchKey=_reasoningEffortQuery()');
      context.meta=scene.meta;
      run('_applyReasoningChip(meta.reasoning_effort,meta)');
    }
    if(scene.replay) run('_applyReasoningChip(_currentReasoningEffort)');
    if(scene.response) {
      context._saveReasoningEffort=effort=>{
        saved.push(effort);
        return Promise.resolve(scene.response);
      };
    }
    if(scene.command) run('cmdReasoning('+JSON.stringify(scene.command)+')');
    await Promise.resolve();
    const subargs=await run("_getSlashSubArgOptions('reasoning')");
    snapshots.push({label:els.composerReasoningLabel.textContent,
      mobile:els.composerMobileReasoningLabel.textContent,
      title:els.composerReasoningChip.title,
      aria:els.composerReasoningChip.attrs['aria-label'],
      mobileTitle:els.composerMobileReasoningAction.title,
      mobileAria:els.composerMobileReasoningAction.attrs['aria-label'],
      visible:options.filter(o=>o.style.display!=='none').map(o=>o.dataset.effort),
      selected:options.filter(o=>o.classList.contains('selected')).map(o=>o.dataset.effort),
      subargs, saved:[...saved], toasts:[...toasts],
      row:run("_formatReasoningEffortLabel('ultra',true)")});
  }
  process.stdout.write(JSON.stringify(snapshots));
})().catch(e=>{console.error(e);process.exit(1)});
"""


@pytest.fixture
def drive(tmp_path):
    node = NODE
    if node is None:
        pytest.skip("node not on PATH")
    script = tmp_path / "reasoning-review.js"
    script.write_text(DRIVER, encoding="utf-8")

    def run(scenes):
        result = subprocess.run(
            [node, str(script), str(ROOT), json.dumps(scenes)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return run


def status(effort, *, annotated=None, extra=()):
    result = {
        "reasoning_effort": effort,
        "supported_efforts": ["low", "high", "xhigh", "max", *extra],
        "supports_thinking_toggle": True,
        "reasoning_effort_display": annotated or effort,
    }
    return result


def test_ultra_chip_is_short_but_dropdown_row_keeps_full_name(drive):
    state = drive([{"meta": status("ultra", extra=("ultra",))}])[0]
    assert state["label"] == state["mobile"] == "Ultra"
    assert state["row"] == "Ultra High"
    assert state["selected"] == ["ultra"]
    assert state["title"] == "Reasoning effort for this chat: Ultra High"


def test_coerced_chip_and_highlight_agree_explanation_stays_accessible(drive):
    explanation = "ultra (sends max on this route)"
    initial, replay = drive([
        {"meta": status("max", annotated=explanation)},
        {"replay": True},
    ])
    for state in (initial, replay):
        assert state["label"] == state["mobile"] == "Max"
        assert state["selected"] == ["max"]
        assert "ultra" not in state["visible"]
        assert state["title"] == state["aria"] == (
            "Reasoning effort for this chat: " + explanation
        )
        assert state["mobileTitle"] == state["mobileAria"] == state["title"]


def test_fresh_metadata_clears_old_explanation_on_desktop_and_mobile(drive):
    _, fresh, replay = drive([
        {"meta": status("max", annotated="ultra (sends max on this route)")},
        {"meta": status("ultra", extra=("ultra",))},
        {"replay": True},
    ])
    for state in (fresh, replay):
        assert state["label"] == state["mobile"] == "Ultra"
        assert "sends" not in state["title"]
        assert state["mobileAria"] == state["aria"]


def test_reasoning_autocomplete_follows_future_and_filtered_route_choices(drive):
    future, filtered = drive([
        {"meta": status("hyper", extra=("ultra", "hyper")), "command": "hyper"},
        {"session": {"model_provider": "openrouter"}, "meta": status("max")},
    ])
    assert future["subargs"] == [
        "show", "hide", "none", "low", "high", "xhigh", "max", "ultra", "hyper"
    ]
    assert future["saved"] == ["hyper"]
    assert filtered["subargs"] == ["show", "hide", "none", "low", "high", "xhigh", "max"]


@pytest.mark.parametrize("change", [
    {"session": {"session_id": "other"}},
    {"session": {"model_provider": "openrouter"}},
    {"session": {"model": "codex-mini-latest"}},
    {"profile": "other"},
])
def test_autocomplete_does_not_borrow_previous_contexts_route(drive, change):
    _, switched = drive([
        {"meta": status("ultra", extra=("ultra",))},
        change,
    ])
    assert "ultra" not in switched["subargs"]


def test_reasoning_command_toast_keeps_full_explanation(drive):
    explanation = "ultra (sends max on this route)"
    state = drive([{
        "meta": status("max", annotated=explanation),
        "response": status("max", annotated=explanation),
        "command": "ultra",
    }])[0]
    assert state["saved"] == ["ultra"]
    assert state["label"] == "Max"
    assert explanation in state["toasts"][-1]


def test_reasoning_command_toast_uses_full_ultra_row_name(drive):
    state = drive([{"meta": status("ultra", extra=("ultra",)), "command": "ultra"}])[0]
    assert state["label"] == "Ultra"
    assert "set to Ultra High" in state["toasts"][-1]


def test_reasoning_chip_css_bounds_and_ellipsizes_future_labels():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    chip_match = re.search(r"\.composer-reasoning-chip\{([^}]+)\}", css)
    label_match = re.search(r"\.composer-reasoning-label\{([^}]+)\}", css)
    assert chip_match and label_match
    chip = chip_match.group(1)
    label = label_match.group(1)
    assert "max-width:none" not in chip
    assert re.search(r"max-width:\s*\d+(?:px|rem|ch)", chip)
    for declaration in (
        "min-width:0", "overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"
    ):
        assert declaration in label
    # Ellipsis requires a block container, not an inline-flex label.
    assert "display:inline-flex" not in label
