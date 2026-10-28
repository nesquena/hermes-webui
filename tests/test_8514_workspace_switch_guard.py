"""Tests for #8514 (second review round) — the workspace-transition guards.

Round one made `_previewDirty` a *derived* fact: true only when the editor is on
screen AND its text differs from its baseline. That fixed the phantom prompt, but
the two `switchToWorkspace()` guards still tested that flag alone. With the Files
panel closed the flag is (correctly) false while the textarea still holds unsaved
text, so:

1. workspaces A and B both contain `note.txt`;
2. edit A's file, close the panel without saving — `_previewDirty` goes false;
3. select B — no prompt, the session's workspace is mutated before `loadDir()`;
4. `_previewHiddenEditsOwnedHere()` compares only the session id, which did not
   change, so A's editor is preserved — and `toggleEditMode()` then saves its
   *relative* path through the session's **current** workspace: B's `note.txt`
   is overwritten with A's text.

The fix: a transition guard must ask about unsaved *text*, not about what is on
screen (`_previewNeedsDiscardPrompt()`), and the hidden-edit ownership check also
expires when the session's workspace moved (`_previewEditWorkspace`), so an
editor can never travel into a tree its relative path does not belong to.

These tests drive the real functions out of `static/workspace.js`,
`static/boot.js` and `static/panels.js` in Node against an in-memory filesystem
whose save route resolves the posted relative path against the session's current
workspace — i.e. the exact composition that produced the defect.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_block(src: str, name: str) -> str:
    """Return `function NAME(...) { ... }` with balanced braces."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"function {name}() not found"
    depth_p = 1
    i = m.end()
    while i < len(src) and depth_p:
        if src[i] == "(":
            depth_p += 1
        elif src[i] == ")":
            depth_p -= 1
        i += 1
    start = src.index("{", i)
    depth = 0
    prefix = m.start()
    if src[max(0, prefix - 6):prefix] == "async ":
        prefix -= 6
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[prefix:i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


# ── Source-level locks ────────────────────────────────────────────────────────


def test_both_workspace_transition_guards_use_the_settling_helper():
    """A transition moves the tree a relative path resolves against, so it must
    not be gated on visibility."""
    block = _function_block(PANELS_JS, "switchToWorkspace")
    assert "_previewDirty" not in block, (
        "switchToWorkspace() still tests the visibility-dependent flag directly: "
        "a closed panel's unsaved text crosses workspaces silently (#8514 review)"
    )
    assert block.count("_settlePreviewForWorkspaceSwitch") == 4, (
        "both transitions (in-place switch and new-chat-on-switch) must settle "
        "the preview through _settlePreviewForWorkspaceSwitch()"
    )
    first = block.index("_settlePreviewForWorkspaceSwitch")
    assert first < block.index("api('/api/session/update'")
    assert first < block.index("await newSession(")
    helper = _function_block(PANELS_JS, "_settlePreviewForWorkspaceSwitch")
    assert "_previewNeedsDiscardPrompt" in helper, (
        "the transition guard must ask about unsaved TEXT, not about what is on "
        "screen (#8514 review)"
    )
    assert "if(!discard) return false;" in helper, (
        "Cancel must abort the transition before the session workspace moves"
    )
    assert "_previewBelongsToWorkspace" in helper, (
        "a preview opened in the workspace being left must be dropped, or its "
        "relative path saves into the new tree (#8514 review)"
    )


def test_hidden_edits_expire_when_the_workspace_moves():
    block = _function_block(WORKSPACE_JS, "_previewHiddenEditsOwnedHere")
    assert "_previewEditWorkspace" in block, (
        "hidden edits must expire when the session's workspace changes — the "
        "save route resolves their relative path against the current one "
        "(#8514 review)"
    )
    assert "_previewEditWorkspace" in _function_block(WORKSPACE_JS, "_resetPreviewDirty")


def test_baseline_records_the_workspace_it_was_taken_in():
    block = _function_block(WORKSPACE_JS, "toggleEditMode")
    assert block.count("_previewEditWorkspace=") == 2, (
        "entering edit mode and a successful save must both record the workspace "
        "the baseline belongs to (#8514 review)"
    )


# ── Behaviour of the composed save/switch path (Node) ─────────────────────────

_HARNESS = r"""
const params = __PARAMS__;

// In-memory workspaces: the save route resolves the posted RELATIVE path
// against the session's CURRENT workspace, which is what makes a carried-over
// editor write into the other workspace's same-named file.
const fs = {A: {"note.txt": "original-A"}, B: {"note.txt": "original-B"}};
const calls = {dialogs: 0, saves: [], newSession: 0, loadDir: 0};

function mkEl(){
  return {value: '', style: {display: ''}, textContent: '', innerHTML: '',
          title: '', src: '', onclick: null, onkeydown: null,
          classList: {contains: () => false, add(){}, remove(){}}};
}
const els = {};
const $ = (id) => (els[id] || (els[id] = mkEl()));
globalThis.document = {documentElement: {dataset: {workspacePanel: 'preview'}}};

let _previewDirty = false;
let _previewSavedText = null;
let _previewEditSessionId = '';
let _previewEditWorkspace = '';
let _previewCurrentPath = '';
let _previewCurrentMode = '';
let _previewServerEditable = null;
let _previewSaveRoute = '/api/file/save';
let _previewOfficeFormat = '';
let _previewPreviewKind = '';
let _previewRawContent = '';
let _previewRawContentPath = '';
let _previewOpenGen = 0;
let _workspacePanelMode = 'preview';

const S = {
  session: {session_id: 's1', workspace: 'A', model: 'm'},
  messages: params.messages,
  busy: false
};
globalThis.window = {_newChatOnWorkspaceSwitch: params.newChatOnSwitch};

const t = (k) => k;
const updateEditBtn = () => {};
const showToast = (m) => {};
const setStatus = (m) => { calls.status = m; };
const syncTopbar = () => {};
const renderMessages = () => {};
const renderSessionList = async () => {};
const renderBreadcrumb = () => {};
const renderCsvPreviewContent = () => {};
const renderMarkdownPreviewContent = () => {};
const closeWsDropdown = () => {};
const closeWorkspacePanel = () => { document.documentElement.dataset.workspacePanel = 'closed'; };
const openWorkspacePanel = () => {};
const syncWorkspacePanelUI = () => {};
const bumpWorkspaceTreeGen = () => {};
const loadMemory = async () => {};
const getWorkspaceFriendlyName = (p) => p;
const _workspacePathIsReadOnly = () => false;
let _currentPanel = '';
const loadDir = async () => { calls.loadDir++; };
const showConfirmDialog = async () => { calls.dialogs++; return params.answer; };
const newSession = async (retry, opts) => {
  calls.newSession++;
  S.session.workspace = opts.workspace;
};

const api = async (route, opts) => {
  const body = (opts && opts.body) ? JSON.parse(opts.body) : {};
  if (route === '/api/session/update') { S.session.workspace = body.workspace; return {}; }
  if (route === '/api/file/save') {
    const ws = S.session.workspace;
    calls.saves.push({ws: ws, path: body.path, content: body.content});
    fs[ws][body.path] = body.content;
    return {content: body.content};
  }
  return {};
};

__ONSCREEN__
__SYNC__
__HAS__
__OWNED__
__NEEDS__
__BELONGS__
__RESET__
__CANCEL__
__TOGGLE__
__CLEAR__
__SETTLE__
__SWITCH__

(async () => {
  const out = {};
  // Open A's note.txt and edit it through the real enter-edit-mode path.
  $('previewCode').textContent = fs.A['note.txt'];
  $('previewEditArea').value = '';
  $('previewEditArea').style.display = 'none';
  _previewCurrentPath = 'note.txt';
  _previewCurrentMode = 'code';
  await toggleEditMode();
  $('previewEditArea').value = params.typeText;
  _syncPreviewDirty();

  // Close the Files panel the way the UI does: the editor stays mounted.
  document.documentElement.dataset.workspacePanel = 'closed';
  _syncPreviewDirty();
  out.visibleDirty = _previewDirty;
  out.holdsEdits = _previewHasUnsavedEdits();
  out.promptNeeded = _previewNeedsDiscardPrompt();
  out.ownedHereAfterClose = _previewHiddenEditsOwnedHere();

  await switchToWorkspace(params.target, params.target);
  out.dialogs = calls.dialogs;
  out.newSessionCalls = calls.newSession;
  out.workspaceAfter = S.session.workspace;
  out.ownedHereAfterSwitch = _previewHiddenEditsOwnedHere();
  out.promptNeededAfterSwitch = _previewNeedsDiscardPrompt();

  // The save consumer: if the editor is still in edit mode, this is the exact
  // click that wrote A's text into B.
  if (els.previewEditArea.style.display !== 'none' && _previewCurrentPath) {
    await toggleEditMode();
  }
  out.saves = calls.saves;
  out.fs = fs;
  console.log(JSON.stringify(out));
})();
"""


def _run(params, tmp_path):
    js = _HARNESS.replace("__PARAMS__", json.dumps(params))
    for token, src, name in (
        ("__ONSCREEN__", WORKSPACE_JS, "_previewEditorOnScreen"),
        ("__SYNC__", WORKSPACE_JS, "_syncPreviewDirty"),
        ("__HAS__", WORKSPACE_JS, "_previewHasUnsavedEdits"),
        ("__OWNED__", WORKSPACE_JS, "_previewHiddenEditsOwnedHere"),
        ("__NEEDS__", WORKSPACE_JS, "_previewNeedsDiscardPrompt"),
        ("__BELONGS__", WORKSPACE_JS, "_previewBelongsToWorkspace"),
        ("__RESET__", WORKSPACE_JS, "_resetPreviewDirty"),
        ("__CANCEL__", WORKSPACE_JS, "cancelEditMode"),
        ("__TOGGLE__", WORKSPACE_JS, "toggleEditMode"),
        ("__CLEAR__", BOOT_JS, "clearPreview"),
        ("__SETTLE__", PANELS_JS, "_settlePreviewForWorkspaceSwitch"),
        ("__SWITCH__", PANELS_JS, "switchToWorkspace"),
    ):
        js = js.replace(token, _function_block(src, name))
    script = Path(tmp_path) / "harness_8514_switch.js"
    script.write_text(js, encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_BASE = {"messages": [], "newChatOnSwitch": False, "answer": False,
         "typeText": "edited", "target": "B"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_closed_panel_edit_crossing_a_workspace_prompts_and_cancel_is_settled_before_the_mutation(tmp_path):
    """The reported defect: no prompt, workspace mutated, A's text saved into B."""
    out = _run(dict(_BASE), tmp_path)
    assert out["visibleDirty"] is False          # nothing on screen to warn about
    assert out["holdsEdits"] is True             # ...but real unsaved text exists
    assert out["promptNeeded"] is True           # so a transition must ask
    assert out["dialogs"] == 1, "the workspace switch must prompt for hidden unsaved edits"
    assert out["workspaceAfter"] == "A", "Cancel must leave the session workspace untouched"
    # Cancel keeps the user in A, so a later Save is a legitimate save of A's file.
    assert all(s["ws"] == "A" for s in out["saves"]), "no save may land in the other workspace"
    assert out["fs"]["B"]["note.txt"] == "original-B", (
        "A's text must never reach B's same-named file"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_chat_on_switch_prompts_before_creating_the_chat(tmp_path):
    """The new-chat branch creates a session; the guard must fire before it."""
    params = dict(_BASE, messages=[{"role": "user", "content": "hi"}], newChatOnSwitch=True)
    out = _run(params, tmp_path)
    assert out["dialogs"] == 1
    assert out["newSessionCalls"] == 0, "Cancel must not create the new chat"
    assert out["workspaceAfter"] == "A"
    assert out["fs"]["B"]["note.txt"] == "original-B"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_discard_then_switch_keeps_both_workspaces_pristine(tmp_path):
    """Confirming the prompt must clear the editor, not carry it across."""
    params = dict(_BASE, answer=True)
    out = _run(params, tmp_path)
    assert out["dialogs"] == 1
    assert out["workspaceAfter"] == "B"
    assert out["promptNeededAfterSwitch"] is False, "the discarded editor must not stay dirty"
    assert out["saves"] == [], "a discarded editor must not be savable afterwards"
    assert out["fs"]["A"]["note.txt"] == "original-A"
    assert out["fs"]["B"]["note.txt"] == "original-B"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_clean_editor_switch_does_not_prompt(tmp_path):
    """Control: typed back to the baseline → no prompt, and the editor is dropped
    rather than left savable against the new workspace."""
    out = _run(dict(_BASE, typeText="original-A"), tmp_path)
    assert out["holdsEdits"] is False
    assert out["dialogs"] == 0, "a clean editor must not block a workspace switch"
    assert out["workspaceAfter"] == "B"
    assert out["saves"] == [], "the dropped editor must not be savable afterwards"
    assert out["fs"]["B"]["note.txt"] == "original-B"
    assert out["fs"]["A"]["note.txt"] == "original-A"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_workspace_selection_keeps_hidden_edits_without_prompting(tmp_path):
    """Control: no workspace transition → the hidden edits stay owned here, which
    is what lets loadDir() preserve them across directory navigation."""
    out = _run(dict(_BASE, target="A"), tmp_path)
    assert out["dialogs"] == 0
    assert out["ownedHereAfterClose"] is True
    assert out["ownedHereAfterSwitch"] is True
    assert out["promptNeededAfterSwitch"] is True
    # Same workspace: the edits stay here and any Save stays here too.
    assert all(s["ws"] == "A" for s in out["saves"])
    assert out["fs"]["B"]["note.txt"] == "original-B"
    assert out["fs"]["A"]["note.txt"] == "edited"


def test_dropping_a_clean_foreign_editor_keeps_the_files_panel_open():
    """A clean editor from another workspace is dropped on switch. In preview
    mode clearPreview() closes the whole panel unless told otherwise, so the
    switch must pass keepPanelOpen (#8515 review: panel ended `closed`)."""
    block = _function_block(PANELS_JS, "_settlePreviewForWorkspaceSwitch")
    assert "clearPreview({keepPanelOpen:true})" in block
    assert "clearPreview()" not in block
