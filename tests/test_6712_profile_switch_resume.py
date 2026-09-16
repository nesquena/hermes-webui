"""Regression tests for #6712 — opt-in resume of the target profile's most
recent session on profile switch.

The gate's exact-head review at `4759a851` bounced the opt-in with four
current-head defects. Each is pinned here against the shipped source:

  F1 — the saved checkbox did not update the runtime authority.
       `window._profileSwitchResumeSession` was seeded only at boot, so turning
       the option on stayed behaviourally off until a reload (and turning it off
       stayed on). The sibling `new_chat_on_workspace_switch` setting mirrors its
       saved value in both the autosave completion path and the settings-panel
       hydration path; this setting must do the same.
  F2 — a failed load was reported as a successful resume.
       `resumed = true` was set unconditionally after `await loadSession(...)`,
       but `loadSession()` swallows metadata failures, auth/stale exits and
       message-load failures, returning normally. A metadata failure could
       therefore leave the PREVIOUS profile's conversation on screen under the
       target profile's cookie while skipping the fresh-session rollback.
       `loadSession()` now reports success and the resume path checks it.
  F3 — the new transition was not generation-owned.
       `_switchGen` was only checked after the list/load, so a superseded
       switch's response could call `loadSession(A_sid)` under switch B, whose
       409 profile-mismatch recovery calls `_switchProfileForSessionLoad(A)` —
       dragging the browser back to a stale profile. Ownership is now checked at
       every await boundary and propagated into `loadSession()`.
  F4 — the advertised behaviour was skipped with no current session.
       The resume block lived only inside `else if (sessionInProgress)`, so a
       switch from the blank/no-message state never attempted a resume even with
       the preference on.

The behavioural cases run the extracted helper through a Node VM with the real
async ordering; the wiring cases assert the shipped call sites, because "the
helper exists but nothing calls it" is exactly the dead-code shape the gate
flagged.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
INDEX_HTML_PATH = REPO_ROOT / "static" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

def _top_level_function_body(js: str, declaration: str) -> str:
    """Slice from `declaration` to the next top-level declaration.

    `extract_function` brace-matches from the first `{`, which is wrong for
    signatures that contain a brace inside the parameter list (e.g.
    `newSession(flash, options={})`) — it stops at the default-value brace.
    These files declare top-level functions at column 0, so anchoring on the
    next such declaration is exact and grows with the function.
    """
    start = js.index(declaration)
    offset = start + len(declaration)
    for line in js[offset:].split("\n"):
        if line.startswith(("async function ", "function ", "const ", "let ", "var ")):
            return js[start:offset]
        offset += len(line) + 1
    return js[start:]


HELPER = "_resumeRecentSessionForProfileSwitch"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _helper_body() -> str:
    return extract_function(_read(PANELS_JS_PATH), HELPER, prefix="async function")


def _switch_to_profile_body() -> str:
    src = _read(PANELS_JS_PATH)
    idx = src.find("async function switchToProfile(name)")
    assert idx != -1, "switchToProfile not found"
    # Brace-match from the opening brace so we get the whole function.
    brace = src.find("{", idx)
    depth = 0
    i = brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
        i += 1
    raise AssertionError("switchToProfile braces unbalanced")


# ── F1: runtime mirror on save and on panel load ─────────────────────────────


def test_autosave_mirrors_the_setting_into_the_runtime_authority():
    src = _read(PANELS_JS_PATH)
    # The autosave completion path must assign the saved value to the global
    # mirror, the way new_chat_on_workspace_switch does.
    pattern = re.compile(
        r"profile_switch_resume_session\s*!==\s*undefined\s*\)\s*\{[^}]*"
        r"window\._profileSwitchResumeSession\s*=",
        re.S,
    )
    assert pattern.search(src), (
        "the autosave path must mirror profile_switch_resume_session into "
        "window._profileSwitchResumeSession; without it the option only applies "
        "after a reload (F1)"
    )


def test_settings_panel_hydration_seeds_the_runtime_authority():
    src = _read(PANELS_JS_PATH)
    # The hydration site is the one that sets `.checked` from settings; the
    # payload site (in _preferencesPayloadFromUi) has no `.checked =` assignment.
    idx = src.find("profileSwitchResumeCb.checked=!!settings.profile_switch_resume_session")
    assert idx != -1, "settings checkbox hydration not found"
    block = src[max(0, idx - 200) : idx + 600]
    assert "window._profileSwitchResumeSession" in block, (
        "loadSettingsPanel() must seed window._profileSwitchResumeSession from "
        "the hydrated checkbox, mirroring new_chat_on_workspace_switch (F1)"
    )


def test_sibling_setting_uses_the_same_mirror_shape():
    """Guards the pattern this PR is matching: if the sibling ever drops its
    mirror, the F1 tests above would be pinning a shape nothing else uses."""
    src = _read(PANELS_JS_PATH)
    assert "window._newChatOnWorkspaceSwitch=" in src, (
        "the sibling new_chat_on_workspace_switch mirror has changed shape; "
        "re-check that the profile_switch_resume_session mirror still matches it"
    )


# ── F2: loadSession must report success/failure ──────────────────────────────


def test_load_session_reports_success_and_cancellation():
    body = extract_function(_read(SESSIONS_JS_PATH), "loadSession", prefix="async function")
    assert re.search(r"return\s+!!\(S\.session && S\.session\.session_id === sid\)", body), (
        "loadSession must report whether the requested session actually became "
        "the active session, otherwise callers cannot tell a swallowed failure "
        "from a real load (F2)"
    )
    assert "return false;" in body, (
        "loadSession must return false on its abort/cancel paths so a cancelled "
        "load is not mistaken for success (F2)"
    )


def test_resume_path_checks_the_load_result():
    """The resume must not treat an assumed success as one."""
    body = _helper_body()
    assert re.search(r"loaded\s*=\s*await\s+loadSession\(", body), (
        "the resume must capture loadSession()'s result (F2)"
    )
    assert re.search(r"if\s*\(!loaded\)\s*return false", body), (
        "the resume must bail out when loadSession() reports failure, so the "
        "fresh-session rollback still runs (F2)"
    )
    assert re.search(r"S\.session\.session_id\s*!==\s*recentSid", body), (
        "the resume must confirm the requested session became active rather than "
        "trusting the load's own report (F2)"
    )


def test_failed_load_falls_back_to_a_fresh_session():
    """The caller must start a fresh session whenever the resume did not
    genuinely happen — that is the rollback the review required."""
    body = _switch_to_profile_body()
    assert re.search(r"const resumed = await " + HELPER, body), (
        "switchToProfile must delegate the resume to the gen-aware helper (F2/F3)"
    )
    assert re.search(r"if \(!resumed\) \{.*?await newSession\(", body, re.S), (
        "switchToProfile must create a fresh session when the resume fails, "
        "otherwise a failed load strands the previous profile's conversation (F2)"
    )
    # The fresh-session call must carry the switch generation (Greptile review:
    # an in-flight newSession() from an older profile must not be adopted).
    assert re.search(r"await newSession\([^)]*profileSwitchGen", body, re.S), (
        "the fresh-session fallback must pass profileSwitchGen so a newSession() "
        "still in flight for an older profile cannot be adopted as this switch's "
        "result"
    )


# ── F3: generation ownership across the async boundaries ─────────────────────


def _run_helper(*, owns_switch=True, sessions=None, api_throws=False,
                load_returns=True, session_after_load=None, resume_setting=True):
    """Drive the real helper through a Node VM with controllable await points."""
    payload = {
        "helper": _helper_body(),
        "ownsSwitch": owns_switch,
        "sessions": sessions if sessions is not None else [],
        "apiThrows": api_throws,
        "loadReturns": load_returns,
        "sessionAfterLoad": session_after_load,
        "resumeSetting": resume_setting,
    }
    js = r"""
const params = __PARAMS__;
const calls = { api: [], loadSession: [], newSession: 0 };

// The helper reads the opt-in from the global mirror and the live profile from
// S.activeProfile. A bare Node VM has neither `window` nor the page's state, so
// stub both — a missing `window` would throw ReferenceError and the harness
// would test an accident rather than the shipped logic.
var window = { _profileSwitchResumeSession: params.resumeSetting };

let _profileSwitchGeneration = 1;
const switchGen = params.ownsSwitch ? 1 : 0;

const S = { activeProfile: 'target', session: null, messages: [] };
let clearWorkspaceTreeSkeletonCalls = 0;
function clearWorkspaceTreeSkeleton(){ clearWorkspaceTreeSkeletonCalls++; }

async function api(url){
  calls.api.push(url);
  if(params.apiThrows) throw new Error('boom');
  return { sessions: params.sessions };
}
async function loadSession(sid, opts){
  calls.loadSession.push({ sid: sid, opts: opts || null });
  if(params.sessionAfterLoad){ S.session = { session_id: params.sessionAfterLoad, profile: 'target' }; }
  return params.loadReturns;
}

eval(params.helper);

_resumeRecentSessionForProfileSwitch(switchGen, true).then(function(resumed){
  console.log(JSON.stringify({
    resumed: resumed,
    api: calls.api,
    loadSession: calls.loadSession,
    clearSkeleton: clearWorkspaceTreeSkeletonCalls,
    sessionId: S.session && S.session.session_id,
  }));
});
""".replace("__PARAMS__", json.dumps(payload))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_superseded_switch_does_not_touch_the_target_profile():
    """A switch that no longer owns the generation must not even list sessions."""
    result = _run_helper(owns_switch=False, sessions=[{"session_id": "A"}])
    assert result["resumed"] is False, "a superseded switch must not report a resume"
    assert result["api"] == [], (
        "a superseded switch issued /api/sessions under the NEW profile cookie — "
        "its response could then drive a load for the wrong profile (F3)"
    )
    assert result["loadSession"] == [], "a superseded switch must not call loadSession (F3)"


def test_resume_passes_generation_ownership_into_load_session():
    result = _run_helper(sessions=[{"session_id": "A"}], load_returns=True,
                         session_after_load="A")
    assert result["resumed"] is True, f"resume should have succeeded: {result}"
    assert len(result["loadSession"]) == 1, f"expected one load, got {result['loadSession']}"
    opts = result["loadSession"][0]["opts"] or {}
    assert opts.get("switchGen") == 1, (
        "loadSession must receive the owning switch generation, otherwise its "
        "409 profile-mismatch recovery can switch the browser to a stale "
        "profile (F3)"
    )
    assert opts.get("profileSwitchOwned") is True, (
        "loadSession must be told this load belongs to a profile switch (F3)"
    )


def test_cancelled_load_is_not_reported_as_a_resume():
    result = _run_helper(sessions=[{"session_id": "A"}], load_returns=False)
    assert result["resumed"] is False, (
        "loadSession returning false (cancelled load) was reported as a "
        "successful resume (F2)"
    )


def test_list_failure_is_not_reported_as_a_resume():
    result = _run_helper(api_throws=True)
    assert result["resumed"] is False, "an errored list request must not resume (F2)"
    assert result["loadSession"] == [], "no load may be attempted after a list failure"


def test_empty_target_profile_is_not_reported_as_a_resume():
    result = _run_helper(sessions=[])
    assert result["resumed"] is False, "an empty target profile must fall back to a fresh session"


def test_load_that_lands_a_different_session_is_not_a_resume():
    """If a concurrent load wins the slot, the resume must not claim success —
    that would leave the wrong conversation on screen under the new profile."""
    result = _run_helper(sessions=[{"session_id": "A"}], load_returns=True,
                         session_after_load="B")
    assert result["resumed"] is False, (
        "the resume reported success while S.session held a different session, "
        "so the rollback to a fresh session was skipped (F2)"
    )


def test_setting_off_never_lists_or_loads():
    result = _run_helper(resume_setting=False, sessions=[{"session_id": "A"}])
    assert result["resumed"] is False, "the opt-in is off, so nothing must resume"
    assert result["api"] == [], "the opt-in is off, so no session list may be requested"


def test_successful_resume_clears_the_workspace_skeleton():
    result = _run_helper(sessions=[{"session_id": "A"}], load_returns=True,
                         session_after_load="A")
    assert result["resumed"] is True
    assert result["clearSkeleton"] == 1, (
        "a successful resume must clear the up-front workspace skeleton so it "
        "cannot strand over the resumed session's tree"
    )


def test_ownership_checked_after_each_await_boundary():
    """The generation must be re-checked after the list and after the load —
    the cookie can move while either request is in flight."""
    body = _helper_body()
    owns = len(re.findall(r"_stillOwnsSwitch\(\)", body))
    assert owns >= 3, (
        f"expected ownership checks before the list, after the list and after the "
        f"load (>=3 call sites), found {owns} (F3)"
    )


# ── F4: the empty/no-session branch must attempt the resume too ──────────────


def test_resume_is_attempted_from_the_no_session_branch():
    body = _switch_to_profile_body()
    # The helper must be invoked outside the sessionInProgress branch as well.
    calls = body.count(HELPER)
    assert calls >= 2, (
        f"the resume helper is invoked {calls} time(s); the blank/no-message "
        f"branch must attempt it too, otherwise the advertised behaviour is "
        f"skipped whenever there is no current session (F4)"
    )


def test_no_session_branch_keeps_its_blank_refresh_fallback():
    """The original in-place refresh must survive as the fallback."""
    body = _switch_to_profile_body()
    assert "resumedFromEmpty" in body, (
        "the no-session branch must branch on the resume result (F4)"
    )
    assert "await renderSessionList()" in body, (
        "the blank in-place refresh must remain the fallback when nothing resumes (F4)"
    )


# ── 409 recovery must respect switch ownership ───────────────────────────────


def test_profile_mismatch_recovery_respects_switch_ownership():
    body = extract_function(_read(SESSIONS_JS_PATH), "loadSession", prefix="async function")
    idx = body.find("_sessionProfileMismatchFromError")
    assert idx != -1, "409 profile-mismatch recovery not found"
    block = body[idx : idx + 1600]
    assert "profileSwitchOwned" in block, (
        "the 409 profile-mismatch recovery must consult the switch ownership "
        "flags; otherwise a stale switch can pull the browser back to its own "
        "profile (F3)"
    )
    assert "_profileSwitchGeneration" in block, (
        "the recovery must compare against the live switch generation (F3)"
    )


# ── Greptile review: in-flight newSession() must be generation-scoped ────────
#
# `newSession()` caches its work in a single module-level promise and reused it
# for ANY caller. During a profile switch the cookie/profile have already moved
# while an earlier run may still be in flight for the previous profile, so a
# caller could adopt a session created for the old profile.


def test_new_session_promise_is_owner_scoped():
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    assert "profileSwitchGen" in body, (
        "newSession must accept the caller's profile-switch generation so it can "
        "tell whether the cached in-flight promise belongs to this owner"
    )
    assert "_newSessionInFlightGen" in body, (
        "newSession must record which generation owns the cached promise"
    )
    # Same owner → reuse; different owner → do not return the stale promise.
    assert re.search(r"if\(_sameOwner\)\s*\{", body), (
        "the cached promise may only be reused when the owner matches"
    )
    assert "await _newSessionInFlight;" in body, (
        "a different owner must wait for the previous run before starting its own"
    )


def test_new_session_owner_is_cleared_when_the_run_finishes():
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    assert re.search(r"finally\s*\{[^}]*_newSessionInFlightGen=null", body, re.S), (
        "the owner generation must be cleared in the finally block together with "
        "the promise, or a later caller inherits a stale owner"
    )


# ── Greptile review: a partially-loaded conversation is not a success ────────
#
# Both message-load paths keep a usable fallback (inflight projection, or the
# "Failed to load messages" notice) and continue to the tail of loadSession, so
# "S.session is the requested sid" alone was not enough to call the load a
# success — a half-loaded conversation was reported as a successful resume and
# the fresh-session rollback was skipped.


def test_message_load_failure_makes_load_session_report_failure():
    src = _read(SESSIONS_JS_PATH)
    body = _top_level_function_body(src, "async function loadSession(")
    # The success return must consult the message-load outcome.
    assert "_loadMessagesFailedForSid(sid)" in body, (
        "loadSession must not report success when its message body failed to load"
    )
    # Both failing paths must record it.
    assert body.count("_loadMessagesFailedSids.add(sid)") >= 2, (
        "both message-load failure paths (inflight catch and idle catch) must "
        "record the failure, otherwise one of them still reports success"
    )
    # And a new load must clear the previous outcome.
    assert "_loadMessagesFailedSids.delete(sid)" in body, (
        "a fresh load for the same sid must clear a previous failure, otherwise a "
        "now-successful load is reported as failed"
    )


def test_partial_load_failure_reported_to_the_resume_path():
    """End-to-end intent: the resume must not treat a partial load as success."""
    helper = _helper_body()
    assert re.search(r"if\s*\(!loaded\)\s*return false", helper), (
        "the resume must bail out when loadSession reports a failure, which now "
        "includes a partially-loaded conversation"
    )


# ── Greptile re-review: a superseded switch must not create a session ────────
#
# The generation guard sat *after* `newSession()`, so a switch that lost
# ownership while the resume helper awaited still called it. `newSession()`
# mints and installs a session from the shared state of whatever profile the
# cookie now points at, so reaching it overwrites the session, URL, transcript
# and stream owned by the newer switch. The guard must come first.
#
# This drives the real `switchToProfile()` through a Node VM: a newer switch
# takes ownership exactly while the resume helper is in flight, which is the
# only ordering where the old code reached `newSession()`.


def _run_stale_switch() -> dict:
    """Run the shipped switch path with a newer switch claiming ownership mid-flight."""
    payload = {
        "switch": _switch_to_profile_body(),
    }
    js = r"""
const params = __PARAMS__;
const calls = { newSession: 0, resume: 0, setEmbargo: [], renderList: 0, toasts: [],
                chipWrites: [], titleWrites: [], openBrowser: 0, loadDir: 0 };

// The branch under test only runs when a conversation is in progress.
var S = { session: { session_id: 'old', workspace: 'ws', profile: 'old' }, messages: [{}],
          activeProfile: 'old' };
var _sessionInProgress = true;

// Ownership: this switch owns generation 1. While the resume helper awaits, a
// newer switch advances the generation to 2 — the exact interleaving the guard
// must survive.
let _profileSwitchGeneration = 1;
const _switchGen = 1;

async function _resumeRecentSessionForProfileSwitch(switchGen, workspaceVisible){
  calls.resume += 1;
  // A newer switch takes over while this helper is in flight. Advance PAST this
  // switch's own generation (switchToProfile pre-incremented it, so this call's
  // _switchGen is already the current value) — otherwise the guard would see the
  // switch as still owning and the harness would model nothing.
  _profileSwitchGeneration += 1;
  return false;   // nothing to resume -> the caller's fresh-session path
}
async function newSession(flash, options){
  calls.newSession += 1;
  // Model the real damage: newSession() mints and installs a session from the
  // shared state of whatever profile the cookie now points at, replacing the one
  // the newer switch owns.
  S.session = { session_id: 'created-by-stale-switch', profile: 'target' };
  return S.session;
}
async function api(){ return {}; }
async function renderSessionList(){ calls.renderList += 1; }
async function loadDir(){ calls.loadDir += 1; }
function syncTopbar(){}
function _setProfileSwitchListEmbargo(v){ calls.setEmbargo.push(v); }
function _openProfileSwitchSessionBrowser(){ calls.openBrowser += 1; }
function clearWorkspaceTreeSkeleton(){}
function animateNextSessionListRefresh(){}
// switchToProfile() touches chrome that does not exist in a bare VM. The
// production code guards every one of these with `if (el)`, so null is the
// faithful stub: it disables the chrome work without changing control flow.
function $(id){ return null; }
var __store = {};
var localStorage = {
  getItem: (k) => (k in __store ? __store[k] : null),
  setItem: (k, v) => { __store[k] = String(v); },
  removeItem: (k) => { delete __store[k]; },
};
function showToast(m){ calls.toasts.push(String(m)); }
function t(k){ return k; }
async function _profileSwitchPanelLoad(){}
function _refreshProfileSwitchBackground(){}

// Minimal stand-ins for the function's DOM touch points.
const _chipLabel = { textContent: '' };
const _titlebarLabel = { textContent: '' };
let _prevProfileName = 'old';
let _openingExistingSidebarSession = false;
const _workspacePanelMode = 'closed';
function _isCompactWorkspaceViewport(){ return false; }
function _syncWorkspacePanelForProfileSwitch(){}
async function loadSettingsPanel(){}
function _workspacePanelEls(){ return { layout: null, panel: null }; }

var window = { _profileSwitchResumeSession: false };

eval(params.switch);

switchToProfile('target').then(function(result){
  console.log(JSON.stringify({
    returned: result,
    newSession: calls.newSession,
    resume: calls.resume,
    setEmbargo: calls.setEmbargo,
    renderList: calls.renderList,
    toasts: calls.toasts,
    openBrowser: calls.openBrowser,
    sessionId: S.session && S.session.session_id,
  }));
}).catch(function(e){
  console.log(JSON.stringify({ error: String(e && e.message || e) }));
});
"""
    js = js.replace("__PARAMS__", json.dumps(payload))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_superseded_switch_does_not_create_a_session():
    """A switch that lost ownership mid-flight must not reach newSession()."""
    result = _run_stale_switch()
    assert "error" not in result, f"the switch path threw: {result['error']}"
    assert result["resume"] == 1, (
        f"the resume helper must have been attempted first: {result}"
    )
    assert result["newSession"] == 0, (
        "a switch that lost its generation while the resume helper awaited still "
        "called newSession(); that call mints and installs a session under the "
        "cookie of the NEWER profile, overwriting the newer switch's session, "
        "URL, transcript and stream (Greptile review)"
    )
    assert result["returned"] is False, (
        f"a superseded switch must report that it did not complete: {result}"
    )


def test_a_superseded_switch_does_not_overwrite_the_newer_session():
    """The damage the review describes, measured: the active session must survive.

    `newSession()` installs a session built from the shared state of whatever
    profile the cookie now points at. If a superseded switch reaches it, the
    session the newer switch owns is replaced — the user's conversation, URL,
    transcript and stream all move to a session that belongs to the wrong switch.
    """
    result = _run_stale_switch()
    assert result["sessionId"] == "old", (
        "a superseded switch replaced the active session: newSession() installed "
        f"{result['sessionId']!r} over the one the newer switch owns "
        "(Greptile review)"
    )
    # Belt and braces: it must not have progressed to the switch-owned UI work
    # under the newer switch's ownership either.
    assert result["renderList"] == 0, (
        f"a superseded switch re-rendered the session list: {result}"
    )
    assert False not in result["setEmbargo"], (
        f"a superseded switch lifted the list embargo, unfreezing the session "
        f"list under the newer switch: {result}"
    )
    assert result["toasts"] == [] and result["openBrowser"] == 0, (
        f"a superseded switch popped a toast / opened the session browser: {result}"
    )


def test_the_generation_guard_precedes_session_creation():
    """Pin the ordering in the shipped source, so the guard cannot drift back.

    Scoped to the resume→create seam: `switchToProfile()` has other generation
    checks earlier in the function, so a bare `find()` would match one of those
    and pass even with this guard removed — the test would not bite.
    """
    body = _switch_to_profile_body()
    resume = body.find("const resumed = await _resumeRecentSessionForProfileSwitch(")
    create = body.find("await newSession(false, {awaitWorkspaceLoad")
    assert resume != -1, "the resume call site is missing"
    assert create != -1, "the fresh-session fallback is missing"
    assert resume < create, "the resume must be attempted before creating a session"
    seam = body[resume:create]
    assert "if (_switchGen !== _profileSwitchGeneration) return false;" in seam, (
        "the generation guard must run in the window between the resume and "
        "newSession(); with the guard only after the call, a superseded switch "
        "still creates a session under the newer profile's cookie and overwrites "
        "its session/URL/transcript/stream (Greptile review)"
    )
