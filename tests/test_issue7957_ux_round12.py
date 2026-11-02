"""Regression coverage for #7957 round-12 UX findings (design review).

1. Removing a project-backed row must warn that the project is archived in
   Hermes Desktop/CLI, not just removed locally (blocking UX item). Both
   remove call sites (detail-pane delete and list remove) select the
   project-specific message when the row carries a project_id.
2. The "Register as Hermes Project" checkbox uses the shared
   `detail-form-check` class (accent color + proper label styling) instead
   of an inline flex style.
3. `.detail-form-check` gets a 44px coarse-pointer min-height (touch target).
4. Shared projects are distinguishable: a "Project" badge on Spaces rows and
   a "Kind" row in the detail card.
5. With the box ticked the Path hint switches to the auto-create wording
   instead of still claiming paths must be existing directories.
6. All six new i18n keys exist in every locale (repo convention on this PR).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")

NEW_KEYS = [
    "workspace_remove_confirm_project_message",
    "workspace_project_badge",
    "workspace_kind_label",
    "workspace_kind_project",
    "workspace_kind_local",
    "workspace_project_path_hint",
]


def _extract_fn(name: str) -> str:
    """Extract a top-level function's source (brace-matched) from panels.js."""
    m = re.search(rf"^(async )?function {re.escape(name)}\(", PANELS_JS, re.M)
    assert m, f"function {name} not found in panels.js"
    # Skip the parameter list first: its braces (destructuring params) are
    # not the body's opening brace.
    p_start = PANELS_JS.index("(", m.end() - 1)
    depth, j = 0, p_start
    while True:
        c = PANELS_JS[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    start = PANELS_JS.index("{", j)
    depth, i = 0, start
    while i < len(PANELS_JS):
        c = PANELS_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[m.start():i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run_node(script: str) -> str:
    assert NODE, "node required for frontend guard tests"
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout


# --- 6. i18n keys in every locale -------------------------------------------

def test_new_keys_present_in_all_fifteen_locales():
    # The file has exactly 15 `workspace_project_created` anchors (one per
    # locale); each new key must appear once per locale too.
    assert I18N_JS.count("workspace_project_created:") == 15
    for key in NEW_KEYS:
        assert I18N_JS.count(f"{key}:") == 15, f"{key} missing from some locale"


# --- 1. project-specific remove confirmation (functional, both call sites) --

_STUBS = """
const MESSAGES = {
  workspace_remove_confirm_title: 'Remove workspace',
  workspace_remove_confirm_message: (p) => `Remove "${p}"?`,
  workspace_remove_confirm_project_message: (p) => `Remove "${p}"? ARCHIVES-IN-DESKTOP`,
  remove: 'Remove',
};
const t = (k, ...a) => { const v = MESSAGES[k]; return typeof v === 'function' ? v(...a) : (v || k); };
let lastConfirm = null;
const showConfirmDialog = async (o) => { lastConfirm = o; return false; };  // cancel: no api call
"""


@pytest.mark.parametrize("fn_name,seed", [
    ("removeWorkspace", """
const _workspaceList = [{path: '/p/a', project_id: 'pid-1'}, {path: '/p/b'}];
const api = async () => { throw new Error('must not be called'); };
"""),
    ("deleteCurrentWorkspace", """
let _workspaceUiGen = 0;
const _currentWorkspaceDetail = {path: '/p/a', project_id: 'pid-1', workspace_kind: 'native'};
const api = async () => { throw new Error('must not be called'); };
"""),
])
def test_project_row_uses_project_remove_warning(fn_name, seed):
    script = _STUBS + seed + _extract_fn(fn_name) + """
(async () => {
  await %s(%s);
  if (!lastConfirm.message.includes('ARCHIVES-IN-DESKTOP'))
    throw new Error('project row must warn about Desktop/CLI archive: ' + lastConfirm.message);
  console.log('OK');
})();
""" % (fn_name, "'/p/a'" if fn_name == "removeWorkspace" else "")
    assert "OK" in _run_node(script)


def test_plain_row_keeps_generic_remove_message():
    script = _STUBS + """
const _workspaceList = [{path: '/p/b'}];
const api = async () => { throw new Error('must not be called'); };
""" + _extract_fn("removeWorkspace") + """
(async () => {
  await removeWorkspace('/p/b');
  if (lastConfirm.message.includes('ARCHIVES-IN-DESKTOP'))
    throw new Error('local space must keep the generic message: ' + lastConfirm.message);
  console.log('OK');
})();
"""
    assert "OK" in _run_node(script)


# --- 2 + 5. workspace create form (functional) -------------------------------

def test_create_form_checkbox_class_and_hint_toggle():
    script = """
const els = {};
const $ = (id) => { if (!els[id]) els[id] = { id, style: {}, value: '', checked: false, textContent: '', innerHTML: '', focus(){} }; return els[id]; };
const esc = (s) => String(s);
const T = {
  workspace_paths_validated_hint: 'Paths are validated as existing directories before saving.',
  workspace_project_path_hint: 'The folder is created automatically if it does not exist yet.',
  workspace_as_hermes_project: 'Register as Hermes Project',
  workspace_name_label: 'Name', workspace_path_label: 'Path',
  workspace_new_title: 'New space', workspace_add_path_placeholder: '/path',
  workspace_name_placeholder: 'name',
};
const t = (k) => T[k] || k;
const _setWorkspaceHeaderButtons = () => {};
const _wireWorkspaceFormPathSuggestions = () => {};
""" + _extract_fn("_renderWorkspaceForm") + """
_renderWorkspaceForm({ name: '', path: '', isEdit: false });
const body = els['workspaceDetailBody'].innerHTML;
if (!/class="detail-form-check" for="workspaceFormAsProject"/.test(body))
  throw new Error('checkbox must use the detail-form-check class');
if (/display:flex;align-items:center;gap:8px;cursor:pointer/.test(body))
  throw new Error('inline flex style must be gone');
if (!body.includes('id="workspaceFormPathHint"'))
  throw new Error('create-form path hint needs an id for the toggle');
if (!els['workspaceFormPathHint']) els['workspaceFormPathHint'] = { textContent: '' };
const box = els['workspaceFormAsProject'];
if (typeof box.onchange !== 'function') throw new Error('checkbox onchange not wired');
const HINT = els['workspaceFormPathHint'];
box.checked = true; box.onchange();
if (HINT.textContent !== T.workspace_project_path_hint)
  throw new Error('ticked box must swap hint to the auto-create wording, got: ' + HINT.textContent);
box.checked = false; box.onchange();
if (HINT.textContent !== T.workspace_paths_validated_hint)
  throw new Error('unticked box must restore the validated-dirs wording, got: ' + HINT.textContent);
console.log('OK');
"""
    assert "OK" in _run_node(script)


# --- 3. coarse-pointer touch target ------------------------------------------

def test_detail_form_check_coarse_pointer_min_height():
    assert re.search(
        r"@media \(pointer:coarse\)\{[^}]*\.detail-form-row label\.detail-form-check\{min-height:44px",
        STYLE_CSS,
    ), "detail-form-check needs a 44px coarse-pointer min-height"


# --- 4. shared-project affordances --------------------------------------------

def test_spaces_row_project_badge():
    fn = _extract_fn("renderWorkspacesPanel")
    assert "w.project_id" in fn and "workspace_project_badge" in fn
    assert "${activeBadge}${projectBadge}" in fn, "badge must render in the row name"


def test_detail_card_kind_row():
    fn = _extract_fn("_renderWorkspaceDetail")
    assert "workspace_kind_label" in fn and "workspace_kind_project" in fn
    assert "workspace_kind_local" in fn
    # Kind row sits between Path and Status in the card.
    assert fn.index("detail-row-label\">Path") < fn.index("workspace_kind_label") < fn.index("detail-row-label\">Status")
