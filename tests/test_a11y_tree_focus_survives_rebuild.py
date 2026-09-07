"""Expanding a directory with the keyboard must not throw focus back to the top.

Raised in review of PR #7258 (P1): pressing Right/Left on a directory row calls
`row.click()`, which triggers `renderFileTree()`. That rebuilds every tree item
from scratch, and the roving tabindex is assigned to the FIRST top-level row
(`focusable: depth===0 && ...length===0`). The row the user just toggled is
detached, so focus lands on `document.body` and the next arrow press restarts
navigation at the beginning of the tree.

Why this matters more than it looks: the whole point of the roving tabindex is
that a tree with a hundred files is not a hundred Tab stops. If every expand
resets the position, a screen reader user cannot walk INTO a directory - they
expand it and are teleported back to the first entry, with no announcement
explaining why. Keyboard operability (WCAG 2.1.1) and focus order (2.4.3).

The fix records which entry was focused before the rebuild and restores the tab
stop to that entry afterwards, falling back to the first row when the entry is
gone (a deleted or collapsed-away path).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
A11Y_JS = (REPO / "static" / "a11y-helpers.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


class TestFocusSurvivesARebuild:
    """The contract that the source must carry for focus to be restorable."""

    def test_helper_to_remember_the_focused_entry_exists(self):
        assert "a11yTreeRememberFocus" in A11Y_JS, (
            "there must be a helper that records the focused entry before a rebuild; "
            "without it renderFileTree cannot restore the tab stop"
        )

    def test_helper_to_restore_the_focused_entry_exists(self):
        assert "a11yTreeRestoreFocus" in A11Y_JS, (
            "there must be a helper that restores the tab stop after a rebuild"
        )

    def test_both_helpers_are_exported(self):
        for name in ("a11yTreeRememberFocus", "a11yTreeRestoreFocus"):
            assert re.search(rf"window\.{name}\s*=", A11Y_JS), (
                f"{name} must be exported on window - renderFileTree lives in ui.js"
            )

    def test_render_remembers_before_and_restores_after(self):
        """Order matters: remember BEFORE the rows are replaced, restore AFTER."""
        start = UI_JS.find("function renderFileTree(")
        assert start > 0, "renderFileTree not found"
        # take a generous window; the function is long
        body = UI_JS[start:start + 6000]
        i_remember = body.find("a11yTreeRememberFocus")
        i_restore = body.find("a11yTreeRestoreFocus")
        assert i_remember > 0, "renderFileTree must record the focused entry"
        assert i_restore > 0, "renderFileTree must restore focus after rebuilding"
        assert i_remember < i_restore, (
            "the entry must be recorded BEFORE the rows are replaced and restored "
            "afterwards; the reverse order records the already-destroyed state"
        )

    def test_rows_carry_the_entry_path(self):
        """Restoring by path requires the path to be on the row."""
        assert "a11yTreePath" in A11Y_JS or "data-a11y-tree-path" in A11Y_JS, (
            "rows must carry a stable identifier (the entry path) - index position "
            "is not usable, because expanding changes how many rows precede it"
        )


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(SRC, 'utf8');

function mkEl(){
  const el = {
    _attrs: {}, dataset: {}, children: [], listeners: {}, tagName: 'DIV',
    textContent: '',
    setAttribute(k, v){ this._attrs[k] = String(v); },
    getAttribute(k){ return k in this._attrs ? this._attrs[k] : null; },
    removeAttribute(k){ delete this._attrs[k]; },
    hasAttribute(k){ return k in this._attrs; },
    classList: {add(){}, remove(){}, contains(){ return false; }},
    appendChild(c){ this.children.push(c); return c; },
    addEventListener(t, fn){ (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener(){},
    focus(){ context.document.activeElement = this; },
    click(){ this._clicked = (this._clicked || 0) + 1; },
    querySelector(){ return null; },
    querySelectorAll(sel){
      if (String(sel).indexOf('treeitem') >= 0) {
        return this.children.filter((c) => c.getAttribute('role') === 'treeitem');
      }
      return [];
    },
  };
  return el;
}

const context = {
  console, JSON, Math, Date, Number, String, Boolean, Array, Object, Error,
  setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, encodeURIComponent, decodeURIComponent, isNaN, parseInt, parseFloat,
  document: {
    activeElement: null,
    getElementById(){ return null; },
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
    createElement(){ return mkEl(); },
    body: mkEl(),
    addEventListener(){},
  },
  location: {pathname: '/'},
  window: {},
  navigator: {userAgent: 'node'},
  t(k){ return k; },
  __mkEl: mkEl,
};
context.globalThis = context;
context.window = context;
vm.createContext(context);
vm.runInContext(src, context);
// The scenario is wrapped in an IIFE: `return` at the top level of a vm script is
// a SyntaxError ("Illegal return statement"), which surfaces as an opaque node
// failure rather than a test assertion.
const out = vm.runInContext('(function(){' + SCENARIO + '})()', context);
console.log(JSON.stringify(out));
process.exit(0);
"""


class TestFocusRestoreBehaviour:
    """Executes the real helpers: the contract must hold in behaviour, not text."""

    def _run(self, scenario):
        if not NODE:
            pytest.skip("node unavailable - cannot measure behaviour")
        script = (
            HARNESS
            .replace("SRC", json.dumps(str(REPO / "static" / "a11y-helpers.js")))
            .replace("SCENARIO", json.dumps(scenario))
        )
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        assert r.returncode == 0, f"node failed: {r.stderr[-2000:]}"
        return json.loads(r.stdout)

    _BUILD = """
    const box = __mkEl();
    const paths = ['a', 'b', 'b/child', 'c'];
    const rows = paths.map((p, i) => {
      const r = __mkEl();
      a11yTreeRow(r, {level: p.indexOf('/') >= 0 ? 2 : 1, expandable: p === 'b',
                      expanded: p === 'b', label: p, focusable: i === 0, path: p});
      box.appendChild(r);
      return r;
    });
    """

    def test_focused_entry_is_restored_after_a_rebuild(self):
        out = self._run(self._BUILD + """
        // user walked to 'b' and expanded it
        document.activeElement = rows[1];
        const token = a11yTreeRememberFocus(box);
        // rebuild: brand-new elements, first row focusable as renderFileTree does
        const box2 = __mkEl();
        const rows2 = paths.map((p, i) => {
          const r = __mkEl();
          a11yTreeRow(r, {level: p.indexOf('/') >= 0 ? 2 : 1, expandable: p === 'b',
                          expanded: p === 'b', label: p, focusable: i === 0, path: p});
          box2.appendChild(r);
          return r;
        });
        a11yTreeRestoreFocus(box2, token);
        return {tabstops: rows2.map(r => r.getAttribute('tabindex')),
                focusedPath: document.activeElement && document.activeElement.dataset
                  ? document.activeElement.dataset.a11yTreePath : null};
        """)
        assert out["tabstops"] == ["-1", "0", "-1", "-1"], (
            "the tab stop must return to the entry the user was on, not to the first "
            f"row (got {out['tabstops']})"
        )
        assert out["focusedPath"] == "b", (
            "focus itself must land on the remembered entry, otherwise the next "
            "arrow press restarts at the top of the tree"
        )

    def test_missing_entry_falls_back_to_the_first_row(self):
        """Negative control: a vanished path must not leave the tree unreachable."""
        out = self._run(self._BUILD + """
        document.activeElement = rows[2];         // 'b/child'
        const token = a11yTreeRememberFocus(box);
        const box2 = __mkEl();
        const kept = ['a', 'b', 'c'];             // 'b/child' collapsed away
        const rows2 = kept.map((p, i) => {
          const r = __mkEl();
          a11yTreeRow(r, {level: 1, expandable: p === 'b', expanded: false,
                          label: p, focusable: i === 0, path: p});
          box2.appendChild(r);
          return r;
        });
        a11yTreeRestoreFocus(box2, token);
        return {tabstops: rows2.map(r => r.getAttribute('tabindex'))};
        """)
        assert out["tabstops"].count("0") == 1, (
            "exactly one row must remain in the Tab order - a tree with none is "
            "unreachable by keyboard, with several it is a Tab trap"
        )
        assert out["tabstops"][0] == "0", (
            "when the remembered entry is gone, the first row takes the tab stop"
        )

    def test_no_token_leaves_the_tree_usable(self):
        """Called with nothing remembered (first render), the tree still works."""
        out = self._run(self._BUILD + """
        a11yTreeRestoreFocus(box, null);
        return {tabstops: rows.map(r => r.getAttribute('tabindex'))};
        """)
        assert out["tabstops"].count("0") == 1, (
            "a null token must not remove the single tab stop"
        )
