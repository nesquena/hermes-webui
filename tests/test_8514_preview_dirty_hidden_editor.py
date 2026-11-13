"""Tests for #8514 — the Files panel keeps asking to discard edits for a preview
editor that is no longer on screen.

Root cause: `_previewDirty` used to be a bare boolean, set by the textarea's
inline `oninput` in `static/index.html` and cleared only on save / Esc / opening
another file / `clearPreview()`.

* `closeWorkspacePanel()` (and `toggleWorkspacePanel(false)`, and the
  compact-viewport resize) hide the panel while deliberately leaving the preview
  and its editor mounted in the DOM, so an unsaved edit became *invisible state*:
  every later `loadDir()` — breadcrumb/folder click, session switch, tree refresh
  — raised `unsaved_confirm` for an editor the user could not see.
* The flag also could not tell "typed something" from "typed back to the
  original text", and a failed save left it set.

The fix derives the fact instead of storing it: dirty ⇔ the editor is on screen
AND its text differs from the text it was loaded with / last saved
(`_previewEditorOnScreen()` + `_syncPreviewDirty()` / `_resetPreviewDirty()`).

These tests pin both halves: the source shape (the keystroke handler no longer
assigns the flag, the panel-mode change recomputes it) and the behaviour of the
derived check, driven through Node against the real slice of `static/workspace.js`.
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
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_block(src: str, name: str) -> str:
    """Return `function NAME(...) { ... }` with balanced braces."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"function {name}() not found"
    # Skip the parameter list first: a default like `opts={}` would otherwise be
    # mistaken for the function body's opening brace.
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
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


# ── Source-level locks ────────────────────────────────────────────────────────


def test_keystroke_handler_no_longer_assigns_the_flag():
    """The setter is what made the flag stale. Typing must only *recompute* it."""
    assert "oninput=\"_previewDirty=true" not in INDEX_HTML, (
        "the preview editor's oninput must not assign _previewDirty directly — "
        "a stored flag goes stale when the editor leaves the screen (#8514)"
    )
    assert 'oninput="_syncPreviewDirty()"' in INDEX_HTML


def test_panel_mode_change_recomputes_dirtiness():
    """Closing the panel hides the editor but keeps it mounted; dirty must follow."""
    block = _function_block(BOOT_JS, "_setWorkspacePanelMode")
    assert "_syncPreviewDirty" in block, (
        "_setWorkspacePanelMode() must recompute the derived dirty state: the panel "
        "can be closed with the preview editor still in the DOM (#8514)"
    )


def test_clear_preview_resets_the_editor_baseline():
    block = _function_block(BOOT_JS, "clearPreview")
    assert "_resetPreviewDirty" in block, (
        "clearPreview() must reset the editor baseline, not just the flag — "
        "otherwise the next preview inherits a stale comparison (#8514)"
    )


def test_enter_edit_mode_records_the_baseline():
    block = _function_block(WORKSPACE_JS, "toggleEditMode")
    assert "_previewSavedText=currentText" in block, (
        "entering edit mode must remember the text the editor was populated with, "
        "so typing and deleting the same character is not 'unsaved' (#8514)"
    )


def test_save_updates_the_baseline():
    start = WORKSPACE_JS.find("const saved=await api(_previewSaveRoute||'/api/file/save'")
    assert start != -1, "file save call not found"
    block = WORKSPACE_JS[start:start + 1200]
    assert "_previewSavedText=savedContent" in block, (
        "a successful save must move the baseline to the saved text, otherwise the "
        "editor stays 'dirty' against its own saved content (#8514)"
    )


def test_gate_sites_still_read_the_dirty_flag():
    """Regression lock on the contract: the confirm must remain gated on dirtiness."""
    load_dir = _function_block(WORKSPACE_JS, "loadDir")
    assert "_previewDirty" in load_dir
    assert "unsaved_confirm" in load_dir
    assert "_previewDirty" in _function_block(WORKSPACE_JS, "refreshOpenPreviewIfMutated")
    assert "_previewDirty" in _function_block(WORKSPACE_JS, "forceRenderMarkdownPreview")


# ── Behaviour of the derived check (Node) ────────────────────────────────────

_HARNESS = r"""
const params = __PARAMS__;
function __mkEl(value, display){
  return {value: value, style: {display: display}};
}
const els = {};
const docEl = {dataset: {}};
globalThis.document = {documentElement: docEl};
const $ = (id) => els[id];
let _previewDirty = params.dirty;
let _previewSavedText = params.savedText;
let _updateEditBtnCalls = 0;
const updateEditBtn = () => { _updateEditBtnCalls++; };

__ONSCREEN__

__SYNC__

els.previewEditArea = __mkEl(params.text, params.display);
docEl.dataset.workspacePanel = params.panel;
_syncPreviewDirty();
console.log(JSON.stringify({dirty: _previewDirty, updates: _updateEditBtnCalls}));
"""


def _run_sync(text: str, saved: str, display: str, panel: str, tmp_path=None):
    payload = {"text": text, "savedText": saved, "display": display,
               "panel": panel, "dirty": False}
    js = (
        _HARNESS.replace("__PARAMS__", json.dumps(payload))
        .replace("__ONSCREEN__", _function_block(WORKSPACE_JS, "_previewEditorOnScreen"))
        .replace("__SYNC__", _function_block(WORKSPACE_JS, "_syncPreviewDirty"))
    )
    script = Path(tmp_path or "/tmp") / "harness_8514.js"
    script.write_text(js, encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def test_visible_editor_with_divergent_text_is_dirty(tmp_path):
    assert _run_sync("edited", "original", "", "preview", tmp_path)["dirty"] is True


def test_text_equal_to_the_baseline_is_not_dirty(tmp_path):
    assert _run_sync("original", "original", "", "preview", tmp_path)["dirty"] is False


def test_hidden_editor_is_never_dirty(tmp_path):
    assert _run_sync("edited", "original", "none", "preview", tmp_path)["dirty"] is False


def test_closed_panel_is_never_dirty(tmp_path):
    """The phantom: the editor is still mounted and its text still differs, but
    the panel holding it is closed — there is nothing to discard."""
    assert _run_sync("edited", "original", "", "closed", tmp_path)["dirty"] is False


def test_no_baseline_is_never_dirty(tmp_path):
    """After clearPreview() the baseline is null while the textarea keeps the old
    text. Reopening the panel must not turn that leftover into a discard prompt
    (#8515 review: edit, Discard via the breadcrumb, then Refresh prompted again)."""
    assert _run_sync("leftover", None, "", "preview", tmp_path)["dirty"] is False


def test_dirty_state_is_recomputed_even_when_it_stays_false(tmp_path):
    """The badge must be refreshed on every sync, not only when the state flips."""
    assert _run_sync("edited", "original", "", "closed", tmp_path)["updates"] == 1


# ── The other half of the review: hidden edits must not be dropped ───────────
#
# `_previewDirty` is false for a hidden editor (nothing to prompt about), but the
# textarea can still hold real unsaved text. `_previewHasUnsavedEdits()` is the
# derived fact loadDir() consults to *preserve* that preview instead of clearing
# it — turning the old nag into silent loss was the regression the review caught.


_HIDDEN_HARNESS = r"""
const params = __PARAMS__;
globalThis.document = {documentElement: {dataset: {}}};
const els = {};
const $ = (id) => els[id];
let _previewSavedText = params.savedText;
let _previewEditSessionId = params.editSession;
const S = {session: {session_id: params.session}};

__HAS__
__OWNED__

els.previewEditArea = {value: params.text, style: {display: params.display}};
console.log(JSON.stringify({
  unsaved: _previewHasUnsavedEdits(),
  ownedHere: _previewHiddenEditsOwnedHere()
}));
"""


def _run_hidden(text, saved, display, session="s1", edit_session="s1", tmp_path=None):
    payload = {"text": text, "savedText": saved, "display": display,
               "session": session, "editSession": edit_session}
    js = (
        _HIDDEN_HARNESS.replace("__PARAMS__", json.dumps(payload))
        .replace("__HAS__", _function_block(WORKSPACE_JS, "_previewHasUnsavedEdits"))
        .replace("__OWNED__", _function_block(WORKSPACE_JS, "_previewHiddenEditsOwnedHere"))
    )
    script = Path(tmp_path or "/tmp") / "harness_8514_hidden.js"
    script.write_text(js, encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_hidden_editor_with_divergent_text_still_holds_edits(tmp_path):
    """Panel closed, editor mounted, text differs → real unsaved work."""
    assert _run_hidden("edited", "original", "none", tmp_path=tmp_path)["unsaved"] is True


def test_no_baseline_never_reports_unsaved_edits(tmp_path):
    """After clearPreview() the baseline is null: a stale textarea value from a
    previously previewed file must not look like unsaved work, or no preview
    could ever be cleared again."""
    assert _run_hidden("leftover", None, "none", tmp_path=tmp_path)["unsaved"] is False


def test_text_equal_to_the_baseline_holds_no_edits(tmp_path):
    assert _run_hidden("original", "original", "none", tmp_path=tmp_path)["unsaved"] is False


def test_hidden_edits_are_owned_by_the_session_they_were_typed_in(tmp_path):
    assert _run_hidden("edited", "original", "none", session="s2", edit_session="s1",
                       tmp_path=tmp_path)["ownedHere"] is False
    assert _run_hidden("edited", "original", "none", session="s1", edit_session="s1",
                       tmp_path=tmp_path)["ownedHere"] is True


def test_load_dir_preserves_hidden_edits_instead_of_clearing():
    """Source lock: inside the session, loadDir() must keep the preview carrying
    hidden unsaved edits and clear only when there is nothing to lose."""
    load_dir = _function_block(WORKSPACE_JS, "loadDir")
    assert "_previewHasUnsavedEdits" in load_dir, (
        "loadDir() must consult the hidden-edit fact — clearing on _previewDirty "
        "alone silently drops edits in a closed panel (#8514 review)"
    )
    assert "keepHiddenEdits" in load_dir


def test_mutated_refresh_does_not_reload_over_hidden_edits():
    block = _function_block(WORKSPACE_JS, "refreshOpenPreviewIfMutated")
    assert "_previewHasUnsavedEdits" in block, (
        "a refresh must not reload the file over a hidden editor's unsaved text "
        "(#8514 review)"
    )
