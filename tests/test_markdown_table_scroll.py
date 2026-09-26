"""Regression tests for wide markdown tables gaining a horizontal scroll wrapper
so columns keep their natural width on narrow viewports instead of being
crushed to a few characters.

These tests drive the REAL enhanceMarkdownTables() function body through a
Node VM with a fake DOM that supports the APIs the function uses.

Why the fake DOM is this faithful (Greptile P2 on the first revision):
the production idempotency mechanism is the SELECTOR itself —
``.msg-body table:not([data-markdown-table-enhanced])``. A fake
``querySelectorAll`` that returns every ``table`` it can find hides that
mechanism entirely: the second run would re-process an already-enhanced table,
and the test would still pass because it only counted wrappers. So the fake
here evaluates the selector (tag + class + ``:not([attr])`` + descendant
ancestor) instead of hard-coding "return all tables", and counts filters too —
a duplicate filter was the visible symptom the selector is supposed to prevent.

The fake also keeps ``className`` and ``classList`` genuinely in sync. In the
first revision ``scrollWrap.className = 'markdown-table-scroll'`` did not update
the separate ``classList._classes`` array, so ``classList.contains(...)`` on the
second run returned false and re-wrapped the table *inside the first wrapper* —
invisible to a test that only inspects the wrapper's original parent.
"""

import json
import shutil
import subprocess
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


# The fake DOM. Kept as a JS source string so every test builds the same
# environment, and so the selector evaluation can be inspected in one place.
_FAKE_DOM = r"""
// ── Minimal selector engine (tag, .class, :not([attr]), descendant) ──────────
function matchesCompound(el, part) {
  if (!el || !el.tag) return false;
  var m;
  var tagMatch = part.match(/^[a-zA-Z][a-zA-Z0-9]*/);
  if (tagMatch && String(el.tag).toLowerCase() !== tagMatch[0].toLowerCase()) return false;
  var classes = [];
  var classRe = /\.([A-Za-z0-9_-]+)/g;
  while ((m = classRe.exec(part))) classes.push(m[1]);
  for (var i = 0; i < classes.length; i++) {
    if (!el._classes.includes(classes[i])) return false;
  }
  var notRe = /:not\(\[([^\]]+)\]\)/g;
  while ((m = notRe.exec(part))) {
    if (el.getAttribute(m[1]) !== null) return false;
  }
  var attrRe = /\[([^\]]+)\]/g;
  while ((m = attrRe.exec(part))) {
    if (part.indexOf(":not([" + m[1] + "])") >= 0) continue;
    if (el.getAttribute(m[1]) === null) return false;
  }
  return true;
}

function querySelectorAllIn(rootNode, sel) {
  var parts = String(sel).trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return [];
  var last = parts[parts.length - 1];
  var ancestors = parts.slice(0, -1);
  var results = [];
  (function walk(node) {
    if (matchesCompound(node, last)) {
      var ok = true;
      for (var a = 0; a < ancestors.length; a++) {
        var found = false;
        var p = node.parentElement;
        while (p) {
          if (matchesCompound(p, ancestors[a])) { found = true; break; }
          p = p.parentElement;
        }
        if (!found) { ok = false; break; }
      }
      if (ok) results.push(node);
    }
    (node._children || []).forEach(walk);
  })(rootNode);
  return results;
}

// ── Fake element ────────────────────────────────────────────────────────────
function fakeEl(tag, attrs) {
  attrs = attrs || {};
  var el = {
    tag: tag,
    _classes: [],
    _children: [],
    parentElement: null,
    style: {},
    innerHTML: "",
    textContent: "",
    hidden: false,
    dataset: {},
    rows: [],
    attrs: {},
    children: [],
    // className and classList are two views of the same list, like the real DOM.
    classList: {
      add: function (c) { if (!el._classes.includes(c)) el._classes.push(c); },
      remove: function (c) { el._classes = el._classes.filter(function (x) { return x !== c; }); },
      toggle: function (c, on) {
        var want = (on === undefined) ? !el._classes.includes(c) : !!on;
        if (want) this.add(c); else this.remove(c);
      },
      contains: function (c) { return el._classes.includes(c); },
    },
    querySelectorAll: function (sel) { return querySelectorAllIn(this, sel); },
    matches: function (sel) { return matchesCompound(this, sel); },
    querySelector: function (sel) {
      var found = querySelectorAllIn(this, sel);
      if (found.length) return found[0];
      if (sel === "tr") {
        for (var i = 0; i < this._children.length; i++) {
          var c = this._children[i];
          if (c.tag === "tr") return c;
          for (var j = 0; j < (c._children || []).length; j++) {
            if (c._children[j].tag === "tr") return c._children[j];
          }
        }
      }
      return null;
    },
    closest: function (sel) {
      var p = this;
      while (p) {
        if (matchesCompound(p, sel)) return p;
        p = p.parentElement;
      }
      return null;
    },
    appendChild: function (child) {
      if (child.parentElement && child.parentElement._children) {
        var at = child.parentElement._children.indexOf(child);
        if (at >= 0) child.parentElement._children.splice(at, 1);
      }
      child.parentElement = this;
      this._children.push(child);
      this.children = this._children;
    },
    insertBefore: function (newEl, refEl) {
      if (newEl.parentElement && newEl.parentElement._children) {
        var at0 = newEl.parentElement._children.indexOf(newEl);
        if (at0 >= 0) newEl.parentElement._children.splice(at0, 1);
      }
      newEl.parentElement = this;
      var idx = this._children.indexOf(refEl);
      if (idx >= 0) this._children.splice(idx, 0, newEl);
      else this._children.push(newEl);
      this.children = this._children;
    },
    setAttribute: function (k, v) { this.attrs[k] = v; },
    getAttribute: function (k) {
      return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
    },
    hasAttribute: function (k) { return this.getAttribute(k) !== null; },
    addEventListener: function () {},
    removeEventListener: function () {},
    setAttributeNS: function () {},
  };
  Object.defineProperty(el, "className", {
    get: function () { return el._classes.join(" "); },
    set: function (v) { el._classes = String(v || "").split(/\s+/).filter(Boolean); },
    enumerable: true,
  });
  if (attrs.className) el.className = attrs.className;
  return el;
}

var document = { createElement: function (tag) { return fakeEl(tag); } };
function t(k) { return k; }
"""

# Builds root > .msg-body > table (+ optionally an outside-msg-body table),
# records the pre-run state, runs enhanceMarkdownTables N times, then reports.
_HARNESS_TEMPLATE = r"""
function buildTable(rows, cols, prefix) {
  var table = fakeEl("table");
  var thead = fakeEl("thead");
  var hrow = fakeEl("tr");
  for (var ci = 0; ci < cols; ci++) {
    var th = fakeEl("th");
    th.textContent = prefix + "H" + ci;
    hrow.appendChild(th);
  }
  thead.appendChild(hrow);
  thead.rows = [hrow];
  table.appendChild(thead);
  var tbody = fakeEl("tbody");
  for (var ri = 0; ri < rows; ri++) {
    var row = fakeEl("tr");
    for (var cj = 0; cj < cols; cj++) {
      var td = fakeEl("td");
      td.textContent = prefix + "cell" + ri + "_" + cj;
      row.appendChild(td);
    }
    tbody.appendChild(row);
  }
  tbody.rows = tbody._children;
  table.appendChild(tbody);
  table.tHead = thead;
  table.tBodies = [tbody];
  table.rows = [hrow].concat(tbody._children);
  return table;
}

var root = fakeEl("div");
var msgBody = fakeEl("div", { className: "msg-body" });
root.appendChild(msgBody);

var table = buildTable(6, 5, "in");
msgBody.appendChild(table);

// A table deliberately OUTSIDE .msg-body: the production selector requires a
// .msg-body ancestor, so this one must never be touched.
var outsideTable = buildTable(6, 5, "out");
root.appendChild(outsideTable);

// A table that WRAPS BUT FAILS TO ENHANCE: it has no body rows, so
// enhanceMarkdownTables() wraps it and then returns at the
// `if(!headerRow||!bodyRows.length) return;` guard — before the
// `data-markdown-table-enhanced` flag is set. The selector therefore still
// matches it on the next run. This is the only path where the
// `.markdown-table-scroll` parent guard is load-bearing: without it, each run
// nests another wrapper around the same table.
var emptyTable = buildTable(0, 3, "empty");
msgBody.appendChild(emptyTable);

function wrapperDepthOf(el) {
  var depth = 0;
  var p = el.parentElement;
  while (p) {
    if (p._classes && p._classes.includes("markdown-table-scroll")) depth++;
    p = p.parentElement;
  }
  return depth;
}

var runs = __RUNS__;
var snapshots = [];
for (var r = 0; r < runs; r++) {
  enhanceMarkdownTables(root);
  // Count wrappers per table rather than a single total: there are two
  // in-scope tables in this tree (the rich one and the wrap-then-fail one), so
  // a bare total would be 2 and would not say anything about double-wrapping.
  var wrapsHoldingMain = 0;
  var wrapsHoldingEmpty = 0;
  msgBody._children.forEach(function (c) {
    if (!c._classes.includes("markdown-table-scroll")) return;
    if (c._children.indexOf(table) >= 0) wrapsHoldingMain++;
    if (c._children.indexOf(emptyTable) >= 0) wrapsHoldingEmpty++;
  });
  snapshots.push({
    wrapsHoldingMain: wrapsHoldingMain,
    wrapsHoldingEmpty: wrapsHoldingEmpty,
    wrapCountInMsgBody: msgBody._children.filter(function (c) {
      return c._classes.includes("markdown-table-scroll");
    }).length,
    filterCountInMsgBody: msgBody._children.filter(function (c) {
      return c.tag === "input";
    }).length,
    filterCountAnywhere: querySelectorAllIn(root, "input").length,
    tableParentClass: table.parentElement ? table.parentElement.className : "none",
    enhancedFlag: table.getAttribute("data-markdown-table-enhanced"),
    outsideTableParentIsRoot: outsideTable.parentElement === root,
    outsideTableEnhanced: outsideTable.getAttribute("data-markdown-table-enhanced"),
    emptyTableWrapperDepth: wrapperDepthOf(emptyTable),
    emptyTableEnhanced: emptyTable.getAttribute("data-markdown-table-enhanced"),
  });
}

// After the final run, what does the production selector still hand out?
var remaining = querySelectorAllIn(root, ".msg-body table:not([data-markdown-table-enhanced])");

var wrapEl = null;
for (var i = 0; i < msgBody._children.length; i++) {
  if (msgBody._children[i]._classes.includes("markdown-table-scroll")) { wrapEl = msgBody._children[i]; break; }
}
var filterEl = null;
for (var j = 0; j < msgBody._children.length; j++) {
  if (msgBody._children[j].tag === "input") { filterEl = msgBody._children[j]; break; }
}

console.log("W8_RESULT " + JSON.stringify({
  runs: runs,
  snapshots: snapshots,
  finalRemainingSelectable: remaining.length,
  wrapExists: wrapEl !== null,
  wrapClassName: wrapEl ? wrapEl.className : null,
  tableIsInsideWrap: wrapEl ? wrapEl._children.indexOf(table) >= 0 : false,
  tableParentClassName: table.parentElement ? table.parentElement.className : "none",
  filterExists: filterEl !== null,
  filterIsOutsideWrap: filterEl && wrapEl ? msgBody._children.indexOf(filterEl) < msgBody._children.indexOf(wrapEl) : false,
  wrapIsChildOfMsgBody: wrapEl ? msgBody._children.indexOf(wrapEl) >= 0 : false,
}));
"""


def _harness(runs: int = 1) -> str:
    """Node script: faithful fake DOM + real enhanceMarkdownTables, run N times."""
    return _FAKE_DOM + "\n" + _extract_enhance_markdown_tables() + "\n" + _HARNESS_TEMPLATE.replace(
        "__RUNS__", str(runs)
    )


def _run(runs: int = 1) -> dict:
    proc = _run_node(_harness(runs))
    assert proc.returncode == 0, proc.stderr
    assert "W8_RESULT" in proc.stdout, proc.stdout
    payload = proc.stdout.split("W8_RESULT ", 1)[1].strip()
    return json.loads(payload)


# ── The wrapper is built correctly ───────────────────────────────────────────


def test_wide_table_gets_scroll_wrapper():
    """A markdown table must be wrapped in a .markdown-table-scroll div."""
    data = _run()
    assert data["wrapExists"] is True, data
    assert data["wrapClassName"] == "markdown-table-scroll", data
    assert data["tableIsInsideWrap"] is True, data


def test_table_reparented_into_scroll_wrapper():
    """After wrapping, the table's direct parent must be the scroll div."""
    data = _run()
    assert data["tableParentClassName"] == "markdown-table-scroll", data
    assert data["wrapIsChildOfMsgBody"] is True, data


def test_filter_placed_before_scroll_wrapper():
    """The filter input (4+ rows) must be a sibling before the scroll wrapper."""
    data = _run()
    assert data["filterExists"] is True, data
    assert data["filterIsOutsideWrap"] is True, data


# ── Idempotency is enforced by the PRODUCTION selector ───────────────────────


def test_idempotent_no_double_wrap():
    """Calling enhanceMarkdownTables twice must not double-wrap."""
    data = _run(runs=2)
    assert data["snapshots"][0]["wrapsHoldingMain"] == 1, data
    assert data["snapshots"][1]["wrapsHoldingMain"] == 1, (
        f"the second run wrapped the table again: {data['snapshots']}"
    )


def test_idempotent_no_duplicate_filter():
    """The second run must not add a second filter.

    This is the assertion the first revision could not make: its fake selector
    returned already-enhanced tables, so a duplicate filter was created while the
    test counted only wrappers and passed.
    """
    data = _run(runs=2)
    assert data["snapshots"][0]["filterCountInMsgBody"] == 1, data
    assert data["snapshots"][1]["filterCountInMsgBody"] == 1, (
        f"a second enhanceMarkdownTables() run added another filter: {data['snapshots']}"
    )
    assert data["snapshots"][1]["filterCountAnywhere"] == 1, (
        f"filter leaked somewhere else in the tree: {data['snapshots']}"
    )


def test_second_run_does_not_reprocess_an_enhanced_table():
    """The production selector must stop handing out an already-enhanced table.

    ``:not([data-markdown-table-enhanced])`` is the real idempotency mechanism;
    if the selector stops excluding it, every run reprocesses the table.

    Exactly one table remains selectable after a run: the unfillable one, which
    takes the wrap-then-fail path before the flag is written (covered by
    test_wrap_fail_path_does_not_nest_wrappers). The main table must NOT be in
    that set.
    """
    data = _run(runs=2)
    assert data["snapshots"][0]["enhancedFlag"] == "1", data
    assert data["snapshots"][1]["enhancedFlag"] == "1", data
    assert data["finalRemainingSelectable"] == 1, (
        f"expected only the wrap-then-fail table to remain selectable, got "
        f"{data['finalRemainingSelectable']} — the enhanced table is being "
        f"reprocessed, so idempotency is gone: {data}"
    )
    # And it is specifically the unfillable one, not the enhanced main table.
    assert data["snapshots"][1]["emptyTableEnhanced"] is None, data
    assert data["snapshots"][1]["wrapsHoldingMain"] == 1, data


def test_repeated_runs_stay_stable():
    """Five runs must leave exactly one wrapper and one filter."""
    data = _run(runs=5)
    for snap in data["snapshots"]:
        assert snap["wrapsHoldingMain"] == 1, data["snapshots"]
        assert snap["wrapsHoldingEmpty"] == 1, data["snapshots"]
        assert snap["filterCountInMsgBody"] == 1, data["snapshots"]


# ── The .msg-body scope in the selector is honoured ──────────────────────────


def test_table_outside_msg_body_is_not_enhanced():
    """The selector requires a .msg-body ancestor — other tables must be left alone."""
    data = _run()
    assert data["snapshots"][0]["outsideTableParentIsRoot"] is True, (
        "a table outside .msg-body was re-parented into a scroll wrapper"
    )
    assert data["snapshots"][0]["outsideTableEnhanced"] is None, (
        "a table outside .msg-body was marked as enhanced"
    )


def test_wrap_fail_path_does_not_nest_wrappers():
    """A table that wraps but then fails to enhance must not be wrapped again.

    The `data-markdown-table-enhanced` flag is only set AFTER the
    `!headerRow || !bodyRows.length` guard, so a table with no body rows stays
    selectable and is matched again on the next run. The
    `.markdown-table-scroll` parent check is what prevents a new wrapper being
    nested around the existing one each time — the one code path where that
    guard is load-bearing.
    """
    data = _run(runs=3)
    assert data["snapshots"][0]["emptyTableEnhanced"] is None, (
        "precondition changed: the unfillable table is now flagged as enhanced, "
        "so this test no longer exercises the wrap-then-fail path"
    )
    depths = [s["emptyTableWrapperDepth"] for s in data["snapshots"]]
    assert depths == [1, 1, 1], (
        f"the wrap-then-fail table accumulated nested wrappers across runs: {depths}"
    )
