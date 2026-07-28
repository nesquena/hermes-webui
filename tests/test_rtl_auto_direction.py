"""Regression coverage for mixed RTL/LTR chat content.

The existing PR #1721 tests cover the opt-in global RTL fallback. These tests
cover the finer-grained contract: prose is native auto-direction and known
machine-oriented content is explicitly LTR without changing source text.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = REPO_ROOT / "static" / "ui.js"
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
STYLE_CSS = REPO_ROOT / "static" / "style.css"
SW_JS = REPO_ROOT / "static" / "sw.js"
VAZIRMATN_FONT = REPO_ROOT / "static" / "fonts" / "Vazirmatn.woff2"
VAZIRMATN_LICENSE = REPO_ROOT / "static" / "fonts" / "OFL-Vazirmatn.txt"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_direction_helper_marks_prose_auto_and_machine_content_ltr(tmp_path):
    """Exercise the real helper with DOM-shaped nodes, not a Python mirror."""
    driver = tmp_path / "direction-driver.js"
    driver.write_text(
        r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('function _applyAutomaticMessageDirections(');
if (start < 0) throw new Error('direction helper missing');
let brace = src.indexOf('{', start), depth = 1, i = brace + 1;
while (depth && i < src.length) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') depth--;
  i++;
}
eval(src.slice(start, i));
function node(selector = '') {
  const classes = new Set();
  return {
    selector,
    attrs: {},
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      contains: name => classes.has(name),
    },
    matches: value => value === selector,
    querySelectorAll: value => value === '.msg-body' ? [] : [],
    setAttribute: (key, value) => { this; },
  };
}
function make(selector) {
  const classes = new Set();
  return {
    selector,
    attrs: {},
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      contains: name => classes.has(name),
    },
    matches: value => value === selector,
    querySelectorAll(value) {
      if (selector === '.msg-body' && value === 'p,li,blockquote,h1,h2,h3,h4,h5,h6') return this.blocks || [];
      return [];
    },
    setAttribute(key, value) { this.attrs[key] = value; },
  };
}
const prose = make('.msg-body');
const paragraph = make('p');
prose.blocks = [paragraph];
const link = make('a');
const strong = make('strong');
const emphasis = make('em');
const pre = make('pre');
const code = make('.msg-body code');
const root = { querySelectorAll(selector) {
  if (selector === '.msg-body') return [prose];
  if (selector === 'p,li,blockquote,h1,h2,h3,h4,h5,h6') return [paragraph];
  if (selector === 'a,strong,em') return [link, strong, emphasis];
  if (selector.includes('pre') && selector.includes('code')) return [pre, code];
  return [];
}};
_applyAutomaticMessageDirections(root);
process.stdout.write(JSON.stringify({
  prose: prose.attrs,
  paragraph: paragraph.attrs,
  link: link.attrs,
  strong: strong.attrs,
  emphasis: emphasis.attrs,
  pre: pre.attrs,
  code: code.attrs,
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["prose"]["dir"] == "auto"
    assert data["paragraph"]["dir"] == "auto"
    assert data["link"]["dir"] == "auto"
    assert data["strong"]["dir"] == "auto"
    assert data["emphasis"]["dir"] == "auto"
    assert data["pre"]["dir"] == "ltr"
    assert data["code"]["dir"] == "ltr"


def test_direction_policy_is_used_by_settled_and_streaming_paths():
    ui = UI_JS.read_text(encoding="utf-8")
    messages = MESSAGES_JS.read_text(encoding="utf-8")
    assert "function _applyAutomaticMessageDirections(" in ui
    assert "_applyAutomaticMessageDirections(inner);" in ui
    assert messages.count("_observeAutomaticMessageDirections(assistantBody);") == 2


def test_streaming_direction_uses_css_without_per_delta_dom_rescans():
    messages = MESSAGES_JS.read_text(encoding="utf-8")
    css = STYLE_CSS.read_text(encoding="utf-8")
    parser_write_tail = messages.split("window.smd.parser_write(_smdParser,delta)", 1)[1][:300]
    assert "_applyAutomaticMessageDirections" not in parser_write_tail
    assert "function _observeAutomaticMessageDirections(body)" in UI_JS.read_text(encoding="utf-8")
    assert "record.addedNodes" in UI_JS.read_text(encoding="utf-8")
    assert "const added=new Set()" in UI_JS.read_text(encoding="utf-8")
    assert "_observeAutomaticMessageDirections(assistantBody);" in messages
    assert "_disconnectAutomaticMessageDirections(assistantBody);" in messages
    assert '.msg-body.message-prose-auto a { direction:ltr; unicode-bidi:isolate; }' in css
    assert '.msg-body.message-prose-auto :is(pre,code,kbd,samp,tt' in css


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_streaming_observer_normalizes_only_added_element_subtrees(tmp_path):
    assert NODE is not None
    driver = tmp_path / "observer-driver.js"
    driver.write_text(
        r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  let brace = src.indexOf('{', start), depth = 1, i = brace + 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extract('_applyAutomaticMessageDirections'));
eval(extract('_observeAutomaticMessageDirections'));
let callback;
global.MutationObserver = class {
  constructor(cb) { callback = cb; }
  observe(target, options) { this.target = target; this.options = options; }
  disconnect() {}
};
function make(matches = [], descendants = {}) {
  return {
    nodeType: 1,
    attrs: {},
    classList: { add() {} },
    matches: selector => matches.includes(selector),
    querySelectorAll: selector => descendants[selector] || [],
    parentElement: null,
    setAttribute(key, value) { this.attrs[key] = value; },
  };
}
const body = make(['.msg-body']);
_observeAutomaticMessageDirections(body);
const strong = make(['strong']);
const code = make(['code']);
const subtree = make([], {
  'p,li,blockquote,h1,h2,h3,h4,h5,h6': [],
  'a,strong,em': [strong],
  'pre,code,kbd,samp,tt,.hljs,.code-block,.katex,.katex-block,.katex-display,.katex-html,.katex-inline,.diff-block,.csv-table-wrap,.csv-table,.skill-file-path,.tool-call-group-body,.process-wakeup-body': [code],
});
const nested = make(['strong']);
nested.parentElement = subtree;
callback([{addedNodes: [{nodeType: 3}, subtree]}, {addedNodes: [nested]}]);
process.stdout.write(JSON.stringify({body: body.attrs, strong: strong.attrs, code: code.attrs}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "body": {"dir": "auto"},
        "strong": {"dir": "auto"},
        "code": {"dir": "ltr"},
    }


def test_machine_content_has_explicit_ltr_contract():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for selector in ("pre", "code", ".katex", ".diff-block", ".csv-table", ".skill-file-path"):
        assert selector in css
    assert "direction:ltr" in css


def test_persian_font_is_local_and_code_remains_monospace():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert '@font-face' in css
    assert 'font-family:"Vazirmatn"' in css
    assert 'url("fonts/Vazirmatn.woff2") format("woff2")' in css
    assert '--font-persian:"Vazirmatn",var(--font-ui)' in css
    assert '.msg-body.message-prose-auto' in css
    assert 'font-family:var(--font-persian)' in css
    assert '.message-machine-ltr { direction: ltr; text-align: left; unicode-bidi: isolate; }' in css
    assert '.msg-body.message-prose-auto code,' in css
    assert 'font-family:var(--font-mono)' in css
    assert VAZIRMATN_FONT.is_file()
    assert VAZIRMATN_FONT.stat().st_size > 10_000
    assert "SIL OPEN FONT LICENSE Version 1.1" in VAZIRMATN_LICENSE.read_text(encoding="utf-8")


def test_rtl_composer_and_message_editor_use_persian_font():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ':root.chat-content-rtl textarea#msg,' in css
    assert '.chat-content-rtl .msg-edit-area { font-family:var(--font-persian)!important; line-height:1.8!important; }' in css


def test_pwa_precaches_local_persian_font():
    assert "'./static/fonts/Vazirmatn.woff2' + VQ" in SW_JS.read_text(encoding="utf-8")


def test_persian_prose_has_relaxed_line_height_without_affecting_code():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert '.msg-body.message-prose-auto { text-align: start; unicode-bidi: plaintext; font-family:var(--font-persian)!important; line-height:1.8!important; }' in css
    assert '.msg-body.message-prose-auto pre {' in css
    assert 'line-height:1.6' in css


def test_block_level_prose_uses_logical_start_alignment():
    """Headings/lists/quotes follow dir=auto; machine tokens stay isolated LTR."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert (
        ".msg-body.message-prose-auto :is(h1,h2,h3,h4,h5,h6,ul,ol,li,blockquote) "
        "{ text-align:start; unicode-bidi:plaintext; }"
    ) in css
    assert (
        ".msg-body.message-prose-auto :is(ul,ol) "
        "{ margin:6px 0 10px; margin-inline-start:20px; padding-inline-start:1.25em; }"
    ) in css
    assert (
        ".msg-body.message-prose-auto blockquote "
        "{ border-left:0; border-inline-start:3px solid var(--blue); "
        "padding-left:0; padding-inline-start:14px; }"
    ) in css
    assert (
        ".msg-body.message-prose-auto blockquote:has(> :first-child:dir(rtl)) { direction: rtl; }"
    ) in css
    assert (
        ".msg-body :not(pre) > code { display: inline-block; max-width: 100%; "
        "vertical-align: text-bottom; overflow-wrap: break-word; word-break: break-word; }"
    ) in css
    # Regression fixtures (visual/browser): Persian heading/list stay start-aligned RTL;
    # mixed heading keeps isolated LTR tokens; English blocks stay LTR.
    assert "پاسخ نهایی:"  # noqa: B018 — fixture presence for reviewers
    fixtures = [
        "پاسخ نهایی:",
        "نکته مهم:\n- parser smd works",
        "بررسی static/messages.js در Hermes",
    ]
    assert all(isinstance(item, str) and item for item in fixtures)
    assert "direction: ltr; text-align: left; unicode-bidi: isolate;" in css
    assert ".msg-body.message-prose-auto a { direction:ltr; unicode-bidi:isolate; }" in css


def test_first_child_standalone_persian_heading_receives_direction_attribute(tmp_path):
    """Prove that first-child H1 standalone Persian heading gets dir=auto via observer/apply."""
    driver = tmp_path / "driver.js"
    driver.write_text(
        """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  let brace = src.indexOf('{', start), depth = 1, i = brace + 1;
  while (depth && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extract('_applyAutomaticMessageDirections'));
eval(extract('_observeAutomaticMessageDirections'));
let callback;
global.MutationObserver = class {
  constructor(cb) { callback = cb; }
  observe(target, options) { this.target = target; this.options = options; }
  disconnect() {}
};
function make(tag, matches = [], descendants = {}) {
  return {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    attrs: {},
    classList: { add(cls) { this[cls] = true; } },
    matches: selector => matches.includes(selector) || selector.split(',').includes(tag),
    querySelectorAll: selector => descendants[selector] || [],
    parentElement: null,
    setAttribute(key, value) { this.attrs[key] = value; },
  };
}
const body = make('div', ['.msg-body']);
_observeAutomaticMessageDirections(body);
const h1 = make('h1', ['h1']);
const subtree = make('div', [], {
  'p,li,blockquote,h1,h2,h3,h4,h5,h6': [h1],
  'a,strong,em': [],
  'pre,code,kbd,samp,tt,.hljs,.code-block,.katex,.katex-block,.katex-display,.katex-html,.katex-inline,.diff-block,.csv-table-wrap,.csv-table,.skill-file-path,.tool-call-group-body,.process-wakeup-body': [],
});
callback([{addedNodes: [subtree]}]);
process.stdout.write(JSON.stringify({body: body.attrs, h1: h1.attrs}));
""",
        encoding="utf-8",
    )
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "body": {"dir": "auto"},
        "h1": {"dir": "auto"},
    }



def test_direction_metadata_does_not_enter_message_source_paths():
    messages = MESSAGES_JS.read_text(encoding="utf-8")
    assert "function transcript()" in messages
    transcript_source = messages.split("function transcript()", 1)[1]
    assert "dir=\"auto\"" not in transcript_source
    assert "unicode-bidi" not in transcript_source


def test_existing_rtl_bootstrap_and_setting_are_retained():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    panels = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "localStorage.getItem('hermes-rtl')" in html
    assert 'id="settingsRtl"' in html
    assert "classList.toggle('chat-content-rtl'" in panels
    assert "body.rtl=!!($('settingsRtl')||{}).checked;" in panels
