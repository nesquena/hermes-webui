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
  let i = src.indexOf('{', start);
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const required = [
  '_markdownTableCopyHtmlEscape',
  '_markdownTableText',
  '_markdownTableCellText',
  '_sanitizeMarkdownTableCellText',
  '_findEnhancedMarkdownTable',
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

function buildEnhancedTableFixture(includeFilter) {
  const root = makeElement('div');
  if (includeFilter) {
    const filter = makeElement('input', {className: 'markdown-table-filter'});
    filter.appendChild(new FakeText('Filter by text'));
    root.appendChild(filter);
  }

  const table = makeElement('table', {attrs: {'data-markdown-table-enhanced': '1'}});
  const header = makeRow([
    makeCell('th', 'Product', true),
    makeCell('th', 'Price', true),
  ]);
  const body = makeRow([
    makeCell('td', 'Widget'),
    makeCell('td', '12'),
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
const {root, table} = buildEnhancedTableFixture(false);
const payload = _markdownTableCopyPayloadForTable(table);
console.log(JSON.stringify(payload));
"""
    )
    assert "<th>Product</th>" in out["html"]
    assert "<th>Price</th>" in out["html"]
    assert "markdown-table-sort" not in out["html"]
    assert out["plain"].startswith("Product\tPrice\n")
    assert "\nWidget\t12" in out["plain"]


def test_copy_payload_strips_sort_controls_and_filter_ui():
    out = _run_js(
        """
const {root, table, body} = buildEnhancedTableFixture(true);

const range = {
  startContainer: body.cells[0].children[0],
  endContainer: body.cells[1].children[0],
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
    assert "markdown-table-filter" not in data["text/html"]
    assert "markdown-table-sort" not in data["text/html"]
    assert out["data"]["text/plain"].startswith("Product\tPrice")
    assert "Widget" in out["data"]["text/plain"]


def test_non_table_selection_leaves_native_copy_unmodified():
    out = _run_js(
        """
const plain = makeElement('p');
plain.appendChild(new FakeText('plain text'));

const range = {
  startContainer: plain.children[0],
  endContainer: plain.children[0],
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
