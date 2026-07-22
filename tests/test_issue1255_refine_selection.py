"""Runtime regression coverage for refine-from-selection (#1255)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_selected_context_user_render_runtime import _run_user_renderer


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];

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

let _selectedTextReplyBtn = null;
let _selectedTextRefineBtn = null;
let _selectedTextReplyGroup = null;
let _selectedTextReplyText = '';
let _selectedTextReplyRaf = 0;
let _pendingSelections = [];
let _selectionIdCounter = 0;
let S = {pendingFiles: ['keep.bin']};

const i18n = {
  selected_text_reply: 'Reply with selection',
  selected_text_reply_title: 'Append selected chat text as quoted context',
  selected_text_refine: 'Refine',
  selected_text_refine_title: 'Start an editable refinement draft from the selection',
  selected_text_refine_instruction: 'Refine instruction:',
};

function makeClassList(owner) {
  return {
    add(name) { owner._classes.add(name); },
    remove(name) { owner._classes.delete(name); },
    contains(name) { return owner._classes.has(name); },
  };
}

function makeElement(tagName) {
  const el = {
    nodeType: 1,
    tagName: String(tagName || 'div').toUpperCase(),
    children: [],
    parentElement: null,
    parentNode: null,
    style: {},
    _classes: new Set(),
    _attrs: {},
    _listeners: {},
    _events: [],
    textContent: '',
    title: '',
    value: '',
    id: '',
    className: '',
    selectionRange: null,
    classList: null,
    appendChild(child) {
      child.parentElement = el;
      child.parentNode = el;
      el.children.push(child);
      return child;
    },
    setAttribute(name, value) {
      el._attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el._attrs, name) ? el._attrs[name] : null;
    },
    addEventListener(type, handler) {
      (el._listeners[type] ||= []).push(handler);
    },
    dispatchEvent(event) {
      event.target = event.target || el;
      event.preventDefault ||= (() => {});
      el._events.push(event.type);
      for (const handler of el._listeners[event.type] || []) handler(event);
      return true;
    },
    focus() {
      el.focused = true;
    },
    setSelectionRange(start, end) {
      el.selectionRange = [start, end];
    },
    getBoundingClientRect() {
      return el._rect || {left: 0, top: 0, width: 180, height: 36};
    },
    contains(node) {
      let cur = node;
      while (cur) {
        if (cur === el) return true;
        cur = cur.parentElement || cur.parentNode || null;
      }
      return false;
    },
    closest(selector) {
      if (!selector.startsWith('.')) return null;
      const want = selector.slice(1);
      let cur = el;
      while (cur) {
        if (cur._classes && cur._classes.has(want)) return cur;
        cur = cur.parentElement || null;
      }
      return null;
    },
  };
  el.classList = makeClassList(el);
  return el;
}

function makeText(text) {
  return {nodeType: 3, textContent: String(text), parentElement: null, parentNode: null};
}

const body = makeElement('body');
const docListeners = {};

function findById(node, id) {
  if (!node) return null;
  if (node.id === id) return node;
  for (const child of node.children || []) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

global.Node = {ELEMENT_NODE: 1};
global.Event = function Event(type, opts) {
  this.type = type;
  Object.assign(this, opts || {});
};
global.document = {
  body,
  createElement: makeElement,
  getElementById(id) {
    return findById(body, id);
  },
  addEventListener(type, handler) {
    (docListeners[type] ||= []).push(handler);
  },
  querySelectorAll() {
    return [];
  },
};
let currentSelection = null;
global.window = {
  innerWidth: 1024,
  getSelection() {
    return currentSelection;
  },
  requestAnimationFrame(cb) {
    cb();
    return 0;
  },
};
global.$ = (id) => document.getElementById(id);
global.t = (key) => i18n[key];
global.autoResize = () => {
  global.__autoResizeCalls = (global.__autoResizeCalls || 0) + 1;
};
global.applyLocaleToDOM = () => {};
global.__replyCalls = [];
global.__sendCalls = 0;
global._addNamedContextBlock = (text) => {
  global.__replyCalls.push(text);
};
global.send = () => {
  global.__sendCalls += 1;
};

eval(extractFunc('_selectedTextReplyT'));
eval(extractFunc('_selectedTextReplyRoot'));
eval(extractFunc('_selectedTextReplyNodeInChat'));
eval(extractFunc('_selectedTextReplySelection'));
eval(extractFunc('_formatSelectedTextReplyQuote'));
eval(extractFunc('_seedSelectedTextRefineDraft'));
eval(extractFunc('_selectedTextReplyLiveText'));
eval(extractFunc('_selectedTextReplyButton'));
eval(extractFunc('_hideSelectedTextReplyButton'));
eval(extractFunc('_positionSelectedTextReplyButton'));
eval(extractFunc('_updateSelectedTextReplyButton'));

if (scenario === 'browser_fixture') {
  process.stdout.write(JSON.stringify({
    helpers: [
      extractFunc('_selectedTextReplyT'),
      extractFunc('_selectedTextReplyRoot'),
      extractFunc('_selectedTextReplyNodeInChat'),
      extractFunc('_selectedTextReplySelection'),
      extractFunc('_formatSelectedTextReplyQuote'),
      extractFunc('_seedSelectedTextRefineDraft'),
      extractFunc('_selectedTextReplyLiveText'),
      extractFunc('_selectedTextReplyButton'),
      extractFunc('_hideSelectedTextReplyButton'),
      extractFunc('_positionSelectedTextReplyButton'),
      extractFunc('_updateSelectedTextReplyButton'),
    ].join('\n'),
  }));
  process.exit(0);
}

function buildShell() {
  body.children = [];
  const messages = makeElement('div');
  messages.id = 'messages';
  const bubble = makeElement('div');
  bubble.className = 'msg-body';
  messages.appendChild(bubble);
  const textNode = makeText('Alpha\nBeta');
  textNode.parentElement = bubble;
  textNode.parentNode = bubble;
  bubble.children.push(textNode);
  const composer = makeElement('textarea');
  composer.id = 'msg';
  body.appendChild(messages);
  body.appendChild(composer);
  return {messages, bubble, textNode, composer};
}

function selectionFor(textNode, text, rect, collapsed = false) {
  return {
    isCollapsed: collapsed,
    rangeCount: 1,
    getRangeAt() {
      return {
        startContainer: textNode,
        endContainer: textNode,
        getBoundingClientRect() {
          return rect;
        },
      };
    },
    toString() {
      return text;
    },
    removeAllRanges() {
      global.__selectionClears = (global.__selectionClears || 0) + 1;
    },
  };
}

function runVisibilityScenario() {
  const {textNode} = buildShell();
  currentSelection = selectionFor(textNode, 'Alpha\nBeta', {left: 160, top: 120, width: 90, height: 18});
  _updateSelectedTextReplyButton();
  const group = document.getElementById('selectedTextActionGroup');
  const reply = document.getElementById('selectedTextReplyBtn');
  const refine = document.getElementById('selectedTextRefineBtn');
  const valid = {
    visible: group.classList.contains('visible'),
    replyText: reply.textContent,
    refineText: refine.textContent,
    selectedText: _selectedTextReplyText,
    left: group.style.left,
    top: group.style.top,
  };
  currentSelection = selectionFor(textNode, 'Alpha\nBeta', {left: 160, top: 120, width: 90, height: 18}, true);
  _updateSelectedTextReplyButton();
  return {
    valid,
    invalidVisible: group.classList.contains('visible'),
  };
}

function runReplyScenario() {
  const {textNode, composer} = buildShell();
  composer.value = 'Existing draft';
  currentSelection = selectionFor(textNode, 'Quoted context', {left: 120, top: 100, width: 90, height: 18});
  const reply = _selectedTextReplyButton();
  _selectedTextReplyText = 'Quoted context';
  _selectedTextReplyGroup.classList.add('visible');
  reply.dispatchEvent({type: 'click', preventDefault() {}});
  return {
    replyCalls: global.__replyCalls,
    composerValue: composer.value,
    hidden: !_selectedTextReplyGroup.classList.contains('visible'),
    selectionClears: global.__selectionClears || 0,
    sendCalls: global.__sendCalls,
    refineCalls: global.__autoResizeCalls || 0,
  };
}

function runRefineScenario() {
  const {textNode, composer} = buildShell();
  composer.value = 'Existing draft';
  currentSelection = selectionFor(textNode, 'Quoted context\nSecond line', {left: 120, top: 100, width: 120, height: 18});
  const reply = _selectedTextReplyButton();
  const refine = document.getElementById('selectedTextRefineBtn');
  _selectedTextReplyText = 'Quoted context\nSecond line';
  _selectedTextReplyGroup.classList.add('visible');
  refine.dispatchEvent({type: 'click', preventDefault() {}});
  return {
    replyText: reply.textContent,
    refineText: refine.textContent,
    composerValue: composer.value,
    selectionRange: composer.selectionRange,
    inputEvents: composer._events.filter((event) => event === 'input').length,
    focused: !!composer.focused,
    hidden: !_selectedTextReplyGroup.classList.contains('visible'),
    selectionClears: global.__selectionClears || 0,
    autoResizeCalls: global.__autoResizeCalls || 0,
    pendingFiles: S.pendingFiles.slice(),
    pendingSelections: _pendingSelections.slice(),
    sendCalls: global.__sendCalls,
  };
}

function runInvalidRefineScenario() {
  const {textNode, composer} = buildShell();
  currentSelection = selectionFor(textNode, 'Quoted context', {left: 120, top: 100, width: 90, height: 18}, true);
  _selectedTextReplyButton();
  const refine = document.getElementById('selectedTextRefineBtn');
  _selectedTextReplyText = 'Stale text';
  _selectedTextReplyGroup.classList.add('visible');
  refine.dispatchEvent({type: 'click', preventDefault() {}});
  return {
    composerValue: composer.value,
    hidden: !_selectedTextReplyGroup.classList.contains('visible'),
    selectionClears: global.__selectionClears || 0,
    autoResizeCalls: global.__autoResizeCalls || 0,
    sendCalls: global.__sendCalls,
  };
}

const out =
  scenario === 'visibility' ? runVisibilityScenario() :
  scenario === 'reply' ? runReplyScenario() :
  scenario === 'refine' ? runRefineScenario() :
  scenario === 'invalid_refine' ? runInvalidRefineScenario() :
  (() => { throw new Error('unknown scenario: ' + scenario); })();

process.stdout.write(JSON.stringify(out));
"""


def _run_js(scenario: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", _DRIVER, str(MESSAGES_JS), scenario],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_valid_in_chat_selection_exposes_reply_and_refine_actions():
    out = _run_js("visibility")
    assert out["valid"]["visible"] is True
    assert out["valid"]["replyText"] == "Reply with selection"
    assert out["valid"]["refineText"] == "Refine"
    assert out["valid"]["selectedText"] == "Alpha\nBeta"
    assert out["valid"]["left"].endswith("px")
    assert out["valid"]["top"].endswith("px")
    assert out["invalidVisible"] is False


def test_reply_action_still_creates_one_named_context_block_without_touching_composer():
    out = _run_js("reply")
    assert out["replyCalls"] == ["Quoted context"]
    assert out["composerValue"] == "Existing draft"
    assert out["hidden"] is True
    assert out["selectionClears"] == 1
    assert out["sendCalls"] == 0
    assert out["refineCalls"] == 0


def test_refine_seeds_editable_draft_without_send():
    out = _run_js("refine")
    assert out["replyText"] == "Reply with selection"
    assert out["refineText"] == "Refine"
    assert out["composerValue"] == (
        "Existing draft\n\n"
        "> Quoted context\n"
        "> Second line\n\n"
        "Refine instruction:"
    )
    assert "hermes-selected-context" not in out["composerValue"]
    assert out["selectionRange"] == [len(out["composerValue"]), len(out["composerValue"])]
    assert out["inputEvents"] == 1
    assert out["focused"] is True
    assert out["hidden"] is True
    assert out["selectionClears"] == 1
    assert out["autoResizeCalls"] == 1
    assert out["pendingFiles"] == ["keep.bin"]
    assert out["pendingSelections"] == []
    assert out["sendCalls"] == 0


def test_invalid_selection_keeps_refine_inert():
    out = _run_js("invalid_refine")
    assert out["composerValue"] == ""
    assert out["hidden"] is True
    assert out["selectionClears"] == 0
    assert out["autoResizeCalls"] == 0
    assert out["sendCalls"] == 0


def test_refine_output_stays_marker_free_in_sent_user_rendering():
    out = _run_js("refine")
    rendered = _run_user_renderer(out["composerValue"])

    assert "hermes-selected-context" not in out["composerValue"]
    assert "hermes-selected-context" not in rendered
    assert 'class="sent-selection-context"' not in rendered
    assert "&lt;!-- hermes-selected-context --&gt;" not in rendered
    assert "&gt; Quoted context" in rendered
    assert "Refine instruction:" in rendered


def test_refine_keys_exist_in_all_locale_blocks():
    assert I18N.count("selected_text_refine:") == 15
    assert I18N.count("selected_text_refine_title:") == 15
    assert I18N.count("selected_text_refine_instruction:") == 15


def test_selected_text_mousedown_preserves_selection_until_click_revalidation_in_browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the selected-text browser test")

    fixture = _run_js("browser_fixture")["helpers"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()
        page.set_content(
            "<!doctype html><html><body>"
            '<div id="messages"><p id="transcript">Alpha Beta</p></div>'
            '<textarea id="msg"></textarea>'
            "</body></html>"
        )
        page.add_script_tag(
            content=(
                "let _selectedTextReplyBtn = null;"
                "let _selectedTextRefineBtn = null;"
                "let _selectedTextReplyGroup = null;"
                "let _selectedTextReplyText = '';"
                "let _selectedTextReplyRaf = 0;"
                "let _pendingSelections = [];"
                "let _selectionIdCounter = 0;"
                + fixture
            )
        )
        page.evaluate(
            """
            window.$ = id => document.getElementById(id);
            window.t = key => ({
              selected_text_reply: 'Reply with selection',
              selected_text_reply_title: 'Reply',
              selected_text_refine: 'Refine',
              selected_text_refine_title: 'Refine',
              selected_text_refine_instruction: 'Refine instruction:',
            }[key] || key);
            window.autoResize = () => {};
            const range = document.createRange();
            const text = document.getElementById('transcript').firstChild;
            range.setStart(text, 0);
            range.setEnd(text, text.length);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            const group = _selectedTextReplyButton().parentElement;
            group.classList.add('visible');
            """
        )
        refine = page.locator("#selectedTextRefineBtn")
        refine.hover()
        page.mouse.down()
        selection_after_mousedown = page.evaluate("window.getSelection().toString()")
        page.mouse.up()
        result = page.evaluate(
            "({draft: document.getElementById('msg').value, selection: window.getSelection().toString()})"
        )
        browser.close()

    assert selection_after_mousedown == "Alpha Beta"
    assert result == {
        "draft": (
            "> Alpha Beta\n\n"
            "Refine instruction:"
        ),
        "selection": "",
    }
