"""The file tree must say what each row is and how to use it.

Report (2026-08-18): in the Files tab the screen reader read
    "▸  .cache  ×      ▸  .cloak-venv  ×      ▸  .cloakbrowser  ×"
The user's words: "and now try to figure out what this is, what it means, and
how to use it". Three things were unreadable at once: what the row is, what
state it is in, and what the adjacent button does.

MEASURED GAPS (all 7 at the same time, step142):
  row has role .................... MISSING  -> <div>, the screen reader does not see a control
  row has tabindex ................ MISSING  -> it cannot be reached by keyboard
  twisty has aria-expanded ........ MISSING  -> no "collapsed/expanded"
  twisty has a name ............... MISSING  -> only the ▸ character is read
  delete button has aria-label .... MISSING  -> the × character is read, or nothing
  row has aria-level .............. MISSING  -> unknown nesting level
  container has role=tree ......... MISSING  -> a set of loose elements

DESIGN DECISIONS enforced by these tests:
 * role=tree, not a list of buttons - folders COLLAPSE and rows are
   NESTED; only a tree has vocabulary for both facts (aria-expanded,
   aria-level) and built-in screen reader navigation,
 * a FILE does not get aria-expanded - on a leaf, that attribute lies and
   suggests something can be expanded,
 * roving tabindex (one row in the Tab order) - a tree with a hundred files
   must not require a hundred Tab presses to skip through it,
 * the delete button name includes THE ENTRY NAME - with 20 rows, plain "delete"
   does not let a screen reader user tell which button deletes what,
 * the twisty and icon are aria-hidden - the row carries the state and type, so otherwise
   the screen reader would repeat the ▸ character before every name.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

HARNESS = Path(__file__).parent / "_harness_drzewo.js"


class TestHelpersExistAndAreWired:
    def test_tree_helpers_exist(self):
        assert "function a11yTreeRow(" in A11Y_JS
        assert "function a11yTree(" in A11Y_JS

    def test_helpers_are_exported(self):
        assert "window.a11yTree = a11yTree" in A11Y_JS
        assert "window.a11yTreeRow = a11yTreeRow" in A11Y_JS

    def test_tree_row_uses_the_helper(self):
        assert "a11yTreeRow(el, {" in UI_JS, (
            "file tree rows must go through the helper"
        )

    def test_tree_container_uses_the_helper(self):
        idx = UI_JS.find("_renderTreeItems(box, visibleEntries, 0);")
        assert idx > 0
        assert "a11yTree(box" in UI_JS[idx:idx + 900], (
            "the container must receive role=tree after every repaint"
        )

    def test_wiring_runs_after_every_repaint(self):
        """innerHTML='' destroys rows, so the contract must be restored."""
        idx = UI_JS.find("function renderFileTree(")
        assert idx > 0
        body = UI_JS[idx:idx + 3000]
        assert "box.innerHTML=''" in body
        assert "a11yTree(box" in body


class TestDecorationsAreHidden:
    def test_twisty_is_hidden_from_the_tree(self):
        idx = UI_JS.find("arrow.className='file-tree-toggle'")
        assert idx > 0
        assert "aria-hidden" in UI_JS[idx:idx + 500], (
            "without this, the screen reader reads the ▸ character before every name"
        )

    def test_icon_is_hidden_from_the_tree(self):
        idx = UI_JS.find("iconEl.className='file-icon'")
        assert idx > 0
        assert "aria-hidden" in UI_JS[idx:idx + 400]


class TestDeleteButtonSaysWhatItDeletes:
    def test_has_an_accessible_name(self):
        assert UI_JS.count("del.setAttribute('aria-label'") >= 2, (
            "both buttons (file and folder) must have a name"
        )

    def test_name_contains_the_entry_name(self):
        assert "_delLabel(item.name)" in UI_JS, (
            "plain 'delete' does not distinguish twenty buttons from one another"
        )

    def test_has_button_type(self):
        assert UI_JS.count("del.setAttribute('type','button')") >= 2

    def test_translation_key_has_a_slot_for_the_name(self):
        assert "delete_entry_aria" in I18N_JS
        assert "{name}" in I18N_JS


class TestTranslationsInEveryLocale:
    @pytest.mark.parametrize("key", [
        "workspace_tree_aria", "tree_folder_aria", "tree_file_aria",
        "tree_external_link_aria", "delete_entry_aria",
    ])
    def test_key_present_in_15_locales(self, key):
        assert I18N_JS.count(f"{key}:") == 15, (
            f"{key}: {I18N_JS.count(key + ':')} occurrences, expected 15"
        )


# ── behavior measurement in Node ───────────────────────────────────────────

@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if not NODE:
        pytest.skip("node niedostepny")
    if not HARNESS.exists():
        pytest.skip("brak harnessu")
    proc = subprocess.run([NODE, str(HARNESS)], capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"harness padl: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestRowBehaviour:
    def test_folder_carries_the_tree_contract(self, behaviour):
        k = behaviour["collapsedFolder"]
        assert k["role"] == "treeitem"
        assert k["expanded"] == "false"
        assert k["level"] == "1"

    def test_folder_states_what_it_is(self, behaviour):
        """The core of the report: instead of "▸" it should be "folder .cache"."""
        label = behaviour["collapsedFolder"]["label"] or ""
        assert "folder" in label and ".cache" in label, f"label={label}"

    def test_expanded_folder_says_so(self, behaviour):
        assert behaviour["expandedFolder"]["expanded"] == "true"

    def test_depth_is_exposed(self, behaviour):
        assert behaviour["expandedFolder"]["level"] == "3"

    def test_file_does_not_pretend_to_be_expandable(self, behaviour):
        assert behaviour["plik"]["maExpanded"] is False, (
            "aria-expanded on a leaf is misleading"
        )

    def test_file_states_it_is_a_file(self, behaviour):
        assert "file" in (behaviour["plik"]["label"] or "")


class TestContainerBehaviour:
    def test_container_is_a_tree(self, behaviour):
        assert behaviour["kontener"]["role"] == "tree"

    def test_container_has_a_name(self, behaviour):
        assert behaviour["kontener"]["maNazwe"] is True

    def test_listener_is_not_duplicated_on_repaint(self, behaviour):
        """renderFileTree calls the helper after every render, so it must be idempotent."""
        assert behaviour["kontener"]["listenersAfterThreeCalls"] == 1


class TestKeyboardNavigation:
    def test_down_and_up_arrows(self, behaviour):
        assert behaviour["nawigacja"]["downToSecond"] is True
        assert behaviour["nawigacja"]["upReturns"] is True

    def test_roving_tabindex(self, behaviour):
        assert behaviour["nawigacja"]["rovingMoved"] is True, (
            "only one row may be in the Tab order"
        )

    def test_home_and_end(self, behaviour):
        assert behaviour["nawigacja"]["endToLast"] is True
        assert behaviour["nawigacja"]["homeToFirst"] is True

    def test_right_expands_left_collapses(self, behaviour):
        assert behaviour["nawigacja"]["rightExpands"] is True
        assert behaviour["nawigacja"]["leftCollapses"] is True

    def test_left_arrow_on_file_moves_to_parent(self, behaviour):
        assert behaviour["nawigacja"]["leftToParent"] is True

    def test_enter_activates(self, behaviour):
        assert behaviour["nawigacja"]["enterActivates"] is True


class TestRobustness:
    def test_null_does_not_break_rendering(self, behaviour):
        assert behaviour["odpornosc"]["nullOk"] is True

    def test_still_a_treeitem_without_options(self, behaviour):
        assert behaviour["odpornosc"]["bezOpcjiRole"] == "treeitem"

    def test_level_is_not_guessed(self, behaviour):
        assert behaviour["odpornosc"]["bezPoziomuMaLevel"] is False
