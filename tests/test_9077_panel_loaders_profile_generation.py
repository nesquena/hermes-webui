"""Regression coverage for #9077.

The Memory and Skills panels (plus the Workspaces panel render, the Kanban
board and the cron gateway notice) load per-profile data through ``api()``.
Before this fix a reply was published unconditionally, so a profile switch that
left the panel hidden — no newer load to supersede the old one — let the
previous profile's response land under the new active profile: its memory text
in ``_memoryData`` / the Memory detail pane, its skill list in ``_skillsData``,
its board in ``_kanbanBoard``, or its failure message painted into the panel.

The fix gives each loader a generation counter plus an owner-profile check
(the ``_cronLoadGeneration`` shape from #6767) and advances every counter from
the shared ``_resetProfileScopedPanelState()`` that both switch paths already
run (#9069/#9080), so an in-flight load for the profile being left behind is
retired at the switch.

These tests drive the REAL helpers (``loadMemory``, ``loadSkills``,
``submitMemorySave``, ``loadWorkspacesPanel``, ``loadKanban``,
``loadCronGatewayNotice``, ``_resetProfileScopedPanelState``,
``_retireProfileScopedPanelLoads`` and sessions.js''
``_switchProfileForSessionLoad``) in node with stubbed collaborators and
deferred HTTP replies.
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
    """Like ``_extract_function`` but empty when absent, so a RED run on the
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
function makeEl(){
  const el = { textContent:'', value:'', disabled:false, style:{ display:'' }, _kids:[],
    _html:'',
    classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    appendChild(child){ this._kids.push((child && child.innerHTML) || ''); },
    querySelector(){ return null; },
    setAttribute(){}, removeAttribute(){} };
  Object.defineProperty(el, 'innerHTML', {
    get(){ return el._html; },
    set(v){ el._html = v; if (v === '') el._kids.length = 0; },
  });
  return el;
}
const els = {};
for (const id of ['memoryPanel','memoryDetailBody','skillsList','memEditContent','memEditError',
  'cronList','cronGatewayNotice','workspacesPanel','kanbanBoard','kanbanList','kanbanStats',
  'taskDetailTitle','taskDetailBody','taskDetailEmpty']) els[id] = makeEl();
function $(id){ return els[id] || null; }
const window = {};
const localStorage = { removeItem(){} };
const S = { activeProfile:'alpha', activeProfileIsDefault:false, session:null, messages:[] };

// ── panels.js module state ──────────────────────────────────────────────
let _currentPanel = 'chat';
let _memoryData = null;
let _currentMemorySection = null;
let _memoryMode = 'read';
let _memoryLoadGeneration = 0;
let _skillsData = null;
let _skillsLoadGeneration = 0;
let _collapsedCats = new Set();
let _workspacePanelLoadGeneration = 0;
let _kanbanLoadGeneration = 0;
let _cronGatewayNoticeGeneration = 0;
let _workspaceList = [];
let _kanbanBoard = null;
let _kanbanCurrentBoard = null;
let _kanbanLatestEventId = 0;
let _cronLoadGeneration = 0;
let _cronDetailRefreshKey = null;
let _stopCronWatchCalls = 0;
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 3;
let _cronPreFormDetail = null;
let _editingCronId = null;
let _cronIsDuplicate = false;
let _currentCronDetail = null;
let _currentCronDetailKey = '';
let _cronMode = 'empty';
let _cronList = [];
let _cronSkillsCache = null;
let _sessionListSkeletonActive = false;

// ── recorded collaborators ──────────────────────────────────────────────
const calls = { apiUrls:[], memoryDetailPaints:[], skillsRendered:[], memoryWrite:[],
  workspaceRenders:[], kanbanRenders:0, kanbanBoards:0, noticeWrites:0, loadCrons:0,
  renderedWorkspacePaths:null };

const MEMORY_SECTIONS = [{ key:'memory', iconKey:'brain' }, { key:'user', iconKey:'user' }];
function _memorySectionLabel(s){ return s.key; }
function _memorySectionPath(){ return ''; }
function _memorySectionMeta(){ return { readOnly:false }; }
function openMemorySection(){}
async function loadNotesSources(){}
function li(){ return ''; }
function esc(v){ return String(v); }
function t(key){ return key; }
function showToast(){}
function _renderMemoryDetail(section){
  const memory = _memoryData ? _memoryData.memory : null;
  calls.memoryDetailPaints.push({ section, memory });
  els.memoryDetailBody.innerHTML = memory || '';
}
function renderSkills(list){
  calls.skillsRendered.push(list.map(s => s.name));
  els.skillsList.innerHTML = list.map(s => s.name).join(',');
}
function renderWorkspacesPanel(list){
  calls.renderedWorkspacePaths = list.map(w => w.path);
  calls.workspaceRenders.push(calls.renderedWorkspacePaths);
}
let loadWorkspaceListImpl = async () => ({ workspaces:[{ name:'ws', path:'/w/' + S.activeProfile }], last:'' });
async function loadWorkspaceList(){ return loadWorkspaceListImpl(); }
async function loadKanbanBoards(){ calls.kanbanBoards++; }
function _kanbanBoardQuery(){ return ''; }
function _kanbanApplyConfigDefaults(){}
function _kanbanCurrentFilters(){ return {}; }
function _kanbanSetSelectOptions(){}
function _kanbanStartPolling(){}
function _kanbanColumnLabel(status){ return status; }
function _kanbanUnavailableHtml(e){ return 'UNAVAILABLE: ' + ((e && e.message) || e); }
function _kanbanRenderBoard(){
  calls.kanbanRenders++;
  const tasks = _kanbanBoard && _kanbanBoard.columns && _kanbanBoard.columns[0]
    ? (_kanbanBoard.columns[0].tasks || []) : [];
  els.kanbanBoard.innerHTML = tasks.map(task => task.title).join(',');
}
function _cronGatewayNoticeHtml(status){
  return status && status.gateway_enabled ? '<div>GATEWAY-ON</div>' : '';
}
async function loadCrons(){ calls.loadCrons++; }
function _stopCronWatch(){ _stopCronWatchCalls++; }
function _setCronHeaderButtons(){}

// ── HTTP: replies are per active profile at REQUEST time ────────────────
const holds = {};
function holdNext(url){ holds[url] = { pending:true, resolve:null, reject:null }; }
function _takeHold(url){
  const h = holds[url];
  if (!h || !h.pending) return null;
  h.pending = false;
  return new Promise((resolve, reject) => { h.resolve = resolve; h.reject = reject; });
}
const payloads = {
  '/api/memory': prof => ({ memory: prof.toUpperCase() + '-ONLY', external_notes_enabled:false }),
  '/api/skills': prof => ({ skills:[{ name: prof + '-skill', category:'core' }] }),
  '/api/kanban/config': () => ({ columns:['todo'] }),
  '/api/kanban/assignees': () => ({ assignees:[] }),
  '/api/kanban/board': prof => ({ columns:[{ name:'todo', tasks:[{ id: prof + '-task',
    title: prof.toUpperCase() + '-ONLY' }] }] }),
  '/api/kanban/stats': prof => ({ by_status:{ todo: prof === 'alpha' ? 1 : 7 } }),
  '/api/gateway/status': prof => ({ gateway_enabled: prof === 'alpha' }),
};
async function api(url, opts){
  calls.apiUrls.push(url);
  if (url === '/api/memory/write') {
    const body = JSON.parse(opts.body);
    calls.memoryWrite.push({ section: body.section, content: body.content, profile: S.activeProfile });
    return { ok:true };
  }
  if (url === '/api/profile/switch') {
    return { active: JSON.parse(opts.body).name, is_default:false };
  }
  const prof = S.activeProfile;   // owner captured when the request is issued
  const held = _takeHold(url);
  if (held) return held;
  if (payloads[url]) return payloads[url](prof);
  throw new Error('unexpected api ' + url);
}
// sessions.js / canonical-switch collaborators (no-ops)
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _invalidateScriptsForProfileSwitch(){}
function _refreshScriptsAfterProfileSwitch(){}
function _beginReasoningProfileSwitch(){ return Promise.resolve(); }
function _endReasoningProfileSwitch(){}
function _resetCronUnreadForProfileSwitch(){}
function _clearPersistedModelState(){}
function refreshProfileTransitionReasoningChip(){}
function startGatewaySSE(){}
function syncTopbar(){}
function renderSessionList(){ return Promise.resolve(); }
function renderSessionListFromCache(){}
function _tick(){ return new Promise(r => setTimeout(r, 5)); }
const document = { title:'', createElement(){ return makeEl(); },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  addEventListener(){}, getElementById(){ return null; } };
"""


def _real_helpers() -> str:
    parts = [
        _extract_function(PANELS_JS, "_clearCronDetail"),
        _extract_optional(PANELS_JS, "_resetProfileScopedPanelState"),
        _extract_optional(PANELS_JS, "_retireProfileScopedPanelLoads"),
        _extract_function(PANELS_JS, "loadMemory"),
        _extract_function(PANELS_JS, "submitMemorySave"),
        _extract_function(PANELS_JS, "loadSkills"),
        _extract_function(PANELS_JS, "loadWorkspacesPanel"),
        _extract_function(PANELS_JS, "loadKanban"),
        _extract_function(PANELS_JS, "loadKanbanStats"),
        _extract_function(PANELS_JS, "loadCronGatewayNotice"),
        _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad"),
    ]
    return "\n".join(parts)


def _script(body: str) -> str:
    return (
        _HARNESS
        + _real_helpers()
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


def test_memory_keeps_the_active_profile_when_the_previous_profile_replies_last():
    """Alpha's Memory is loaded, Beta's load is issued and held, the user goes
    back to Alpha (whose load publishes), and only then does Beta's reply land.
    It must not touch the cache, the detail pane or the section list."""
    result = _run_node(_script(r"""
const out = {};
S.activeProfile = 'alpha';
_currentPanel = 'memory';
_currentMemorySection = 'memory';   // panel opened on a section, as in the app
await loadMemory();
out.afterAlpha = { data: _memoryData.memory, detail: els.memoryDetailBody.innerHTML,
  panelKids: els.memoryPanel._kids.length };
// Beta: Memory hidden across the switch, so nothing supersedes the load Beta
// starts when the user opens the panel there.
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'memory';
holdNext('/api/memory');
const betaLoad = loadMemory();
await _tick();
const betaRequestIssued = !!(holds['/api/memory'] && !holds['/api/memory'].pending);
// Back to Alpha, Memory hidden again, then Alpha's own reply publishes first.
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'memory';
await loadMemory();
out.beforeStaleReply = { active: S.activeProfile, data: _memoryData.memory,
  detail: els.memoryDetailBody.innerHTML, panelKids: els.memoryPanel._kids.length };
holds['/api/memory'].resolve({ memory:'BETA-ONLY', external_notes_enabled:false });
await betaLoad;
await _tick();
out.betaRequestIssued = betaRequestIssued;
out.afterStaleReply = { active: S.activeProfile, data: _memoryData.memory,
  detail: els.memoryDetailBody.innerHTML, panelKids: els.memoryPanel._kids.length,
  paints: calls.memoryDetailPaints.map(p => p.memory) };
process.stdout.write(JSON.stringify(out));
"""))
    assert result["afterAlpha"] == {"data": "ALPHA-ONLY", "detail": "ALPHA-ONLY", "panelKids": 2}
    assert result["betaRequestIssued"] is True, "Beta's Memory load never reached the server"
    assert result["beforeStaleReply"]["data"] == "ALPHA-ONLY"
    assert result["beforeStaleReply"]["detail"] == "ALPHA-ONLY"
    after = result["afterStaleReply"]
    assert after["active"] == "alpha"
    assert after["data"] == "ALPHA-ONLY", "Beta's late /api/memory reply overwrote Alpha's cache"
    assert after["detail"] == "ALPHA-ONLY", "Beta's late reply repainted Alpha's Memory detail"
    assert after["panelKids"] == result["beforeStaleReply"]["panelKids"]
    assert "BETA-ONLY" not in after["paints"], "Beta's late reply reached the Memory renderer"


def test_skills_keeps_the_active_profile_when_the_previous_profile_replies_last():
    """Same A -> B -> A sequence for Skills: Beta's late /api/skills reply must
    not replace Alpha's cached list or repaint the panel."""
    result = _run_node(_script(r"""
const out = {};
S.activeProfile = 'alpha';
_currentPanel = 'skills';
await loadSkills();
out.afterAlpha = { cached: _skillsData.map(s => s.name), dom: els.skillsList.innerHTML };
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'skills';
holdNext('/api/skills');
const betaLoad = loadSkills();
await _tick();
const betaRequestIssued = !!(holds['/api/skills'] && !holds['/api/skills'].pending);
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'skills';
await loadSkills();
out.beforeStaleReply = { cached: _skillsData.map(s => s.name), dom: els.skillsList.innerHTML };
holds['/api/skills'].resolve({ skills:[{ name:'beta-skill', category:'core' }] });
await betaLoad;
await _tick();
out.betaRequestIssued = betaRequestIssued;
out.afterStaleReply = { active: S.activeProfile, cached: _skillsData.map(s => s.name),
  dom: els.skillsList.innerHTML, rendered: calls.skillsRendered };
process.stdout.write(JSON.stringify(out));
"""))
    assert result["afterAlpha"] == {"cached": ["alpha-skill"], "dom": "alpha-skill"}
    assert result["betaRequestIssued"] is True, "Beta's Skills load never reached the server"
    assert result["beforeStaleReply"] == {"cached": ["alpha-skill"], "dom": "alpha-skill"}
    after = result["afterStaleReply"]
    assert after["cached"] == ["alpha-skill"], "Beta's late /api/skills reply replaced Alpha's cache"
    assert after["dom"] == "alpha-skill", "Beta's late reply repainted the Skills panel"
    assert ["beta-skill"] not in after["rendered"], f"beta list rendered: {after['rendered']}"


def test_switch_retires_a_memory_load_pending_for_the_profile_being_left():
    """The switch's shared reset must retire an in-flight Memory load even
    though no panel reload runs: its reply then cannot publish at all."""
    result = _run_node(_script(r"""
S.activeProfile = 'alpha';
_currentPanel = 'memory';
holdNext('/api/memory');
const alphaLoad = loadMemory();
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
const gensAfterSwitch = { memory:_memoryLoadGeneration, skills:_skillsLoadGeneration,
  workspaces:_workspacePanelLoadGeneration, kanban:_kanbanLoadGeneration,
  notice:_cronGatewayNoticeGeneration, crons:_cronLoadGeneration };
holds['/api/memory'].resolve({ memory:'ALPHA-ONLY', external_notes_enabled:false });
await alphaLoad;
await _tick();
process.stdout.write(JSON.stringify({ gensAfterSwitch, active: S.activeProfile,
  data: _memoryData, detail: els.memoryDetailBody.innerHTML,
  paints: calls.memoryDetailPaints.length, panelKids: els.memoryPanel._kids.length }));
"""))
    gens = result["gensAfterSwitch"]
    assert gens["memory"] >= 1 and gens["skills"] >= 1 and gens["workspaces"] >= 1, gens
    assert gens["kanban"] >= 1 and gens["notice"] >= 1 and gens["crons"] >= 1, gens
    assert result["active"] == "beta"
    assert result["data"] is None, "the retired Memory reply published under Beta"
    assert result["detail"] == ""
    assert result["paints"] == 0
    assert result["panelKids"] == 0


def test_delayed_rejections_do_not_paint_the_current_profiles_panels():
    """A Memory or Skills request issued for Beta and rejected after the switch
    back to Alpha must not replace Alpha's panel with an error box."""
    result = _run_node(_script(r"""
const out = {};
S.activeProfile = 'alpha';
_currentPanel = 'memory';
_currentMemorySection = 'memory';
await loadMemory();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'memory';
holdNext('/api/memory');
const betaMemory = loadMemory();
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'memory';
await loadMemory();
holds['/api/memory'].reject(new Error('memory backend exploded'));
let memoryRejected = null;
try { await betaMemory; } catch (e) { memoryRejected = String(e && e.message); }
await _tick();
out.memory = { rejected: memoryRejected, panel: els.memoryPanel.innerHTML,
  detail: els.memoryDetailBody.innerHTML, data: _memoryData.memory };

S.activeProfile = 'alpha';
_currentPanel = 'skills';
await loadSkills();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'skills';
holdNext('/api/skills');
const betaSkills = loadSkills();
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'skills';
await loadSkills();
out.beforeSkillRejection = { dom: els.skillsList.innerHTML, cached: _skillsData.map(s => s.name) };
holds['/api/skills'].reject(new Error('skills backend exploded'));
let skillsRejected = null;
try { await betaSkills; } catch (e) { skillsRejected = String(e && e.message); }
await _tick();
out.skills = { rejected: skillsRejected, dom: els.skillsList.innerHTML,
  cached: _skillsData.map(s => s.name) };
process.stdout.write(JSON.stringify(out));
"""))
    assert result["memory"]["rejected"] is None, "the retired rejection escaped loadMemory"
    assert "exploded" not in result["memory"]["panel"], "stale Memory rejection painted the panel"
    assert result["memory"]["detail"] == "ALPHA-ONLY"
    assert result["memory"]["data"] == "ALPHA-ONLY"
    assert result["beforeSkillRejection"] == {"dom": "alpha-skill", "cached": ["alpha-skill"]}
    assert result["skills"]["rejected"] is None, "the retired rejection escaped loadSkills"
    assert "exploded" not in result["skills"]["dom"], "stale Skills rejection painted the panel"
    assert result["skills"]["dom"] == "alpha-skill"
    assert result["skills"]["cached"] == ["alpha-skill"]


def test_memory_save_after_a_dropped_reply_writes_the_active_profiles_content():
    """After the guard drops Beta's reply, the Memory form still shows Alpha's
    text, and Save posts exactly that under the active profile."""
    result = _run_node(_script(r"""
S.activeProfile = 'alpha';
_currentPanel = 'memory';
_currentMemorySection = 'memory';
await loadMemory();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'memory';
holdNext('/api/memory');
const betaLoad = loadMemory();
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'memory';
await loadMemory();
holds['/api/memory'].resolve({ memory:'BETA-ONLY', external_notes_enabled:false });
await betaLoad;
// The user edits what the panel shows and saves it.
_currentMemorySection = 'memory';
_memoryMode = 'edit';
els.memEditContent.value = _memoryData.memory + '-edited';
await submitMemorySave();
process.stdout.write(JSON.stringify({ active: S.activeProfile, write: calls.memoryWrite,
  data: _memoryData.memory, detail: els.memoryDetailBody.innerHTML }));
"""))
    assert result["active"] == "alpha"
    assert result["write"] == [
        {"section": "memory", "content": "ALPHA-ONLY-edited", "profile": "alpha"}
    ], f"Save did not post the active profile's content: {result['write']}"
    assert result["data"] == "ALPHA-ONLY"


def test_workspace_panel_render_from_the_previous_profile_is_dropped():
    """A held /api/workspaces reply issued for Alpha must not repaint the
    Workspaces panel after the switch to Beta."""
    result = _run_node(_script(r"""
S.activeProfile = 'alpha';
_currentPanel = 'workspaces';
let release;
loadWorkspaceListImpl = () => new Promise(r => { release = r; });
const pending = loadWorkspacesPanel();
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
release({ workspaces:[{ name:'alpha-ws', path:'/w/alpha' }], last:'' });
await pending;
await _tick();
process.stdout.write(JSON.stringify({ active: S.activeProfile, renders: calls.workspaceRenders }));
"""))
    assert result["active"] == "beta"
    assert result["renders"] == [], "Alpha's late workspaces reply repainted Beta's panel"


def test_cron_gateway_notice_from_the_previous_profile_is_dropped():
    """/api/gateway/status is display-only but still per profile: Beta must not
    get Alpha's notice, and Alpha's notice must not survive as a stale paint."""
    result = _run_node(_script(r"""
els.cronGatewayNotice.innerHTML = 'ALPHA-NOTICE';
S.activeProfile = 'alpha';
holdNext('/api/gateway/status');
const pending = loadCronGatewayNotice();
await _tick();
await _switchProfileForSessionLoad('beta');
holds['/api/gateway/status'].resolve({ gateway_enabled:true });
await pending;
await _tick();
process.stdout.write(JSON.stringify({ active: S.activeProfile,
  notice: els.cronGatewayNotice.innerHTML }));
"""))
    assert result["active"] == "beta"
    assert result["notice"] == "ALPHA-NOTICE", "stale gateway status repainted the notice box"


def test_kanban_board_from_the_previous_profile_is_dropped_and_its_error_unpainted():
    """Beta's held /api/kanban/board reply (success or failure) must neither
    replace Alpha's board nor paint the unavailable panel under Alpha."""
    result = _run_node(_script(r"""
const out = {};
S.activeProfile = 'alpha';
_currentPanel = 'kanban';
await loadKanban(true);
out.afterAlpha = { dom: els.kanbanBoard.innerHTML, board: _kanbanBoard.columns[0].tasks[0].title };
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'kanban';
holdNext('/api/kanban/board');
const betaLoad = loadKanban(true);
await _tick();
const betaRequestIssued = !!(holds['/api/kanban/board'] && !holds['/api/kanban/board'].pending);
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'kanban';
await loadKanban(true);
out.beforeStale = { dom: els.kanbanBoard.innerHTML, board: _kanbanBoard.columns[0].tasks[0].title };
holds['/api/kanban/board'].resolve({ columns:[{ name:'todo', tasks:[{ id:'beta-task', title:'BETA-ONLY' }] }] });
await betaLoad;
await _tick();
out.betaRequestIssued = betaRequestIssued;
out.afterStale = { active: S.activeProfile, dom: els.kanbanBoard.innerHTML,
  board: _kanbanBoard.columns[0].tasks[0].title };
// Same shape with a failure instead of a reply.
holdNext('/api/kanban/board');
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
_currentPanel = 'kanban';
const betaFail = loadKanban(true);
await _tick();
_currentPanel = 'chat';
await _switchProfileForSessionLoad('alpha');
_currentPanel = 'kanban';
await loadKanban(true);
holds['/api/kanban/board'].reject(new Error('kanban backend exploded'));
let rejected = null;
try { await betaFail; } catch (e) { rejected = String(e && e.message); }
await _tick();
out.afterStaleFailure = { rejected, dom: els.kanbanBoard.innerHTML,
  list: els.kanbanList.innerHTML, board: _kanbanBoard.columns[0].tasks[0].title };
process.stdout.write(JSON.stringify(out));
"""))
    assert result["afterAlpha"] == {"dom": "ALPHA-ONLY", "board": "ALPHA-ONLY"}
    assert result["betaRequestIssued"] is True, "Beta's Kanban load never reached the server"
    assert result["beforeStale"] == {"dom": "ALPHA-ONLY", "board": "ALPHA-ONLY"}
    stale = result["afterStale"]
    assert stale["active"] == "alpha"
    assert stale["board"] == "ALPHA-ONLY", "Beta's late board reply replaced Alpha's board"
    assert stale["dom"] == "ALPHA-ONLY"
    failed = result["afterStaleFailure"]
    assert failed["rejected"] is None, "the retired rejection escaped loadKanban"
    assert "UNAVAILABLE" not in failed["dom"], "stale Kanban failure painted Alpha's board"
    assert "UNAVAILABLE" not in failed["list"]
    assert failed["board"] == "ALPHA-ONLY"


def test_every_guard_is_a_latest_wins_generation_check_and_the_reset_advances_it():
    """Contract: each loader captures a generation and an owner profile, checks
    both after every await and in its catch, and the shared switch reset bumps
    every counter (the #6767 shape, shared by both switch paths)."""
    loaders = {
        "loadMemory": ("_memoryLoadGeneration", "S.activeProfile"),
        "loadSkills": ("_skillsLoadGeneration", "S.activeProfile"),
        "loadWorkspacesPanel": ("_workspacePanelLoadGeneration", "S.activeProfile"),
        "loadKanban": ("_kanbanLoadGeneration", "S.activeProfile"),
        "loadCronGatewayNotice": ("_cronGatewayNoticeGeneration", "S.activeProfile"),
    }
    for name, (counter, owner) in loaders.items():
        src = _extract_function(PANELS_JS, name)
        assert f"++{counter}" in src, f"{name} does not take a new generation"
        assert owner in src, f"{name} does not check its owner profile"
        # loadWorkspacesPanel has no catch: loadWorkspaceList() swallows its own
        # failures and returns an empty list, so there is nothing to un-paint.
        body, catch = (src.rsplit("catch", 1) if "catch" in src else (src, ""))
        compares = f"!== {counter}" in body or f"!=={counter}" in body
        assert compares, f"{name} does not compare its generation\n{src}"
        if catch:
            stale_in_catch = (
                "stale()" in catch
                or f"!== {counter}" in catch
                or f"!=={counter}" in catch
            )
            assert stale_in_catch, f"{name}: the catch has no stale check\n{src}"
        # every extra await in the chain re-checks before publishing again
        if name in ("loadMemory", "loadKanban"):
            assert body.count("stale()") >= 2, f"{name}: publishes after an unchecked await\n{src}"
    reset = _extract_function(PANELS_JS, "_resetProfileScopedPanelState")
    assert "++_cronLoadGeneration;" in reset
    assert "_retireProfileScopedPanelLoads()" in reset
    retire = _extract_function(PANELS_JS, "_retireProfileScopedPanelLoads")
    for counter, _ in loaders.values():
        assert f"++{counter};" in retire, f"the switch reset does not retire {counter}"
    # loadKanbanStats rides on the current loadKanban generation
    stats = _extract_function(PANELS_JS, "loadKanbanStats")
    assert "const seq = _kanbanLoadGeneration;" in stats
    assert "!== _kanbanLoadGeneration" in stats


def test_reset_advances_every_panel_generation_on_the_shared_path():
    """Behavioural form of the contract above: running the shared reset alone
    (as the session-load path does) advances all five panel counters."""
    result = _run_node(_script(r"""
const before = { memory:_memoryLoadGeneration, skills:_skillsLoadGeneration,
  workspaces:_workspacePanelLoadGeneration, kanban:_kanbanLoadGeneration,
  notice:_cronGatewayNoticeGeneration, crons:_cronLoadGeneration };
_resetProfileScopedPanelState();
const after = { memory:_memoryLoadGeneration, skills:_skillsLoadGeneration,
  workspaces:_workspacePanelLoadGeneration, kanban:_kanbanLoadGeneration,
  notice:_cronGatewayNoticeGeneration, crons:_cronLoadGeneration };
process.stdout.write(JSON.stringify({ before, after }));
"""))
    for key, value in result["before"].items():
        assert result["after"][key] == value + 1, f"{key}: {result['after'][key]} != {value + 1}"
