"""Regression test: workspace selector dropdown must follow the Workspaces view order.

renderWorkspaceDropdownInto() (composer/titlebar workspace switcher) used to
re-sort workspaces alphabetically by name before rendering:

    const sorted=[...workspaces].sort((a,b)=>(a.name||'').localeCompare(b.name||''));

The Workspaces settings panel (renderWorkspacesPanel) renders the server's
stored order — which the user controls via drag-and-drop reorder
(/api/workspaces/reorder persists the new order, and load_workspaces()
returns it verbatim). The dropdown ignoring that order meant the two
surfaces disagreed: a user who dragged "Home" to the top still saw it
 buried mid-list in the switcher.

Fix: render the dropdown in the server's stored order (no client-side sort).

This is an EXECUTED test, not a source-string check: it extracts the real
renderWorkspaceDropdownInto() from panels.js, runs it in a node VM against a
mock DOM, and asserts the rendered row order equals the input (stored) order
for a deliberately non-alphabetical list.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE_BIN = shutil.which("node")

_node_tests = pytest.mark.skipif(NODE_BIN is None, reason="node not on PATH")


def _extract_fn(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found in panels.js"
    brace = src.find("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"{name} body did not close")


def _run_node_vm(source: str) -> dict:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE_BIN, str(script_path)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


def _node_test_preamble(panels_js_repr: str) -> str:
    return (
        "const PANELS_JS = " + panels_js_repr + ";\n"
        """
// ── Mock DOM ─────────────────────────────────────────────────────────────
function makeEl(tag) {
  const el = {
    tag,
    children: [],
    style: {},
    dataset: {},
    _innerHTML: '',
    onclick: null,
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._innerHTML; },
    set(v) { el._innerHTML = String(v); },
  });
  el.classList = {
    _set: new Set(),
    add(c) { this._set.add(c); },
    remove(c) { this._set.delete(c); },
    contains(c) { return this._set.has(c); },
    toggle(c, force) {
      if (force === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); }
      else if (force) { this._set.add(c); } else { this._set.delete(c); }
    },
  };
  // className assignments (e.g. opt.className='ws-opt active') must be
  // visible to classList.contains — production DOM semantics.
  Object.defineProperty(el, 'className', {
    get() { return Array.from(el.classList._set).join(' '); },
    set(v) { el.classList._set = new Set(String(v).split(/\\s+/).filter(Boolean)); },
  });
  el.appendChild = (child) => { el.children.push(child); return child; };
  el.addEventListener = () => {};
  el.setAttribute = (k, v) => { el.dataset[k] = v; };
  el.classList = {
    _set: new Set(),
    add(c) { this._set.add(c); },
    remove(c) { this._set.delete(c); },
    contains(c) { return this._set.has(c); },
    toggle(c, force) {
      if (force === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); }
      else if (force) { this._set.add(c); } else { this._set.delete(c); }
    },
  };
  // querySelector: return a generic stub for the search input/clear button
  // (their children come from innerHTML which the mock does not parse).
  el.querySelector = () => makeEl('stub');
  // querySelectorAll: filter descendants by className token.
  el.querySelectorAll = (sel) => {
    const cls = sel.startsWith('.') ? sel.slice(1) : sel;
    const out = [];
    const walk = (n) => { for (const c of n.children) { walk(c); if ((c.className || '').split(/\\s+/).includes(cls)) out.push(c); } };
    walk(el);
    return out;
  };
  return el;
}

const document = { createElement: (tag) => makeEl(tag) };

// ── Mock helpers referenced by the extracted functions ───────────────────
const esc = (s) => (s == null ? '' : String(s));
const t = (key) => key; // return the key; label text is irrelevant to order
const li = () => '<svg/>';
function switchToWorkspace(){ /* click handler only — never invoked at render */ }
"""

    )


def _extracted_functions_js() -> str:
    panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    return (
        _extract_fn(panels, "_renderWorkspaceAction")
        + "\n"
        + _extract_fn(panels, "renderWorkspaceDropdownInto")
    )


@_node_tests
def test_workspace_dropdown_renders_in_stored_order():
    """Dropdown rows must match the caller's (server stored) order exactly.

    Input is deliberately NON-alphabetical: an alphabetical sort would
    produce fantasy-football, Hermes Dev, Home — this test fails under the
    old client-side sort and passes with server order.
    """
    panels_js_repr = repr(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    )
    source = (
        _node_test_preamble(panels_js_repr)
        + _extracted_functions_js()
        + """
const input = [
  { name: 'Hermes Dev',        path: '/Users/x/hermes-webui' },
  { name: 'Home',              path: '/Users/x' },
  { name: 'fantasy-football',  path: '/Users/x/fantasy-football' },
];
const dd = makeEl('div');
renderWorkspaceDropdownInto(dd, input, '/Users/x');

// The workspace list container holds the .ws-opt rows (plus the noResults
// element appended last). Recover rendered order from the live child tree.
const listContainer = dd.children.find((c) => c.className === 'ws-list-container');
if (!listContainer) { console.log(JSON.stringify({ error: 'ws-list-container not found' })); process.exit(0); }
const rows = listContainer.children.filter((c) => (c.className || '').split(/\\s+/).includes('ws-opt'));
const renderedNames = rows.map((r) => r.dataset.name);
const renderedPaths = rows.map((r) => r.dataset.path);
const activeRow = rows.find((r) => r.classList.contains('active'));
console.log(JSON.stringify({
  renderedNames,
  renderedPaths,
  activePath: activeRow ? activeRow.dataset.path : null,
  rowCount: rows.length,
}));
"""
    )
    result = _run_node_vm(source)
    assert "error" not in result, result

    # Exact rendered sequence === input (stored) sequence.
    assert result["renderedNames"] == ["Hermes Dev", "Home", "fantasy-football"], (
        "Workspace dropdown rows are not in the caller's (server stored) order. "
        "If a client-side alphabetical sort was reintroduced in "
        "renderWorkspaceDropdownInto, the Workspaces view order (drag-and-drop "
        "reorder, 'Home' first) is being ignored by the switcher dropdown."
    )
    assert result["renderedPaths"] == [
        "/Users/x/hermes-webui",
        "/Users/x",
        "/Users/x/fantasy-football",
    ]
    assert result["rowCount"] == 3
    # Active workspace highlighting must survive the ordering change.
    assert result["activePath"] == "/Users/x"


@_node_tests
def test_workspace_dropdown_handles_empty_list():
    """Empty workspace list renders the dropdown shell without throwing."""
    panels_js_repr = repr(
        (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    )
    source = (
        _node_test_preamble(panels_js_repr)
        + _extracted_functions_js()
        + """
const dd = makeEl('div');
renderWorkspaceDropdownInto(dd, [], '');
const listContainer = dd.children.find((c) => c.className === 'ws-list-container');
const rows = listContainer
  ? listContainer.children.filter((c) => (c.className || '').split(/\\s+/).includes('ws-opt'))
  : [];
console.log(JSON.stringify({ rowCount: rows.length, hasList: !!listContainer }));
"""
    )
    result = _run_node_vm(source)
    assert result["hasList"] is True
    assert result["rowCount"] == 0


@_node_tests
def test_workspace_dropdown_no_client_side_sort_present():
    """Source check: the alphabetical sort must not return to the render path.

    Complements the executed test above — catches a reintroduced sort even
    when a caller happens to pass pre-sorted data (which would make the
    executed order assertion vacuous).
    """
    panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    fn_start = panels.find("function renderWorkspaceDropdownInto")
    assert fn_start != -1, "renderWorkspaceDropdownInto must exist in panels.js"
    fn_src = _extract_fn(panels, "renderWorkspaceDropdownInto")
    assert "localeCompare" not in fn_src, (
        "renderWorkspaceDropdownInto re-sorts workspaces client-side. The "
        "dropdown must render the server's stored order — the same order as "
        "the Workspaces settings panel (drag-and-drop reorder)."
    )
    assert re.search(r"\.sort\(", fn_src) is None, (
        "renderWorkspaceDropdownInto contains a .sort() call — client-side "
        "reordering of the workspace dropdown is not allowed; render the "
        "caller's (server stored) order verbatim."
    )


import re  # noqa: E402  (used only by the source-check test above)
