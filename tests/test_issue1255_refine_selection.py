"""Runtime regression coverage for refine-from-selection (#1255)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_selected_context_user_render_runtime import _run_user_renderer


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = Path(os.environ.get("HERMES_ISSUE1255_MESSAGES_JS") or (ROOT / "static" / "messages.js"))
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _locale_blocks(src: str) -> dict[str, str]:
    matches = list(
        re.finditer(
            r"\n  (?:(['\"])([A-Za-z][A-Za-z0-9-]*)\1|([A-Za-z][A-Za-z0-9-]*)): \{",
            src,
        )
    )
    blocks: dict[str, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else src.rfind("\n};")
        blocks[match.group(2) or match.group(3)] = src[start:end]
    return blocks


def _instruction_values(src: str) -> list[str]:
    blocks = _locale_blocks(src)
    values: list[str] = []
    pattern = re.compile(r"^\s{4}selected_text_refine_instruction:\s*(['\"])(.*?)\1,", re.MULTILINE)
    for block in blocks.values():
        match = pattern.search(block)
        assert match, "selected_text_refine_instruction missing from locale block"
        values.append(match.group(2))
    return values


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
const scenarioArg = process.argv[3] || '';

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

function hasFunc(name) {
  return new RegExp('function\\s+' + name + '\\s*\\(').test(src);
}

const liveSelectionHelper = hasFunc('_consumeSelectedTextReplySelection')
  ? '_consumeSelectedTextReplySelection'
  : '_selectedTextReplyLiveText';

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

global.__log = [];
global.__replyCalls = [];
global.__sendCalls = 0;
global.__autoResizeCalls = 0;
global.__selectionClears = 0;

function pushLog(entry) {
  global.__log.push(entry);
}

function makeClassList(owner) {
  return {
    add(name) { owner._classes.add(name); },
    remove(name) { owner._classes.delete(name); },
    contains(name) { return owner._classes.has(name); },
  };
}

function makeElement(tagName) {
  let value = '';
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
    id: '',
    className: '',
    selectionRange: null,
    selectionStart: 0,
    selectionEnd: 0,
    focused: false,
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
      if (event.type === 'input') pushLog(`input:${el.id || el.tagName.toLowerCase()}`);
      for (const handler of el._listeners[event.type] || []) handler(event);
      return true;
    },
    focus() {
      el.focused = true;
      pushLog(`focus:${el.id || el.tagName.toLowerCase()}`);
    },
    setSelectionRange(start, end) {
      el.selectionRange = [start, end];
      el.selectionStart = start;
      el.selectionEnd = end;
      pushLog(`setSelectionRange:${el.id || el.tagName.toLowerCase()}:${start}:${end}`);
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
  Object.defineProperty(el, 'value', {
    enumerable: true,
    configurable: true,
    get() {
      return value;
    },
    set(next) {
      value = String(next);
      pushLog(`value:${el.id || el.tagName.toLowerCase()}:${value.length}`);
    },
  });
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
  global.__autoResizeCalls += 1;
  pushLog('autoResize');
};
global.applyLocaleToDOM = () => {};
global._addNamedContextBlock = (text) => {
  pushLog('addNamedContextBlock');
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
if (hasFunc('_appendComposerText')) eval(extractFunc('_appendComposerText'));
eval(extractFunc('insertSavedPromptIntoComposer'));
eval(extractFunc('_seedSelectedTextRefineDraft'));
eval(extractFunc(liveSelectionHelper));
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
      ...(hasFunc('_appendComposerText') ? [extractFunc('_appendComposerText')] : []),
      extractFunc('insertSavedPromptIntoComposer'),
      extractFunc('_seedSelectedTextRefineDraft'),
      extractFunc(liveSelectionHelper),
      extractFunc('_selectedTextReplyButton'),
      extractFunc('_hideSelectedTextReplyButton'),
      extractFunc('_positionSelectedTextReplyButton'),
      extractFunc('_updateSelectedTextReplyButton'),
    ].join('\n'),
  }));
  process.exit(0);
}

function resetSignals() {
  global.__log = [];
  global.__replyCalls = [];
  global.__sendCalls = 0;
  global.__autoResizeCalls = 0;
  global.__selectionClears = 0;
}

function buildShell() {
  _selectedTextReplyBtn = null;
  _selectedTextRefineBtn = null;
  _selectedTextReplyGroup = null;
  _selectedTextReplyText = '';
  _pendingSelections = [];
  _selectionIdCounter = 0;
  S = {pendingFiles: ['keep.bin']};
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
  const outside = makeElement('div');
  outside.id = 'outside';
  const outsideText = makeText('Outside');
  outsideText.parentElement = outside;
  outsideText.parentNode = outside;
  outside.children.push(outsideText);
  const composer = makeElement('textarea');
  composer.id = 'msg';
  body.appendChild(messages);
  body.appendChild(outside);
  body.appendChild(composer);
  return {messages, bubble, textNode, outside, outsideText, composer};
}

function selectionFor(startNode, endNode, text, rect, collapsed = false) {
  return {
    isCollapsed: collapsed,
    rangeCount: 1,
    getRangeAt() {
      return {
        startContainer: startNode,
        endContainer: endNode,
        getBoundingClientRect() {
          return rect;
        },
      };
    },
    toString() {
      return text;
    },
    removeAllRanges() {
      global.__selectionClears += 1;
      pushLog('removeAllRanges');
    },
  };
}

function runVisibilityScenario() {
  const {textNode} = buildShell();
  currentSelection = selectionFor(textNode, textNode, 'Alpha\nBeta', {left: 160, top: 120, width: 90, height: 18});
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
  currentSelection = selectionFor(textNode, textNode, 'Alpha\nBeta', {left: 160, top: 120, width: 90, height: 18}, true);
  _updateSelectedTextReplyButton();
  return {
    valid,
    invalidVisible: group.classList.contains('visible'),
  };
}

function runReplyScenario() {
  const {textNode, composer} = buildShell();
  composer.value = 'Existing draft';
  currentSelection = selectionFor(textNode, textNode, 'Quoted context', {left: 120, top: 100, width: 90, height: 18});
  const reply = _selectedTextReplyButton();
  _selectedTextReplyText = 'Quoted context';
  _selectedTextReplyGroup.classList.add('visible');
  resetSignals();
  reply.dispatchEvent({type: 'click', preventDefault() {}});
  return {
    replyCalls: global.__replyCalls,
    composerValue: composer.value,
    composerFocused: !!composer.focused,
    hidden: !_selectedTextReplyGroup.classList.contains('visible'),
    selectionClears: global.__selectionClears,
    sendCalls: global.__sendCalls,
    autoResizeCalls: global.__autoResizeCalls,
    pendingFiles: S.pendingFiles.slice(),
    log: global.__log.slice(),
  };
}

function runRefineScenario(instructionOverride) {
  const {textNode, composer} = buildShell();
  composer.value = 'Existing draft';
  if (instructionOverride) i18n.selected_text_refine_instruction = instructionOverride;
  currentSelection = selectionFor(textNode, textNode, 'Quoted context\nSecond line', {left: 120, top: 100, width: 120, height: 18});
  _selectedTextReplyButton();
  const refine = document.getElementById('selectedTextRefineBtn');
  _selectedTextReplyText = 'Quoted context\nSecond line';
  _selectedTextReplyGroup.classList.add('visible');
  resetSignals();
  refine.dispatchEvent({type: 'click', preventDefault() {}});
  return {
    composerValue: composer.value,
    selectionRange: composer.selectionRange,
    selectionStart: composer.selectionStart,
    selectionEnd: composer.selectionEnd,
    inputEvents: composer._events.filter((event) => event === 'input').length,
    focused: !!composer.focused,
    hidden: !_selectedTextReplyGroup.classList.contains('visible'),
    selectionClears: global.__selectionClears,
    autoResizeCalls: global.__autoResizeCalls,
    pendingFiles: S.pendingFiles.slice(),
    pendingSelections: _pendingSelections.slice(),
    sendCalls: global.__sendCalls,
    log: global.__log.slice(),
  };
}

function runInvalidMatrixScenario() {
  const cases = {};

  {
    const {textNode, composer} = buildShell();
    composer.value = 'Untouched draft';
    _selectedTextReplyButton();
    const refine = document.getElementById('selectedTextRefineBtn');
    _selectedTextReplyText = 'Stale text';
    _selectedTextReplyGroup.classList.add('visible');
    currentSelection = selectionFor(textNode, textNode, 'Quoted context', {left: 120, top: 100, width: 90, height: 18}, true);
    resetSignals();
    refine.dispatchEvent({type: 'click', preventDefault() {}});
    cases.collapsed = {
      composerValue: composer.value,
      hidden: !_selectedTextReplyGroup.classList.contains('visible'),
      selectionClears: global.__selectionClears,
      autoResizeCalls: global.__autoResizeCalls,
      sendCalls: global.__sendCalls,
      pendingSelections: _pendingSelections.slice(),
    };
  }

  {
    const {textNode, outsideText, composer} = buildShell();
    composer.value = 'Untouched draft';
    _selectedTextReplyButton();
    const refine = document.getElementById('selectedTextRefineBtn');
    _selectedTextReplyText = 'Stale text';
    _selectedTextReplyGroup.classList.add('visible');
    currentSelection = selectionFor(textNode, outsideText, 'Mixed context', {left: 120, top: 100, width: 90, height: 18});
    resetSignals();
    refine.dispatchEvent({type: 'click', preventDefault() {}});
    cases.outside = {
      composerValue: composer.value,
      hidden: !_selectedTextReplyGroup.classList.contains('visible'),
      selectionClears: global.__selectionClears,
      autoResizeCalls: global.__autoResizeCalls,
      sendCalls: global.__sendCalls,
      pendingSelections: _pendingSelections.slice(),
    };
  }

  {
    const {textNode, composer} = buildShell();
    composer.value = 'Untouched draft';
    _selectedTextReplyButton();
    const refine = document.getElementById('selectedTextRefineBtn');
    _selectedTextReplyText = 'Stale text';
    _selectedTextReplyGroup.classList.add('visible');
    currentSelection = selectionFor(textNode, textNode, 'Quoted context', {left: 120, top: 100, width: 0, height: 0});
    resetSignals();
    refine.dispatchEvent({type: 'click', preventDefault() {}});
    cases.zero_rect = {
      composerValue: composer.value,
      hidden: !_selectedTextReplyGroup.classList.contains('visible'),
      selectionClears: global.__selectionClears,
      autoResizeCalls: global.__autoResizeCalls,
      sendCalls: global.__sendCalls,
      pendingSelections: _pendingSelections.slice(),
    };
  }

  {
    const {composer} = buildShell();
    composer.value = 'Untouched draft';
    _selectedTextReplyButton();
    const refine = document.getElementById('selectedTextRefineBtn');
    _selectedTextReplyText = 'Stale text';
    _selectedTextReplyGroup.classList.add('visible');
    currentSelection = null;
    resetSignals();
    refine.dispatchEvent({type: 'click', preventDefault() {}});
    cases.stale = {
      composerValue: composer.value,
      hidden: !_selectedTextReplyGroup.classList.contains('visible'),
      selectionClears: global.__selectionClears,
      autoResizeCalls: global.__autoResizeCalls,
      sendCalls: global.__sendCalls,
      pendingSelections: _pendingSelections.slice(),
    };
  }

  return cases;
}

function runSavedPromptScenario() {
  const {composer} = buildShell();
  composer.value = 'Draft tail \n\t';
  resetSignals();
  insertSavedPromptIntoComposer('Saved prompt');
  return {
    composerValue: composer.value,
    selectionStart: composer.selectionStart,
    selectionEnd: composer.selectionEnd,
    inputEvents: composer._events.filter((event) => event === 'input').length,
    focused: !!composer.focused,
    autoResizeCalls: global.__autoResizeCalls,
    pendingFiles: S.pendingFiles.slice(),
    sendCalls: global.__sendCalls,
  };
}

const out =
  scenario === 'visibility' ? runVisibilityScenario() :
  scenario === 'reply' ? runReplyScenario() :
  scenario === 'refine' ? runRefineScenario('') :
  scenario === 'refine_with_instruction' ? runRefineScenario(scenarioArg) :
  scenario === 'invalid_matrix' ? runInvalidMatrixScenario() :
  scenario === 'saved_prompt' ? runSavedPromptScenario() :
  (() => { throw new Error('unknown scenario: ' + scenario); })();

process.stdout.write(JSON.stringify(out));
"""


def _run_js(scenario: str, arg: str = "") -> dict:
    completed = subprocess.run(
        [NODE, "-e", _DRIVER, str(MESSAGES_JS), scenario, arg],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _log_index(log: list[str], prefix: str) -> int:
    return next(idx for idx, entry in enumerate(log) if entry.startswith(prefix))


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
    assert out["composerFocused"] is False
    assert out["hidden"] is True
    assert out["selectionClears"] == 1
    assert out["sendCalls"] == 0
    assert out["autoResizeCalls"] == 0
    assert out["pendingFiles"] == ["keep.bin"]


def test_transcript_selection_is_consumed_before_composer_focus():
    reply = _run_js("reply")
    refine = _run_js("refine")

    assert _log_index(reply["log"], "removeAllRanges") < _log_index(reply["log"], "addNamedContextBlock")
    assert _log_index(refine["log"], "removeAllRanges") < _log_index(refine["log"], "value:msg:")
    assert _log_index(refine["log"], "removeAllRanges") < _log_index(refine["log"], "focus:msg")
    assert _log_index(refine["log"], "removeAllRanges") < _log_index(refine["log"], "setSelectionRange:msg:")


def test_refine_seeds_marker_free_draft_ending_in_localized_instruction_and_space_without_send():
    out = _run_js("refine")
    assert out["composerValue"] == (
        "Existing draft\n\n"
        "> Quoted context\n"
        "> Second line\n\n"
        "Refine instruction: "
    )
    assert "hermes-selected-context" not in out["composerValue"]
    assert out["selectionRange"] == [len(out["composerValue"]), len(out["composerValue"])]
    assert out["selectionStart"] == len(out["composerValue"])
    assert out["selectionEnd"] == len(out["composerValue"])
    assert out["inputEvents"] == 1
    assert out["focused"] is True
    assert out["hidden"] is True
    assert out["selectionClears"] == 1
    assert out["autoResizeCalls"] == 1
    assert out["pendingFiles"] == ["keep.bin"]
    assert out["pendingSelections"] == []
    assert out["sendCalls"] == 0


def test_refine_preserves_existing_draft_and_pending_files():
    out = _run_js("refine")
    assert out["composerValue"].startswith("Existing draft\n\n> Quoted context")
    assert out["pendingFiles"] == ["keep.bin"]
    assert out["sendCalls"] == 0


def test_invalid_selection_keeps_refine_inert():
    out = _run_js("invalid_matrix")
    for case in out.values():
        assert case["composerValue"] == "Untouched draft"
        assert case["hidden"] is True
        assert case["selectionClears"] == 0
        assert case["autoResizeCalls"] == 0
        assert case["sendCalls"] == 0
        assert case["pendingSelections"] == []


def test_refine_output_stays_marker_free_in_sent_user_rendering():
    out = _run_js("refine")
    rendered = _run_user_renderer(out["composerValue"])

    assert "hermes-selected-context" not in out["composerValue"]
    assert "hermes-selected-context" not in rendered
    assert 'class="sent-selection-context"' not in rendered
    assert "&lt;!-- hermes-selected-context --&gt;" not in rendered
    assert "&gt; Quoted context" in rendered
    assert "Refine instruction: " in rendered


def test_refine_draft_ends_with_localized_instruction_and_one_space_across_all_locales():
    values = _instruction_values(I18N)
    assert len(values) == 15
    for instruction in values:
        out = _run_js("refine_with_instruction", instruction)
        assert out["composerValue"].endswith(f"{instruction} ")


def test_saved_prompt_wrapper_preserves_existing_output_shape_and_effects():
    out = _run_js("saved_prompt")
    assert out["composerValue"] == "Draft tail\n\nSaved prompt\n\n"
    assert out["selectionStart"] == len(out["composerValue"])
    assert out["selectionEnd"] == len(out["composerValue"])
    assert out["inputEvents"] == 1
    assert out["focused"] is True
    assert out["autoResizeCalls"] == 1
    assert out["pendingFiles"] == ["keep.bin"]
    assert out["sendCalls"] == 0


def test_refine_click_transfers_native_caret_after_consuming_selection():
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
                "let S = {pendingFiles:['keep.bin']};"
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
            window.applyLocaleToDOM = () => {};
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
        immediate = page.evaluate(
            """
            () => {
              const msg = document.getElementById('msg');
              return {
                draft: msg.value,
                activeElement: document.activeElement && document.activeElement.id,
                selectionStart: msg.selectionStart,
                selectionEnd: msg.selectionEnd,
                valueLength: msg.value.length,
                selection: window.getSelection().toString(),
              };
            }
            """
        )
        next_frame = page.evaluate(
            """
            () => new Promise(resolve => {
              requestAnimationFrame(() => {
                const msg = document.getElementById('msg');
                resolve({
                  selectionStart: msg.selectionStart,
                  selectionEnd: msg.selectionEnd,
                  valueLength: msg.value.length,
                  selection: window.getSelection().toString(),
                });
              });
            })
            """
        )
        browser.close()

    assert selection_after_mousedown == "Alpha Beta"
    assert immediate == {
        "draft": "> Alpha Beta\n\nRefine instruction: ",
        "activeElement": "msg",
        "selectionStart": len("> Alpha Beta\n\nRefine instruction: "),
        "selectionEnd": len("> Alpha Beta\n\nRefine instruction: "),
        "valueLength": len("> Alpha Beta\n\nRefine instruction: "),
        "selection": "",
    }
    assert next_frame == {
        "selectionStart": len("> Alpha Beta\n\nRefine instruction: "),
        "selectionEnd": len("> Alpha Beta\n\nRefine instruction: "),
        "valueLength": len("> Alpha Beta\n\nRefine instruction: "),
        "selection": "",
    }
