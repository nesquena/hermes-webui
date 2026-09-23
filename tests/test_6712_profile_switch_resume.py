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
import sys
import textwrap
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
    # Gate round 8: with several waiters a single await is not enough — the slot
    # must be re-examined after each wait, and a superseded caller must abort
    # rather than queue behind the run the newer switch owns.
    assert "for(;;)" in body, (
        "newSession must re-check the shared slot after every await, not await once"
    )
    assert "await _incumbent;" in body, (
        "a different owner must wait for the previous run before starting its own"
    )
    assert re.search(r"_supersededByNewerSwitch\(\)\)\s*return null", body), (
        "a superseded caller must abort before starting its own run"
    )


def test_new_session_owner_is_cleared_when_the_run_finishes():
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    # Gate round 8: the clear is now conditional — only while the slot still
    # identifies THIS run and owner. An older caller's finally must not delete a
    # newer owner's live slot (which would let a second creation start and have
    # neither adopted).
    assert re.search(r"finally\s*\{[^}]*_newSessionInFlight===_run", body, re.S), (
        "the promise/owner may only be cleared while the slot still identifies "
        "this run, or a later caller inherits stale owner state (and an older "
        "finally can clear a newer owner's live slot)"
    )
    assert re.search(r"finally\s*\{[^}]*_newSessionInFlightGen=null", body, re.S), (
        "the owner generation must still be cleared in the finally block together "
        "with the promise"
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


# ── Greptile review: a superseded newSession() must not INSTALL its session ───
#
# The earlier fix scoped the shared in-flight promise to its owning switch, and
# the caller re-checks the generation around the call. Neither stops the install
# that happens INSIDE newSession(): once POST /api/session/new resolves it
# unconditionally adopted data.session, the localStorage key, the URL and the
# session stream. A switch superseded mid-flight therefore installed a session
# belonging to the PREVIOUS profile, while the newer switch — following its
# empty-session fallback — left no replacement. The browser was left on the new
# profile holding profile-gated state it could not load or stream.
#
# The session is still created server-side; the fix only declines to adopt it
# into browser state that a newer switch now owns.

_NEW_SESSION_INSTALL_HARNESS = r"""
const params = __PARAMS__;

// ── module state the shipped body touches ────────────────────────────────────
let S = { session: {session_id: 'seed-session', messages: [], workspace: ''},
          messages: [], toolCalls: [], _pendingSessionToolsets: null,
          lastUsage: {} };
const storage = { 'hermes-webui-session': 'seed-session' };
const localStorage = {
  setItem: (k, v) => { storage[k] = String(v); },
  getItem: (k) => (k in storage ? storage[k] : null),
};
let _profileSwitchGeneration = params.genAtStart;
let _newSessionInFlight = null;
let _newSessionInFlightGen = null;
let _activeProject = null;
const NO_PROJECT_FILTER = '__none__';
let _sessionSourceFilter = 'webui';
let _messagesTruncated = false;
let _oldestIdx = 0;

const calls = { urls: [], setUrl: [], startStream: [], renderList: 0,
                sessionNewStarted: false };

// A deferred the driver resolves once the newer switch owns the state. The
// request must be genuinely pending at that moment — that is the whole point.
let _gateResolve = null;
const gate = new Promise(resolve => { _gateResolve = resolve; });

// Globals the shipped body reaches through `window.` / `document.`. `typeof`
// guards an undeclared identifier, not a member access on an undefined one.
var window = { _clearPendingSelections(){}, _defaultModel: null,
               _activeProvider: null };
var document = { documentElement: { dataset: {} }, getElementById(){ return null; } };
function _readPersistedModelState(){ return null; }
function _modelStateForSelect(){ return null; }
function _readEmptyComposerModelOverride(){ return null; }
function $(id){
  return { value: '', style: {}, classList: {add(){},remove(){},toggle(){}},
           setAttribute(){}, getAttribute(){ return null; }, textContent: '' };
}

function api(path){
  calls.urls.push(String(path));
  if(String(path).includes('/api/session/new')){
    calls.sessionNewStarted = true;
    const payload = {session: {session_id: 'stale-created', messages: [],
      workspace: '', message_count: 0, last_usage: {}}};
    return params.holdRequest ? gate.then(() => payload) : Promise.resolve(payload);
  }
  return Promise.resolve({});
}
function _setActiveSessionUrl(sid){ calls.setUrl.push(sid); }
function startSessionStream(sid){ calls.startStream.push(sid); }
function _setSessionViewedCount(){}
function updateQueueBadge(){}
function clearLiveToolCards(){}
function _setNewSessionPending(){}
function _newSessionPendingText(){ return 'pending'; }
function showToast(){}
function _rememberNewChatDraftSession(){}
function _deferWorkspaceRefreshForSession(){}
function loadDir(){ return Promise.resolve(); }
function refreshSessionList(){ calls.renderList++; return Promise.resolve(); }
function t(k){ return k; }
function _adoptRegenerationRevision(){}
function _hydrateTodosFromSession(){}
function setComposerStatus(){}
function renderSessionList(){ calls.renderList++; return Promise.resolve(); }

__NEW_SESSION_BODY__

(async () => {
  const out = {};
  out.genAtStart = _profileSwitchGeneration;

  // Switch A (its own generation) starts creating a session.
  const p = newSession(false, {worktree: false, profileSwitchGen: params.callerGen});

  // Wait until the POST is genuinely in flight before moving ownership.
  for(let i = 0; i < 500 && !calls.sessionNewStarted; i++){
    await new Promise(r => setImmediate(r));
  }
  out.requestStarted = calls.sessionNewStarted;
  out.duringFlight = { session: S.session && S.session.session_id,
                       stored: storage['hermes-webui-session'] };

  // Switch B takes ownership while A's request is still in flight.
  _profileSwitchGeneration = params.genAfter;
  _gateResolve();

  let returned;
  try{ returned = await p; }catch(e){ returned = 'threw:' + e.message; }
  for(let i = 0; i < 20; i++){ await new Promise(r => setImmediate(r)); }

  out.returned = returned;
  out.afterFlight = {
    session: S.session && S.session.session_id,
    stored: storage['hermes-webui-session'],
    url: calls.setUrl.slice(),
    streams: calls.startStream.slice(),
  };
  out.sessionNewCalls = calls.urls.filter(u => u.includes('/api/session/new')).length;
  console.log(JSON.stringify(out));
})();
"""


def _run_new_session_install(*, caller_gen=5, gen_after=6, hold: bool = True) -> dict:
    """Drive the REAL newSession() body while a newer switch takes ownership."""
    import json as _json

    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    payload = {
        "callerGen": caller_gen,
        "genAtStart": caller_gen,
        "genAfter": gen_after,
        "holdRequest": hold,
    }
    js = _NEW_SESSION_INSTALL_HARNESS.replace("__NEW_SESSION_BODY__", body).replace(
        "__PARAMS__", _json.dumps(payload)
    )
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return _json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_harness_drives_a_genuinely_pending_request():
    """Precondition: without this the superseded probes could pass vacuously."""
    out = _run_new_session_install()
    assert out["requestStarted"] is True, (
        f"the POST never reached the in-flight state, so the scenario under test "
        f"was never exercised: {out}"
    )
    assert out["sessionNewCalls"] == 1, out


def test_a_superseded_new_session_does_not_install_its_session():
    """The install the caller's generation checks cannot reach."""
    out = _run_new_session_install()
    assert out["duringFlight"]["session"] == "seed-session", out
    assert out["afterFlight"]["session"] == "seed-session", (
        f"a superseded newSession() installed {out['afterFlight']['session']!r} "
        f"into S.session — the browser now holds a session owned by the previous "
        f"profile while the newer switch owns the state (Greptile review)"
    )


def test_a_superseded_new_session_does_not_write_storage_url_or_stream():
    """The same damage, measured on the other three install points."""
    out = _run_new_session_install()
    assert out["afterFlight"]["stored"] == "seed-session", (
        f"localStorage was repointed at a stale session: {out['afterFlight']['stored']!r}"
    )
    assert out["afterFlight"]["url"] == [], (
        f"the URL was switched to a stale session: {out['afterFlight']['url']}"
    )
    assert out["afterFlight"]["streams"] == [], (
        f"a stream was opened for a stale session: {out['afterFlight']['streams']}"
    )


def test_a_superseded_new_session_still_made_the_request_once():
    """The fix declines to ADOPT the session; the caller still owns creating it."""
    out = _run_new_session_install()
    assert out["sessionNewCalls"] == 1, (
        f"the request must still be made exactly once: {out}"
    )
    assert out["returned"] is None, (
        f"a superseded run must report that it produced no session: {out['returned']!r}"
    )


def test_an_unchanged_generation_still_installs_normally():
    """The guard must not suppress a switch that still owns the browser state."""
    out = _run_new_session_install(caller_gen=5, gen_after=5)
    assert out["afterFlight"]["session"] == "stale-created", (
        f"a switch that still owns the state must install its session: {out}"
    )
    assert out["afterFlight"]["stored"] == "stale-created", out
    assert out["afterFlight"]["streams"] == ["stale-created"], out


def test_an_unowned_new_session_still_installs_normally():
    """Callers that pass no generation (New Chat, boot, commands) are unaffected:
    callerGen is null, so the profile-switch guard cannot apply to them."""
    out = _run_new_session_install(caller_gen=None, gen_after=9)
    assert out["afterFlight"]["session"] == "stale-created", (
        f"a plain New Chat must not be suppressed by the switch guard: {out}"
    )
    assert out["afterFlight"]["streams"] == ["stale-created"], out


# ── Gate round 8 (22 Sep): three objective lifecycle blockers ─────────────────
#
# Each scenario below composes the SHIPPED bodies. The pre-existing helper tests
# stub loadSession() or cover a single changing owner, so they never exercised
# these schedules — which is exactly what the gate asked for.


_SUPERSEDED_LOAD_TEMPLATE = js = r"""
const params = __PARAMS__;

// ── module state the shipped body touches ────────────────────────────────────
var S = { session: { session_id: 'seed', messages: [], workspace: '' },
          messages: [{ role: 'assistant', content: 'seed' }], toolCalls: [],
          _pendingSessionToolsets: null, lastUsage: {}, busy: false, activeStreamId: null };
const INFLIGHT = {};
let _loadingSessionId = null;
let _loadingOlder = false;
let _loadSessionGeneration = 0;
let _loadMessagesFailedSids = new Set();
let _loadMessagesFailedForSid = (sid) => _loadMessagesFailedSids.has(sid);
let _pendingCarryForwardSnapshot = null;
let _messagesTruncated = false;
let _oldestIdx = 0;
let _messageRenderWindowSize = 0;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _keepStaleUntilLoaded = false;
// The switch generation: this load is owned by gen 1.
let _profileSwitchGeneration = 1;
const _switchGen = 1;

const calls = { setUrl: [], startStream: [], storage: [], renderMessages: 0, rearm: 0,
                bodyFetches: 0 };
const storage = {};

// A deferred metadata response so the driver owns the interleaving exactly.
let _metaResolve = null;
const metaGate = new Promise(resolve => { _metaResolve = resolve; });

function api(path){
  if(String(path).includes('messages=0')) return metaGate;
  if(String(path).includes('messages=1')) calls.bodyFetches += 1;
  return Promise.resolve({});
}
const wait = () => new Promise(r => setImmediate(r));

// ── DOM / helper stubs (faithful: every production call is `if (typeof …)`) ──
var window = {};
var history = { replaceState(){} };
var localStorage = { setItem(k,v){ storage[k]=String(v); calls.storage.push(k); },
                     removeItem(k){ delete storage[k]; }, getItem(k){ return storage[k] ?? null; } };
function $(id){ return id === 'msgInner' ? { innerHTML: '' } : null; }
function _rearmActiveSessionStream(){ calls.rearm++; }
function _setActiveSessionUrl(sid){ calls.setUrl.push(sid); }
function startSessionStream(sid){ calls.startStream.push(sid); }
function renderMessages(){ calls.renderMessages++; }
function _appRootPath(){ return '/'; }
function _clearSameSessionForceReloadHint(){}
function _clearStuckSessionOnBoot(){}
function _sessionVisitHasUnreadState(){ return false; }
function _acknowledgeSessionVisit(){}
function _setSessionViewedCount(){}
function scheduleTodosRefresh(){}
function syncTopbar(){}
function _captureSameSessionForceReloadHint(){}
function _clearSameSessionForceReloadHint(){}
function _resolveSessionModelForDisplaySoon(){}
function _setSessionCompletionUnread(){}
function _deferWorkspaceRefreshForSession(){}
function _applyPendingSessionModelForSession(){}
function _hydrateTodosFromSession(){}
function _sessionProfileMismatchFromError(){ return null; }
function _switchProfileForSessionLoad(){ return Promise.resolve(); }
function _clearMessageCache(){}
function _syncToolCallsForLoadedMessages(){}
function clearVisibleMessageRowCache(){}
function clearLiveToolCards(){}
function _syncCtxIndicator(){}
function _renderPendingPromptsForActiveSession(){}
function _restoreComposerDraft(){}
function _checkAndShowHandoffHint(){}
function _hideHandoffHint(){}
function _isMessagingSession(){ return true; }
function _clearDeferredActiveSessionExternalRefresh(){}
function setStatus(){}
function setComposerStatus(){}
function setBusy(){}
function updateSendBtn(){}
function updateQueueBadge(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function stopApprovalPolling(){}
function hideApprovalCard(){}
function stopSessionStream(){}
function stopClarifyPolling(){}
function hideClarifyCard(){}
let _yoloEnabled = false;
function _updateYoloPill(){}
function clearCompressionUi(){}
function _saveComposerDraftNow(){ return Promise.resolve(); }
function _clearPendingSelections(){}
function _clearQueueCardDisplay(){}
function loadInflightState(){ return null; }
function _messageReloadLimitForSession(){ return 2; }
function _uploadPendingFilesSyncProgressForSession(){}
function autoResize(){}
function showToast(){}
function _selectLiveRecoveryInflight(){ return null; }
function _inflightHasVisibleLiveState(){ return false; }
function _serverLiveSnapshotInflight(){ return null; }
function _ensureInflightLiveAssistantMessage(){}
function _projectInflightMessagesForActivityBursts(){ return []; }
function _prepareRunningLiveTail(){ return false; }
function _dropCurrentTurnAssistantMessages(m){ return m; }
function _mergeInflightTailMessages(m){ return m; }
function _mergePendingSessionMessage(){ return false; }
function clearInflightState(){}
function _renderRuntimeJournalAnchorActivityScene(){ return false; }
function attachLiveStream(){}
function restoreLiveTurnHtmlForSession(){ return false; }
function ensureLiveWorklogShell(){}
function appendThinking(){}
function ensureRunActivityForCurrentTurn(){}
function placeLiveToolCardsHost(){}
function resumeManualCompressionForSession(){}
function projectSessionArtifactsForOwner(){}
function queueSessionMessage(){}
function _readPersistedSessionQueue(){ return []; }
function _clearPersistedSessionQueue(){}
function closeOtherLiveStreams(){}
function _messageRenderableMessageCount(){ return 1; }
function _currentMessageRenderWindowSize(){ return 1; }
function _isSessionLocallyStreaming(){ return false; }
function _hermesNotifySessionOpen(){}
function _isSessionActivelyViewedForList(){ return true; }
function _syncToolCallsForLoadedMessages(){}
function clearVisibleMessageRowCache(){}
// The idle branch's message fetch. Returning true keeps the harness honest when
// the guard is intact; when the guard is removed this call is what proves the
// stale load proceeded to fetch and install a body.
let _bodyFetchesFromEnsure = 0;
function _ensureMessagesLoaded(sid, opts){
  _bodyFetchesFromEnsure += 1;
  if (opts && opts.force === 1) {}
  return Promise.resolve(true);
}

__OWNERSHIP_BODY__

__LOAD_SESSION_BODY__

(async () => {
  const p = loadSession(params.sid, { force: false, switchGen: params.switchGen,
                                      profileSwitchOwned: true });

  // Wait until the metadata request is genuinely in flight.
  for(let i = 0; i < 200; i++){ await wait(); }

  // Switch B takes ownership and takes its no-load fallback: nothing else starts
  // a loadSession, so A remains the "current" load by generation alone.
  _profileSwitchGeneration = params.genAfter;
  _metaResolve({ session: { session_id: params.sid, message_count: 1,
                            active_stream_id: null } });

  const returned = await p;
  for(let i = 0; i < 20; i++){ await wait(); }

  console.log(JSON.stringify({
    // JSON.stringify drops an `undefined` value; the early return produces one,
    // so name it explicitly rather than letting the key vanish.
    returned: (returned === undefined ? 'undefined' : returned),
    session: S.session && S.session.session_id,
    stored: storage['hermes-webui-session'] ?? null,
    urls: calls.setUrl.slice(),
    streams: calls.startStream.slice(),
    messages: (S.messages || []).map(m => m.content),
    rearm: calls.rearm,
    bodyFetches: calls.bodyFetches,
    loadingSid: (_loadingSessionId === undefined ? 'undefined' : _loadingSessionId),
  }));
})();
"""


def _run_superseded_profile_load():
    """Drive the real `loadSession()` for a switch-owned load that loses ownership.

    Switch B advances authority and takes its no-load fallback while A's metadata
    request is in flight, so nothing replaces A: by the load generation alone A is
    still the current load.
    """
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function loadSession(")
    ownership = _read(SESSIONS_JS_PATH)
    ownership = ownership[ownership.index("function _profileSwitchOwnsLoad("):]
    ownership = ownership[: ownership.index("\n}\n") + 3]
    js = _SUPERSEDED_LOAD_TEMPLATE.replace("__OWNERSHIP_BODY__", ownership).replace(
        "__LOAD_SESSION_BODY__", body).replace("__PARAMS__", json.dumps(
        {"sid": "A-sid", "switchGen": 1, "genAfter": 2}))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_superseded_profile_load_writes_nothing():
    """B1 (gate round 8): a switch-owned load that lost ownership writes nothing.

    Asserts A installs no session, leaves localStorage/URL/stream untouched, fetches
    no body and does not report success.
    """
    out = _run_superseded_profile_load()
    assert out["session"] == "seed", (
        f"a superseded profile load installed {out['session']!r} into S.session — the "
        f"browser now holds a session under the cookie the newer switch owns: {out}"
    )
    assert out["stored"] is None, f"localStorage was repointed at a stale session: {out}"
    assert out["urls"] == [], f"the URL was switched to a stale session: {out}"
    assert out["streams"] == [], f"a stream was opened for a stale session: {out}"
    # The transcript clear at the top of loadSession() is the legitimate
    # pre-navigation teardown (it runs before the switch moved on), so the state
    # to pin is that NO stale body was fetched or installed after ownership was
    # lost — without the fix this load proceeded to fetch and adopt its body.
    assert out["bodyFetches"] == 0, (
        f"a superseded profile load still fetched the message body after losing "
        f"ownership: {out}"
    )
    assert out["messages"] == [], (
        f"a superseded profile load installed a transcript: {out}"
    )
    assert out["returned"] == "undefined", (
        f"a superseded load must not report success: {out}"
    )


def test_a_superseded_profile_load_releases_its_loading_marker():
    """Greptile P1 (round 9): the stale exit must not strand `_loadingSessionId`.

    When a switch-owned load loses profile-switch ownership and nothing replaces it,
    the load's own stale exit is the only writer left. Marker ownership therefore has
    to be narrower than the install predicate: a load still owns the marker until a
    newer loadSession() supersedes it, so readers of the form
    `_loadingSessionId !== null && _loadingSessionId !== sid` stop rejecting the
    current pane.
    """
    out = _run_superseded_profile_load()
    assert out["loadingSid"] is None, (
        f"the superseded profile load left _loadingSessionId={out['loadingSid']!r} "
        f"behind: the abandoned session stays marked as loading, which suppresses "
        f"active-session reconciliation and rejects current-pane stream events until "
        f"another navigation overwrites the marker (Greptile P1, round 9): {out}"
    )
    # The installs must still be refused — narrowing the marker must not reopen B1.
    assert out["session"] == "seed", out
    assert out["stored"] is None, out
    assert out["urls"] == [], out


def test_message_body_without_session_is_a_failed_resume():
    """B2: metadata succeeds but the message response has no `session`, asserting
    the resume returns false and takes the fresh-session fallback.

    `_ensureMessagesLoaded()` returned NORMALLY on `!data || !data.session`
    without recording a failure, and `loadSession()` then reported true because
    metadata had installed the requested session and no failure was recorded.
    """
    ensure_body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function _ensureMessagesLoaded(")
    js = r"""
const params = __PARAMS__;

let S = { session: { session_id: 'A-sid', messages: [] }, messages: [], toolCalls: [], lastUsage: {} };
const INFLIGHT = {};
let _loadingSessionId = 'A-sid';
let _loadSessionGeneration = 7;
let _messagesTruncated = false;
let _oldestIdx = 0;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
let _pendingCarryForwardSnapshot = null;
// The caller belongs to profile switch 1, which still owns the generation.
let _profileSwitchGeneration = 1;

const calls = { hints: 0 };
function api(){ return Promise.resolve(params.malformed ? {} : { session: params.payload }); }
function _clearSameSessionForceReloadHint(){ calls.hints++; }
function _messageReloadLimitForSession(){ return 2; }
function _syncToolCallsForLoadedMessages(){}
function clearLiveToolCards(){}
function clearVisibleMessageRowCache(){}
function _hydrateTodosFromSession(){}
function scheduleTodosRefresh(){}
function syncTopbar(){}
function _setSessionViewedCount(){}
function _isSessionActivelyViewedForList(){ return true; }
var window = {};

__OWNERSHIP_BODY__

__ENSURE_BODY__

(async () => {
  const result = await _ensureMessagesLoaded('A-sid', {
    force: false, loadGeneration: 7, switchGen: 1,
  });
  // JSON.stringify drops an `undefined` value; the old silent no-op returned one,
  // so name it explicitly instead of letting the key vanish.
  console.log(JSON.stringify({
    result: (result === undefined ? 'undefined' : result),
    messages: (S.messages || []).length,
  }));
})();
"""
    ownership = _read(SESSIONS_JS_PATH)
    ownership = ownership[ownership.index("function _profileSwitchOwnsLoad("):]
    ownership = ownership[: ownership.index("\n}\n") + 3]
    js = js.replace("__OWNERSHIP_BODY__", ownership).replace(
        "__ENSURE_BODY__", ensure_body).replace(
        "__PARAMS__", json.dumps({"malformed": True, "payload": None}))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["result"] is False, (
        f"a message response without `session` was not reported as a failure: {out} — "
        f"loadSession() would then report a successful resume and skip the "
        f"fresh-session fallback (gate round 8)"
    )


def _run_three_owner_schedule(*, replace_slot_for_run1=None):
    """Drive the real `newSession()` with three owners behind one incumbent.

    `replace_slot_for_run1` optionally simulates a newer owner having replaced the
    shared slot while the incumbent was still in flight; it returns a label for
    the synthetic run so the caller can tell what survived.
    """
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    js = r"""
const params = __PARAMS__;

var S = { session: { session_id: 'seed-session', messages: [], workspace: '' },
          messages: [], toolCalls: [], _pendingSessionToolsets: null, lastUsage: {} };
const storage = { 'hermes-webui-session': 'seed-session' };
const localStorage = { setItem: (k, v) => { storage[k] = String(v); },
                       getItem: (k) => (k in storage ? storage[k] : null) };
let _profileSwitchGeneration = 1;
let _newSessionInFlight = null;
let _newSessionInFlightGen = null;
let _activeProject = null;
const NO_PROJECT_FILTER = '__none__';
let _sessionSourceFilter = 'webui';
let _messagesTruncated = false;
let _oldestIdx = 0;

const calls = { urls: [], posts: 0, pending: false };
let _g1Resolve = null, _g2Resolve = null;
const g1 = new Promise(r => { _g1Resolve = r; });
const g2 = new Promise(r => { _g2Resolve = r; });

function api(path){
  calls.urls.push(String(path));
  if(String(path).includes('/api/session/new')){
    calls.posts += 1;
    calls.pending = true;
    const payload = { session: { session_id: 'created-by-' + calls.posts, messages: [],
                                 workspace: '', message_count: 0, last_usage: {} } };
    return (calls.posts === 1) ? g1.then(() => payload) : g2.then(() => payload);
  }
  return Promise.resolve({});
}

var window = { _clearPendingSelections(){}, _defaultModel: null, _activeProvider: null };
var document = { documentElement: { dataset: {} }, getElementById(){ return null; },
                 createElement(){ return { dataset: {}, style: {} }; } };
function _readPersistedModelState(){ return null; }
function _modelStateForSelect(){ return null; }
function _readEmptyComposerModelOverride(){ return null; }
function $(id){
  return { value: '', style: {}, classList: { add(){}, remove(){}, toggle(){} },
           setAttribute(){}, getAttribute(){ return null; }, textContent: '',
           appendChild(){}, querySelectorAll(){ return []; } };
}
function _setActiveSessionUrl(){}
function startSessionStream(){}
function _setSessionViewedCount(){}
function updateQueueBadge(){}
function clearLiveToolCards(){}
function _setNewSessionPending(){}
function _newSessionPendingText(){ return 'pending'; }
function showToast(){}
function _rememberNewChatDraftSession(){}
function _deferWorkspaceRefreshForSession(){}
function loadDir(){ return Promise.resolve(); }
function refreshSessionList(){ return Promise.resolve(); }
function renderSessionList(){ return Promise.resolve(); }
function t(k){ return k; }
function _adoptRegenerationRevision(){}
function _hydrateTodosFromSession(){}
function setComposerStatus(){}
function setStatus(){}
function updateSendBtn(){}
function _setLiveAssistantTps(){}
function _syncCtxIndicator(){}
function syncTopbar(){}
function _announceNewSessionWorkspace(){}
function renderMessages(){}

__NEW_SESSION_BODY__

(async () => {
  const out = {};
  const settle = async (n) => { for(let i = 0; i < n; i++){ await new Promise(r => setImmediate(r)); } };
  const marker = (p) => p.then(v => (v === null ? 'aborted' : 'ran'));

  // Generation 1 is the incumbent: its POST is held open so the others queue.
  const p1 = marker(newSession(false, { worktree: false, profileSwitchGen: 1 }));
  for(let i = 0; i < 500 && !calls.pending; i++){ await settle(1); }
  out.incumbentStarted = calls.pending;

  // Two more owners queue behind it; gen 2 is then superseded, gen 3 is current.
  const p2 = marker(newSession(false, { worktree: false, profileSwitchGen: 2 }));
  _profileSwitchGeneration = 3;
  const p3 = marker(newSession(false, { worktree: false, profileSwitchGen: 3 }));
  await settle(5);
  out.postsWhileQueued = calls.posts;

  _g1Resolve();
  await settle(30);
  out.postsAfterIncumbent = calls.posts;

  _g2Resolve();
  out.results = await Promise.all([p1, p2, p3]);
  await settle(20);
  out.postsTotal = calls.posts;
  out.session = S.session && S.session.session_id;
  console.log(JSON.stringify(out));
})();
"""
    js = js.replace("__NEW_SESSION_BODY__", body).replace(
        "__PARAMS__", json.dumps({"replaceSlot": bool(replace_slot_for_run1)}))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_three_queued_owners_only_the_current_one_installs():
    """B3 (behavioural): three different owners queue behind one incumbent.

    Only the current owner may start/install; the superseded waiter aborts. A
    single await without re-checking let every waiter POST and overwrite the
    shared slot.
    """
    out = _run_three_owner_schedule()
    assert out["incumbentStarted"] is True, f"the incumbent POST never became in-flight: {out}"
    assert out["postsWhileQueued"] == 1, (
        f"a queued waiter issued its own POST instead of parking on the slot: {out}"
    )
    assert out["results"][1] == "aborted", (
        f"a caller superseded while queued still ran its own creation: {out} — it must "
        f"abort instead of installing a session under the newer switch's cookie"
    )
    assert out["results"][2] == "ran", f"the current owner must still run and install: {out}"
    assert out["postsTotal"] == 2, (
        f"expected exactly two POSTs (the incumbent and the surviving owner); more means "
        f"a superseded waiter created one too: {out}"
    )
    assert out["session"] == "created-by-2", (
        f"the surviving owner's session must be the installed one: {out}"
    )


def test_the_slot_is_cleared_only_while_it_still_belongs_to_this_run():
    """B3 (slot clause, contract): the shared slot's clear is ownership-guarded.

    Note on coverage, stated plainly: with the re-check loop in place the older
    `finally` cannot observe a slot a newer owner has taken over — the loop makes
    the waiters serialize, so that interleaving is unreachable. This test therefore
    pins the CONTRACT rather than reproducing the damage: the clear must stay
    conditional so it cannot be relaxed back into an unconditional one, which WAS
    reachable in the pre-loop shape (a third waiter overwrote the slot, then an
    earlier caller's unconditional finally cleared the newer run's slot).
    """
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function newSession(")
    finally_block = body[body.rindex("finally{"):]
    assert "_newSessionInFlight===_run" in finally_block, (
        "the shared slot may only be cleared while it still identifies this run; an "
        "unconditional clear can delete a newer owner's live run, after which the next "
        "caller starts a second concurrent creation and neither is adopted"
    )
    assert "_newSessionInFlightGen===callerGen" in finally_block, (
        "the owner generation must be part of the clear condition"
    )


# ── Gate round 10 (23 Sep): a profile switch must not adopt the old profile's
#    live stream, and the per-session stream carries no profile to filter on ────
#
# `/api/session/stream` subscribes by session id alone and its
# `server_turn_started` frame carries no profile (api/routes.py), while the SSE
# list channel DOES filter by profile (`_sessionEventProfilesMatch`). So during a
# profile switch — cookie moved, S.session still naming the old profile's session —
# the only place that can reject the old profile's live turn is the frontend.
# Measured before the fix: with the marker retained the old pane's frame was
# REJECTED (so the pre-round-9 shape was load-bearing), and clearing the marker at
# the stale exit let it through.

MESSAGES_JS_PATH = REPO_ROOT / "static" / "messages.js"
UNREAD_JS_PATH = REPO_ROOT / "static" / "sessions.js"

_REARM_HARNESS = r"""
const params = __PARAMS__;

const S = { session: params.session, activeProfile: params.activeProfile };
let started = [];
function startSessionStream(sid){ started.push(sid); }
// Revalidation is a network hop; the harness records the REQUEST. The contract under
// test is "an unresolved scope must ask instead of silently rejecting", and the
// recovered case proves a resolved scope is adopted and re-armed.
let revalidated = false;
function _revalidateActiveProfileRootScope(){ revalidated = true; }
let _loadingSessionId = null;

// The production ownership helper, injected verbatim.
__OWNERSHIP_BODY__

// The profile comparisons the re-arm guard consults (verbatim from sessions.js):
// the base matcher, the symmetric root-alias rule, and the cache-backed root resolver.
function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}
function _cronProfileNameIsRootAlias(name) { return name === 'default'; }
function _paneProfileMatchesActiveProfile(paneProfile, activeProfile){
  if(_profileMatchesActiveProfile(paneProfile, activeProfile)) return true;
  const paneName = (typeof paneProfile === 'string' && paneProfile.trim()) ? paneProfile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(paneName === activeName) return true;
  return activeName === 'default'
    && !!(typeof S !== 'undefined' && S && S.activeProfileIsDefault)
    && typeof _cronProfileNameIsRootAlias === 'function'
    && _cronProfileNameIsRootAlias(paneName);
}

__REARM_BODY__

_rearmActiveSessionStream();
console.log(JSON.stringify({ started: started }));
"""


def _run_rearm(*, session_profile, active_profile):
    """Drive the real `_rearmActiveSessionStream()` with the pane still on the old profile."""
    src_js = _read(SESSIONS_JS_PATH)
    rearm = src_js[src_js.index("function _rearmActiveSessionStream("):]
    rearm = rearm[: rearm.index("\n}\n") + 3]
    ownership = src_js[src_js.index("function _profileSwitchOwnsLoad("):]
    ownership = ownership[: ownership.index("\n}\n") + 3]
    js = _REARM_HARNESS.replace("__OWNERSHIP_BODY__", ownership).replace(
        "__REARM_BODY__", rearm).replace("__PARAMS__", json.dumps({
            "session": {"session_id": "old-pane", "profile": session_profile},
            "activeProfile": active_profile,
        }))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_profile_switch_does_not_re_arm_the_old_profiles_stream():
    """Round 10: arming a stream for the pane we are leaving subscribes under the
    NEW cookie for the OLD profile's session, whose frames can attach here."""
    out = _run_rearm(session_profile="old-profile", active_profile="target-profile")
    assert out["started"] == [], (
        f"re-arming armed a stream for the previous profile's session under the new "
        f"profile's cookie; /api/session/stream is keyed by session id alone and its "
        f"frames carry no profile, so that profile's live turn can attach to this "
        f"pane (Greptile P1, round 10): {out}"
    )
    # Control: a pane that DOES belong to the active profile still gets its stream.
    ok = _run_rearm(session_profile="target-profile", active_profile="target-profile")
    assert ok["started"] == ["old-pane"], (
        f"the guard must not block arming for a session the active profile owns: {ok}"
    )


_PANE_HARNESS = r"""
const params = __PARAMS__;

const S = { session: params.session, activeProfile: params.activeProfile,
            activeProfileIsDefault: false };
let _loadingSessionId = params.marker;

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}
function _cronProfileNameIsRootAlias(name) { return name === 'default'; }
function _paneProfileMatchesActiveProfile(paneProfile, activeProfile){
  if(_profileMatchesActiveProfile(paneProfile, activeProfile)) return true;
  const paneName = (typeof paneProfile === 'string' && paneProfile.trim()) ? paneProfile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(paneName === activeName) return true;
  return activeName === 'default'
    && !!(typeof S !== 'undefined' && S && S.activeProfileIsDefault)
    && typeof _cronProfileNameIsRootAlias === 'function'
    && _cronProfileNameIsRootAlias(paneName);
}

__PANE_BODY__

console.log(JSON.stringify({ current: _isSessionCurrentPane('old-pane') }));
"""


def _run_pane_guard(*, pane_profile, active_profile, marker=None):
    """Drive the real `_isSessionCurrentPane()` (messages.js) for the pane on screen."""
    src_js = _read(MESSAGES_JS_PATH)
    pane = src_js[src_js.index("function _isSessionCurrentPane("):]
    pane = pane[: pane.index("\n}\n") + 3]
    js = _PANE_HARNESS.replace("__PANE_BODY__", pane).replace("__PARAMS__", json.dumps({
        "session": {"session_id": "old-pane", "profile": pane_profile},
        "activeProfile": active_profile,
        "marker": marker,
    }))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_frames_for_a_pane_from_another_profile_are_rejected():
    """Round 10: once the marker no longer covers the old pane (the round-9 fix),
    the profile is the last thing that can reject the old profile's frames."""
    stale = _run_pane_guard(pane_profile="old-profile", active_profile="target-profile")
    assert stale["current"] is False, (
        f"a frame for the previous profile's pane was accepted after the profile "
        f"switched; the turn would attach to the wrong profile's pane: {stale}"
    )
    # With the marker still set the pre-existing guard already rejected it — that is
    # why the old shape was load-bearing, and why the profile check must carry it now.
    marked = _run_pane_guard(pane_profile="old-profile", active_profile="target-profile",
                             marker="A-sid")
    assert marked["current"] is False, marked
    # Control: the pane's own profile is unaffected.
    ok = _run_pane_guard(pane_profile="target-profile", active_profile="target-profile")
    assert ok["current"] is True, (
        f"the guard must not reject a pane that belongs to the active profile: {ok}"
    )


# ── Gate round 11 (23 Sep): the profile guards must treat a renamed root and
#    'default' as the same root in BOTH directions ──────────────────────────────
#
# The backend treats a renamed root (e.g. `kinni`) and `default` as equivalent.
# `_profileMatchesActiveProfile` covered literal equality plus the FORWARD alias
# (a name tagged 'default' while the active surface is a renamed root). The pane
# guard and the re-arm guard added in round 10 used only that rule, so a session
# created under the renamed root — restored by a later boot that reports the root as
# 'default' — had its own frames rejected and its stream never reopened:
# `_isSessionCurrentPane()` returned false and `_rearmActiveSessionStream()` bailed,
# so the restored conversation stopped receiving live updates.

_RENAME_ROOT_HARNESS = r"""
const params = __PARAMS__;

const S = { session: params.session, activeProfile: params.activeProfile,
            activeProfileIsDefault: !!params.activeProfileIsDefault,
            activeProfileRootNames: params.rootNames,
            // False == the server listing failed, so this scope is the fail-closed
            // default rather than a resolved view of the profiles that exist.
            activeProfileRootNamesAuthoritative: !!params.rootNamesAuthoritative };
let _loadingSessionId = null;
let started = [];
function startSessionStream(sid){ started.push(sid); }
// Revalidation is a network hop; the harness records the REQUEST. The contract under
// test is "an unresolved scope must ask instead of silently rejecting", and the
// recovered case proves a resolved scope is adopted and re-armed.
let revalidated = false;
function _revalidateActiveProfileRootScope(){ revalidated = true; }
// Server-provided profile cache: the entry flagged is_default IS the root, whatever
// its name — that is exactly how a renamed root is reported. Gate round 13: authority
// no longer comes from this roster, so the harness also carries the CANONICAL scope
// the server delivers with the active-profile state.
const _profilesCache = { profiles: params.profiles };

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}

function _cronProfileNameIsRootAlias(name) {
  if (name === 'default') return true;
  if (typeof _profilesCache !== 'undefined' && _profilesCache
    && Array.isArray(_profilesCache.profiles)) {
    const entry = _profilesCache.profiles.find((p) => p && p.name === name);
    if (entry && entry.is_default) return true;
  }
  return false;
}

__HELPER_BODY__

__REARM_BODY__

const pane = _isSessionCurrentPane('pane');
const rearm = (() => { _rearmActiveSessionStream(); return started.slice(); })();
console.log(JSON.stringify({
  pane: pane,
  revalidated: revalidated,
  rearm: rearm,
}));
"""


def _run_renamed_root(*, pane_profile, active_profile, active_is_default, profiles):
    """Drive the real pane predicate + re-arm guard for a renamed-root session."""
    src_js = _read(SESSIONS_JS_PATH)
    # The authority chain the pane predicate consults, shipped verbatim: the canonical
    # scope reader, the canonical-only admission rule, and the pane rule itself.
    chain = ""
    for _name in ("_activeProfileRootNamesSet", "_canonicalProfileRootAlias",
                  "_paneProfileMatchesActiveProfile"):
        _seg = src_js[src_js.index("function %s(" % _name):]
        chain += _seg[: _seg.index("\n}\n") + 3] + "\n"
    helper = chain.rstrip("\n")
    rearm = src_js[src_js.index("function _rearmActiveSessionStream("):]
    rearm = rearm[: rearm.index("\n}\n") + 3]
    pane = _read(MESSAGES_JS_PATH)
    pane_body = pane[pane.index("function _isSessionCurrentPane("):]
    pane_body = pane_body[: pane_body.index("\n}\n") + 3]
    js = _RENAME_ROOT_HARNESS.replace("__HELPER_BODY__", helper + "\n" + pane_body).replace(
        "__REARM_BODY__", rearm).replace("__PARAMS__", json.dumps({
            "session": {"session_id": "pane", "profile": pane_profile},
            "activeProfile": active_profile,
            "activeProfileIsDefault": active_is_default,
            "profiles": profiles,
            # Canonical scope, as delivered by /api/profile/active: the root-flagged
            # roster entries ARE the canonical root names.
            "rootNames": [p["name"] for p in profiles if p.get("is_default") and p.get("name")] or None,
            "rootNamesAuthoritative": True,
        }))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_renamed_root_and_default_are_the_same_root_for_the_profile_guards():
    """Round 11: a session created under the renamed root must keep its stream when a
    later boot reports the same root as 'default'."""
    # Boot reports the root as 'default'; the session carries the renamed-root name.
    out = _run_renamed_root(pane_profile="kinni", active_profile="default",
                            active_is_default=True,
                            profiles=[{"name": "kinni", "is_default": True}])
    assert out["pane"] is True, (
        f"a pane belonging to the renamed root was treated as another profile and its "
        f"frames rejected — the restored conversation would stop receiving live "
        f"updates (Greptile P1, round 11): {out}"
    )
    assert out["rearm"] == ["pane"], (
        f"the stream of a renamed-root pane was not reopened: {out}"
    )


def test_the_profile_guards_still_reject_a_genuinely_different_profile():
    """Control: symmetry must not weaken round 10 — a real cross-profile pane stays out."""
    out = _run_renamed_root(pane_profile="other-profile", active_profile="default",
                            active_is_default=True,
                            profiles=[{"name": "kinni", "is_default": True}])
    assert out["pane"] is False, (
        f"a pane from a genuinely different profile was accepted: {out}"
    )
    assert out["rearm"] == [], f"a cross-profile stream was armed: {out}"
    # And the forward direction (pane 'default' while the surface is the renamed root)
    # must still work, so the new rule is symmetric rather than merely relaxed.
    fwd = _run_renamed_root(pane_profile="default", active_profile="kinni",
                            active_is_default=True,
                            profiles=[{"name": "kinni", "is_default": True}])
    assert fwd["pane"] is True and fwd["rearm"] == ["pane"], fwd


# ── Gate round 12 (23 Sep): three blockers ────────────────────────────────────
#
# G1 malformed truthy bodies still reported a successful resume; G2 several stale
# exits stranded `_loadingSessionId`; G3 profile-scope authority depended on the UI
# roster, which is empty at cold boot and can be stale in the other direction.

_ENSURE_BODY_HARNESS = r"""
const params = __PARAMS__;

let S = { session: { session_id: 'A-sid', messages: [], last_usage: {} },
          messages: [], toolCalls: [], lastUsage: {} };
const INFLIGHT = {};
let _loadingSessionId = 'A-sid';
let _loadSessionGeneration = 7;
let _messagesTruncated = false;
let _oldestIdx = 0;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
let _pendingCarryForwardSnapshot = null;
let _profileSwitchGeneration = 1;

function api(){ return Promise.resolve(params.response); }
function _clearSameSessionForceReloadHint(){}
function _messageReloadLimitForSession(){ return 2; }
function _syncToolCallsForLoadedMessages(){}
function clearLiveToolCards(){}
function clearVisibleMessageRowCache(){}
function _hydrateTodosFromSession(){}
function scheduleTodosRefresh(){}
function syncTopbar(){}
function _setSessionViewedCount(){}
function _isSessionActivelyViewedForList(){ return true; }
var window = {};

// The ownership rule the body consults (verbatim from sessions.js).
__OWNERSHIP_BODY__

__ENSURE_BODY__

(async () => {
  const result = await _ensureMessagesLoaded('A-sid', {
    force: false, loadGeneration: 7, switchGen: 1,
  });
  console.log(JSON.stringify({
    result: (result === undefined ? 'undefined' : result),
    messages: Array.isArray(S.messages) ? S.messages.length : null,
    truncated: _messagesTruncated,
    oldestIdx: _oldestIdx,
  }));
})();
"""


def _run_message_body(response):
    """Drive the real `_ensureMessagesLoaded()` against a chosen response envelope."""
    body = _top_level_function_body(_read(SESSIONS_JS_PATH), "async function _ensureMessagesLoaded(")
    ownership = _read(SESSIONS_JS_PATH)
    ownership = ownership[ownership.index("function _profileSwitchOwnsLoad("):]
    ownership = ownership[: ownership.index("\n}\n") + 3]
    js = _ENSURE_BODY_HARNESS.replace("__OWNERSHIP_BODY__", ownership).replace(
        "__ENSURE_BODY__", body).replace("__PARAMS__", json.dumps({"response": response}))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_malformed_truthy_message_bodies_are_rejected_before_any_mutation():
    """G1: `{session:{}}`, `{session:"x"}`, a wrong id and non-array messages must all
    fail — not install an empty transcript and report a successful resume."""
    bad = [
        {"session": {}},                        # no session_id
        {"session": "x"},                       # primitive
        {"session": ["A-sid"]},                 # array
        {"session": {"session_id": "OTHER", "messages": []}},   # wrong id
        {"session": {"session_id": "A-sid", "messages": "nope"}},  # wrong-typed messages
        {"session": {"session_id": "A-sid"}},                      # messages missing
        {"session": {"session_id": "A-sid", "messages": None}},    # messages null
    ]
    for response in bad:
        out = _run_message_body(response)
        assert out["result"] is False, (
            f"a malformed truthy body was accepted as a successful resume: "
            f"response={response!r} -> {out}"
        )
        # Nothing may be installed on a rejected envelope.
        assert out["messages"] == 0 and out["truncated"] is False and out["oldestIdx"] == 0, (
            f"a rejected envelope still mutated state: {response!r} -> {out}"
        )


def test_an_empty_messages_array_is_still_a_successful_body():
    """Positive control: a well-formed envelope with zero messages is a valid resume,
    so the new validation tightens the envelope without rejecting empty sessions."""
    out = _run_message_body({"session": {"session_id": "A-sid", "messages": []}})
    assert out["result"] is True, f"a valid empty transcript must still succeed: {out}"


# ── G3: profile-scope authority must not depend on the UI roster ──────────────

_AUTHORITY_HARNESS = r"""
const params = __PARAMS__;

// Deliberately NO _profilesCache: the gate's cold-boot schedule, where the roster
// has not been fetched yet. Authority must still resolve.
const S = { session: params.session, activeProfile: params.activeProfile,
            activeProfileIsDefault: !!params.activeProfileIsDefault,
            activeProfileRootNames: params.rootNames,
            // False == the server listing failed, so this scope is the fail-closed
            // default rather than a resolved view of the profiles that exist.
            activeProfileRootNamesAuthoritative: !!params.rootNamesAuthoritative };
let _loadingSessionId = null;
let started = [];
function startSessionStream(sid){ started.push(sid); }
// Revalidation is a network hop; the harness records the REQUEST. The contract under
// test is "an unresolved scope must ask instead of silently rejecting", and the
// recovered case proves a resolved scope is adopted and re-armed.
let revalidated = false;
function _revalidateActiveProfileRootScope(){ revalidated = true; }

function _profileMatchesActiveProfile(profile, activeProfile){
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if(eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}
// The roster-backed resolver. Gate round 13: it is NOT an authority input, so a
// STALE roster must have no effect — the harness seeds one deliberately.
function _cronProfileNameIsRootAlias(name) {
  if (name === 'default') return true;
  if (typeof _profilesCache !== 'undefined' && _profilesCache
    && Array.isArray(_profilesCache.profiles)) {
    const entry = _profilesCache.profiles.find((p) => p && p.name === name);
    if (entry && entry.is_default) return true;
  }
  return false;
}

// A deliberately STALE roster: it still claims `kinni` is the root even when the
// server scope says otherwise (or is absent).
var _profilesCache = params.staleRoster
  ? { profiles: [{ name: 'kinni', is_default: true }] }
  : null;

__HELPER_BODY__

__REARM_BODY__

const pane = _isSessionCurrentPane('pane');
const rearm = (() => { _rearmActiveSessionStream(); return started.slice(); })();
console.log(JSON.stringify({ pane: pane, rearm: rearm, revalidated: revalidated }));
"""


def _run_authority(*, pane_profile, active_profile, active_is_default, root_names,
                   stale_roster=False, root_names_authoritative=True):
    src_js = _read(SESSIONS_JS_PATH)
    for name in ("_activeProfileRootNamesSet", "_activeProfileRootNamesResolved",
                 "_canonicalProfileRootAlias",
                 "_cronProfileNameIsRootAlias", "_paneProfileMatchesActiveProfile"):
        helper = src_js[src_js.index("function %s(" % name):]
        if name == "_cronProfileNameIsRootAlias":
            helper = helper[: helper.index("\n}\n") + 3]
        elif name == "_activeProfileRootNamesSet":
            helper = helper[: helper.index("\n}\n") + 3]
        else:
            helper = helper[: helper.index("\n}\n") + 3]
        globals()["_h_" + name] = helper
    helpers = "\n".join(globals()["_h_" + n] for n in
                        ("_activeProfileRootNamesSet", "_activeProfileRootNamesResolved",
                         "_canonicalProfileRootAlias",
                         "_cronProfileNameIsRootAlias", "_paneProfileMatchesActiveProfile"))
    rearm = src_js[src_js.index("function _rearmActiveSessionStream("):]
    rearm = rearm[: rearm.index("\n}\n") + 3]
    pane = _read(MESSAGES_JS_PATH)
    pane_body = pane[pane.index("function _isSessionCurrentPane("):]
    pane_body = pane_body[: pane_body.index("\n}\n") + 3]
    js = _AUTHORITY_HARNESS.replace("__HELPER_BODY__", helpers + "\n" + pane_body).replace(
        "__REARM_BODY__", rearm).replace("__PARAMS__", json.dumps({
            "session": {"session_id": "pane", "profile": pane_profile},
            "activeProfile": active_profile,
            "activeProfileIsDefault": active_is_default,
            "rootNames": root_names,
            "rootNamesAuthoritative": bool(root_names_authoritative),
            "staleRoster": bool(stale_roster),
        }))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_profile_authority_resolves_on_cold_boot_without_the_roster():
    """G3: with no `_profilesCache` at all (the cold boot this PR is meant to fix), the
    server-provided root set must still authorise the renamed-root pane."""
    out = _run_authority(pane_profile="kinni", active_profile="default",
                         active_is_default=True, root_names=["default", "kinni"])
    assert out["pane"] is True, (
        f"profile authority failed at cold boot because it depended on the UI roster: "
        f"{out} (gate G3)"
    )
    assert out["rearm"] == ["pane"], f"the stream was not reopened at cold boot: {out}"


def test_profile_authority_ignores_a_stale_roster_in_the_other_direction():
    """G3: a roster that no longer lists the pane's name must not override the
    server-provided root set (the stale-cache case the gate called out)."""
    out = _run_authority(pane_profile="kinni", active_profile="default",
                         active_is_default=True, root_names=["default", "kinni"])
    assert out["pane"] is True, (
        f"authority disagreed with the server-provided root set: {out} (gate G3)"
    )
    # Control: a name the server does NOT list as a root is still rejected, so the
    # rule is driven by the server set rather than by is_default alone.
    ctl = _run_authority(pane_profile="other-profile", active_profile="default",
                         active_is_default=True, root_names=["default", "kinni"])
    assert ctl["pane"] is False and ctl["rearm"] == [], (
        f"a non-root profile was authorised: {ctl}"
    )


def test_a_stale_roster_cannot_grant_stream_authority():
    """Gate round 13 + Greptile round 14: a stale roster must not stand in for canonical
    authority. This harness genuinely creates the stale states (a real roster insisting
    `kinni` is root, a genuinely unresolved scope), which the earlier case did not."""
    # A roster insisting `kinni` is the root, with NO canonical scope resolved: the
    # roster must not stand in for canonical authority.
    no_scope = _run_authority(pane_profile="kinni", active_profile="default",
                              active_is_default=True, root_names=None,
                              stale_roster=True)
    assert no_scope["pane"] is False and no_scope["rearm"] == [], (
        f"a stale roster granted stream authority with no canonical scope: {no_scope}"
    )
    # A genuinely UNRESOLVED scope (server listing failed -> fail-closed default) must
    # not be treated as a resolved view of the profiles that exist.
    unresolved = _run_authority(pane_profile="kinni", active_profile="default",
                                active_is_default=True, root_names=["default"],
                                root_names_authoritative=False, stale_roster=True)
    assert unresolved["pane"] is False, (
        f"an unresolved scope must still fail closed: {unresolved}"
    )
    assert unresolved["revalidated"] is True, (
        f"an unresolved scope must trigger revalidation instead of silently rejecting "
        f"the renamed root for the life of the page (Greptile P1, round 14): {unresolved}"
    )
    # …and revalidation must actually RECONNECT the pane once a resolved scope arrives.
    recovered = _run_authority(pane_profile="kinni", active_profile="default",
                               active_is_default=True, root_names=["default", "kinni"],
                               root_names_authoritative=True, stale_roster=True)
    assert recovered["pane"] is True and recovered["rearm"] == ["pane"], (
        f"a resolved scope must re-admit the renamed root and re-arm its stream: {recovered}"
    )
    # A roster still listing a name the canonical scope does NOT: canonical wins.
    stale_extra = _run_authority(pane_profile="rogue-root", active_profile="default",
                                 active_is_default=True, root_names=["default"],
                                 stale_roster=True)
    assert stale_extra["pane"] is False and stale_extra["rearm"] == [], (
        f"a roster overrode the canonical scope: {stale_extra}"
    )
    # Reverse control: with canonical scope PRESENT, the renamed root is admitted.
    canon = _run_authority(pane_profile="kinni", active_profile="default",
                           active_is_default=True, root_names=["default", "kinni"],
                           stale_roster=True)
    assert canon["pane"] is True and canon["rearm"] == ["pane"], (
        f"canonical scope should admit the renamed root even with a stale roster: {canon}"
    )


# ── G2: the draft-save and 409 abandonment exits, with the replacement switch
#        deliberately NOT starting another loadSession() ─────────────────────────
#
# Both exits returned without retiring a marker they still owned, so a superseded
# switch that took a no-load fallback left the old id installed and
# `_isSessionCurrentPane()` then rejected the current pane's frames.

_ABANDON_HARNESS = r"""
const params = __PARAMS__;

var S = { session: params.seedSession, messages: [{ role: 'assistant', content: 'seed' }],
          toolCalls: [], pendingFiles: [], busy: false, activeStreamId: null,
          _pendingSessionToolsets: null, lastUsage: {} };
const INFLIGHT = {};
let _loadingSessionId = null;
let _loadingOlder = false;
let _loadSessionGeneration = 0;
let _loadMessagesFailedSids = new Set();
function _loadMessagesFailedForSid(sid){ return _loadMessagesFailedSids.has(sid); }
let _pendingCarryForwardSnapshot = null;
let _messagesTruncated = false;
let _oldestIdx = 0;
let _messageRenderWindowSize = 0;
let _msgLimitMax = 500;
const _MSG_LIMIT_MAX = 500;
let _messageUserUnpinned = false;
let _scrollPinned = true;
let _profileSwitchGeneration = 1;
const _switchGen = 1;

const calls = { draftSaves: 0, rearm: 0, switchProfileCalls: 0, metaCalls: 0 };

// The metadata request is held open so the driver owns the interleaving, and is
// REJECTED with a profile-mismatch body to reach the 409 recovery path.
let _metaReject = null;
const metaGate = new Promise((_res, rej) => { _metaReject = rej; });
function api(path){
  if (String(path).includes('messages=0')) { calls.metaCalls += 1; return metaGate; }
  return Promise.resolve({});
}
function rejectMetaWithProfileMismatch(){
  const err = new Error('profile mismatch');
  err.status = 409;
  err.body = JSON.stringify({ code: 'session_profile_mismatch',
                              profile: 'other-profile', session_id: 'target-sid' });
  _metaReject(err);
}
const wait = () => new Promise(r => setImmediate(r));

// The awaited draft save: yields the event loop exactly like the shipped one.
function _saveComposerDraftNow(){ calls.draftSaves += 1; return Promise.resolve(); }
// The 409 recovery's profile switch: another await boundary the load can lose at.
// HELD open by the driver so ownership can be moved while it is genuinely in flight —
// otherwise the continuation's own retry would run first and the scenario would model
// the wrong interleaving.
let _switchRelease = null;
const switchGate = new Promise(res => { _switchRelease = res; });
let _holdSwitch = false;
function _switchProfileForSessionLoad(){
  calls.switchProfileCalls += 1;
  return _holdSwitch ? switchGate : Promise.resolve();
}
function _sessionProfileMismatchFromError(e){
  return (e && e.body && JSON.parse(e.body).profile) ? JSON.parse(e.body) : null;
}
function _profileMatchesActiveProfile(p, a){ return String(p||'default').trim() === String(a||'default').trim(); }
function _cronProfileNameIsRootAlias(){ return false; }
function _activeProfileRootNamesSet(){ return null; }
function _rearmActiveSessionStream(){ calls.rearm += 1; }
function _setActiveSessionUrl(){}
function startSessionStream(){}
function _appRootPath(){ return '/'; }
function $(){ return null; }
var window = {}; var history = { replaceState(){} };
var localStorage = { setItem(){}, removeItem(){}, getItem(){ return null; } };
function _clearSameSessionForceReloadHint(){}
function _clearStuckSessionOnBoot(){}
function _sessionVisitHasUnreadState(){ return false; }
function _acknowledgeSessionVisit(){}
function _setSessionViewedCount(){}
function scheduleTodosRefresh(){}
function syncTopbar(){}
function _captureSameSessionForceReloadHint(){}
function _resolveSessionModelForDisplaySoon(){}
function _deferWorkspaceRefreshForSession(){}
function _applyPendingSessionModelForSession(){}
function _hydrateTodosFromSession(){}
function _syncCtxIndicator(){}
function _renderPendingPromptsForActiveSession(){}
function _restoreComposerDraft(){}
function _checkAndShowHandoffHint(){}
function _hideHandoffHint(){}
function _isMessagingSession(){ return false; }
function _clearDeferredActiveSessionExternalRefresh(){}
function setStatus(){}
function setComposerStatus(){}
function setBusy(){}
function updateSendBtn(){}
function updateQueueBadge(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function stopApprovalPolling(){}
function hideApprovalCard(){}
function stopSessionStream(){}
function stopClarifyPolling(){}
function hideClarifyCard(){}
let _yoloEnabled = false;
function _updateYoloPill(){}
function clearCompressionUi(){}
function _clearPendingSelections(){}
function _clearQueueCardDisplay(){}
function loadInflightState(){ return null; }
function _messageReloadLimitForSession(){ return 2; }
function _uploadPendingFilesSyncProgressForSession(){}
function autoResize(){}
function showToast(){}
function closeOtherLiveStreams(){}
function _isSessionActivelyViewedForList(){ return true; }
function _syncToolCallsForLoadedMessages(){}
function clearVisibleMessageRowCache(){}
function clearLiveToolCards(){}
function _ensureMessagesLoaded(){ return Promise.resolve(true); }
function _selectLiveRecoveryInflight(){ return null; }
function _inflightHasVisibleLiveState(){ return false; }

__OWNERSHIP_BODY__

__LOAD_BODY__

(async () => {
  const out = {};

  // ── Scenario 1: lose ownership during the awaited draft save ───────────────
  S.messages = [{ role: 'assistant', content: 'seed' }];
  const p1 = loadSession(params.targetSid, { switchGen: 1, profileSwitchOwned: true });
  for(let i = 0; i < 200 && calls.draftSaves === 0; i++){ await wait(); }
  out.draftSaveStarted = calls.draftSaves === 1;
  // A newer switch takes over WITHOUT starting another loadSession.
  _profileSwitchGeneration = 2;
  await p1;
  for(let i = 0; i < 20; i++){ await wait(); }
  out.markerAfterDraftSave = (_loadingSessionId === undefined ? 'undefined' : _loadingSessionId);

  // ── Scenario 2: lose ownership during the 409 recovery's profile switch ────
  _loadingSessionId = null;
  _profileSwitchGeneration = 1;
  calls.switchProfileCalls = 0;
  S.session = params.seedSession;
  const p2 = loadSession(params.targetSid, { switchGen: 1, profileSwitchOwned: true,
                                             skipProfileResolve: false, force: true });
  // Wait until the metadata request is genuinely in flight, then fail it with the
  // 409 mismatch the recovery path is keyed on.
  for(let i = 0; i < 200 && calls.metaCalls === 0; i++){ await wait(); }
  out.metaRequestStarted = calls.metaCalls === 1;
  _holdSwitch = true;
  rejectMetaWithProfileMismatch();
  for(let i = 0; i < 200 && calls.switchProfileCalls === 0; i++){ await wait(); }
  out.switchRecoveryStarted = calls.switchProfileCalls === 1;
  // Ownership moves while the recovery switch is genuinely in flight.
  _profileSwitchGeneration = 2;
  _switchRelease();
  await p2;
  for(let i = 0; i < 20; i++){ await wait(); }
  out.markerAfterSwitchRecovery = (_loadingSessionId === undefined ? 'undefined' : _loadingSessionId);

  console.log(JSON.stringify(out));
})();
"""


def _run_abandonment_scenarios():
    """Drive the real `loadSession()` through both abandonment exits with no successor load."""
    src_js = _read(SESSIONS_JS_PATH)
    body = _top_level_function_body(src_js, "async function loadSession(")
    ownership = src_js[src_js.index("function _profileSwitchOwnsLoad("):]
    ownership = ownership[: ownership.index("\n}\n") + 3]
    js = _ABANDON_HARNESS.replace("__OWNERSHIP_BODY__", ownership).replace(
        "__LOAD_BODY__", body).replace("__PARAMS__", json.dumps({
            "targetSid": "target-sid",
            "seedSession": {"session_id": "seed-pane", "messages": []},
        }))
    # The 409 path needs the metadata request to reject with a profile-mismatch body,
    # and any later metadata call must be a well-formed body for the target session so a
    # (buggy) retry cannot crash the harness and hide the marker assertion.
    js = js.replace(
        "return calls.metaCalls === 1 ? metaGate : Promise.resolve({});",
        "return calls.metaCalls === 1 ? metaGate : Promise.resolve("
        "{ session: { session_id: 'target-sid', message_count: 0, "
        "active_stream_id: null, messages: [] } });")
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_marker_is_retired_when_ownership_is_lost_at_each_abandonment_exit():
    """G2: the draft-save exit and the 409 recovery exit must each retire the marker
    they still own, even when the replacement switch starts no other loadSession()."""
    out = _run_abandonment_scenarios()
    assert out["draftSaveStarted"] is True, (
        f"precondition: the awaited draft save must actually run, otherwise the "
        f"scenario is never exercised: {out}"
    )
    assert out["markerAfterDraftSave"] is None, (
        f"the draft-save abandonment left _loadingSessionId={out['markerAfterDraftSave']!r} "
        f"installed with no successor load to overwrite it, so the current pane's frames "
        f"stay rejected (gate G2): {out}"
    )
    assert out["metaRequestStarted"] is True and out["switchRecoveryStarted"] is True, (
        f"precondition: the metadata request must be in flight and the 409 recovery's "
        f"profile switch must actually run, otherwise the scenario is never "
        f"exercised: {out}"
    )
    assert out["markerAfterSwitchRecovery"] is None, (
        f"the 409 recovery abandonment left _loadingSessionId="
        f"{out['markerAfterSwitchRecovery']!r} installed (gate G2): {out}"
    )

# ── Greptile round 14: authority never comes from a stale cache ───────────────
#
# `_root_profile_scope()` must fail closed to ['default'] when the listing raises,
# and must NOT publish the memoized root-name cache: that cache is invalidated only
# by mutations this process performed, so a root renamed out-of-band while the WebUI
# stays up would leave stale aliases deciding authority (AGENTS.md: authority checks
# fail closed; caches scoped by the complete identity). The same failure is reported
# as NON-authoritative so the client revalidates instead of treating the partial set
# as the final word.

PROFILES_PY = REPO_ROOT / "api" / "profiles.py"


def _run_root_scope_on_failure(*, cache_loaded: bool):
    """Drive the real `_root_profile_scope()` with the profile listing failing."""
    py = textwrap.dedent("""
        import json
        import sys
        sys.path.insert(0, {repo!r})
        import api.profiles as p
        with p._root_profile_name_cache_lock:
            p._root_profile_name_cache.clear()
            p._root_profile_name_cache.add('default')
            p._root_profile_name_cache.add('kinni')
            p._root_profile_name_cache_loaded = {loaded!r}
        p.list_profiles_api = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        names, authoritative = p._root_profile_scope()
        print(json.dumps({{'names': names, 'authoritative': authoritative}}))
    """).format(repo=str(REPO_ROOT), loaded=cache_loaded)
    proc = subprocess.run([sys.executable, "-c", py], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_failed_listing_fails_closed_and_never_publishes_the_stale_cache():
    """Greptile round 14: stale memoized aliases must not become authority."""
    loaded = _run_root_scope_on_failure(cache_loaded=True)
    assert loaded["names"] == ["default"], (
        f"a failed listing published the memoized cache instead of failing closed: "
        f"{loaded!r}. A root renamed out-of-band never invalidates that cache, so its "
        f"stale aliases would become canonical authority (Greptile P1, round 14)."
    )
    assert loaded["authoritative"] is False, (
        f"a failed listing must be reported NON-authoritative so the client revalidates "
        f"rather than treating ['default'] as final: {loaded!r}"
    )
    cold = _run_root_scope_on_failure(cache_loaded=False)
    assert cold["names"] == ["default"] and cold["authoritative"] is False, cold


def test_a_successful_listing_is_authoritative():
    """Positive control for the fail-closed path above."""
    import api.profiles as p
    scope_names, authoritative = p._root_profile_scope()
    assert scope_names and scope_names[0] == "default", scope_names
    assert authoritative is True, (
        f"a successful listing must be authoritative, otherwise the client revalidates "
        f"forever: {scope_names!r}"
    )


# ── Greptile round 14: fail closed, then RECONCILE ────────────────────────────
#
# With an unresolved root scope a renamed-root pane is correctly rejected — but the
# pane must not go silent for the life of the page. The pane-frame guard has to request
# one refresh so the stream reconnects once a resolved scope arrives. This pins the
# guard's own hook, not the re-arm path (which also requests it).

_PANE_HOOK_HARNESS = r"""
const params = __PARAMS__;
const S = { session: params.session, activeProfile: params.activeProfile,
            activeProfileIsDefault: !!params.activeProfileIsDefault };
let _loadingSessionId = null;
let revalidated = false;
function _revalidateActiveProfileRootScope(){ revalidated = true; }
// Reject everything: the point is what the guard does about a rejection.
function _paneProfileMatchesActiveProfile(){ return false; }
__PANE_BODY__
console.log(JSON.stringify({ pane: _isSessionCurrentPane('pane'), revalidated: revalidated }));
"""


def test_a_rejected_pane_requests_scope_revalidation():
    """Greptile round 14: a rejected pane must ask for a fresh scope, not go silent."""
    pane = _read(MESSAGES_JS_PATH)
    pane_body = pane[pane.index("function _isSessionCurrentPane("):]
    pane_body = pane_body[: pane_body.index("\n}\n") + 3]
    js = _PANE_HOOK_HARNESS.replace("__PANE_BODY__", pane_body).replace(
        "__PARAMS__", json.dumps({
            "session": {"session_id": "pane", "profile": "kinni"},
            "activeProfile": "default",
            "activeProfileIsDefault": True,
        }))
    proc = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["pane"] is False, (
        f"the guard must still reject an unprovable pane (fail closed): {out}"
    )
    assert out["revalidated"] is True, (
        f"a rejected pane went silent without requesting a fresh root scope, so a renamed "
        f"root would stop receiving live updates permanently (Greptile P1, round 14): {out}"
    )
