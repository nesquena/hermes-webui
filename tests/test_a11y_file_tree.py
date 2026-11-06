"""Static wiring for the workspace file tree's ARIA tree contract.

Behaviour (roles, keyboard, focus across re-renders, Delete, rename keys) is
proven in a real browser by tests/browser_file_tree_keyboard.py. These checks
keep the pieces that gate depends on from being dropped silently.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"
A11Y_JS = (STATIC / "a11y-helpers.js").read_text(encoding="utf-8")
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SW_JS = (STATIC / "sw.js").read_text(encoding="utf-8")

TREE_KEYS = (
    "delete_entry_aria",
    "tree_external_link_aria",
    "tree_file_aria",
    "tree_folder_aria",
    "tree_symlink_aria",
    "tree_row_aria",
    "workspace_tree_aria",
)


def _function_body(src, name):
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("){", start) + 1, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_helpers_load_before_ui_js():
    helpers = INDEX_HTML.index('src="static/a11y-helpers.js?v=__WEBUI_VERSION__"')
    ui = INDEX_HTML.index('src="static/ui.js?v=__WEBUI_VERSION__"')
    assert helpers < ui


def test_helpers_are_precached_for_the_pwa_shell():
    assert "'./static/a11y-helpers.js' + VQ" in SW_JS


def test_tree_container_is_not_a_tab_stop_of_its_own():
    """Firefox makes a scrolling container a Tab stop; the tree has one, its row."""
    assert "container.setAttribute('tabindex', '-1');" in _function_body(A11Y_JS, "a11yTree")


def test_helpers_are_exported():
    for name in ("a11yLabel", "a11yTree", "a11yTreeRow", "a11yTreeReset",
                 "a11yTreeRememberFocus", "a11yTreeRestoreFocus", "a11yTreeDropCarry"):
        assert f"window.{name} = {name};" in A11Y_JS


def test_render_wires_the_tree_after_every_rebuild():
    body = _function_body(UI_JS, "renderFileTree")
    wipe = body.index("\n  box.innerHTML='';")
    assert body.index("a11yTreeRememberFocus(box") < wipe
    render = body.index("_renderTreeItems(box, visibleEntries, 0);")
    assert render < body.index("a11yTree(box") < body.index("a11yTreeRestoreFocus(box, prevTreeFocus")


def test_every_writer_that_empties_the_tree_resets_its_role():
    """An empty container (skeleton, no session, empty folder) is not a tree."""
    workspace_js = (STATIC / "workspace.js").read_text(encoding="utf-8")
    sessions_js = (STATIC / "sessions.js").read_text(encoding="utf-8")
    assert "a11yTreeReset(box,{" in _function_body(UI_JS, "renderFileTree")
    assert _function_body(UI_JS, "renderFileTree").count("treeEmptied();") == 2
    body = _function_body(workspace_js, "showWorkspaceTreeSkeleton")
    # Reset before the wipe, so focus in the tree can move to the Files tab.
    assert body.index("a11yTreeReset(tree, {focusTo: $('workspaceFilesTab')})") < body.index("tree.innerHTML = ''")
    assert sessions_js.index("a11yTreeReset($('fileTree'), {focusTo: $('workspaceFilesTab')})") \
        < sessions_js.index("$('fileTree').innerHTML=''")


def test_every_locale_has_the_tree_strings():
    locales = re.findall(r"^  '?([a-zA-Z-]+)'?: \{", I18N_JS, re.M)
    assert len(locales) >= 15
    for key in TREE_KEYS:
        assert len(re.findall(rf"^    {key}: ", I18N_JS, re.M)) == len(locales), key
    # The row name is assembled by each locale from (name, kind, size).
    assert len(re.findall(r"^    tree_row_aria: \(name, kind, size\) => ", I18N_JS, re.M)) == len(locales)
    # The delete name interpolates the entry name through t(key, name).
    assert len(re.findall(r"^    delete_entry_aria: \(name\) => `", I18N_JS, re.M)) == len(locales)
