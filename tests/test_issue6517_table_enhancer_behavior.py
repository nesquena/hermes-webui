"""Behavior coverage for PR #6517's `enhanceMarkdownTables()` — executed against a
DOM, not asserted from source text. The review asked specifically for runtime proof
that a second enhancement pass is idempotent (one wrapper, one filter), that the
filter is pinned above the scroll area and filters rows, that sorting is stable in
both directions, and that CSV tables (already wrapped) are excluded.

There is no jsdom in this repo, so — following the existing `test_issue4945_markdown_table_copy.py`
convention — a tiny purpose-built DOM runs the real function in node. Only the DOM
surface the enhancer actually touches is implemented.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Balanced-brace extractor (same technique as test_issue4945_markdown_table_copy.py).
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const braceStart = src.indexOf('{', start);
  let depth = 0, inString = null, escaped = false, inLine = false, inBlock = false;
  for (let i = braceStart; i < src.length; i++) {
    const ch = src[i], next = i + 1 < src.length ? src[i + 1] : '';
    if (inLine) { if (ch === '\n') inLine = false; continue; }
    if (inBlock) { if (ch === '*' && next === '/') { inBlock = false; i++; } continue; }
    if (inString) {
      if (escaped) escaped = false; else if (ch === '\\') escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === '/' && next === '/') { inLine = true; i++; continue; }
    if (ch === '/' && next === '*') { inBlock = true; i++; continue; }
    if (ch === "'" || ch === '"' || ch === '`') { inString = ch; continue; }
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(name + ' brace scan failed');
}

// ---- minimal DOM ---------------------------------------------------------- //
class ClassList {
  constructor(node) { this.node = node; }
  _set() { return new Set((this.node.className || '').split(/\s+/).filter(Boolean)); }
  contains(n) { return this._set().has(n); }
  add(n) { const s = this._set(); s.add(n); this.node.className = [...s].join(' '); }
  remove(n) { const s = this._set(); s.delete(n); this.node.className = [...s].join(' '); }
}

class TextNode {
  constructor(text) { this.nodeType = 3; this.textContent = String(text); this.parentNode = null; this.parentElement = null; }
}

class El {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag ? tag.toUpperCase() : undefined;
    this.childNodes = [];
    this.parentNode = null;
    this.parentElement = null;
    this.className = '';
    this.attrs = {};
    this.dataset = {};
    this.classList = new ClassList(this);
    this._listeners = {};
    this._text = null;      // explicit text override (leaf)
    this.hidden = false;
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  _detach(node) {
    const p = node.parentNode;
    if (p) { const i = p.childNodes.indexOf(node); if (i >= 0) p.childNodes.splice(i, 1); }
  }
  appendChild(node) {
    this._detach(node);
    node.parentNode = this; node.parentElement = this;
    this.childNodes.push(node);
    this._text = null;
    return node;
  }
  insertBefore(node, ref) {
    this._detach(node);
    node.parentNode = this; node.parentElement = this;
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(node); else this.childNodes.splice(i, 0, node);
    return node;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  set textContent(v) { this.childNodes = [new TextNode(v)]; this.childNodes[0].parentNode = this; this._text = null; }
  get textContent() {
    if (this._text !== null) return this._text;
    return this.childNodes.map((n) => n.textContent).join('');
  }
  // table-family live views
  get rows() {
    if (this.tagName === 'TABLE') {
      const out = [];
      this.children.forEach((c) => {
        if (c.tagName === 'THEAD' || c.tagName === 'TBODY' || c.tagName === 'TFOOT') out.push(...c.rows);
        else if (c.tagName === 'TR') out.push(c);
      });
      return out;
    }
    return this.children.filter((c) => c.tagName === 'TR');
  }
  get tHead() { return this.children.find((c) => c.tagName === 'THEAD') || null; }
  get tBodies() { return this.children.filter((c) => c.tagName === 'TBODY'); }
  get cells() { return this.children.filter((c) => c.tagName === 'TD' || c.tagName === 'TH'); }
  // selector engine (comma > descendant > compound: tag/.class/[attr]/[attr=v]/:not(simple))
  _matchSimple(tok, sel) {
    sel = sel.trim();
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    const notm = sel.match(/^:not\((.+)\)$/);
    if (notm) return !this._matchSimple(tok, notm[1]);
    const attrm = sel.match(/^\[([^\]=]+)(?:=['"]?([^'"\]]*)['"]?)?\]$/);
    if (attrm) {
      if (!this.hasAttribute(attrm[1])) return false;
      return attrm[2] === undefined || this.getAttribute(attrm[1]) === attrm[2];
    }
    return this.tagName === sel.toUpperCase();
  }
  _tokenize(compound) {
    // whole tokens, parens-aware so `:not([data-x])` is not split at its inner `[`
    return compound.match(/:not\([^)]*\)|\[[^\]]*\]|\.[-\w]+|[-\w]+/g) || [];
  }
  _matchCompound(compound) {
    const toks = this._tokenize(compound);
    return toks.length > 0 && toks.every((p) => this._matchSimple(this, p));
  }
  matches(sel) {
    if (this.nodeType !== 1) return false;
    // rightmost compound only (used by closest/simple matches)
    return sel.split(',').some((group) => this._matchCompound(group.trim().split(/\s+/).pop()));
  }
  _matchGroup(group) {
    const parts = group.trim().split(/\s+(?![^\[]*\])/);
    if (!this._matchCompound(parts[parts.length - 1])) return false;
    let anc = this.parentElement, idx = parts.length - 2;
    while (idx >= 0) {
      let ok = false;
      while (anc) { if (anc._matchCompound(parts[idx])) { ok = true; anc = anc.parentElement; break; } anc = anc.parentElement; }
      if (!ok) return false;
      idx--;
    }
    return true;
  }
  querySelectorAll(sel) {
    const groups = sel.split(',').map((g) => g.trim());
    const out = [];
    const walk = (node) => node.children.forEach((c) => {
      if (groups.some((g) => c._matchGroup(g))) out.push(c);
      walk(c);
    });
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { let n = this; while (n) { if (n.nodeType === 1 && n.matches(sel)) return n; n = n.parentElement; } return null; }
}

const document = { createElement: (tag) => new El(tag) };
global.document = document;

function dispatch(el, type) { (el._listeners[type] || []).forEach((fn) => fn()); }

function makeTable(headers, rows) {
  const table = new El('table');
  const thead = new El('thead');
  const htr = new El('tr');
  headers.forEach((h) => { const th = new El('th'); th.appendChild(new TextNode(h)); htr.appendChild(th); });
  thead.appendChild(htr);
  const tbody = new El('tbody');
  rows.forEach((cells) => {
    const tr = new El('tr');
    cells.forEach((c) => { const td = new El('td'); td.appendChild(new TextNode(c)); tr.appendChild(td); });
    tbody.appendChild(tr);
  });
  table.appendChild(thead); table.appendChild(tbody);
  return table;
}

function makeMsgBody(child) { const d = new El('div'); d.className = 'msg-body'; d.appendChild(child); return d; }

for (const name of ['_markdownTableText', '_markdownTableCellText', 'enhanceMarkdownTables']) {
  eval(extractFunc(name));
}
"""


def _run(body: str):
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as fh:
        fh.write(_DRIVER)
        fh.write(body)
        script = Path(fh.name)
    try:
        res = subprocess.run([NODE, str(script), str(MESSAGES_JS)],
                             capture_output=True, text=True, timeout=30, cwd=str(ROOT))
    finally:
        script.unlink(missing_ok=True)
    if res.returncode != 0:
        raise RuntimeError(f"node helper failed:\n{res.stderr}")
    return json.loads(res.stdout.strip())


# Fixture: a root with one 4-row table (gets filter + sort) inside .msg-body, plus a
# CSV table already inside .csv-table-wrap that must be left untouched.
_FIXTURE = r"""
const root = new El('div');

const table = makeTable(['Name', 'Grp'], [
  ['Widget', 'b'],   // idx 0
  ['Gadget', 'a'],   // idx 1
  ['Gizmo',  'a'],   // idx 2
  ['Wodget', 'b'],   // idx 3
]);
root.appendChild(makeMsgBody(table));

// CSV table: already wrapped -> enhancer must skip it entirely.
const csvWrap = new El('div'); csvWrap.className = 'csv-table-wrap';
const csvTable = makeTable(['X', 'Y'], [['1', '2'], ['3', '4'], ['5', '6'], ['7', '8']]);
csvWrap.appendChild(csvTable);
root.appendChild(makeMsgBody(csvWrap));
"""


def test_double_invocation_is_idempotent_one_wrapper_and_one_filter():
    out = _run(_FIXTURE + r"""
enhanceMarkdownTables(root);
enhanceMarkdownTables(root);   // second pass must be a no-op on the same tables

const scrolls = root.querySelectorAll('.markdown-table-scroll');
const filters = root.querySelectorAll('.markdown-table-filter');
const bareTable = root.querySelectorAll('.msg-body table')
  .find((t) => !t.closest('.csv-table-wrap'));
console.log(JSON.stringify({
  scrollWrappers: scrolls.length,
  filters: filters.length,
  enhancedFlag: bareTable.getAttribute('data-markdown-table-enhanced'),
  tableParentIsScroll: bareTable.parentElement.classList.contains('markdown-table-scroll'),
}));
""")
    assert out["scrollWrappers"] == 1, "second pass double-wrapped the table"
    assert out["filters"] == 1, "second pass added a duplicate filter"
    assert out["enhancedFlag"] == "1"
    assert out["tableParentIsScroll"] is True


def test_filter_is_pinned_above_the_scroll_area():
    out = _run(_FIXTURE + r"""
enhanceMarkdownTables(root);
const scroll = root.querySelector('.markdown-table-scroll');
const host = scroll.parentElement;
const kids = host.children.map((c) => c.className || c.tagName);
console.log(JSON.stringify({
  order: kids,
  filterBeforeScroll: kids.indexOf('markdown-table-filter') < kids.indexOf('markdown-table-scroll'),
}));
""")
    # the filter input sits immediately before the scroll wrapper, so it never scrolls away
    assert out["filterBeforeScroll"] is True, f"filter not pinned above scroll area: {out['order']}"


def test_filter_hides_non_matching_rows():
    out = _run(_FIXTURE + r"""
enhanceMarkdownTables(root);
const filter = root.querySelector('.markdown-table-filter');
const bodyRows = root.querySelector('.msg-body table tbody').rows;
filter.value = 'get';                 // matches Widget / Gadget / Wodget, hides Gizmo
dispatch(filter, 'input');
const shown = bodyRows.filter((r) => !r.hidden).map((r) => r.cells[0].textContent);
filter.value = '';                    // clearing restores every row
dispatch(filter, 'input');
const afterClear = bodyRows.filter((r) => !r.hidden).length;
console.log(JSON.stringify({ shown, afterClear }));
""")
    assert sorted(out["shown"]) == ["Gadget", "Widget", "Wodget"], out["shown"]
    assert "Gizmo" not in out["shown"], "non-matching row must be hidden"
    assert out["afterClear"] == 4, "clearing the filter must restore all rows"


def test_sorting_is_stable_ascending_and_descending():
    out = _run(_FIXTURE + r"""
enhanceMarkdownTables(root);
const tbl = root.querySelector('.msg-body table');
const grpHeaderButton = tbl.tHead.rows[0].cells[1].querySelector('.markdown-table-sort');
const order = () => tbl.tBodies[0].rows.map((r) => r.cells[0].textContent);

dispatch(grpHeaderButton, 'click');   // ascending by Grp (a,a,b,b)
const asc = order();
dispatch(grpHeaderButton, 'click');   // descending by Grp (b,b,a,a)
const desc = order();
console.log(JSON.stringify({ asc, desc, ariaAsc: tbl.tHead.rows[0].cells[1].getAttribute('aria-sort') }));
""")
    # ties keep original document order in BOTH directions (stable: tiebreak is original index)
    assert out["asc"] == ["Gadget", "Gizmo", "Widget", "Wodget"], out["asc"]
    assert out["desc"] == ["Widget", "Wodget", "Gadget", "Gizmo"], out["desc"]


def test_csv_tables_are_excluded():
    out = _run(_FIXTURE + r"""
enhanceMarkdownTables(root);
const theCsv = root.querySelector('.csv-table-wrap table');
console.log(JSON.stringify({
  csvEnhanced: theCsv.getAttribute('data-markdown-table-enhanced'),
  csvWrappedInScroll: theCsv.parentElement.classList.contains('markdown-table-scroll'),
  csvHasSortButton: !!theCsv.querySelector('.markdown-table-sort'),
}));
""")
    assert out["csvEnhanced"] is None, "CSV table must not be enhanced"
    assert out["csvWrappedInScroll"] is False, "CSV table must keep its .csv-table-wrap, not gain a scroll wrapper"
    assert out["csvHasSortButton"] is False
