"""Regression coverage for the #9077 follow-up review (2026-11-27T21:07:19Z).

Three behavioural gaps left by the accepted #9077 owner guards:

1. ``_scheduleKanbanRefresh`` started a fresh ``loadKanban(true)`` per SSE burst
   entry, and every load retires the one still in flight. During activity the
   board was repainted with the "Loading…" placeholder and the held reply was
   dropped, so the panel never settled. The scheduler must be single-flight with
   exactly one trailing re-run, and must keep the displayed board on screen.
2. ``static/boot.js`` resolves ``/api/profile/active`` asynchronously. A
   profile-scoped panel opened inside that window captured the placeholder owner
   ``default``; when the authoritative profile arrived the load was retired and
   the panel stayed on "Loading…" until a manual reload.
3. ``_switchProfileForSessionLoad`` reloaded only a visible Tasks panel and kept
   the Skills cache while Skills was visible. Back/Forward into another
   profile's session therefore left the panel on the previous profile's content
   (or on "Loading…") until a Save was refused.

These tests drive the REAL bodies extracted from ``static/panels.js``,
``static/sessions.js`` and ``static/boot.js`` with stubbed collaborators.
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
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    if src[max(0, start - 6) : start] == "async ":
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
    """Empty when absent, so the pre-fix run fails on behaviour, not on import."""
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
            timeout=60,
            cwd=str(REPO_ROOT),
        )
    finally:
        path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node failed: {result.stderr}\nstdout:\n{result.stdout}")
    return json.loads(result.stdout)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Kanban live-refresh scheduler: single-flight + one trailing re-run
# ─────────────────────────────────────────────────────────────────────────────

_KANBAN_HARNESS = r"""
'use strict';
function makeEl(){ return { textContent:'', innerHTML:'', value:'', disabled:false, style:{ display:'' },
  classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } } }; }
const els = { kanbanBoard: makeEl(), kanbanList: makeEl(), kanbanStats: makeEl() };
function $(id){ return els[id] || null; }
function esc(v){ return String(v); }
function t(key){ return key; }
const S = { activeProfile:'alpha' };

// ── panels.js module state (measured in static/panels.js) ────────────────
let _currentPanel = 'kanban';
let _kanbanBoard = null;
let _kanbanCurrentBoard = null;
let _kanbanLatestEventId = 0;
let _kanbanCurrentTaskId = null;
let _kanbanLoadGeneration = 0;
let _kanbanPollTimer = null;
let _kanbanEventSource = null;
let _kanbanEventSourceFailures = 0;
// Scheduler state (measured in static/panels.js next to _scheduleKanbanRefresh);
// the single-flight flags are what the fix adds there.
let _kanbanRefreshScheduled = false;
let _kanbanRefreshPendingTaskIds = new Set();
let _kanbanRefreshInFlight = false;
let _kanbanRefreshRerunPending = false;

// ── recorded collaborators ───────────────────────────────────────────────
const calls = { boardRequests:0, concurrent:0, maxConcurrent:0, renders:0, stats:0, tasks:0 };
const held = [];
function holdBoard(){
  return new Promise(resolve => { held.push(resolve); });
}
async function api(url){
  if (url.startsWith('/api/kanban/board')) {
    calls.boardRequests += 1;
    calls.concurrent += 1;
    calls.maxConcurrent = Math.max(calls.maxConcurrent, calls.concurrent);
    try { return await holdBoard(); }
    finally { calls.concurrent -= 1; }
  }
  if (url.startsWith('/api/kanban/config')) return { columns:[{ name:'Todo' }] };
  if (url.startsWith('/api/kanban/assignees')) return { assignees:[] };
  throw new Error('unexpected api ' + url);
}
async function loadKanbanBoards(){}
async function loadKanbanStats(){ calls.stats += 1; }
async function loadKanbanTask(id){ calls.tasks += 1; }
function _kanbanApplyConfigDefaults(){}
function _kanbanCurrentFilters(){ return {}; }
function _kanbanBoardQuery(){ return ''; }
function _kanbanSetSelectOptions(){}
function _kanbanColumnLabel(name){ return name; }
function _kanbanStartPolling(){}
function _kanbanRenderBoard(){
  calls.renders += 1;
  els.kanbanBoard.innerHTML = '<div class="board">rendered:' + calls.renders + '</div>';
}
const document = { querySelector(){ return null; } };
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
function releaseBoard(payload){
  const resolver = held.shift();
  if (!resolver) throw new Error('no held board request to release');
  resolver(payload);
}
const BOARD_TWO_TASKS = { columns:[{ name:'Todo', tasks:[{ id:'t1' }, { id:'t2' }] }] };
"""


def _kanban_script(body: str) -> str:
    parts = [
        _extract_function(PANELS_JS, "loadKanban"),
        _extract_function(PANELS_JS, "_scheduleKanbanRefresh"),
    ]
    return (
        _KANBAN_HARNESS
        + "\n".join(parts)
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


def test_second_refresh_during_in_flight_board_reply_keeps_the_board_rendered():
    """A burst entry while a board reply is held must neither repaint the
    "Loading…" placeholder over a displayed board nor start a second load."""
    body = r"""
_kanbanBoard = { columns:[{ name:'Todo', tasks:[{ id:'t1' }] }] };
els.kanbanBoard.innerHTML = '<div class="board">alpha-board</div>';
_scheduleKanbanRefresh([{ task_id:'t1' }]);
await sleep(350);
const firstBoardRequests = calls.boardRequests;
const innerWhileHeld = els.kanbanBoard.innerHTML;
// The user keeps working: more SSE events land while the first reply is held.
_scheduleKanbanRefresh([{ task_id:'t2' }]);
_scheduleKanbanRefresh([{ task_id:'t3' }]);
await sleep(350);
console.log(JSON.stringify({
  firstBoardRequests,
  boardRequestsWhileHeld: calls.boardRequests,
  maxConcurrent: calls.maxConcurrent,
  innerWhileHeld,
  innerAfterBurst: els.kanbanBoard.innerHTML,
  renders: calls.renders,
}));
"""
    result = _run_node(_kanban_script(body))
    assert result["firstBoardRequests"] == 1, "the first scheduled refresh must issue one board request"
    assert result["boardRequestsWhileHeld"] == 1, (
        "a refresh scheduled while the board reply is in flight must wait for it, "
        "not retire it with a second load (board requests: %r)" % result["boardRequestsWhileHeld"]
    )
    assert result["maxConcurrent"] == 1, "two board loads ran concurrently: %r" % result["maxConcurrent"]
    assert "alpha-board" in result["innerWhileHeld"], (
        "the displayed board was replaced while a refresh was loading: %r" % result["innerWhileHeld"]
    )
    assert "alpha-board" in result["innerAfterBurst"], (
        "the displayed board was replaced by a burst entry: %r" % result["innerAfterBurst"]
    )
    assert result["renders"] == 0, "no render can happen before the held reply is released"


def test_held_reply_renders_and_the_trailing_re_run_happens_exactly_once():
    body = r"""
_kanbanBoard = { columns:[{ name:'Todo', tasks:[{ id:'t1' }] }] };
els.kanbanBoard.innerHTML = '<div class="board">alpha-board</div>';
_scheduleKanbanRefresh([{ task_id:'t1' }]);
await sleep(350);
_scheduleKanbanRefresh([{ task_id:'t2' }]);
_scheduleKanbanRefresh([{ task_id:'t3' }]);
await sleep(350);
releaseBoard(BOARD_TWO_TASKS);
await sleep(350);
const afterFirstRelease = { renders: calls.renders, boardRequests: calls.boardRequests };
// The trailing re-run is now in flight and its own reply is held.
releaseBoard(BOARD_TWO_TASKS);
await sleep(350);
console.log(JSON.stringify({
  afterFirstRelease,
  final: { renders: calls.renders, boardRequests: calls.boardRequests, maxConcurrent: calls.maxConcurrent },
  inner: els.kanbanBoard.innerHTML,
}));
"""
    result = _run_node(_kanban_script(body))
    assert result["afterFirstRelease"]["renders"] == 1, (
        "the held reply was dropped instead of rendered: %r" % result["afterFirstRelease"]
    )
    assert result["afterFirstRelease"]["boardRequests"] == 2, (
        "the burst must schedule exactly one trailing re-run: %r" % result["afterFirstRelease"]
    )
    assert result["final"]["renders"] == 2, "the trailing re-run must render once: %r" % result["final"]
    assert result["final"]["boardRequests"] == 2, (
        "a third load means the coalesced burst multiplied: %r" % result["final"]
    )
    assert result["final"]["maxConcurrent"] == 1, "loads overlapped: %r" % result["final"]
    assert "rendered:2" in result["inner"], "the board must show the trailing render: %r" % result["inner"]


def test_first_kanban_load_still_paints_the_loading_placeholder():
    """Control: with nothing displayed yet the placeholder is correct."""
    body = r"""
_scheduleKanbanRefresh([{ task_id:'t1' }]);
await sleep(350);
const innerWhileHeld = els.kanbanBoard.innerHTML;
releaseBoard(BOARD_TWO_TASKS);
await sleep(350);
console.log(JSON.stringify({ innerWhileHeld, renders: calls.renders, inner: els.kanbanBoard.innerHTML }));
"""
    result = _run_node(_kanban_script(body))
    assert "loading" in result["innerWhileHeld"].lower(), "the first load must still show progress"
    assert result["renders"] == 1
    assert "rendered:1" in result["inner"]


# ─────────────────────────────────────────────────────────────────────────────
# 2. boot: restart the visible panel once the authoritative profile arrives
# ─────────────────────────────────────────────────────────────────────────────

_BOOT_HARNESS = r"""
'use strict';
function makeEl(){ return { textContent:'', innerHTML:'', value:'', style:{ display:'' } }; }
const els = {};
function $(id){ return els[id] || null; }
const S = { activeProfile:'default', activeProfileIsDefault:true, session:null };
const calls = { loadMemory:0, loadSkills:0, reset:0, crons:0, kanban:0 };
let _currentPanel = 'memory';
// module counters of panels.js
let _memoryLoadGeneration = 0;
let _skillsLoadGeneration = 0;
let _workspacePanelLoadGeneration = 0;
let _kanbanLoadGeneration = 0;
let _cronGatewayNoticeGeneration = 0;
let _cronLoadGeneration = 0;
let _cronList = [];
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 0;
let _cronPreFormDetail = null;
let _editingCronId = null;
let _cronIsDuplicate = false;
let _cronSkillsCache = null;
function _clearCronDetail(){}
async function loadCrons(){ calls.crons += 1; }
async function loadSkills(){ calls.loadSkills += 1; }
async function loadMemory(){ calls.loadMemory += 1; }
async function loadKanban(){ calls.kanban += 1; }
async function loadProfilesPanel(){}
async function loadWorkspacesPanel(){}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
"""


def _boot_script(body: str) -> str:
    parts = [
        _extract_optional(PANELS_JS, "_restartVisiblePanelLoadForResolvedProfile"),
        _extract_function(PANELS_JS, "_profileSwitchPanelLoad"),
        _extract_function(PANELS_JS, "_resetProfileScopedPanelState"),
        _extract_optional(PANELS_JS, "_retireProfileScopedPanelLoads"),
    ]
    return (
        _BOOT_HARNESS
        + "\n".join(parts)
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


def test_boot_restarts_the_visible_panel_for_a_named_resolved_profile():
    body = r"""
S.activeProfile = 'alpha';
S.activeProfileIsDefault = false;
const handled = _restartVisiblePanelLoadForResolvedProfile('alpha');
await sleep(50);
console.log(JSON.stringify({
  handled,
  loads: calls.loadMemory,
  generations: { memory:_memoryLoadGeneration, skills:_skillsLoadGeneration, kanban:_kanbanLoadGeneration },
}));
"""
    result = _run_node(_boot_script(body))
    assert result["handled"] is True, "the boot hook must claim the restart"
    assert result["loads"] == 1, "the visible Memory panel was not restarted for the resolved profile"
    assert result["generations"]["memory"] >= 1, (
        "the load captured under the placeholder owner was not retired: %r" % result["generations"]
    )


def test_boot_restart_is_a_no_op_for_the_default_profile():
    """Control: the placeholder owner already is the real one at boot."""
    body = r"""
S.activeProfile = 'default';
const handled = _restartVisiblePanelLoadForResolvedProfile('default');
await sleep(50);
const empty = _restartVisiblePanelLoadForResolvedProfile('');
await sleep(50);
console.log(JSON.stringify({ handled, empty, loads: calls.loadMemory }));
"""
    result = _run_node(_boot_script(body))
    assert result["handled"] is False
    assert result["empty"] is False
    assert result["loads"] == 0, "the default profile must not trigger a spurious reload"


def test_boot_calls_the_restart_after_it_sets_the_authoritative_profile():
    setter = BOOT_JS.index("S.activeProfile = activeProfileState.profile;")
    call = BOOT_JS.index("_restartVisiblePanelLoadForResolvedProfile(S.activeProfile)")
    assert call > setter, "the restart must run after the authoritative profile is recorded"
    window = BOOT_JS[setter:call]
    assert "typeof _restartVisiblePanelLoadForResolvedProfile === 'function'" in window, (
        "the boot call must be typeof-guarded like the file's other cross-file helpers"
    )
    assert call - setter < 1200, "the restart drifted away from the profile assignment: %r chars" % (call - setter)


# ─────────────────────────────────────────────────────────────────────────────
# 3. session-load profile switch: reload the visible panel, drop the caches
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_HARNESS = r"""
'use strict';
function makeEl(){ return { textContent:'', innerHTML:'', value:'', style:{ display:'' },
  classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } } }; }
const els = { memEditContent: makeEl() };
function $(id){ return els[id] || null; }
const window = {};
const localStorage = { removeItem(){} };
const S = { activeProfile:'alpha', activeProfileIsDefault:false, session:null, messages:[] };

// ── panels.js / sessions.js module state ─────────────────────────────────
let _currentPanel = 'skills';
let _skillsData = [{ name:'alpha-skill' }];
let _skillsLoadGeneration = 0;
let _memoryData = { memory:'alpha memory' };
let _memoryLoadGeneration = 0;
let _memoryMode = 'read';
let _currentMemorySection = 'memory';
let _notesSourcesData = null;
let _workspacePanelLoadGeneration = 0;
let _workspaceList = [];
let _kanbanLoadGeneration = 0;
let _cronGatewayNoticeGeneration = 0;
let _cronLoadGeneration = 0;
let _showAllCronProfiles = true;
let _cronOtherProfileCount = 0;
let _cronPreFormDetail = null;
let _editingCronId = null;
let _cronIsDuplicate = false;
let _cronList = [];
let _cronSkillsCache = null;
let _sessionListSkeletonActive = false;

const calls = { loadSkills:0, loadMemory:0, loadCrons:0, loadKanban:0, apiUrls:[], renders:0 };
function esc(v){ return String(v); }
function t(key){ return key; }
function renderSkills(){ calls.renders += 1; }
function renderMd(v){ return String(v); }
function li(){ return ''; }
async function loadSkills(){ calls.loadSkills += 1; }
async function loadMemory(){ calls.loadMemory += 1; }
async function loadCrons(){ calls.loadCrons += 1; }
async function loadKanban(){ calls.loadKanban += 1; }
async function loadProfilesPanel(){}
async function loadWorkspacesPanel(){}
async function api(url, opts){
  calls.apiUrls.push(url);
  if (url === '/api/profile/switch') return { active: JSON.parse(opts.body).name, is_default:false };
  throw new Error('unexpected api ' + url);
}
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
function _clearCronDetail(){}
function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }
"""


def _session_script(body: str) -> str:
    parts = [
        _extract_function(SESSIONS_JS, "_switchProfileForSessionLoad"),
        _extract_function(PANELS_JS, "_profileSwitchPanelLoad"),
        _extract_function(PANELS_JS, "_resetProfileScopedPanelState"),
        _extract_optional(PANELS_JS, "_retireProfileScopedPanelLoads"),
    ]
    return (
        _SESSION_HARNESS
        + "\n".join(parts)
        + "\n(async () => {\n"
        + body
        + "\n})().catch(err => { console.error(err && err.stack || err); process.exit(1); });\n"
    )


def test_session_load_switch_drops_the_skills_cache_and_reloads_the_visible_panel():
    body = r"""
await _switchProfileForSessionLoad('beta');
await sleep(50);
console.log(JSON.stringify({
  activeProfile: S.activeProfile,
  skillsData: _skillsData,
  loads: { skills: calls.loadSkills, memory: calls.loadMemory },
  generations: { skills:_skillsLoadGeneration, memory:_memoryLoadGeneration },
}));
"""
    result = _run_node(_session_script(body))
    assert result["activeProfile"] == "beta"
    assert result["skillsData"] is None, (
        "the previous profile's Skills cache survived a visible Skills panel: %r" % result["skillsData"]
    )
    assert result["loads"]["skills"] == 1, "the visible Skills panel was not reloaded for the new profile"
    assert result["generations"]["skills"] >= 1


def test_session_load_switch_keeps_an_open_memory_editor_draft():
    body = r"""
_currentPanel = 'memory';
_memoryMode = 'edit';
els.memEditContent.value = 'draft about beta runbook';
await _switchProfileForSessionLoad('beta');
await sleep(50);
console.log(JSON.stringify({
  loadMemory: calls.loadMemory,
  mode: _memoryMode,
  draft: els.memEditContent.value,
}));
"""
    result = _run_node(_session_script(body))
    assert result["loadMemory"] == 1, "the visible Memory panel was not reloaded for the new profile"
    assert result["mode"] == "edit", "the open editor was closed by the profile switch"
    assert result["draft"] == "draft about beta runbook", "the open editor draft was lost"


def test_session_load_switch_does_not_reload_hidden_panels():
    """Control: only the visible panel reloads; the caches are dropped anyway."""
    body = r"""
_currentPanel = 'chat';
await _switchProfileForSessionLoad('beta');
await sleep(50);
console.log(JSON.stringify({
  skillsData: _skillsData,
  loads: { skills: calls.loadSkills, memory: calls.loadMemory, crons: calls.loadCrons },
}));
"""
    result = _run_node(_session_script(body))
    assert result["skillsData"] is None, "a hidden Skills panel must still drop the previous cache"
    assert result["loads"] == {"skills": 0, "memory": 0, "crons": 0}, (
        "hidden panels must not fetch during a session-load switch: %r" % result["loads"]
    )


def test_session_load_switch_does_not_wait_for_the_panel_reload():
    body = r"""
_currentPanel = 'skills';
// The reload is fire-and-forget: the switch resolves without it.
const started = Date.now();
await _switchProfileForSessionLoad('beta');
const elapsed = Date.now() - started;
await sleep(50);
console.log(JSON.stringify({ elapsed, loads: calls.loadSkills, activeProfile: S.activeProfile }));
"""
    result = _run_node(_session_script(body))
    assert result["elapsed"] < 500, "the session load waited for the panel reload: %r ms" % result["elapsed"]
    assert result["loads"] == 1
