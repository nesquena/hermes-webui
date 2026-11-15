"""Regression coverage for #9080.

After a profile switch the Tasks list (``#cronList``) kept the previous
profile's rendered rows until the new profile's ``/api/crons`` reply arrived.
Those rows stayed clickable and their handlers held the previous profile's job
objects: opening one and pressing Duplicate rendered a create form seeded from
the old job, ``loadCrons()`` keeps a form in ``create`` mode when the reply
lands, so Save created the previous profile's job (prompt included) in the new
profile. Same class: ``_cronSkillsCache`` survived both switch paths, so the
form's skill autocomplete offered the previous profile's skills.

The fix extends the shared synchronous reset ``_resetProfileScopedPanelState``
(run by both the canonical picker switch, through ``_profileSwitchPanelLoad``,
and the session-load switch ``_switchProfileForSessionLoad``) to drop the
cached list (``_cronList = []``), the rendered rows and the cron skill cache.

These tests drive the REAL helpers (``loadCrons``, ``openCronDetail``,
``_findCronJob``, ``duplicateCurrentCron``, ``editCurrentCron``/``openCronEdit``,
``saveCronForm``, ``_clearCronDetail``, ``_resetProfileScopedPanelState``,
``_profileSwitchPanelLoad``, ``switchToProfile`` and
``_switchProfileForSessionLoad``) in node with stubbed collaborators and a
minimal fake DOM that tracks appended rows, following the harness approach of
``tests/test_issue9069_session_load_profile_panel_reset.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

PATHS = ("picker", "session")


def _extract_function(src: str, name: str) -> str:
    """Return the full source of ``[async ]function name(...) {...}``."""
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    if src[max(0, start - 6):start] == "async ":
        start -= 6
    open_brace = src.find("{", src.find(")", start))
    depth = 0
    for idx in range(open_brace, len(src)):
        char = src[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"{name} closing brace not found")


def _run_node(script: str) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(REPO_ROOT),
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}\nstdout:\n{result.stdout}")
    return json.loads(result.stdout)


_HARNESS = r"""
'use strict';
// ── minimal fake DOM: elements track appended children; assigning innerHTML
// drops them, like the real DOM does for the rows loadCrons appends. ──────
function makeEl(tag){
  const cls = new Set();
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id:'', className:'', textContent:'', value:'',
    checked:true, disabled:false, hidden:false, open:false, scrollTop:0, dataset:{}, style:{ display:'' },
    children:[], onclick:null, _html:'',
    classList:{ add(c){ cls.add(c); }, remove(c){ cls.delete(c); },
      toggle(c, on){ if (on === undefined ? !cls.has(c) : on) cls.add(c); else cls.delete(c); },
      contains(c){ return cls.has(c); } },
    appendChild(child){ el.children.push(child); return child; },
    addEventListener(){}, setAttribute(){}, querySelector(){ return null; }, remove(){},
  };
  Object.defineProperty(el, 'innerHTML', {
    get(){ return el._html; },
    set(v){ el._html = String(v); el.children = []; },
  });
  return el;
}
const els = { taskDetailTitle: makeEl(), taskDetailBody: makeEl(), taskDetailEmpty: makeEl(), cronList: makeEl() };
function $(id){ return els[id] || null; }
const document = { title:'', createElement(tag){ return makeEl(tag); }, querySelectorAll(){ return []; } };
const window = {};
const localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){} };
const S = { activeProfile:'alpha', activeProfileIsDefault:false, session:null, messages:[] };

// ── panels.js module state (initial values as declared in panels.js) ─────
let _currentPanel = 'tasks';
let _showAllCronProfiles = false;
let _cronOtherProfileCount = 0;
let _cronPreFormDetail = null;
let _editingCronId = null;
let _cronIsDuplicate = false;
let _currentCronDetail = null;
let _currentCronDetailKey = '';
let _cronDetailRefreshKey = null;
let _cronMode = 'empty';
let _cronModelPickerTouched = false;
let _cronSelectedSkills = [];
let _cronSkillsCache = null;
let _cronList = null;
let _cronLoadGeneration = 0;
let _cronAcceptedLoadGeneration = 0;
let _cronLatestLoadSettled = Promise.resolve();
const _cronNewJobIds = new Set();
let _skillsData = null;
let _workspaceList = [];
let _profileSwitchGeneration = 0;
let _sessionListSkeletonActive = false;

// ── data per profile ─────────────────────────────────────────────────────
const ALPHA_JOB = { id:'alpha-job', name:'Alpha digest', schedule:'0 9 * * *', prompt:'ALPHA-ONLY PROMPT',
  deliver:'local', profile:'alpha', model:'alpha-model', provider:'alpha-provider', skills:[] };
const BETA_JOB = { id:'beta-job', name:'Beta digest', schedule:'0 10 * * *', prompt:'BETA-ONLY PROMPT',
  deliver:'local', profile:'beta', skills:[] };
const JOBS_BY_PROFILE = { alpha:[ALPHA_JOB], beta:[BETA_JOB] };
const SKILLS_BY_PROFILE = { alpha:[{ name:'alpha-skill' }], beta:[{ name:'beta-skill' }] };

// ── recorded collaborators ───────────────────────────────────────────────
const calls = { apiUrls:[], renderCronForm:[], cronCreate:[], bindSkillPicker:0 };
// /api/crons replies are per active profile AT REQUEST TIME. While holdCrons
// is set a reply is parked until releaseCrons(), like a slow network.
let holdCrons = false;
const heldCronReplies = [];
function releaseCrons(){ holdCrons = false; while (heldCronReplies.length) heldCronReplies.shift()(); }
async function api(url, opts){
  calls.apiUrls.push(url);
  if (url === '/api/profile/switch') return { active: JSON.parse(opts.body).name, is_default:false };
  if (url === '/api/crons/create') {
    calls.cronCreate.push({ profile: S.activeProfile, body: JSON.parse(opts.body) });
    return { id:'created-1' };
  }
  if (url === '/api/skills') return { skills: (SKILLS_BY_PROFILE[S.activeProfile] || []).map(s => ({ ...s })) };
  if (url.startsWith('/api/crons')) {
    const owner = S.activeProfile;
    const reply = () => ({ jobs: (JOBS_BY_PROFILE[owner] || []).map(j => ({ ...j })) });
    if (holdCrons) return new Promise(resolve => heldCronReplies.push(() => resolve(reply())));
    return reply();
  }
  throw new Error('unexpected api ' + url);
}

// loadCrons / openCronDetail collaborators
async function loadCronProfiles(){ return []; }
function loadCronGatewayNotice(){}
function _cronStatusMeta(){ return { state:'active', listClass:'', label:'' }; }
function _cronJobKey(job){ return (job.profile || '') + ':' + job.id; }
function _cronItemId(job){ return 'cron-' + job.id; }
function _cronOwnerProfileName(job){ return job.profile || ''; }
function _cronProfileLabel(name){ return name; }
function _appendCronProfileToggle(){}
function _clearCronUnreadForJob(){}
function _checkCronWatchOnDetail(){}
function _closeMobileSidebarAfterPanelSelection(){}
function _stopCronWatch(){}
function _setCronHeaderButtons(){}
function _renderCronDetail(job){            // the state the real renderer sets
  if (_currentCronDetailKey !== _cronJobKey(job)) _cronDetailRefreshKey = null;
  _currentCronDetail = job;
  _currentCronDetailKey = _cronJobKey(job);
  els.taskDetailTitle.textContent = job.name;
  els.taskDetailBody.innerHTML = '<div>detail ' + job.prompt + '</div>';
  _cronMode = 'read';
}
// The create/edit form: renders the inputs saveCronForm reads.
function _renderCronForm(args){
  calls.renderCronForm.push({ name: args.name, prompt: args.prompt, isEdit: args.isEdit });
  els.taskDetailBody.innerHTML = '<form id="cronForm">' + args.prompt + '</form>';
  const field = v => { const el = makeEl('input'); el.value = v; return el; };
  els.cronFormName = field(args.name);
  els.cronFormSchedule = field(args.schedule || '0 9 * * *');
  els.cronFormPrompt = field(args.prompt);
  els.cronFormDeliver = field(args.deliver || 'local');
  els.cronFormProfile = field(args.profile || '');
  els.cronFormError = makeEl('div');
}
function _cronScheduleForEdit(job){ return job.schedule || ''; }
function _bindCronSkillPicker(){ calls.bindSkillPicker++; }
function _refreshCronProfileSelect(){}
function switchPanel(name){ _currentPanel = name; }
function esc(v){ return String(v); }
function t(key){ return key; }
function showToast(){}

// canonical / session-load switch collaborators (no-ops)
async function loadSkills(){}
async function loadMemory(){}
async function loadKanban(){}
async function loadProfilesPanel(){}
async function loadWorkspacesPanel(){}
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
async function renderSessionList(){}
function renderSessionListFromCache(){}
function _refreshProfileSwitchBackground(){}
function _tick(){ return new Promise(r => setTimeout(r, 5)); }

// ── helpers ──────────────────────────────────────────────────────────────
function cronRows(){
  const out = [];
  (function walk(node){
    for (const child of node.children || []) {
      if (child.className === 'cron-item') out.push(child);
      walk(child);
    }
  })(els.cronList);
  return out;
}
function rowIds(){ return cronRows().map(r => r.id); }
function listIds(){ return Array.isArray(_cronList) ? _cronList.map(j => j.id) : _cronList; }
function names(list){ return Array.isArray(list) ? list.map(s => s.name) : list; }
// Alpha is active with Tasks visible and its rows rendered by the REAL loadCrons.
async function primeAlpha(){
  S.activeProfile = 'alpha';
  _currentPanel = 'tasks';
  await loadCrons();
}
// Switch through either path. The picker switch awaits loadCrons for a visible
// Tasks panel, so it is not awaited here: callers await `done` after release.
async function switchTo(path, name){
  if (path === 'session') {
    await _switchProfileForSessionLoad(name);
    await _tick();
    return { done: Promise.resolve(true) };
  }
  const done = switchToProfile(name);
  await _tick();
  return { done };
}
"""


def _real_helpers() -> str:
    panels = [
        "_findCronJob",
        "loadCrons",
        "_clearCronDetail",
        "openCronDetail",
        "editCurrentCron",
        "openCronEdit",
        "duplicateCurrentCron",
        "_modelBareNameForProvider",
        "_cronModelBareName",
        "_cronProviderForClear",
        "saveCronForm",
        "_profileSwitchPanelLoad",
        "_resetProfileScopedPanelState",
        "switchToProfile",
    ]
    parts = [_extract_function(PANELS_JS, name) for name in panels]
    parts.append(_extract_function(SESSIONS_JS, "_switchProfileForSessionLoad"))
    return "\n".join(parts)


def _script(body: str, **subst: str) -> str:
    for key, value in subst.items():
        body = body.replace(key, value)
    return (
        _HARNESS
        + _real_helpers()
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


@pytest.mark.parametrize("tasks_visible", [True, False], ids=["tasks-visible", "tasks-hidden"])
@pytest.mark.parametrize("path", PATHS)
def test_switch_drops_previous_profile_rows_and_cached_list(path, tasks_visible):
    """Right after the switch (the new profile's /api/crons reply not yet
    in), both switch paths leave ``_cronList`` an empty ARRAY, no stale row in
    ``#cronList`` and no previous-profile job reachable through the list."""
    result = _run_node(_script(r"""
await primeAlpha();
const before = { rows: rowIds(), list: listIds() };
_currentPanel = __PANEL__;
holdCrons = true;                         // Beta's /api/crons reply is slow
const sw = await switchTo(__PATH__, 'beta');
const after = {
  active: S.activeProfile,
  isArray: Array.isArray(_cronList),
  list: listIds(),
  rows: rowIds(),
  rowText: cronRows().map(r => r.innerHTML).join(''),
  findAlpha: _findCronJob('alpha-job') ? 'found' : null,
};
releaseCrons();
const switchOk = await sw.done;
await _tick();
process.stdout.write(JSON.stringify({ before, after, switchOk,
  settled: { rows: rowIds(), list: listIds() } }));
""", __PATH__=json.dumps(path), __PANEL__=json.dumps("tasks" if tasks_visible else "chat")))
    # precondition: the real loadCrons rendered Alpha's clickable row
    assert result["before"] == {"rows": ["cron-alpha-job"], "list": ["alpha-job"]}
    after = result["after"]
    assert after["active"] == "beta"
    assert after["isArray"] is True, f"_cronList is not an array after the reset: {after['list']!r}"
    assert after["list"] == [], "previous profile's cached cron list survived the switch"
    assert after["rows"] == [], "previous profile's Tasks rows are still rendered and clickable"
    assert "Alpha digest" not in after["rowText"]
    assert after["findAlpha"] is None, "previous profile's job is still reachable through _cronList"
    assert result["switchOk"] is not False
    if tasks_visible:
        assert result["settled"] == {"rows": ["cron-beta-job"], "list": ["beta-job"]}
    else:
        # A hidden Tasks panel is not reloaded; the next open loads it.
        assert result["settled"] == {"rows": [], "list": []}


@pytest.mark.parametrize("path", PATHS)
def test_stale_row_duplicate_cannot_save_previous_profile_job_in_new_profile(path):
    """The #9080 reproduction: hold Beta's /api/crons reply, switch
    Alpha -> Beta with Tasks visible, click whatever row is still listed and
    press Duplicate, then let the reply land and press Save. No Alpha job may
    reach a create form or a create POST; after the reply only Beta's rows
    render. A Beta Duplicate + Save afterwards still works (positive control:
    the harness CAN reach a create POST)."""
    result = _run_node(_script(r"""
await primeAlpha();
holdCrons = true;
const sw = await switchTo(__PATH__, 'beta');
const staleRows = cronRows();
for (const row of staleRows) if (typeof row.onclick === 'function') row.onclick();
duplicateCurrentCron();
const beforeReply = {
  active: S.activeProfile,
  staleRows: staleRows.map(r => r.id),
  forms: calls.renderCronForm.slice(),
  mode: _cronMode, dup: _cronIsDuplicate,
  body: els.taskDetailBody.innerHTML,
};
releaseCrons();
await sw.done;
await _tick();
const afterReply = { rows: rowIds(), list: listIds(), mode: _cronMode, dup: _cronIsDuplicate,
  body: els.taskDetailBody.innerHTML };
// Save whatever form is open now.
await Promise.race([saveCronForm().catch(() => {}), _tick()]);
await _tick();
const createsFromStaleRow = calls.cronCreate.slice();
// Positive control: Beta's own row -> Duplicate -> Save does POST a create.
const betaRow = cronRows().find(r => r.id === 'cron-beta-job');
if (betaRow) betaRow.onclick();
duplicateCurrentCron();
await Promise.race([saveCronForm().catch(() => {}), _tick()]);
await _tick();
process.stdout.write(JSON.stringify({ beforeReply, afterReply, createsFromStaleRow,
  createsAfterControl: calls.cronCreate.slice() }));
""", __PATH__=json.dumps(path)))
    before = result["beforeReply"]
    assert before["active"] == "beta"
    assert before["staleRows"] == [], "previous profile's rows were still clickable after the switch"
    assert before["forms"] == [], f"a create form was opened from a stale row: {before['forms']}"
    assert before["mode"] != "create"
    assert before["dup"] is False
    assert "ALPHA-ONLY PROMPT" not in before["body"]
    after = result["afterReply"]
    assert after["rows"] == ["cron-beta-job"], "after the reply only the new profile's rows may render"
    assert after["list"] == ["beta-job"]
    assert after["mode"] != "create", "a create form seeded from the previous profile survived the reply"
    assert "ALPHA-ONLY PROMPT" not in after["body"]
    assert result["createsFromStaleRow"] == [], (
        f"Save created the previous profile's job in the new profile: {result['createsFromStaleRow']}"
    )
    control = result["createsAfterControl"]
    assert len(control) == 1, f"positive control did not reach a create POST: {control}"
    assert control[0]["profile"] == "beta"
    assert control[0]["body"]["prompt"] == "BETA-ONLY PROMPT"


@pytest.mark.parametrize("opener", ["duplicate", "edit"])
@pytest.mark.parametrize("path", PATHS)
def test_switch_drops_cron_skill_cache_so_next_form_refetches(path, opener):
    """``_cronSkillsCache`` is null after the switch, so the next Duplicate /
    Edit form open (both guard the fetch behind ``if (!_cronSkillsCache)``)
    requests /api/skills again and offers the NEW profile's skills."""
    result = _run_node(_script(r"""
await primeAlpha();
cronRows()[0].onclick();                  // open Alpha's job
duplicateCurrentCron();                   // first form open caches Alpha's skills
await _tick();
const alphaCache = names(_cronSkillsCache);
calls.apiUrls.length = 0;
const sw = await switchTo(__PATH__, 'beta');
await sw.done;
await _tick();
const cacheAfterSwitch = names(_cronSkillsCache);
const betaRow = cronRows().find(r => r.id === 'cron-beta-job');
if (betaRow) betaRow.onclick();
if (__OPENER__ === 'edit') editCurrentCron(); else duplicateCurrentCron();
await _tick();
process.stdout.write(JSON.stringify({ alphaCache, cacheAfterSwitch, betaRowFound: !!betaRow,
  forms: calls.renderCronForm.map(f => f.prompt),
  skillsFetches: calls.apiUrls.filter(u => u === '/api/skills').length,
  cacheNow: names(_cronSkillsCache) }));
""", __PATH__=json.dumps(path), __OPENER__=json.dumps(opener)))
    assert result["alphaCache"] == ["alpha-skill"]
    assert result["betaRowFound"] is True
    assert result["forms"][-1] == "BETA-ONLY PROMPT"
    assert result["cacheAfterSwitch"] is None, (
        f"previous profile's cron skill cache survived the switch: {result['cacheAfterSwitch']}"
    )
    assert result["skillsFetches"] == 1, "the next form open did not refetch /api/skills"
    assert result["cacheNow"] == ["beta-skill"], "skill autocomplete offers the previous profile's skills"


@pytest.mark.parametrize("path", PATHS)
def test_new_profile_rows_publish_after_switch_with_visible_tasks(path):
    """Order pin: with Tasks visible the new profile's rows (from the REAL
    loadCrons) publish after the switch. If ``loadCrons()`` ran BEFORE the
    reset, the reset's generation bump would retire that load and the list
    would stay empty (or stale): this test must fail then."""
    result = _run_node(_script(r"""
await primeAlpha();
const sw = await switchTo(__PATH__, 'beta');
await sw.done;
await _tick();
await _tick();
process.stdout.write(JSON.stringify({ active: S.activeProfile, rows: rowIds(), list: listIds(),
  crons: calls.apiUrls.filter(u => u.startsWith('/api/crons')).length }));
""", __PATH__=json.dumps(path)))
    assert result["active"] == "beta"
    assert result["crons"] == 2, "expected exactly Alpha's initial load and Beta's reload"
    assert result["list"] == ["beta-job"], "the new profile's Tasks list did not publish after the switch"
    assert result["rows"] == ["cron-beta-job"]


def test_picker_switch_drops_stale_rows_at_accept_not_after_the_list_render():
    """The picker switch must drop the previous profile's rows as soon as the
    server accepts the switch, not only in ``_profileSwitchPanelLoad`` after
    ``await renderSessionList()``. Hold that render (a slow /api/sessions): in
    the window the cookie already serves Beta, so a stale Alpha row + Duplicate
    + Save would create Alpha's job in Beta."""
    result = _run_node(_script(r"""
await primeAlpha();
let releaseList;
renderSessionList = () => new Promise(resolve => { releaseList = resolve; });
const done = switchToProfile('beta');
await _tick();
const staleRows = cronRows();
for (const row of staleRows) if (typeof row.onclick === 'function') row.onclick();
duplicateCurrentCron();
const inWindow = {
  held: typeof releaseList === 'function',
  active: S.activeProfile,
  rows: staleRows.map(r => r.id),
  list: listIds(),
  forms: calls.renderCronForm.slice(),
  dup: _cronIsDuplicate,
};
await Promise.race([saveCronForm().catch(() => {}), _tick()]);
await _tick();
inWindow.creates = calls.cronCreate.slice();
if (releaseList) releaseList();
const ok = await done;
await _tick();
process.stdout.write(JSON.stringify({ inWindow, ok, settled: { rows: rowIds(), list: listIds() } }));
"""))
    w = result["inWindow"]
    assert w["held"] is True, "precondition: the switch is parked inside renderSessionList()"
    assert w["active"] == "beta"
    assert w["rows"] == [], "previous profile's rows were still clickable while the switch rendered the session list"
    assert w["list"] == []
    assert w["forms"] == [] and w["dup"] is False
    assert w["creates"] == [], f"Save in the window created the previous profile's job in the new profile: {w['creates']}"
    assert result["ok"] is not False
    assert result["settled"] == {"rows": ["cron-beta-job"], "list": ["beta-job"]}
