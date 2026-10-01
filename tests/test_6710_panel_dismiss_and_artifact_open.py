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
        # Shipped, not mirrored: the generation counter's bump policy IS the
        # behaviour under test for the reopen-during-read case, so the harness
        # must run the real function or it could pass against a stale copy.
        "_setWorkspacePanelDismissed": extract_function(boot_js, "_setWorkspacePanelDismissed"),
        # closeWorkspacePanel() now delegates the close bookkeeping here, so the
        # split policy (fence on every viewport, flag on compact only) is the
        # real shipped code too, not a harness mirror.
        "_markWorkspacePanelClosedByUser": extract_function(boot_js, "_markWorkspacePanelClosedByUser"),
    }


_HARNESS = r"""
const params = __PARAMS__;
const fn = params.functions;

// ── minimal DOM/state doubles ────────────────────────────────────────────────
let _workspacePanelMode = 'closed';
let _workspacePanelUserDismissed = false;
// The shipped code tracks a dismissal GENERATION so a read that is still
// pending when the user dismisses cannot erase that newer intent. The harness
// installs the REAL `_setWorkspacePanelDismissed()` (see the eval() block
// below) so the generation's bump policy is exercised, not mirrored.
let _workspacePanelDismissGen = 0;
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
function _isCompactWorkspaceViewport(){ return params.compactViewport; }
function _workspacePanelEls(){ return { layout: $('layout'), panel: $('panel') }; }
function setStatus(){}
function t(k){ return k; }
function switchWorkspacePanelTab(){}
const S = { session: { session_id: 's1', workspace: '/ws' } };

// openFile: mirrors the shipped reveal contract. On success it flips the
// preview DOM visible and returns true; on a failed read it returns false and
// leaves the preview hidden (the real function swallows read errors and
// reports them via its return value).
let openFileCalls = [];
async function openFile(path){
  openFileCalls.push(path);
  if(params.openFileSettles === 'never'){ return new Promise(()=>{}); }
  if(params.openFileReadFails){ return false; }
  if(params.openFileDownloadsOnly){ return false; }   // download-only (e.g. .zip)
  // The OLD contract: a bare `return;`. openArtifactPath() must fail closed on it
  // too, so the fix cannot rest on openFile() alone.
  if(params.openFileReturnsUndefined){ return undefined; }
  // A slow read: the harness can dismiss the panel while this is in flight, so
  // the "newer intent wins" guard has something to protect.
  if(params.slowRead){
    await new Promise((r) => setTimeout(r, 20));
  }
  previewVisible = true;
  return true;
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

eval(fn._setWorkspacePanelDismissed);
eval(fn._markWorkspacePanelClosedByUser);
eval(fn.syncWorkspacePanelState);
eval(fn.ensureWorkspacePreviewVisible);
eval(fn.openWorkspacePanel);
eval(fn.closeWorkspacePanel);
eval(fn.openArtifactPath);

// ── scenario driver ──────────────────────────────────────────────────────────
(async () => {
  const out = { steps: [] };
  const dismissGenStart = _workspacePanelDismissGen;
  const snap = (label) => out.steps.push({
    label,
    mode: _workspacePanelMode,
    dismissed: _workspacePanelUserDismissed,
    previewVisible,
    syncUiCalls,
  });
  const tick = () => new Promise((resolve) => setImmediate(resolve));

  // 1. user dismisses the panel while a preview is open. This is a SINGLE
  //    deliberate close, so it isolates the "one bump per close" policy from
  //    the second close the dismissDuringRead scenario performs later.
  previewVisible = true;
  const genBeforeFirstClose = _workspacePanelDismissGen;
  closeWorkspacePanel();
  out.genDeltaForSingleClose = _workspacePanelDismissGen - genBeforeFirstClose;
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

  // 3b. a slow read: the user dismisses the panel while openFile() is in
  //     flight. That newer intent must survive the read completing.
  if(params.dismissDuringRead){
    await tick();
    closeWorkspacePanel();
    snap('dismissed during read');
  }

  // 3b'. the mirror image: the user explicitly REOPENS the panel while the
  //      read is in flight. That intent agrees with the pending reveal, so it
  //      must not invalidate it — the read still completes and promotes.
  if(params.reopenDuringRead){
    await tick();
    await tick();
    openWorkspacePanel('browse');
    snap('reopened during read');
  }

  // 3c. settle only when the scenario actually settles
  if(params.existsMode !== 'pending'){
    out.openReturned = await openPromise;
    await tick();
  }
  snap('after open settled');

  out.openFileCalls = openFileCalls;
  out.existsCalls = existsCalls;
  out.setModeCalls = setModeCalls;
  // How far the action-generation fence moved across the whole scenario. A
  // deliberate close must move it exactly once on any viewport; a clear must
  // not move it at all.
  out.dismissGenDelta = _workspacePanelDismissGen - dismissGenStart;
  console.log(JSON.stringify(out));
})();
"""


def _run_scenario(*, exists_mode: str, open_file_settles: str = "immediately",
                  preview_visible_initially: bool = True,
                  open_file_read_fails: bool = False,
                  open_file_downloads_only: bool = False,
                  compact_viewport: bool = True,
                  dismiss_during_read: bool = False,
                  reopen_during_read: bool = False,
                  slow_read: bool = False,
                  open_file_returns_undefined: bool = False) -> dict:
    payload = {
        "functions": _shipped_functions(),
        "existsMode": exists_mode,
        "openFileSettles": open_file_settles,
        "previewVisibleInitially": preview_visible_initially,
        "openFileReadFails": open_file_read_fails,
        "openFileDownloadsOnly": open_file_downloads_only,
        "compactViewport": compact_viewport,
        "dismissDuringRead": dismiss_during_read,
        "reopenDuringRead": reopen_during_read,
        "slowRead": slow_read,
        "openFileReturnsUndefined": open_file_returns_undefined,
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


# ── Greptile review: a failed READ must not resurrect the dismissed panel ────
#
# The existence check passing does not mean the file could be read: the read can
# still fail (403 grant expired, oversized, binary→download, network error), and
# openFile() swallows those errors. Promoting the panel unconditionally after
# the await therefore treated a failed read as a successful open and cleared the
# user's dismissal, force-opening the panel onto stale or empty preview content.


def test_failed_read_preserves_dismissal():
    """A read failure after the existence check passed must not clear the
    dismissal flag — that is the state the flag exists to protect."""
    result = _run_scenario(exists_mode="ok", open_file_read_fails=True)
    settled = _step(result, "after open settled")

    assert result["openFileCalls"] == ["dir/valid.md"], (
        f"openFile should still be attempted, got {result['openFileCalls']}"
    )
    assert settled["dismissed"] is True, (
        "a failed read cleared the dismissal flag, so the next viewport change "
        "force-opens the panel onto stale or empty preview content"
    )
    assert settled["mode"] == "closed", (
        "a failed read promoted the panel to preview mode"
    )


def test_failed_read_does_not_reopen_after_viewport_churn():
    """The end-to-end consequence: dismiss → artifact link whose read fails →
    keyboard/rotation resize must leave the panel closed."""
    result = _run_scenario(exists_mode="ok", open_file_read_fails=True)
    after_link = _step(result, "after open settled")
    assert after_link["mode"] == "closed", (
        f"precondition: panel should still be closed after the failed read, got "
        f"{after_link}"
    )
    # A subsequent resize must not find a cleared flag to act on.
    assert after_link["dismissed"] is True, (
        "the dismissal guard was consumed by a failed read"
    )


# ── Gate review (17 Sep): the three silent correctness/scope gaps ────────────
#
# The gate confirmed the headline bug is fixed and asked for three more things:
# a download-only artifact must not count as a reveal, a read that outlives a
# later dismissal must not override it, and the guard must be mobile-scoped so
# desktop behaviour is untouched.

_DEFAULT_OPENFILE_NOTE = """
`openFile()` contract used by the harness: only a literal `true` means something
was previewed. The real function returns false for a download-only format
(``DOWNLOAD_EXTS`` → ``downloadFile()``) and for any swallowed read failure; the
harness mirrors that so ``openArtifactPath()`` cannot pass a test by reading
``undefined`` as success.
"""


def test_download_only_artifact_is_not_treated_as_a_reveal():
    """A .zip-style artifact downloads; it must not clear the dismissal or open
    the panel onto the stale preview underneath."""
    result = _run_scenario(exists_mode="ok", open_file_downloads_only=True)
    settled = _step(result, "after open settled")

    assert result["openFileCalls"] == ["dir/valid.md"], (
        f"openFile should still be attempted, got {result['openFileCalls']}"
    )
    assert settled["dismissed"] is True, (
        "a download-only artifact cleared the dismissal, force-opening the panel "
        "onto stale preview content"
    )
    assert settled["mode"] == "closed", (
        "a download-only artifact force-opened the panel onto stale preview content"
    )
    # The stub above cannot prove the shipped branch reports failure — assert the
    # production source too, otherwise this test passes even with `return;` back.
    branch = _open_file_body().split("DOWNLOAD_EXTS.has(ext)", 1)[1][:250]
    assert "return false;" in branch, (
        "the shipped download-only branch must return false, not bare-return; "
        "otherwise a download is indistinguishable from a successful preview"
    )


def test_open_artifact_path_fails_closed_on_a_bare_return():
    """The gate's blocker was that a download-only artifact returned *undefined*,
    which `openArtifactPath()` read as a successful preview. Fixing `openFile()`
    alone is not enough — the caller must require a literal true, so any
    non-preview outcome (old contract or new) fails closed."""
    result = _run_scenario(exists_mode="ok", open_file_returns_undefined=True)
    settled = _step(result, "after open settled")

    assert settled["dismissed"] is True, (
        "openArtifactPath treated `undefined` as a successful preview: a download "
        "cleared the dismissal and the next resize reopens the stale panel (the "
        "gate's download-only blocker)"
    )
    assert settled["mode"] == "closed", (
        "a non-preview outcome promoted the panel to preview mode"
    )


def test_dismissal_during_a_slow_read_wins():
    """The user dismisses the panel while the artifact read is still in flight.
    That newer intent must not be erased when the read completes."""
    result = _run_scenario(exists_mode="ok", slow_read=True, dismiss_during_read=True)
    during = _step(result, "dismissed during read")
    settled = _step(result, "after open settled")

    assert during["dismissed"] is True, "precondition: the dismissal must be recorded"
    assert settled["dismissed"] is True, (
        "a read that completed after the user dismissed the panel cleared the "
        "dismissal, so the next resize force-reopens the panel the user closed"
    )
    assert settled["mode"] == "closed", (
        "a stale read promoted the panel back to preview over the user's dismissal"
    )


def test_desktop_close_then_resize_keeps_its_original_behaviour():
    """The dismissal is mobile-specific: on desktop a visible preview has always
    been restored by a resize, and this PR must not silently change that."""
    result = _run_scenario(exists_mode="ok", compact_viewport=False)
    after_sync = _step(result, "after resize sync")

    assert after_sync["mode"] == "preview", (
        "desktop close→resize stopped restoring the visible preview; the "
        "mobile-keyboard guard must not apply on desktop"
    )


def test_desktop_close_during_a_pending_read_is_fenced():
    """The gate's 21 Sep finding: the action-generation fence must advance on

    EVERY viewport, not just compact. Previously the fence only moved through
    the compact-only dismissal write, so on desktop a close during an in-flight
    artifact read left the generation unchanged and the read promoted the panel
    back open over the newer close."""
    result = _run_scenario(
        exists_mode="ok",
        slow_read=True,
        dismiss_during_read=True,
        compact_viewport=False,
    )
    during = _step(result, "dismissed during read")
    settled = _step(result, "after open settled")

    # preconditions: the read really was in flight and the close really happened
    assert during["mode"] == "closed", f"precondition: the close must take effect, got {during}"
    assert result["existsCalls"] == 1, (
        "precondition: the existence check must have run"
    )

    assert settled["mode"] == "closed", (
        "a desktop close during a pending artifact read was not fenced: the read "
        "promoted the panel back to preview over the newer close"
    )
    assert result["openReturned"] is False, (
        "openArtifactPath() reported a reveal for a read the user overtook by "
        "closing the panel on desktop"
    )


def test_desktop_close_does_not_set_the_mobile_resize_guard():
    """The split: desktop close advances the fence but must leave the

    mobile-only dismissal flag alone, so desktop close→resize still restores."""
    result = _run_scenario(
        exists_mode="ok",
        slow_read=True,
        dismiss_during_read=True,
        compact_viewport=False,
    )
    during = _step(result, "dismissed during read")
    assert during["dismissed"] is False, (
        "a desktop close set the mobile resize guard, which would suppress the "
        "long-standing desktop close→resize restore"
    )


def test_compact_close_advances_the_fence_exactly_once():
    """Guard against the obvious fix-side bug: bumping on both the setter call

    and an explicit increment would double-count a close. Measured on a single
    deliberate close, on both viewport classes."""
    for compact in (True, False):
        result = _run_scenario(
            exists_mode="ok",
            slow_read=True,
            dismiss_during_read=True,
            compact_viewport=compact,
        )
        assert result["genDeltaForSingleClose"] == 1, (
            f"one deliberate close on compact_viewport={compact} advanced the "
            f"action-generation fence {result['genDeltaForSingleClose']} times; "
            f"it must advance exactly once"
        )


def test_reopen_and_reveal_clear_without_advancing_the_fence():
    """The other half of the policy: clears must not advance the fence, or an

    explicit reopen during a pending read would invalidate the reveal it agrees
    with (the earlier Greptile finding)."""
    result = _run_scenario(
        exists_mode="ok", slow_read=True, reopen_during_read=True
    )
    settled = _step(result, "after open settled")
    assert settled["dismissed"] is False, settled
    assert result["openReturned"] is True, (
        "a clear advanced the fence and made the in-flight reveal look stale"
    )


def test_mobile_close_then_resize_stays_closed():
    """The counterpart: on a compact viewport the dismissal still holds."""
    result = _run_scenario(exists_mode="ok", compact_viewport=True)
    after_sync = _step(result, "after resize sync")

    assert after_sync["mode"] == "closed", (
        "the mobile keyboard-churn guard regressed: a resize reopened a "
        "deliberately dismissed panel"
    )


def test_the_mobile_scope_covers_both_the_write_and_the_guard():
    """Both halves of the scope must be conditional, in the shipped source.

    The write moved into `_markWorkspacePanelClosedByUser()` when the fence was
    split from the resize guard, so assert the branch where it now lives."""
    boot = _read(BOOT_JS_PATH)
    close_body = extract_function(boot, "closeWorkspacePanel")
    mark_body = extract_function(boot, "_markWorkspacePanelClosedByUser")
    sync_body = extract_function(boot, "syncWorkspacePanelState")
    assert "_markWorkspacePanelClosedByUser()" in close_body, (
        "closeWorkspacePanel no longer routes its close bookkeeping through the "
        "marker, so this test can no longer reach the viewport branch"
    )
    assert "_isCompactWorkspaceViewport()" in mark_body, (
        "the close bookkeeping marks the dismissal unconditionally, which changes "
        "desktop behaviour (the gate's scope blocker)"
    )
    assert "_isCompactWorkspaceViewport()" in sync_body, (
        "syncWorkspacePanelState applies the dismissal guard on every viewport, "
        "so desktop no longer restores a visible preview on resize"
    )


def _open_file_body() -> str:
    """`extract_function()` brace-matches from the first `{`, which for
    `openFile(path, opts={})` is the default-value brace — it returns the
    signature only. Slice to the next top-level declaration instead (the file
    declares top-level functions at column 0).
    """
    src = _read(WORKSPACE_JS_PATH)
    declaration = "async function openFile(path, opts={}){"
    start = src.index(declaration)
    offset = start + len(declaration)
    for line in src[offset:].split("\n"):
        if line.startswith(("function ", "async function ", "const ", "let ", "var ")):
            return src[start:offset]
        offset += len(line) + 1
    return src[start:]


def test_open_file_fails_closed_for_non_preview_outcomes():
    """`openFile()` must report a real reveal, not merely finish."""
    body = _open_file_body()
    assert "DOWNLOAD_EXTS.has(ext)" in body, "the download-only branch is missing"
    branch = body.split("DOWNLOAD_EXTS.has(ext)", 1)[1][:250]
    assert "return false;" in branch, (
        "the download-only branch must return false so a download is not read as "
        "a successful preview"
    )
    assert "if(!S.session)return false;" in body, (
        "the no-session early return must fail closed too, otherwise a missing "
        "session reads as a successful preview"
    )


# ── Greptile review (17 Sep): reopen during a pending read ───────────────────
#
# The mirror image of "dismissal during a slow read wins". An explicit reopen
# (`openWorkspacePanel`) clears the dismissal flag, and the generation counter
# used to bump on EVERY flag write — including that clear. The in-flight read
# had captured the pre-reopen generation, so when it resolved it saw a changed
# counter, concluded the user had dismissed the panel, and skipped the reveal:
# the panel stayed in browse mode with no artifact shown, even though the read
# had succeeded and the user had asked for the panel to be open.
#
# Bumping only on an actual dismissal keeps the guard aimed at what it was
# written for (a newer *dismiss*) without rejecting the reveal the reopen
# agrees with.

def test_reopen_during_a_pending_read_still_reveals_the_artifact():
    """Reopening the panel while the artifact read is in flight must not
    invalidate that read — the artifact still gets revealed."""
    result = _run_scenario(
        exists_mode="ok", slow_read=True, reopen_during_read=True
    )
    during = _step(result, "reopened during read")
    settled = _step(result, "after open settled")

    assert during["mode"] == "browse", (
        f"precondition: the explicit reopen must take effect, got {during}"
    )
    # The contract: the read succeeded, so openArtifactPath() must report the
    # reveal instead of rejecting it as stale. The panel legitimately stays in
    # 'browse' mode — the user opened it to browse, and a tree click previews
    # without forcing 'preview' mode — so mode alone is not the signal here.
    assert result["openReturned"] is True, (
        "an explicit reopen during a pending read made openArtifactPath() "
        "reject a successful read as stale (Greptile: reopen invalidates "
        "artifact reveal)"
    )
    assert settled["dismissed"] is False, (
        "the reopen's intent was not honoured: the dismissal came back"
    )
    assert settled["mode"] != "closed", (
        f"the panel did not stay open after the reveal, got {settled}"
    )


def test_the_generation_only_advances_on_a_dismissal():
    """Both halves, in the shipped source: a dismiss bumps the counter, a clear

    must not — otherwise the counter conflates 'user closed' with 'user opened'
    and the in-flight reveal cannot tell them apart."""
    boot = _read(BOOT_JS_PATH)
    body = extract_function(boot, "_setWorkspacePanelDismissed")
    compact = "".join(body.split())
    assert "_workspacePanelDismissGen++" in compact, (
        "the counter is no longer bumped, so a dismissal during a pending read "
        "can no longer be detected"
    )
    assert "if(dismissed)_workspacePanelDismissGen++;" in compact, (
        "the counter still advances on every flag write, so a reopen during a "
        "pending read is indistinguishable from a dismissal"
    )


def test_the_close_bookkeeping_separates_the_fence_from_the_resize_guard():
    """The gate's required shape, asserted in the shipped source: every

    deliberate close advances the action-generation fence on ALL viewports,
    while only a compact close sets the mobile resize guard."""
    boot = _read(BOOT_JS_PATH)
    mark = "".join(extract_function(boot, "_markWorkspacePanelClosedByUser").split())
    close = "".join(extract_function(boot, "closeWorkspacePanel").split())

    assert "_markWorkspacePanelClosedByUser()" in close, (
        "closeWorkspacePanel no longer routes its close bookkeeping through the "
        "split helper"
    )
    assert "_isCompactWorkspaceViewport()" in mark, (
        "the marker no longer branches on the viewport, so the mobile-only "
        "resize guard would apply on desktop too"
    )
    # The fence must move exactly once on either viewport: the setter's own bump
    # for compact, an explicit increment for desktop — never both.
    assert "_setWorkspacePanelDismissed(compact)" in mark, (
        "the marker no longer records a compact dismissal through the setter"
    )
    assert "if(!compact)_workspacePanelDismissGen++;" in mark, (
        "a non-compact close no longer advances the action-generation fence — "
        "that is the desktop race the gate found"
    )
