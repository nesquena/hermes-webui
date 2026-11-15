"""Regression coverage for #9069.

A cross-profile ``loadSession()`` (browser Back/Forward, an in-app
``/session/<id>`` link, boot restore, ...) switches the server's active
profile through ``_switchProfileForSessionLoad`` in ``static/sessions.js``.
That path used to skip the profile-scoped panel reset the canonical
``switchToProfile`` runs through ``_profileSwitchPanelLoad`` in
``static/panels.js``. The Tasks panel then kept the previous profile's cron
detail, edit id, pre-form snapshot and duplicate flag, so Duplicate + Save
created the previous profile's job in the NEW profile.

The fix shares the synchronous half of that reset
(``_resetProfileScopedPanelState``) between both switch paths, drops the
Skills cache like ``switchToProfile`` does, and reloads only a visible Tasks
panel (``loadCrons`` is latest-wins, #6767) on the session-load path. Memory
and Skills are deliberately NOT reloaded there: their loaders have no
owner-profile/latest-response check yet (separate hardening follow-up).

These tests drive the REAL helpers (``_switchProfileForSessionLoad``,
``_resetProfileScopedPanelState``, ``_profileSwitchPanelLoad``,
``_clearCronDetail``, ``duplicateCurrentCron``, ``loadSkills`` and, for the
parity check, ``switchToProfile``) in node with stubbed collaborators.
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


def _extract_optional(src: str, name: str) -> str:
    """Like ``_extract_function`` but empty when absent, so the RED run on the
    pre-fix tree fails on behaviour rather than on a missing helper."""
    return _extract_function(src, name) if f"function {name}(" in src else ""


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
function makeEl(){ return { textContent:'', innerHTML:'', value:'', disabled:false, style:{ display:'' },
  classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } } }; }
const els = { taskDetailTitle: makeEl(), taskDetailBody: makeEl(), taskDetailEmpty: makeEl() };
function $(id){ return els[id] || null; }
const window = {};
const localStorage = { removeItem(){} };
const S = { activeProfile:'alpha', activeProfileIsDefault:false, session:null, messages:[] };

// ── panels.js module state ──────────────────────────────────────────────
let _currentPanel = 'tasks';
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 4;
let _cronPreFormDetail = null;
let _editingCronId = null;
let _cronIsDuplicate = false;
let _currentCronDetail = null;
let _currentCronDetailKey = '';
let _cronDetailRefreshKey = null;
let _cronMode = 'empty';
let _cronModelPickerTouched = false;
let _cronSelectedSkills = [];
let _cronSkillsCache = [];
let _cronList = [];
let _cronLoadGeneration = 0;
let _cronAcceptedLoadGeneration = 0;
let _cronLatestLoadSettled = Promise.resolve();
let _skillsData = null;
let _workspaceList = [];
let _profileSwitchGeneration = 0;
let _sessionListSkeletonActive = false;

// ── recorded collaborators ──────────────────────────────────────────────
const calls = { loadCrons:0, loadSkills:0, loadMemory:0, loadKanban:0, loadProfilesPanel:0,
  loadWorkspacesPanel:0, renderCronForm:[], cronCreate:[], skillsCacheAtLoad:'unset', stopCronWatch:0,
  apiUrls:[], renderedSkills:null };
// loadSkills' render collaborators (used when the REAL loadSkills is loaded)
els.skillsList = makeEl();
let _collapsedCats = new Set();
function renderSkills(list){ calls.renderedSkills = list.map(s => s.name); }
const SKILLS_BY_PROFILE = { alpha:[{ name:'alpha-skill' }], beta:[{ name:'beta-skill' }] };
function esc(v){ return String(v); }
let loadCronsImpl = async () => {};
let switchMode = 'ok';          // 'ok' | 'fail'
let renderSessionListImpl = async () => {};
async function loadCrons(){ calls.loadCrons++; return loadCronsImpl(); }  // __CRONS_STUB__
// loadCrons' collaborators (used when the REAL loadCrons is loaded)
els.cronList = makeEl();
const _cronNewJobIds = new Set();
async function loadCronProfiles(){ return []; }
async function loadCronGatewayNotice(){}
function _cronStatusMeta(){ return { state:'active', listClass:'', label:'' }; }
function _cronJobKey(job){ return (job.profile || '') + ':' + job.id; }
function _cronItemId(job){ return 'cron-' + job.id; }
function _cronOwnerProfileName(job){ return job.profile || ''; }
function _cronProfileLabel(name){ return name; }
function _appendCronProfileToggle(){}
function _renderCronDetail(){}
const document = { title:'', createElement(){ const el = makeEl(); el.appendChild = () => {}; return el; } };
els.cronList.appendChild = () => {};
// /api/crons replies are per active profile at request time; a profile listed
// in cronDeferred gets a reply the test resolves later.
const cronDeferred = {};
function cronsReply(profile){
  if (cronDeferred[profile]) return new Promise(r => { cronDeferred[profile] = r; });
  return Promise.resolve({ jobs:[{ id: profile + '-job', name: profile, profile, prompt: profile.toUpperCase() + '-ONLY' }] });
}
async function loadSkills(){ calls.loadSkills++; calls.skillsCacheAtLoad = _skillsData; }  // __SKILLS_STUB__
async function loadMemory(){ calls.loadMemory++; }
async function loadKanban(){ calls.loadKanban++; }
async function loadProfilesPanel(){ calls.loadProfilesPanel++; }
async function loadWorkspacesPanel(){ calls.loadWorkspacesPanel++; }
function _stopCronWatch(){ calls.stopCronWatch++; }
function _setCronHeaderButtons(mode){ calls.headerMode = mode; }
function _renderCronForm(args){
  calls.renderCronForm.push(args);
  els.taskDetailBody.innerHTML = '<form id="cronForm">' + args.prompt + '</form>';
}
function _cronScheduleForEdit(job){ return job.schedule || ''; }
function _bindCronSkillPicker(){}
function switchPanel(name){ _currentPanel = name; }
async function api(url, opts){
  calls.apiUrls.push(url);
  if (url === '/api/profile/switch') {
    if (switchMode === 'fail') throw new Error('switch refused');
    return { active: JSON.parse(opts.body).name, is_default:false };
  }
  if (url === '/api/crons/create') { calls.cronCreate.push(JSON.parse(opts.body)); return { id:'new' }; }
  if (url === '/api/skills') return { skills: SKILLS_BY_PROFILE[S.activeProfile] || [] };
  if (url === '/api/crons') return cronsReply(S.activeProfile);
  throw new Error('unexpected api ' + url);
}
// sessions.js / canonical-switch collaborators (no-ops)
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
function showToast(){}
function t(key){ return key; }
function renderSessionList(){ return renderSessionListImpl(); }
function renderSessionListFromCache(){}
function _refreshProfileSwitchBackground(){}
function _tick(){ return new Promise(r => setTimeout(r, 5)); }

// The ALPHA job the user had open in Tasks before navigating.
const ALPHA_JOB = { id:'alpha-job', name:'Alpha digest', schedule:'0 9 * * *', prompt:'ALPHA-ONLY PROMPT',
  deliver:'local', profile:'alpha', model:'alpha-model', provider:'alpha-provider' };
function showAlphaJob(){
  _currentCronDetail = { ...ALPHA_JOB };
  _currentCronDetailKey = 'alpha:alpha-job';
  _cronMode = 'read';
  _cronList = [{ ...ALPHA_JOB }];
  els.taskDetailTitle.textContent = ALPHA_JOB.name;
  els.taskDetailBody.innerHTML = '<div>alpha detail</div>';
  els.taskDetailEmpty.style.display = 'none';
}
function cronSnapshot(){
  return {
    activeProfile: S.activeProfile,
    currentCronDetail: _currentCronDetail ? _currentCronDetail.id : null,
    cronMode: _cronMode,
    editingCronId: _editingCronId,
    cronPreFormDetail: _cronPreFormDetail ? _cronPreFormDetail.id : null,
    cronIsDuplicate: _cronIsDuplicate,
    showAllCronProfiles: _showAllCronProfiles,
    cronOtherProfileCount: _cronOtherProfileCount,
    detailBody: els.taskDetailBody.innerHTML,
    detailTitle: els.taskDetailTitle.textContent,
    skillsDataIsNull: _skillsData === null,
    loadCrons: calls.loadCrons,
  };
}
"""


def _real_helpers(*, canonical: bool = False, real_skills: bool = False, real_crons: bool = False) -> str:
    parts = [
        _extract_function(PANELS_JS, "_clearCronDetail"),
        _extract_optional(PANELS_JS, "_resetProfileScopedPanelState"),
        _extract_function(PANELS_JS, "_profileSwitchPanelLoad"),
        _extract_function(PANELS_JS, "duplicateCurrentCron"),
        _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad"),
    ]
    if canonical:
        parts.append(_extract_function(PANELS_JS, "switchToProfile"))
    if real_skills:
        parts.append(_extract_function(PANELS_JS, "loadSkills"))
        parts.append(_extract_function(PANELS_JS, "filterSkills"))
    if real_crons:
        parts.append(_extract_function(PANELS_JS, "loadCrons"))
    return "\n".join(parts)


def _script(body: str, *, canonical: bool = False, real_skills: bool = False,
            real_crons: bool = False) -> str:
    harness = _HARNESS
    drop = set()
    if real_skills:
        drop.add("__SKILLS_STUB__")
    if real_crons:
        drop.add("__CRONS_STUB__")
    if drop:
        harness = "\n".join(
            line for line in harness.splitlines() if not any(m in line for m in drop)
        )
    return (
        harness
        + _real_helpers(canonical=canonical, real_skills=real_skills, real_crons=real_crons)
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


def test_session_load_switch_resets_tasks_state_and_reloads_visible_tasks():
    """The reproduced #9069 state: Tasks visible, Alpha job open in edit +
    duplicate form state. A cross-profile session load to Beta must drop all
    of it and reload the Tasks list for Beta, like the canonical switch."""
    result = _run_node(_script(r"""
showAlphaJob();
_editingCronId = 'alpha-job';
_cronPreFormDetail = { ...ALPHA_JOB };
_cronIsDuplicate = true;
_skillsData = [{ name:'alpha-skill' }];
await _switchProfileForSessionLoad('beta');
process.stdout.write(JSON.stringify(cronSnapshot()));
"""))
    assert result["activeProfile"] == "beta"
    assert result["currentCronDetail"] is None, "previous profile's cron detail survived"
    assert result["cronMode"] == "empty"
    assert result["editingCronId"] is None
    assert result["cronPreFormDetail"] is None
    assert result["cronIsDuplicate"] is False
    assert result["showAllCronProfiles"] is False
    assert result["cronOtherProfileCount"] == 0
    assert result["detailBody"] == ""
    assert result["detailTitle"] == ""
    assert result["skillsDataIsNull"] is True, "previous profile's Skills cache survived"
    assert result["loadCrons"] == 1, "visible Tasks panel was not reloaded for the new profile"


def test_duplicate_after_session_load_switch_cannot_clone_previous_profile_job():
    """Duplicate after the switch must not open a create form seeded from the
    previous profile's job, and a Duplicate form opened BEFORE the switch must
    not survive it (its Save would POST /api/crons/create under the new
    profile cookie)."""
    result = _run_node(_script(r"""
// (a) Duplicate pressed after the cross-profile load.
showAlphaJob();
await _switchProfileForSessionLoad('beta');
duplicateCurrentCron();
const afterSwitchThenDuplicate = {
  formsRendered: calls.renderCronForm.length,
  cronIsDuplicate: _cronIsDuplicate,
  preForm: _cronPreFormDetail ? _cronPreFormDetail.id : null,
  body: els.taskDetailBody.innerHTML,
};
// (b) Duplicate form already open for the Alpha job, then Back to a Beta session.
S.activeProfile = 'alpha';
showAlphaJob();
duplicateCurrentCron();
const formOpenBefore = { formsRendered: calls.renderCronForm.length, body: els.taskDetailBody.innerHTML,
  cronIsDuplicate: _cronIsDuplicate };
await _switchProfileForSessionLoad('beta');
process.stdout.write(JSON.stringify({
  afterSwitchThenDuplicate,
  formOpenBefore,
  afterSwitchWithOpenForm: cronSnapshot(),
}));
"""))
    a = result["afterSwitchThenDuplicate"]
    assert a["formsRendered"] == 0, "Duplicate cloned the previous profile's job after the switch"
    assert a["cronIsDuplicate"] is False
    assert a["preForm"] is None
    assert "ALPHA-ONLY PROMPT" not in a["body"]
    # precondition for (b): the Alpha duplicate form really was open
    assert result["formOpenBefore"]["formsRendered"] == 1
    assert "ALPHA-ONLY PROMPT" in result["formOpenBefore"]["body"]
    assert result["formOpenBefore"]["cronIsDuplicate"] is True
    b = result["afterSwitchWithOpenForm"]
    assert b["activeProfile"] == "beta"
    assert "ALPHA-ONLY PROMPT" not in b["detailBody"], "Alpha duplicate form survived the switch"
    assert b["cronIsDuplicate"] is False
    assert b["cronPreFormDetail"] is None
    assert b["currentCronDetail"] is None


def test_session_load_switch_reloads_only_a_visible_tasks_panel_and_no_memory_or_skills():
    """Whatever panel is visible, the session-load switch drops the previous
    profile's Tasks state (and the Skills cache unless Skills is visible), but
    the only panel reload it starts is loadCrons for a VISIBLE Tasks panel.
    Memory/Skills (and the other panels) are not reloaded and issue no
    network request on this path."""
    result = _run_node(_script(r"""
const out = {};
for (const panel of ['chat', 'memory', 'skills', 'kanban', 'profiles', 'workspaces', 'tasks']) {
  S.activeProfile = 'alpha';
  _currentPanel = panel;
  showAlphaJob();
  _editingCronId = 'alpha-job';
  _skillsData = [{ name:'alpha-skill' }];
  for (const k of ['loadCrons', 'loadSkills', 'loadMemory', 'loadKanban', 'loadProfilesPanel', 'loadWorkspacesPanel']) calls[k] = 0;
  calls.apiUrls.length = 0;
  await _switchProfileForSessionLoad('beta');
  await _tick();
  out[panel] = { snap: cronSnapshot(), apiUrls: calls.apiUrls.slice(),
    loaders: { loadCrons: calls.loadCrons, loadSkills: calls.loadSkills, loadMemory: calls.loadMemory,
      loadKanban: calls.loadKanban, loadProfilesPanel: calls.loadProfilesPanel,
      loadWorkspacesPanel: calls.loadWorkspacesPanel } };
}
process.stdout.write(JSON.stringify(out));
"""))
    for panel, row in result.items():
        snap = row["snap"]
        assert snap["activeProfile"] == "beta", panel
        assert snap["currentCronDetail"] is None, f"{panel}: previous profile's cron detail survived"
        assert snap["editingCronId"] is None, panel
        # A visible Skills panel keeps its cache (its controls re-render from
        # it); every other panel drops it so the next Skills open refetches.
        assert snap["skillsDataIsNull"] is (panel != "skills"), f"{panel}: wrong Skills cache handling"
        assert row["apiUrls"] == ["/api/profile/switch"], f"{panel}: unexpected network on the session-load path"
        expected = {k: 0 for k in row["loaders"]}
        if panel == "tasks":
            expected["loadCrons"] = 1
        assert row["loaders"] == expected, f"{panel}: wrong panel reloads {row['loaders']}"


def test_skills_opened_after_session_load_switch_refetch_for_the_new_profile():
    """The Skills cache is dropped (not refreshed) on the session-load path,
    so the next Skills open refetches under the new profile instead of
    re-rendering the previous profile's cached list."""
    result = _run_node(_script(r"""
_currentPanel = 'chat';
await loadSkills();                       // Alpha's list gets cached
const before = { rendered: calls.renderedSkills, cached: _skillsData.map(s => s.name) };
calls.apiUrls.length = 0;
await _switchProfileForSessionLoad('beta');
await _tick();
const afterSwitch = calls.apiUrls.slice();
_currentPanel = 'skills';
await loadSkills();                       // what switchPanel('skills') runs
process.stdout.write(JSON.stringify({ before, afterSwitch, apiUrls: calls.apiUrls,
  rendered: calls.renderedSkills, cached: _skillsData.map(s => s.name) }));
""", real_skills=True))
    assert result["before"]["cached"] == ["alpha-skill"]
    assert result["afterSwitch"] == ["/api/profile/switch"], "Skills was fetched on the session-load path"
    assert result["apiUrls"] == ["/api/profile/switch", "/api/skills"]
    assert result["rendered"] == ["beta-skill"], "Skills re-rendered the previous profile's cached list"
    assert result["cached"] == ["beta-skill"]


def test_visible_skills_panel_keeps_working_controls_through_session_load_switch():
    """No Skills reload runs on this path, so a VISIBLE Skills panel keeps its
    cache: search re-renders the same list instead of going inert, as on
    master (the stale display itself is the loader-ownership follow-up)."""
    result = _run_node(_script(r"""
_currentPanel = 'skills';
await loadSkills();                       // Alpha's list rendered and cached
calls.renderedSkills = null;
calls.apiUrls.length = 0;
await _switchProfileForSessionLoad('beta');
await _tick();
filterSkills();
process.stdout.write(JSON.stringify({ apiUrls: calls.apiUrls, rendered: calls.renderedSkills,
  cached: _skillsData && _skillsData.map(s => s.name) }));
""", real_skills=True))
    assert result["apiUrls"] == ["/api/profile/switch"]
    assert result["cached"] == ["alpha-skill"]
    assert result["rendered"] == ["alpha-skill"], "Skills search went inert after the switch"


def test_in_flight_tasks_reload_cannot_publish_after_returning_to_previous_profile():
    """A -> B (session load, Tasks visible: B's loadCrons in flight), Tasks
    hidden, back to A with Tasks hidden (session load, then the picker). When
    B's late /api/crons reply lands it must not publish B's writable rows
    under A: the shared reset retires in-flight loads (#6767 generation)."""
    result = _run_node(_script(r"""
_currentPanel = 'tasks';
cronDeferred.beta = true;
await _switchProfileForSessionLoad('beta');      // starts Beta's loadCrons (deferred)
await _tick();
const betaReply = cronDeferred.beta;
_currentPanel = 'chat';                          // user leaves Tasks
await _switchProfileForSessionLoad('alpha');     // Tasks hidden: no reload
if (typeof betaReply === 'function') betaReply({ jobs:[{ id:'beta-job', name:'beta', profile:'beta', prompt:'BETA-ONLY' }] });
await _tick();
const sessionLoadBack = { active: S.activeProfile, list: _cronList.map(j => j.id) };
// Same shape, but back to Alpha through the canonical picker switch.
_cronList = [];
S.activeProfile = 'alpha';
_currentPanel = 'tasks';
cronDeferred.beta = true;
await _switchProfileForSessionLoad('beta');
await _tick();
const betaReply2 = cronDeferred.beta;
_currentPanel = 'chat';
const pickerOk = await switchToProfile('alpha');
if (typeof betaReply2 === 'function') betaReply2({ jobs:[{ id:'beta-job', name:'beta', profile:'beta', prompt:'BETA-ONLY' }] });
await _tick();
process.stdout.write(JSON.stringify({ sessionLoadBack, pickerOk,
  betaRequestIssued: typeof betaReply === 'function' && typeof betaReply2 === 'function',
  pickerBack: { active: S.activeProfile, list: _cronList.map(j => j.id) } }));
""", canonical=True, real_crons=True))
    assert result["betaRequestIssued"] is True, "visible Tasks was not reloaded for Beta"
    assert result["sessionLoadBack"]["active"] == "alpha"
    assert "beta-job" not in result["sessionLoadBack"]["list"], "Beta's late Tasks reply published under Alpha"
    assert result["pickerOk"] is True
    assert result["pickerBack"]["active"] == "alpha"
    assert "beta-job" not in result["pickerBack"]["list"], "Beta's late Tasks reply published under Alpha (picker)"


def test_session_load_switch_does_not_wait_for_or_fail_on_panel_reload():
    """The panel reload must not delay or fail the session load: the switch
    returns while loadCrons is still pending, and a loadCrons rejection
    neither rejects the switch nor escapes as an unhandled rejection."""
    result = _run_node(_script(r"""
showAlphaJob();
loadCronsImpl = () => new Promise(() => {});           // never settles
let settled = false;
const p1 = _switchProfileForSessionLoad('beta').then(() => { settled = true; });
await _tick();
const pendingReloadDidNotBlock = settled;
await p1;
S.activeProfile = 'alpha';
showAlphaJob();
loadCronsImpl = async () => { throw new Error('cron list failed'); };
let rejected = null;
try { await _switchProfileForSessionLoad('beta'); } catch (e) { rejected = String(e && e.message); }
await _tick();
process.stdout.write(JSON.stringify({ pendingReloadDidNotBlock, rejected,
  activeProfile: S.activeProfile, loadCrons: calls.loadCrons, detail: _currentCronDetail }));
"""))
    assert result["pendingReloadDidNotBlock"] is True
    assert result["rejected"] is None
    assert result["activeProfile"] == "beta"
    assert result["detail"] is None


def test_session_load_switch_resets_even_if_list_render_fails_after_accept():
    """Once the server accepted the switch the panel state belongs to the new
    profile, so a later session-list render failure must not skip the reset."""
    result = _run_node(_script(r"""
showAlphaJob();
_editingCronId = 'alpha-job';
renderSessionListImpl = async () => { throw new Error('list failed'); };
let rejected = null;
try { await _switchProfileForSessionLoad('beta'); } catch (e) { rejected = String(e && e.message); }
process.stdout.write(JSON.stringify({ rejected, snap: cronSnapshot() }));
"""))
    assert result["rejected"] == "list failed"
    assert result["snap"]["activeProfile"] == "beta"
    assert result["snap"]["currentCronDetail"] is None
    assert result["snap"]["editingCronId"] is None


def test_failed_session_load_switch_keeps_previous_profile_tasks_state():
    """A refused switch leaves the previous profile active, so its Tasks
    state must stay intact and no reload fires."""
    result = _run_node(_script(r"""
showAlphaJob();
_editingCronId = 'alpha-job';
_skillsData = [{ name:'alpha-skill' }];
switchMode = 'fail';
let rejected = null;
try { await _switchProfileForSessionLoad('beta'); } catch (e) { rejected = String(e && e.message); }
process.stdout.write(JSON.stringify({ rejected, snap: cronSnapshot() }));
"""))
    assert result["rejected"] == "switch refused"
    snap = result["snap"]
    assert snap["activeProfile"] == "alpha"
    assert snap["currentCronDetail"] == "alpha-job"
    assert snap["editingCronId"] == "alpha-job"
    assert snap["skillsDataIsNull"] is False
    assert snap["loadCrons"] == 0


def test_canonical_picker_switch_and_session_load_switch_reset_tasks_identically():
    """Parity: from the same Alpha Tasks state, the canonical picker switch and
    the session-load switch must leave identical synchronous profile-scoped
    state (Tasks detail/edit/duplicate flags, cross-profile view, Skills
    cache), each reloading the visible Tasks list exactly once."""
    result = _run_node(_script(r"""
function prime(){
  S.activeProfile = 'alpha'; S.session = null;
  _currentPanel = 'tasks'; _showAllCronProfiles = true; _cronOtherProfileCount = 4;
  showAlphaJob();
  _editingCronId = 'alpha-job'; _cronPreFormDetail = { ...ALPHA_JOB }; _cronIsDuplicate = true;
  _skillsData = [{ name:'alpha-skill' }];
  calls.loadCrons = 0;
}
prime();
const canonicalOk = await switchToProfile('beta');
const canonical = cronSnapshot();
prime();
await _switchProfileForSessionLoad('beta');
const sessionLoad = cronSnapshot();
process.stdout.write(JSON.stringify({ canonicalOk, canonical, sessionLoad }));
""", canonical=True))
    assert result["canonicalOk"] is True
    assert result["canonical"]["loadCrons"] == 1
    assert result["canonical"]["currentCronDetail"] is None
    assert result["canonical"]["skillsDataIsNull"] is True
    assert result["sessionLoad"] == result["canonical"]


def test_session_load_switch_reuses_the_shared_synchronous_reset():
    """Contract: one shared synchronous reset, not a copy of cron lines; the
    canonical switch still runs it through _profileSwitchPanelLoad (awaited
    once, Skills cache dropped once); the session-load path never calls the
    full visible-panel reload nor the Memory/Skills loaders."""
    alt = _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad")
    assert "_resetProfileScopedPanelState();" in alt
    assert "loadCrons()" in alt
    for forbidden in ("_profileSwitchPanelLoad(", "loadMemory(", "loadSkills(", "await loadCrons"):
        assert forbidden not in alt, f"{forbidden} on the session-load path"
    for copied in ("_editingCronId", "_cronIsDuplicate", "_cronPreFormDetail", "_clearCronDetail"):
        assert copied not in alt, f"{copied} copied into sessions.js instead of reusing the shared reset"
    reset = _extract_function(PANELS_JS, "_resetProfileScopedPanelState")
    assert "++_cronLoadGeneration;" in reset
    loader = _extract_function(PANELS_JS, "_profileSwitchPanelLoad")
    assert "async function _profileSwitchPanelLoad(){" in loader
    assert loader.index("_resetProfileScopedPanelState();") < loader.index("await loadSkills()")
    canonical = _extract_function(PANELS_JS, "switchToProfile")
    assert canonical.count("await _profileSwitchPanelLoad();") == 1
    assert canonical.count("_skillsData = null;") == 1


def test_canonical_panel_load_still_resets_and_reloads_exactly_the_visible_panel():
    """Picker path unchanged: _profileSwitchPanelLoad resets the Tasks state
    and reloads exactly the visible panel's loader once, for every panel."""
    result = _run_node(_script(r"""
const out = {};
for (const panel of ['chat', 'skills', 'memory', 'tasks', 'kanban', 'profiles', 'workspaces']) {
  _currentPanel = panel;
  showAlphaJob();
  _editingCronId = 'alpha-job'; _cronIsDuplicate = true; _showAllCronProfiles = true;
  for (const k of ['loadCrons', 'loadSkills', 'loadMemory', 'loadKanban', 'loadProfilesPanel', 'loadWorkspacesPanel']) calls[k] = 0;
  await _profileSwitchPanelLoad();
  out[panel] = { detail: _currentCronDetail, editing: _editingCronId, dup: _cronIsDuplicate,
    showAll: _showAllCronProfiles,
    loaders: { loadSkills: calls.loadSkills, loadMemory: calls.loadMemory, loadCrons: calls.loadCrons,
      loadKanban: calls.loadKanban, loadProfilesPanel: calls.loadProfilesPanel,
      loadWorkspacesPanel: calls.loadWorkspacesPanel } };
}
process.stdout.write(JSON.stringify(out));
"""))
    visible_loader = {"skills": "loadSkills", "memory": "loadMemory", "tasks": "loadCrons",
                      "kanban": "loadKanban", "profiles": "loadProfilesPanel",
                      "workspaces": "loadWorkspacesPanel"}
    for panel, row in result.items():
        assert row["detail"] is None and row["editing"] is None, panel
        assert row["dup"] is False and row["showAll"] is False, panel
        expected = {k: 0 for k in row["loaders"]}
        if panel in visible_loader:
            expected[visible_loader[panel]] = 1
        assert row["loaders"] == expected, f"{panel}: {row['loaders']}"
