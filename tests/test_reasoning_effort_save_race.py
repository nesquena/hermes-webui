"""Drive real dropdown/command callbacks with delayed reasoning responses."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const root = process.argv[2];
const [entry, change] = JSON.parse(process.argv[3]);
function el() {
  return {style:{}, dataset:{}, textContent:'', value:'',
    classList:{toggle(){},remove(){},add(){},contains(){return false}},
    setAttribute(){}, querySelectorAll(){return []}};
}
const els = Object.fromEntries(['modelSelect','composerReasoningWrap',
  'composerReasoningLabel','composerReasoningChip','composerReasoningDropdown',
  'composerMobileReasoningLabel','composerMobileReasoningAction'].map(k=>[k,el()]));
const clicks = [];
const requests = [];
const context = vm.createContext({
  S:{activeProfile:'default',session:{session_id:'A',model:'gpt-5',model_provider:'openai',profile:'default'}},
  window:{}, URLSearchParams, Promise,
  document:{addEventListener(type, fn){if(type==='click') clicks.push(fn);}},
  $:id=>els[id]||null, showToast(){},
  api(url, options){
    const request = {url,options}; requests.push(request);
    return new Promise((ok, fail)=>{request.ok=ok; request.fail=fail;});
  }
});
const ui = fs.readFileSync(root+'/static/ui.js','utf8');
vm.runInContext(ui.slice(ui.indexOf('// ── Reasoning effort chip'),
  ui.indexOf('// ── Session toolsets chip')),context);
const commands = fs.readFileSync(root+'/static/commands.js','utf8');
vm.runInContext(commands.slice(commands.indexOf('function cmdReasoning('),
  commands.indexOf('function cmdVoice(')),context);
const run = code=>vm.runInContext(code,context);
const flush = ()=>new Promise(r=>setImmediate(r));
const status = effort=>({reasoning_effort:effort,supported_efforts:['low','high']});
const posts = ()=>requests.filter(r=>r.options?.method==='POST');
function pick(effort) {
  if(entry==='dropdown') {
    const option={dataset:{effort}};
    clicks[0]({target:{closest(selector){return selector==='.reasoning-option'?option:null;}}});
  } else run(`cmdReasoning('${effort}')`);
}

(async()=>{
  run('syncReasoningChip()'); requests.at(-1).ok(status('low')); await flush();
  pick('high'); await flush();
  const post = posts().at(-1);
  if(!post) throw new Error('POST not dispatched');
  const payload = JSON.parse(post.options.body);
  if(payload.session_id!=='A'||payload.model!=='gpt-5'||payload.provider!=='openai')
    throw new Error('wrong request owner');
  let oldGet;
  if(change==='old_get') { run('fetchReasoningChip()'); oldGet=requests.at(-1); }
  let serialized = null;
  let immediateReread = false;
  if(change==='newer_save'||change==='newer_save_failed') {
    // A newer pick in the same chat must not reach the server until the older
    // save settles, so the server cannot store the older pick last.
    pick('low'); await flush();
    serialized = posts().length===1;
  }
  if(change==='session') run("S.session={...S.session,session_id:'B'}");
  if(change==='model') run("S.session.model='gpt-5.5'");
  if(change==='provider') run("S.session.model_provider='custom:test'");
  if(change==='profile') run("S.activeProfile='work'");
  if(change==='session'||change==='model'||change==='provider') {
    run('syncReasoningChip()'); requests.at(-1).ok(status('low')); await flush();
  }
  const beforeSaveResult = requests.length;
  post.ok(status('high')); await flush();
  // A model/provider change in the same chat re-reads the stored value.
  const resync = requests.slice(beforeSaveResult).find(r=>!r.options);
  const resynced = Boolean(resync);
  if(resync) { resync.ok(status('high')); await flush(); }
  if(serialized!==null) {
    const second = posts().at(-1);
    if(second===post) throw new Error('newer save never dispatched');
    if(JSON.parse(second.options.body).effort!=='low') throw new Error('wrong newer save');
    if(change==='newer_save') second.ok(status('low'));
    else {
      second.fail(new Error('boom')); await flush();
      // The failed newest save re-reads the stored value immediately.
      const reread = requests.at(-1);
      immediateReread = !reread.options && reread!==second;
      reread.ok(status('high'));
    }
    await flush();
  }
  if(oldGet) { oldGet.ok(status('low')); await flush(); }
  const afterSave = els.composerReasoningLabel.textContent;
  const beforeSync = requests.length;
  run('syncReasoningChip()');
  let refetched = requests.length>beforeSync;
  if(refetched) { requests.at(-1).ok(status('high')); await flush(); }
  const afterSync = els.composerReasoningLabel.textContent;
  // Returning to A must read its persisted setting instead of borrowing B's cache.
  if(change==='session') {
    run("S.session.session_id='A';syncReasoningChip()");
    requests.at(-1).ok(status('high')); await flush();
  }
  process.stdout.write(JSON.stringify({afterSave,afterSync,serialized,refetched,resynced,immediateReread,
    mobile:els.composerMobileReasoningLabel.textContent,
    restored:els.composerReasoningLabel.textContent}));
})().catch(e=>{console.error(e); process.exit(1);});
"""


@pytest.mark.parametrize("entry", ["dropdown", "command"])
@pytest.mark.parametrize(
    "change",
    ["session", "model", "provider", "profile", "unchanged", "old_get",
     "newer_save", "newer_save_failed"],
)
def test_delayed_save_keeps_its_context(tmp_path, entry, change):
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    result = subprocess.run(
        [NODE, str(driver), str(ROOT), json.dumps([entry, change])],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    if change in ("newer_save", "newer_save_failed"):
        assert out["serialized"] is True
    if change == "newer_save":
        assert out["afterSave"] == "Low"
        assert out["refetched"] is False
        assert out["afterSync"] == "Low"
        return
    if change == "newer_save_failed":
        # The older save's late response must not claim the chip; the failed
        # newest save drops the cache so the next sync reads the stored value.
        assert out["immediateReread"] is True
        assert out["afterSave"] == "High"
        assert out["refetched"] is False
        assert out["afterSync"] == "High"
        return
    if change in ("model", "provider"):
        # The save landed for this chat under its old model; the chip re-reads
        # the stored value for the new model instead of keeping the stale GET.
        assert out["resynced"] is True
        assert out["afterSave"] == "High"
        assert out["refetched"] is False
        assert out["afterSync"] == "High"
        return
    expected = "High" if change in ("unchanged", "old_get") else "Low"
    assert out["afterSave"] == expected
    assert out["refetched"] is False
    assert out["afterSync"] == expected
    restored = "High" if change == "session" else expected
    assert out["restored"] == restored
    assert out["mobile"] == restored


CROSS_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const root = process.argv[2];
const [entry, change] = JSON.parse(process.argv[3]);
function el() {
  return {style:{}, dataset:{}, textContent:'', value:'',
    classList:(()=>{const c=new Set(); return {toggle(k,on){(on===undefined?!c.has(k):on)?c.add(k):c.delete(k);},remove(k){c.delete(k)},add(k){c.add(k)},contains(k){return c.has(k)}};})(),
    setAttribute(){}, querySelectorAll(){return []}};
}
const els = Object.fromEntries(['modelSelect','composerReasoningWrap',
  'composerReasoningLabel','composerReasoningChip','composerReasoningDropdown',
  'composerMobileReasoningLabel','composerMobileReasoningAction'].map(k=>[k,el()]));
const clicks = [];
const requests = [];
const context = vm.createContext({
  S:{activeProfile:'default',session:{session_id:'A',model:'gpt-5',model_provider:'openai',profile:'default'}},
  window:{}, URLSearchParams, Promise,
  document:{addEventListener(type, fn){if(type==='click') clicks.push(fn);}},
  $:id=>els[id]||null, showToast(){},
  api(url, options){
    const request = {url,options,profile:context.S.activeProfile}; requests.push(request);
    return new Promise((ok, fail)=>{request.ok=ok; request.fail=fail;});
  }
});
const ui = fs.readFileSync(root+'/static/ui.js','utf8');
vm.runInContext(ui.slice(ui.indexOf('// ── Reasoning effort chip'),
  ui.indexOf('// ── Session toolsets chip')),context);
const commands = fs.readFileSync(root+'/static/commands.js','utf8');
vm.runInContext(commands.slice(commands.indexOf('function cmdReasoning('),
  commands.indexOf('function cmdVoice(')),context);
const run = code=>vm.runInContext(code,context);
const flush = ()=>new Promise(r=>setImmediate(r));
const status = effort=>({reasoning_effort:effort,supported_efforts:['low','high']});
const posts = ()=>requests.filter(r=>r.options?.method==='POST');
function pick(effort) {
  if(entry==='dropdown') {
    const option={dataset:{effort}};
    clicks[0]({target:{closest(selector){return selector==='.reasoning-option'?option:null;}}});
  } else run(`cmdReasoning('${effort}')`);
}

(async()=>{
  run('syncReasoningChip()'); requests.at(-1).ok(status('low')); await flush();
  if(change==='profile_switch') {
    pick('high'); await flush();
    pick('low'); await flush();
    els.composerReasoningDropdown.classList.add('open');
    const begun = run('_beginReasoningProfileSwitch()');
    let drained = false; begun.then(()=>{drained=true;});
    const controlsFrozen = els.composerReasoningChip.disabled===true
      && els.composerMobileReasoningAction.disabled===true;
    run('toggleReasoningDropdown()');
    const opened = els.composerReasoningDropdown.classList.contains('open');
    pick('high'); await flush();  // frozen: must not be queued under either cookie
    posts()[0].ok(status('high')); await flush();
    const drainedEarly = drained;
    posts()[1].ok(status('low')); await flush();
    const drainedAfter = drained;
    run("S.activeProfile='work'; _endReasoningProfileSwitch()");
    const controlsReleased = els.composerReasoningChip.disabled===false
      && els.composerMobileReasoningAction.disabled===false;
    pick('high'); await flush();
    process.stdout.write(JSON.stringify({
      bodies: posts().map(r=>JSON.parse(r.options.body).effort),
      profiles: posts().map(r=>r.profile), drainedEarly, drainedAfter,
      controlsFrozen, opened, controlsReleased}));
    return;
  }
  // A picks High (delayed), B picks Low, return to A before either completes.
  pick('high'); await flush();
  const postA = posts().at(-1);
  run("S.session={...S.session,session_id:'B'}; syncReasoningChip()");
  requests.at(-1).ok(status('low')); await flush();
  pick('low'); await flush();
  run("S.session={...S.session,session_id:'A'}; syncReasoningChip()");
  const getA = requests.at(-1);
  if(change==='get_first') { getA.ok(status('low')); await flush(); }
  postA.ok(status('high')); await flush();
  const postB = posts().at(-1);
  if(postB===postA) throw new Error('B save never dispatched');
  postB.ok(status('low')); await flush();
  if(change==='save_first') { getA.ok(status('low')); await flush(); }
  const afterSave = els.composerReasoningLabel.textContent;
  const before = requests.length;
  run('syncReasoningChip()');
  process.stdout.write(JSON.stringify({afterSave, refetched: requests.length>before,
    afterSync: els.composerReasoningLabel.textContent}));
})().catch(e=>{console.error(e); process.exit(1);});
"""


def _run_cross(tmp_path, entry, change):
    driver = tmp_path / "cross.js"
    driver.write_text(CROSS_DRIVER)
    result = subprocess.run(
        [NODE, str(driver), str(ROOT), json.dumps([entry, change])],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("entry", ["dropdown", "command"])
@pytest.mark.parametrize("change", ["get_first", "save_first"])
def test_other_chats_pick_does_not_hide_returned_chats_save(tmp_path, entry, change):
    out = _run_cross(tmp_path, entry, change)
    assert out == {"afterSave": "High", "refetched": False, "afterSync": "High"}


@pytest.mark.parametrize("entry", ["dropdown", "command"])
def test_profile_switch_drains_queued_saves_under_old_profile(tmp_path, entry):
    out = _run_cross(tmp_path, entry, "profile_switch")
    # High and Low go out under the original profile before the switch proceeds;
    # the pick made mid-switch is refused; a pick after the switch uses the new one.
    assert out["bodies"] == ["high", "low", "high"]
    assert out["profiles"] == ["default", "default", "work"]
    assert out["drainedEarly"] is False
    assert out["drainedAfter"] is True
    # The controls are disabled (and the dropdown can't open) for the whole freeze.
    assert out["controlsFrozen"] is True
    assert out["opened"] is False
    assert out["controlsReleased"] is True


def test_both_profile_switch_paths_drain_reasoning_saves():
    for rel, call in (("static/panels.js", "await api('/api/profile/switch', {"),
                      ("static/sessions.js", "await api('/api/profile/switch',{")):
        src = (ROOT / rel).read_text()
        start = src.index(call)
        begin = src.rindex("_beginReasoningProfileSwitch", 0, start)
        assert start - begin < 600, rel
        assert "_endReasoningProfileSwitch" in src[start:src.index("\n}\n", start)], rel
