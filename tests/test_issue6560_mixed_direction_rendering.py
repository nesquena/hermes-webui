"""Regression coverage for mixed RTL/LTR message direction handling.

The review on PR #6560 called out that message direction must be applied at the
prose/container level, not by isolating every inline emphasis/link, and that
ordinary Markdown tables need explicit LTR treatment as machine-oriented
content, not per-element dir="auto" prose treatment.

A later review (PR #7135) caught that the original fix classified plain
`table`/`thead`/`tbody`/`tfoot`/`tr`/`th`/`td` in BOTH `blockSelector` (auto
direction) and claimed LTR treatment only through `.csv-table`, which ordinary
Markdown tables never carry — so real Markdown tables never actually got
forced LTR and could flip direction based on their (often numeric/short)
content. These tests execute the real helper in a minimal Node DOM shim
against actual table/paragraph markup and assert the resulting `dir`
attributes, rather than only checking for selector substrings in the source.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_source(name: str, source: str = UI_JS) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} not found in static/ui.js"
    brace = source.find("{", start)
    assert brace != -1, f"opening brace not found for {name}"
    depth = 1
    i = brace + 1
    while depth and i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"unterminated function {name}"
    return source[start:i]


# Minimal DOM shim: enough tag/class/attribute selector support to exercise
# _applyAutomaticMessageDirections' querySelectorAll/matches calls against a
# real element tree, without pulling in jsdom.
_DOM_SHIM = r"""
class FakeElement {
  constructor(tag, className) {
    this.tagName = tag.toUpperCase();
    this.className = className || '';
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.nodeType = 1;
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  get classList() {
    const self = this;
    return {
      add(name) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        set.add(name);
        self.className = [...set].join(' ');
      },
    };
  }
  _selfMatchesSimple(simple) {
    // simple selector: tag name, or .class, no combinators
    if (simple.startsWith('.')) {
      const cls = simple.slice(1);
      return this.className.split(/\s+/).filter(Boolean).includes(cls);
    }
    return this.tagName === simple.toUpperCase();
  }
  matches(selector) {
    return selector.split(',').map(s => s.trim()).some(s => this._selfMatchesSimple(s));
  }
  _walk(list) {
    for (const child of this.children) {
      list.push(child);
      child._walk(list);
    }
  }
  querySelectorAll(selector) {
    const all = [];
    this._walk(all);
    return all.filter(node => node.matches(selector));
  }
}
function el(tag, className, children) {
  const e = new FakeElement(tag, className);
  for (const c of (children || [])) e.appendChild(c);
  return e;
}
"""


def _run_node(harness: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover - depends on environment
        pytest.skip("node not available")
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _harness_for_scope() -> str:
    fn_src = _function_source("_applyAutomaticMessageDirections")
    prune_src = _function_source("_pruneAutomaticMessageDirectionObservers")
    return textwrap.dedent(
        f"""
        {_DOM_SHIM}
        const _automaticMessageDirectionObservers = new Map();
        {prune_src}
        {fn_src}
        // A .msg-body containing: one prose paragraph, and one ordinary
        // Markdown table (th/td), mirroring real chat rendering output.
        const p = el('p', '');
        const th = el('th', '');
        const td = el('td', '');
        const tr = el('tr', '', [th, td]);
        const thead = el('thead', '', [tr]);
        const table = el('table', '', [thead]);
        const body = el('div', 'msg-body', [p, table]);
        _applyAutomaticMessageDirections(body);
        console.log(JSON.stringify({{
          bodyDir: body.getAttribute('dir'),
          pDir: p.getAttribute('dir'),
          tableDir: table.getAttribute('dir'),
          theadDir: thead.getAttribute('dir'),
          trDir: tr.getAttribute('dir'),
          thDir: th.getAttribute('dir'),
          tdDir: td.getAttribute('dir'),
        }}));
        """
    )


class TestMixedDirectionHelper:
    def test_ordinary_markdown_table_ends_up_ltr_not_auto(self):
        result = _run_node(_harness_for_scope())
        # Prose paragraph gets per-element auto direction (bidi-aware).
        assert result["pDir"] == "auto"
        # Table and all of its structural descendants are machine-oriented
        # content: forced LTR, never left as per-content "auto" (the bug
        # PR #7135 caught -- table was in blockSelector with no matching LTR
        # rule, so it silently kept whatever direction dir="auto" produced).
        for key in ("tableDir", "theadDir", "trDir", "thDir", "tdDir"):
            assert result[key] == "ltr", f"{key} expected ltr, got {result[key]!r}"

    def test_helper_exists_and_wires_observer_cleanup(self):
        src = _function_source("_applyAutomaticMessageDirections")
        assert "body.setAttribute('dir','auto');" in src
        assert "const machineSelector=[" in src
        assert "'.csv-table-wrap','.csv-table'" in src
        assert "a,strong,em" not in src
        assert "MutationObserver" in src
        assert "record.addedNodes" in src
        assert "_pruneAutomaticMessageDirectionObservers();" in src

    def test_block_selector_excludes_table_structural_tags(self):
        src = _function_source("_applyAutomaticMessageDirections")
        block_selector_line = next(
            line for line in src.splitlines() if "const blockSelector=" in line
        )
        for tag in ("table", "thead", "tbody", "tfoot", "tr", "th", "td"):
            assert tag not in block_selector_line, (
                f"blockSelector must not include '{tag}' -- ordinary Markdown "
                "tables are machine-oriented content and must only receive "
                "forced LTR treatment via machineSelector, not per-element "
                "dir=\"auto\" prose treatment"
            )

    def test_machine_selector_includes_table_structural_tags(self):
        src = _function_source("_applyAutomaticMessageDirections")
        machine_block_start = src.find("const machineSelector=[")
        machine_block_end = src.find("].join(',');", machine_block_start)
        machine_block = src[machine_block_start:machine_block_end]
        for tag in ("table", "thead", "tbody", "tfoot", "tr", "th", "td"):
            assert f"'{tag}'" in machine_block, (
                f"machineSelector must include '{tag}' so ordinary Markdown "
                "tables are forced LTR like the existing .csv-table handling"
            )

    def test_helper_is_invoked_from_render_and_postprocess_paths(self):
        render_src = _function_source("renderMessages")
        post_src = _function_source("postProcessRenderedMessages")
        assert "_applyAutomaticMessageDirections(inner);" in render_src
        assert "_applyAutomaticMessageDirections(container);" in post_src

    def test_observer_cleanup_helper_exists(self):
        src = _function_source("_disconnectAutomaticMessageDirections")
        assert "_automaticMessageDirectionObservers.get(body)" in src
        assert "observer.disconnect()" in src
