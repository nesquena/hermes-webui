"""Regression tests for wide markdown tables gaining a horizontal scroll wrapper
so columns keep their natural width on narrow viewports instead of being
crushed to a few characters.

These tests drive the REAL enhanceMarkdownTables() function body through a
Node VM with a minimal fake DOM that supports the APIs the function uses.
"""

import shutil
import subprocess
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _read(rel: str) -> str:
    with open(REPO_ROOT / rel, encoding="utf-8") as f:
        return f.read()


def _extract_enhance_markdown_tables() -> str:
    """Extract the enhanceMarkdownTables function body from messages.js."""
    src = _read("static/messages.js")
    start = src.find("function enhanceMarkdownTables(root){")
    assert start >= 0, "enhanceMarkdownTables not found"
    body_open = src.find("{", start)
    depth = 0
    i = body_open
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError("could not find enhanceMarkdownTables closing brace")


def _run_node(js: str) -> subprocess.CompletedProcess:
    assert NODE, "node is required"
    return subprocess.run(
        [NODE, "-e", js], capture_output=True, text=True, cwd=REPO_ROOT, timeout=30
    )


def _harness() -> str:
    """Build a Node script with a fake DOM and a 6-row table inside .msg-body.
    Returns the script; the caller adds assertions on the output."""
    enhance_fn = _extract_enhance_markdown_tables()
    preamble = '''
function fakeEl(tag, attrs) {
  attrs = attrs || {};
  var el = {
    tag: tag,
    className: attrs.className || "",
    _children: [],
    parentElement: null,
    style: {},
    innerHTML: "",
    textContent: "",
    hidden: false,
    dataset: {},
    rows: [],
    classList: {
      _classes: (attrs.className||"").split(/\\s+/).filter(Boolean),
      add: function(c) { if(!this._classes.includes(c)) this._classes.push(c); el.className = this._classes.join(" "); },
      remove: function(c) { this._classes = this._classes.filter(function(x) { return x !== c; }); el.className = this._classes.join(" "); },
      contains: function(c) { return this._classes.includes(c); },
    },
    children: [],
    querySelectorAll: function(sel) {
      var results = [];
      (function walk(node) {
        if (node.tag === "table" && sel.indexOf("table") >= 0) results.push(node);
        (node._children||[]).forEach(walk);
      })(this);
      return results;
    },
    querySelector: function(sel) {
      if (sel === "tr") {
        for (var i = 0; i < (this._children||[]).length; i++) {
          var c = this._children[i];
          if (c.tag === "tr") return c;
          if (c._children) {
            for (var j = 0; j < c._children.length; j++) {
              if (c._children[j].tag === "tr") return c._children[j];
            }
          }
        }
      }
      return null;
    },
    closest: function(sel) {
      if (sel === ".csv-table-wrap") {
        var p = el;
        while (p) { if (p.className && p.className.indexOf("csv-table-wrap") >= 0) return p; p = p.parentElement; }
        return null;
      }
      return null;
    },
    appendChild: function(child) {
      child.parentElement = this;
      this._children.push(child);
      this.children = this._children;
    },
    insertBefore: function(newEl, refEl) {
      newEl.parentElement = this;
      var idx = this._children.indexOf(refEl);
      if (idx >= 0) this._children.splice(idx, 0, newEl);
      else this._children.push(newEl);
      this.children = this._children;
    },
    setAttribute: function(k, v) { if (!this.attrs) this.attrs = {}; this.attrs[k] = v; if (k.indexOf("data-") === 0) this.dataset[k.replace("data-", "")] = v; },
    getAttribute: function(k) { return this.attrs ? this.attrs[k] : null; },
    addEventListener: function() {},
    removeEventListener: function() {},
  };
  return el;
}

var document = { createElement: function(tag) { return fakeEl(tag); } };
function t(k) { return k; }

// Build DOM: root > .msg-body > table
var root = fakeEl("div");
var msgBody = fakeEl("div", {className: "msg-body"});
root.appendChild(msgBody);

var table = fakeEl("table");
var thead = fakeEl("thead");
var hrow = fakeEl("tr");
for (var ci = 0; ci < 5; ci++) {
  var th = fakeEl("th");
  th.textContent = "H" + ci;
  hrow.appendChild(th);
}
thead.appendChild(hrow);
thead.rows = [hrow];
table.appendChild(thead);

var tbody = fakeEl("tbody");
for (var ri = 0; ri < 6; ri++) {
  var row = fakeEl("tr");
  var td = fakeEl("td");
  td.textContent = "cell" + ri;
  row.appendChild(td);
  tbody.appendChild(row);
}
tbody.rows = tbody._children;
table.appendChild(tbody);
table.tHead = thead;
table.tBodies = [tbody];
table.rows = [hrow].concat(tbody._children);
msgBody.appendChild(table);
'''

    lifecycle = '''
// Record state before
var msgBodyChildrenBefore = msgBody._children.length;
var tableParentBefore = table.parentElement ? table.parentElement.className : "none";

// Run the function
enhanceMarkdownTables(root);

// Check results
var msgBodyChildren = msgBody._children;
var scrollWrap = null;
var filterInput = null;
for (var i = 0; i < msgBodyChildren.length; i++) {
  var c = msgBodyChildren[i];
  if (c.className === "markdown-table-scroll") scrollWrap = c;
  if (c.tag === "input") filterInput = c;
}
var tableParent = table.parentElement ? table.parentElement.className : "none";
var filterBeforeWrap = false;
if (filterInput && scrollWrap) {
  filterBeforeWrap = msgBodyChildren.indexOf(filterInput) < msgBodyChildren.indexOf(scrollWrap);
}

console.log("W8_RESULT " + JSON.stringify({
  scrollWrapCreated: scrollWrap !== null,
  scrollWrapClassName: scrollWrap ? scrollWrap.className : null,
  tableParentClassName: tableParent,
  filterFound: filterInput !== null,
  filterBeforeWrap: filterBeforeWrap,
}));
'''
    return preamble + enhance_fn + "\n" + lifecycle


def _idempotent_harness() -> str:
    """Same as _harness but calls enhanceMarkdownTables twice."""
    base = _harness()
    # Insert a second call before the result logging
    base = base.replace(
        'console.log("W8_RESULT ',
        'enhanceMarkdownTables(root);\nconsole.log("W8_RESULT ',
    )
    # Change the result to include the second call's wrap count
    base = base.replace(
        'console.log("W8_RESULT ',
        '''// Check wraps after second call
var wrapsAfterSecond = 0;
for (var i = 0; i < msgBody._children.length; i++) {
  if (msgBody._children[i].className === "markdown-table-scroll") wrapsAfterSecond++;
}
console.log("W8_RESULT ''',
    )
    # Add wrapsAfterSecond to the result object
    base = base.replace(
        '"filterBeforeWrap": filterBeforeWrap,',
        '"filterBeforeWrap": filterBeforeWrap, "wrapsAfterSecond": wrapsAfterSecond,',
    )
    return base


# ── Tests ────────────────────────────────────────────────────────────────────


def test_wide_table_gets_scroll_wrapper():
    """A markdown table must be wrapped in a .markdown-table-scroll div."""
    proc = _run_node(_harness())
    assert proc.returncode == 0, proc.stderr
    assert "W8_RESULT" in proc.stdout, proc.stdout
    payload = proc.stdout.split("W8_RESULT ", 1)[1].strip()
    data = json.loads(payload)
    assert data["scrollWrapCreated"] is True, data
    assert data["scrollWrapClassName"] == "markdown-table-scroll", data


def test_table_reparented_into_scroll_wrapper():
    """After wrapping, the table's direct parent must be the scroll div."""
    proc = _run_node(_harness())
    assert proc.returncode == 0, proc.stderr
    assert "W8_RESULT" in proc.stdout, proc.stdout
    payload = proc.stdout.split("W8_RESULT ", 1)[1].strip()
    data = json.loads(payload)
    assert data["tableParentClassName"] == "markdown-table-scroll", data


def test_filter_placed_before_scroll_wrapper():
    """The filter input (4+ rows) must be a sibling before the scroll wrapper."""
    proc = _run_node(_harness())
    assert proc.returncode == 0, proc.stderr
    assert "W8_RESULT" in proc.stdout, proc.stdout
    payload = proc.stdout.split("W8_RESULT ", 1)[1].strip()
    data = json.loads(payload)
    assert data["filterFound"] is True, data
    assert data["filterBeforeWrap"] is True, data


def test_idempotent_no_double_wrap():
    """Calling enhanceMarkdownTables twice must not double-wrap."""
    enhance_fn = _extract_enhance_markdown_tables()
    single = _harness()
    # Append a second enhanceMarkdownTables call before the result logging
    # and add wrapsAfterSecond to the output object.
    lines = single.split('\n')
    for i, line in enumerate(lines):
        if 'console.log("W8_RESULT' in line:
            # Insert second call + wrapsAfterSecond before this line
            indent = line[:len(line) - len(line.lstrip())]
            lines.insert(i, indent + 'enhanceMarkdownTables(root);')
            lines.insert(i+1, indent + 'var wrapsAfterSecond = 0;')
            lines.insert(i+2, indent + 'for (var ci = 0; ci < msgBody._children.length; ci++) {')
            lines.insert(i+3, indent + '  if (msgBody._children[ci].className === "markdown-table-scroll") wrapsAfterSecond++;')
            lines.insert(i+4, indent + '}')
            break
    # Also add wrapsAfterSecond to the JSON object
    for i, line in enumerate(lines):
        if 'filterBeforeWrap:' in line and 'wrapsAfterSecond' not in lines[i]:
            lines[i] = line.rstrip() + ' "wrapsAfterSecond": wrapsAfterSecond,'
            break
    double_js = '\n'.join(lines)
    proc = _run_node(double_js)
    assert proc.returncode == 0, proc.stderr
    assert "W8_RESULT" in proc.stdout, proc.stdout
    payload = proc.stdout.split("W8_RESULT ", 1)[1].strip()
    data = json.loads(payload)
    assert data["wrapsAfterSecond"] == 1, data