"""Regression tests for #4945: copy from rendered markdown tables."""

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

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  const braceStart = src.indexOf('{', start);
  let depth = 0;
  let inString = null;
  let escaped = false;
  let inLineComment = false;
  let inBlockComment = false;
  for (let i = braceStart; i < src.length; i++) {
    const ch = src[i];
    const next = i + 1 < src.length ? src[i + 1] : '';
    if (inLineComment) {
      if (ch === '\n') inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === '*' && next === '/') {
        inBlockComment = false;
        i++;
      }
      continue;
    }
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === '/' && next === '/') {
      inLineComment = true;
      i++;
      continue;
    }
    if (ch === '/' && next === '*') {
      inBlockComment = true;
      i++;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      inString = ch;
      continue;
    }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' brace scan failed');
}

const required = [
  '_markdownTableCopyHtmlEscape',
  '_markdownTableText',
  '_markdownTableCellText',
  '_sanitizeMarkdownTableCellText',
  '_findEnhancedMarkdownTable',
  '_findMarkdownTableCell',
  '_markdownTableNodeChildren',
  '_markdownTableNodeBoundaryLength',
  '_markdownTableBoundaryWithinCell',
  '_markdownTableEdgeCell',
  '_isFullEnhancedMarkdownTableSelection',
  '_findEnhancedMarkdownTableFromRange',
  '_markdownTableCopyPayloadForTable',
  '_handleMarkdownTableCopy',
];

for (const name of required) {
  eval(extractFunc(name));
}

function toNodeTypeElement(tagName) {
  return tagName ? 1 : 3;
}

function classSet(value) {
  return new Set((value || '').split(/\\s+/).filter(Boolean));
}

class FakeClassList {
  constructor(node) {
    this.node = node;
  }

  contains(name) {
    return classSet(this.node.className).has(name);
  }
}

class FakeText {
  constructor(text = '') {
    this.nodeType = 3;
    this.textContent = String(text);
  }
}

class FakeElement {
  constructor(tagName = '', {className = '', text = '', attrs = {}} = {}) {
    this.nodeType = toNodeTypeElement(tagName);
    this.tagName = tagName ? String(tagName).toUpperCase() : undefined;
    this.children = [];
    this.parentElement = null;
    this.parentNode = null;
    this.className = className;
    this._text = text;
    this.attrs = {...attrs};
    this.classList = new FakeClassList(this);
  }

  appendChild(child) {
    child.parentElement = this;
    child.parentNode = this;
    this.children.push(child);
    if (this.nodeType === 1 && this.tagName && this.tagName.toUpperCase() === 'TR') {
      if (!this.cells) this.cells = [];
      if (child.nodeType === 1 && (child.tagName === 'TD' || child.tagName === 'TH')) {
        this.cells.push(child);
      }
    }
    return child;
  }

  get textContent() {
    if (this._text) return this._text;
    return this.children.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this.children = [new FakeText(String(value))];
    this.children[0].parentElement = this;
    this.children[0].parentNode = this;
    this._text = '';
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name);
  }

  removeAttribute(name) {
    delete this.attrs[name];
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      node.children.forEach((child) => {
        if (child.nodeType === 1 && child.matches(selector)) {
          out.push(child);
        }
        if (child.children && child.children.length) {
          walk(child);
        }
      });
    };
    walk(this);
    return out;
  }

  matches(selector) {
    if (!selector || this.nodeType !== 1) return false;
    if (selector.includes(',')) {
      return selector
        .split(',')
        .map((part) => part.trim())
        .filter(Boolean)
        .some((part) => this.matches(part));
    }
    if (selector.startsWith('.')) {
      return this.classList.contains(selector.slice(1));
    }
    const dataMatch = selector.match(/^([a-zA-Z0-9-]+)\[([^=\]]+)(?:=['"]?([^'"]*)['"]?)?\]$/);
    if (dataMatch) {
      const tag = dataMatch[1].toUpperCase();
      const attr = dataMatch[2];
      const expected = dataMatch[3];
      if (this.tagName !== tag) return false;
      if (!Object.prototype.hasOwnProperty.call(this.attrs, attr)) return false;
      if (!expected) return true;
      return String(this.attrs[attr]) === expected;
    }
    return selector && this.tagName && this.tagName.toLowerCase() === selector.toLowerCase();
  }

  get rows() {
    return this._rows || [];
  }

  set rows(values) {
    this._rows = values || [];
  }
}

function makeElement(tagName, options = {}) {
  return new FakeElement(tagName, options);
}

function makeCell(tagName, text, withSortControls = false) {
  const cell = makeElement(tagName);
  if (withSortControls) {
    const button = makeElement('button', {className: 'markdown-table-sort'});
    const label = makeElement('span', {className: 'markdown-table-sort-label'});
    label.appendChild(new FakeText(text));
    const indicator = makeElement('span', {className: 'markdown-table-sort-indicator'});
    indicator.appendChild(new FakeText('↑'));
    button.appendChild(label);
    button.appendChild(indicator);
    cell.appendChild(button);
    return cell;
  }
  cell.appendChild(new FakeText(text));
  return cell;
}

function makeRow(cells) {
  const row = makeElement('tr');
  cells.forEach((cell) => row.appendChild(cell));
  return row;
}

function buildEnhancedTableFixture(includeFilter, options = {}) {
  const headerTexts = options.headerTexts || ['Product', 'Price'];
  const bodyTexts = options.bodyTexts || ['Widget', '12'];
  const root = makeElement('div');
  if (includeFilter) {
    const filter = makeElement('input', {className: 'markdown-table-filter'});
    filter.appendChild(new FakeText('Filter by text'));
    root.appendChild(filter);
  }

  const table = makeElement('table', {attrs: {'data-markdown-table-enhanced': '1'}});
  const header = makeRow([
    makeCell('th', headerTexts[0], true),
    makeCell('th', headerTexts[1], true),
  ]);
  const body = makeRow([
    makeCell('td', bodyTexts[0]),
    makeCell('td', bodyTexts[1]),
  ]);
  table.rows = [header, body];
  root.appendChild(table);
  return {root, table, header, body};
}

global.window = {
  getSelection() {
    return null;
  }
};

"""


def _run_js(driver_body: str):
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False) as handle:
        handle.write(_DRIVER)
        handle.write(driver_body)
        script = Path(handle.name)

    try:
        result = subprocess.run(
            [NODE, str(script), str(MESSAGES_JS)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
        )
    finally:
        script.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"node helper failed: {result.stderr}")
    return json.loads(result.stdout.strip())


def test_copy_payload_restores_header_row_and_plain_cells():
    out = _run_js(
        """
const {table} = buildEnhancedTableFixture(false, {
  headerTexts: ['Product <Name>', 'Price & Tax'],
  bodyTexts: ['Widget > Basic', '12 & change'],
});
const payload = _markdownTableCopyPayloadForTable(table);
console.log(JSON.stringify(payload));
"""
    )
    assert "<thead><tr><th>Product &lt;Name&gt;</th><th>Price &amp; Tax</th></tr></thead>" in out["html"]
    assert "<tbody><tr><td>Widget &gt; Basic</td><td>12 &amp; change</td></tr></tbody>" in out["html"]
    assert "markdown-table-sort" not in out["html"]
    assert out["plain"].startswith("Product <Name>\tPrice & Tax\n")
    assert "\nWidget > Basic\t12 & change" in out["plain"]


def test_partial_multi_cell_selection_leaves_native_copy_unmodified():
    out = _run_js(
        """
const {root, table, body} = buildEnhancedTableFixture(true);

const range = {
  startContainer: body.cells[0].children[0],
  startOffset: 0,
  endContainer: body.cells[1].children[0],
  endOffset: body.cells[1].children[0].textContent.length,
  commonAncestorContainer: table,
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}


def test_full_table_selection_exports_sanitized_table_payload():
    out = _run_js(
        """
const {table, header, body} = buildEnhancedTableFixture(true, {
  headerTexts: ['Product <Name>', 'Price & Tax'],
  bodyTexts: ['Widget > Basic', '12 & change'],
});

const range = {
  startContainer: header.cells[0].children[0].children[0],
  startOffset: 0,
  endContainer: body.cells[1].children[0],
  endOffset: body.cells[1].children[0].textContent.length,
  commonAncestorContainer: table,
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    data = out["data"]
    assert out["prevented"] is True
    assert "<table>" in data["text/html"]
    assert "<thead><tr><th>Product &lt;Name&gt;</th><th>Price &amp; Tax</th></tr></thead>" in data["text/html"]
    assert "<tbody><tr><td>Widget &gt; Basic</td><td>12 &amp; change</td></tr></tbody>" in data["text/html"]
    assert "markdown-table-filter" not in data["text/html"]
    assert "markdown-table-sort" not in data["text/html"]
    assert data["text/plain"].startswith("Product <Name>\tPrice & Tax")
    assert "\nWidget > Basic\t12 & change" in data["text/plain"]


def test_non_table_selection_leaves_native_copy_unmodified():
    out = _run_js(
        """
const plain = makeElement('p');
plain.appendChild(new FakeText('plain text'));

const range = {
  startContainer: plain.children[0],
  startOffset: 0,
  endContainer: plain.children[0],
  endOffset: plain.children[0].textContent.length,
  commonAncestorContainer: plain,
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}


def test_table_copy_without_clipboard_data_leaves_native_copy_unmodified():
    out = _run_js(
        """
const {table, header, body} = buildEnhancedTableFixture(true);

const range = {
  startContainer: header.cells[0].children[0].children[0],
  startOffset: 0,
  endContainer: body.cells[1].children[0],
  endOffset: body.cells[1].children[0].textContent.length,
  commonAncestorContainer: table,
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled }));
"""
    )
    assert out["prevented"] is False


def test_mixed_selection_that_crosses_table_leaves_native_copy_unmodified():
    out = _run_js(
        """
const {root, table, body} = buildEnhancedTableFixture(true);
const before = makeElement('p');
before.appendChild(new FakeText('before table'));
const after = makeElement('p');
after.appendChild(new FakeText('after table'));
root.children.unshift(before);
before.parentElement = root;
before.parentNode = root;
root.appendChild(after);

const range = {
  startContainer: before.children[0],
  startOffset: 0,
  endContainer: after.children[0],
  endOffset: after.children[0].textContent.length,
  commonAncestorContainer: root,
  intersectsNode(node) {
    return node === table;
  },
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}


def test_single_cell_subselection_leaves_native_copy_unmodified():
    out = _run_js(
        """
const {table, body} = buildEnhancedTableFixture(true);

const range = {
  startContainer: body.cells[0].children[0],
  startOffset: 0,
  endContainer: body.cells[0].children[0],
  endOffset: body.cells[0].children[0].textContent.length,
  commonAncestorContainer: body.cells[0],
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}


def test_table_element_anchored_selection_leaves_native_copy_unmodified():
    """#5013: a drag/selection anchored on the <table> element itself (rather than
    inside corner cells) must fall through to native copy — sanitization only fires
    for a true full-cell-span selection."""
    out = _run_js(
        """
const {table} = buildEnhancedTableFixture(true);

// Selection anchored on the <table> node itself (a real-browser whole-table drag
// can land start/end on the table/tr rather than inside the corner text nodes).
const range = {
  startContainer: table,
  startOffset: 0,
  endContainer: table,
  endOffset: table.rows.length,
  commonAncestorContainer: table,
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}


def test_cell_to_prose_selection_leaves_native_copy_unmodified():
    """#5013: a selection that starts inside a table cell and ends in surrounding
    prose must fall through to native copy (it is not a full-table selection, and
    capturing it would clobber the trailing prose)."""
    out = _run_js(
        """
const {root, table, body} = buildEnhancedTableFixture(true);
const after = makeElement('p');
after.appendChild(new FakeText('trailing prose'));
root.appendChild(after);

// Start inside a body cell's text, end in the trailing prose paragraph.
const range = {
  startContainer: body.cells[0].children[0],
  startOffset: 0,
  endContainer: after.children[0],
  endOffset: after.children[0].textContent.length,
  commonAncestorContainer: root,
  intersectsNode(node) {
    return node === table;
  },
};

window.getSelection = () => ({
  isCollapsed: false,
  rangeCount: 1,
  getRangeAt: () => range,
});

const clipboard = {
  data: {},
  setData(type, value) {
    this.data[type] = value;
  }
};

const event = {
  preventDefaultCalled: false,
  preventDefault() {
    this.preventDefaultCalled = true;
  },
  clipboardData: clipboard,
};

_handleMarkdownTableCopy(event);
console.log(JSON.stringify({ prevented: event.preventDefaultCalled, data: clipboard.data }));
"""
    )
    assert out["prevented"] is False
    assert out["data"] == {}
