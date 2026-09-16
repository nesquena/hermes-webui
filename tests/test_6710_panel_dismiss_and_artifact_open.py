"""Regression tests for #6710 — workspace panel must stay closed after an
explicit user dismissal, while a genuine user-initiated file open still opens it.

The original fix recorded deliberate closes in ``_workspacePanelUserDismissed``
so the keyboard-resize viewport churn (which re-runs
``syncWorkspacePanelState()``) could no longer force the panel back open while
the user was typing. The gate's exact-head review found two gaps that this file
covers — both are about the *asynchronous* boundary in
``openArtifactPath()`` (``static/workspace.js``):

  Gap A1 — pending open must not resurrect a dismissed preview.
      ``openArtifactPath()`` used to clear ``_workspacePanelUserDismissed``
      *before* ``await _workspacePathExists(rel)``. While that request was in
      flight the stale preview was still ``.visible`` with dismissal already
      cleared, so any keyboard/rotation/URL-bar sync promoted the panel back to
      ``preview`` mid-reply.

  Gap A2 — a failed open must preserve dismissal.
      Both failure exits (missing file, request error) returned without
      restoring the flag, so a user action that failed permanently removed the
      guard and every later resize reopened the panel.

  Gap A3 — a successful open must transition the panel atomically.
      ``openFile(rel)`` was fire-and-forget and ``ensureWorkspacePreviewVisible()``
      had no production caller at all, so a valid path left panel mode
      ``closed`` until some later unrelated sync happened to fix it.

These tests drive the *real shipped functions* through a Node VM rather than
regex-matching source, so they fail if the async ordering regresses even when
the source still "looks" right.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS_PATH = REPO_ROOT / "static" / "workspace.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _shipped_functions() -> dict:
    """Extract the exact shipped function bodies that participate in the fix."""
    workspace_js = _read(WORKSPACE_JS_PATH)
    boot_js = _read(BOOT_JS_PATH)
    return {
        "openArtifactPath": extract_function(workspace_js, "openArtifactPath", prefix="async function"),
        "syncWorkspacePanelState": extract_function(boot_js, "syncWorkspacePanelState"),
        "ensureWorkspacePreviewVisible": extract_function(boot_js, "ensureWorkspacePreviewVisible"),
        "openWorkspacePanel": extract_function(boot_js, "openWorkspacePanel"),
        "closeWorkspacePanel": extract_function(boot_js, "closeWorkspacePanel"),
    }


_HARNESS = r"""
const params = __PARAMS__;
const fn = params.functions;

// ── minimal DOM/state doubles ────────────────────────────────────────────────
let _workspacePanelMode = 'closed';
let _workspacePanelUserDismissed = false;
let syncUiCalls = 0;
let setModeCalls = [];
let previewVisible = params.previewVisibleInitially;

function $(id){
  if(id === 'previewArea'){
    return { classList: { contains: (c) => c === 'visible' ? previewVisible : false } };
  }
  return { classList: { contains: () => false, toggle(){}, add(){}, remove(){} },
           style:{}, setAttribute(){}, getAttribute(){ return null; },
           textContent:'', addEventListener(){} };
}
function _hasWorkspacePreviewVisible(){
  const p = $('previewArea');
  return !!(p && p.classList.contains('visible'));
}
function _setWorkspacePanelMode(mode){
  _workspacePanelMode = (mode === 'browse' || mode === 'preview') ? mode : 'closed';
  setModeCalls.push(_workspacePanelMode);
}
function syncWorkspacePanelUI(){ syncUiCalls++; }
function _isCompactWorkspaceViewport(){ return true; }
function _workspacePanelEls(){ return { layout: $('layout'), panel: $('panel') }; }
function setStatus(){}
function t(k){ return k; }
function switchWorkspacePanelTab(){}
const S = { session: { session_id: 's1', workspace: '/ws' } };

// openFile: mirrors the shipped reveal contract — it flips the preview DOM
// visible. `params.openFileResolves` controls whether it settles immediately.
let openFileCalls = [];
async function openFile(path){
  openFileCalls.push(path);
  if(params.openFileSettles === 'never'){ return new Promise(()=>{}); }
  previewVisible = true;
}

// _workspacePathExists: async gate the harness controls.
let existsCalls = 0;
async function _workspacePathExists(rel){
  existsCalls++;
  if(params.existsMode === 'pending'){ return new Promise(()=>{}); }
  if(params.existsMode === 'error'){ throw new Error('boom'); }
  if(params.existsMode === 'missing'){ return false; }
  return true;
}

// ── install the exact shipped functions ──────────────────────────────────────
eval(fn.syncWorkspacePanelState);
eval(fn.ensureWorkspacePreviewVisible);
eval(fn.openWorkspacePanel);
eval(fn.closeWorkspacePanel);
eval(fn.openArtifactPath);

// ── scenario driver ──────────────────────────────────────────────────────────
(async () => {
  const out = { steps: [] };
  const snap = (label) => out.steps.push({
    label,
    mode: _workspacePanelMode,
    dismissed: _workspacePanelUserDismissed,
    previewVisible,
    syncUiCalls,
  });
  const tick = () => new Promise((resolve) => setImmediate(resolve));

  // 1. user dismisses the panel while a preview is open
  previewVisible = true;
  closeWorkspacePanel();
  snap('after closeWorkspacePanel');

  // 2. viewport churn (keyboard/rotation/URL-bar) must NOT reopen it
  syncWorkspacePanelState();
  syncWorkspacePanelState();
  snap('after resize sync');

  // 3. user clicks an artifact link — do NOT await yet: the 'pending' scenario
  //    deliberately never settles its existence check, so awaiting here would
  //    hang the harness instead of measuring the in-flight state.
  const openPromise = openArtifactPath('/ws/dir/valid.md');

  // 3a. let the async boundary be entered. The mid-flight resize is the
  //     Gap A1 probe, so fire it ONLY for the pending scenario — otherwise it
  //     would paper over a non-atomic reveal and the atomicity test could pass
  //     for the wrong reason.
  await tick();
  await tick();
  if(params.existsMode === 'pending'){
    syncWorkspacePanelState();
  }
  snap('during pending open');

  // 3b. settle only when the scenario actually settles
  if(params.existsMode !== 'pending'){
    await openPromise;
    await tick();
  }
  snap('after open settled');

  out.openFileCalls = openFileCalls;
  out.existsCalls = existsCalls;
  out.setModeCalls = setModeCalls;
  console.log(JSON.stringify(out));
})();
"""


def _run_scenario(*, exists_mode: str, open_file_settles: str = "immediately",
                  preview_visible_initially: bool = True) -> dict:
    payload = {
        "functions": _shipped_functions(),
        "existsMode": exists_mode,
        "openFileSettles": open_file_settles,
        "previewVisibleInitially": preview_visible_initially,
    }
    js = _HARNESS.replace("__PARAMS__", json.dumps(payload))
    proc = subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _step(result: dict, label: str) -> dict:
    for step in result["steps"]:
        if step["label"] == label:
            return step
    raise AssertionError(f"step {label!r} missing from {result['steps']}")


# ── 1. the original bug stays fixed: dismissal survives viewport churn ───────


def test_resize_sync_does_not_reopen_a_dismissed_preview():
    """Keyboard/rotation/URL-bar resize must leave a deliberately closed panel
    closed, even though the preview DOM is still visible underneath."""
    result = _run_scenario(exists_mode="ok")
    after_close = _step(result, "after closeWorkspacePanel")
    after_sync = _step(result, "after resize sync")

    assert after_close["mode"] == "closed", "closeWorkspacePanel must close the panel"
    assert after_close["dismissed"] is True, "closeWorkspacePanel must record the dismissal"
    assert after_sync["mode"] == "closed", (
        "a viewport resize re-opened a preview the user had deliberately dismissed"
    )


# ── Gap A1: the async boundary ───────────────────────────────────────────────


def test_pending_open_does_not_reopen_dismissed_preview():
    """While `_workspacePathExists()` is still in flight, viewport churn must not
    resurrect the stale preview — the dismissal flag must not be cleared early."""
    result = _run_scenario(exists_mode="pending")
    during = _step(result, "during pending open")

    assert during["dismissed"] is True, (
        "openArtifactPath cleared the dismissal flag BEFORE the async existence "
        "check resolved, so a resize during the pending request reopens the "
        "stale preview mid-reply"
    )
    assert during["mode"] == "closed", (
        "a pending artifact open must leave the panel closed until it succeeds"
    )


# ── Gap A2: failure paths must preserve the guard ─────────────────────────────


def test_missing_file_preserves_dismissal():
    """A failed open (file does not exist) must not strip the dismissal guard."""
    result = _run_scenario(exists_mode="missing")
    settled = _step(result, "after open settled")

    assert settled["dismissed"] is True, (
        "a failed open permanently removed the dismissal guard, so every later "
        "resize reopens the panel"
    )
    assert settled["mode"] == "closed", "a missing file must not open the panel"
    assert result["openFileCalls"] == [], "openFile must not run for a missing file"


def test_erroring_exists_check_preserves_dismissal():
    """A request error in the existence check must not strip the guard either."""
    result = _run_scenario(exists_mode="error")
    settled = _step(result, "after open settled")

    assert settled["dismissed"] is True, (
        "an errored existence check permanently removed the dismissal guard"
    )
    assert settled["mode"] == "closed", "an errored existence check must not open the panel"
    assert result["openFileCalls"] == [], "openFile must not run when the check errors"


# ── Gap A3: the success path must be atomic ──────────────────────────────────


def test_successful_open_transitions_panel_to_preview_atomically():
    """A valid artifact path must leave the panel in preview mode as part of the
    same user action — not wait for some later unrelated sync."""
    result = _run_scenario(exists_mode="ok")
    settled = _step(result, "after open settled")

    assert result["openFileCalls"] == ["dir/valid.md"], (
        f"openFile must be called once with the workspace-relative path, "
        f"got {result['openFileCalls']}"
    )
    assert settled["dismissed"] is False, (
        "a successful open must clear the dismissal so future previews auto-open"
    )
    assert settled["mode"] == "preview", (
        "a successful artifact open left the panel in 'closed' — the reveal was "
        "not atomic with the user action"
    )


def test_successful_open_while_panel_was_never_dismissed():
    """Closing the loop on the reopen path: with no prior dismissal the same
    atomic transition must still land in preview mode."""
    result = _run_scenario(exists_mode="ok", preview_visible_initially=False)
    settled = _step(result, "after open settled")

    assert settled["mode"] == "preview", (
        "artifact open from a never-opened panel must end in preview mode"
    )
