"""Regression coverage for RTL/bidi handling outside `.msg-body`.

The `_applyAutomaticMessageDirections` helper (added for PR #6560) only walks
`.msg-body` subtrees, so two surfaces that render user-authored/model-authored
free text outside that scope never got bidi resolution:

1. The "thinking" card body (`_thinkingCardHtml` in `static/ui.js`) renders
   reasoning text in a plain `<pre>` for whitespace preservation. Without an
   explicit `dir="auto"` on that element, the browser inherits the surrounding
   chat's base direction, so a mostly-Hebrew reasoning block gets left-aligned
   and its bidi runs are resolved incorrectly.
2. The sidebar session list is rendered by a completely separate code path
   (`static/sessions.js`) that never received a `dir` attribute or bidi-aware
   CSS at all, so Hebrew session titles/metadata/previews render left-aligned
   with no bidi resolution.

These tests pin the source-level fix for both so it cannot silently regress:
the `<pre>` tag must carry `dir="auto"`, and the CSS must give the *actual
rendered* sidebar classes an explicit bidi-aware right-aligned rule scoped to
the RTL chat skin (`.chat-content-rtl`), matching the pattern already used for
`.msg-body`.

A first version of this fix targeted a `.session-preview` class that
`static/sessions.js` never creates (real classes are `.session-title`,
`.session-meta`, `.session-search-preview` — see
`_renderSessionRow`/`renderSessionListFromCache` around
`static/sessions.js:8148-8268`), so the CSS rule silently never matched any
rendered preview text. This test asserts against the classes the renderer
actually instantiates, sourced from `static/sessions.js` itself, so a
class-name mismatch like that cannot pass silently again.

A second review round (on PR #7135) caught that the `dir="auto"` added to the
thinking `<pre>` never survived to the final rendered DOM: the SAME
`_applyAutomaticMessageDirections` machine-classification pass that forces
`.msg-body pre` back to `dir="ltr"` for real code blocks also matched the
thinking-card `<pre>` (it has no `.msg-body` class scoping, `pre` alone is in
`machineSelector`), silently overwriting `dir="auto"` back to `dir="ltr"`
right after it was set. On top of that, the live/legacy streaming path
(`_thinkingMarkup()`, used while a response is still streaming) never got the
`dir="auto"` attribute added to it at all, unlike the cached/history render
path (`_thinkingCardHtml()`). Prior tests only checked the constructors'
*source* for the attribute — none of them ran the full pipeline (constructor
+ `_applyAutomaticMessageDirections`) and inspected the *final* DOM, which is
exactly why the overwrite slipped through review. `TestThinkingCardFinalDom`
below closes that gap by executing both constructors AND the direction pass
in a Node DOM shim and asserting the post-pipeline `dir` attribute.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = UI_JS.find(f"function {name}(")
    assert start != -1, f"{name} not found in static/ui.js"
    brace = UI_JS.find("{", start)
    assert brace != -1, f"opening brace not found for {name}"
    depth = 1
    i = brace + 1
    while depth and i < len(UI_JS):
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"unterminated function {name}"
    return UI_JS[start:i]


class TestThinkingCardBidi:
    def test_thinking_card_body_pre_has_dir_auto(self):
        src = _function_source("_thinkingCardHtml")
        assert '<pre dir="auto">' in src, (
            "thinking-card-body <pre> must carry dir=\"auto\" so the browser "
            "resolves per-paragraph bidi direction for Hebrew reasoning text "
            "instead of inheriting the chat's base LTR/RTL direction"
        )

    def test_thinking_card_body_pre_auto_is_right_aligned_in_rtl_skin(self):
        assert re.search(
            r'\.chat-content-rtl\s+\.thinking-card-body\s+pre\[dir="auto"\]\s*\{[^}]*text-align:\s*right',
            STYLE_CSS,
        ), (
            "expected a .chat-content-rtl .thinking-card-body pre[dir=\"auto\"] "
            "rule setting text-align:right so dir=\"auto\" resolution is "
            "reflected visually, not just in the DOM attribute"
        )


class TestSessionListBidi:
    # The classes static/sessions.js actually instantiates for row title,
    # detailed-density metadata, and search-match preview text. Extracted
    # from the renderer's own className assignments so a rename there is
    # caught here too, instead of the CSS test drifting from reality.
    RENDERED_CLASSNAMES = [
        m.group(1)
        for m in re.finditer(r"\.className\s*=\s*'(session-[a-z-]+)'", SESSIONS_JS)
    ]

    def test_renderer_still_uses_expected_classnames(self):
        # Guards the test itself: if sessions.js stops emitting any of these
        # classes, the CSS-rule assertions below would trivially pass against
        # a selector nothing renders, exactly like the .session-preview bug.
        for expected in ("session-title", "session-meta", "session-search-preview"):
            assert expected in self.RENDERED_CLASSNAMES, (
                f"static/sessions.js no longer assigns className='{expected}' -- "
                "update this test and the CSS selectors in static/style.css to "
                "match whatever the renderer actually emits"
            )

    def test_no_stale_session_preview_class_referenced_in_css(self):
        assert not re.search(r"\.chat-content-rtl\s+\.session-preview\b", STYLE_CSS), (
            ".session-preview is not a class static/sessions.js ever creates "
            "(see .session-search-preview) -- a rule targeting it can never "
            "match rendered markup"
        )

    def test_rendered_sidebar_classes_have_bidi_rule_in_rtl_skin(self):
        for selector in (".session-title", ".session-meta", ".session-search-preview"):
            assert selector.lstrip(".") in self.RENDERED_CLASSNAMES
            pattern = (
                r"\.chat-content-rtl\s+" + re.escape(selector) + r"[,{]"
            )
            assert re.search(pattern, STYLE_CSS), (
                f"expected a .chat-content-rtl {selector} rule — the sidebar "
                "session list is rendered outside .msg-body so the automatic "
                "per-message direction pass never reaches it; it needs its "
                "own bidi-aware rule"
            )

    def test_rendered_sidebar_classes_use_plaintext_bidi_and_right_align(self):
        block_match = re.search(
            r"\.chat-content-rtl\s+\.session-title,\s*"
            r"\.chat-content-rtl\s+\.session-meta,\s*"
            r"\.chat-content-rtl\s+\.session-search-preview\s*\{([^}]*)\}",
            STYLE_CSS,
        )
        assert block_match, (
            "expected a combined .chat-content-rtl .session-title, "
            ".session-meta, .session-search-preview rule block"
        )
        body = block_match.group(1)
        assert "unicode-bidi:plaintext" in body.replace(" ", ""), (
            "unicode-bidi:plaintext is required so each title/preview "
            "resolves its own base direction from its first strong "
            "character, the same algorithm dir=\"auto\" uses (plain CSS has "
            "no direction:auto)"
        )
        assert "text-align:right" in body.replace(" ", ""), (
            "session list entries should default to right-aligned layout "
            "to match a predominantly Hebrew/RTL session list"
        )


# --- Full-pipeline DOM execution: constructor + _applyAutomaticMessageDirections ---
#
# A minimal DOM shim with just enough support (tag/class selectors,
# querySelectorAll, matches, closest, innerHTML->children materialization) to
# run the real _applyAutomaticMessageDirections against markup produced by
# the real _thinkingCardHtml/_thinkingMarkup constructors, and inspect the
# attribute the browser would actually end up with.
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
    if (simple.startsWith('.')) {
      const cls = simple.slice(1);
      return this.className.split(/\s+/).filter(Boolean).includes(cls);
    }
    return this.tagName === simple.toUpperCase();
  }
  matches(selector) {
    return selector.split(',').map(s => s.trim()).some(s => this._selfMatchesSimple(s));
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
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
// Extremely small HTML->tree parser: enough for the thinking-card markup
// (nested divs/spans/pre/button, no attributes we need to preserve besides
// class and dir, text content escaped by esc() already).
function parseFragment(html) {
  const root = new FakeElement('div', '__root__');
  const stack = [root];
  const tagRe = /<(\/?)([a-zA-Z0-9-]+)([^>]*)>/g;
  let lastIndex = 0;
  let m;
  function pushText(text) {
    if (text) {
      const textNode = new FakeElement('#text', '');
      textNode.matches = () => false;
      textNode.text = text;
      stack[stack.length - 1].appendChild(textNode);
    }
  }
  while ((m = tagRe.exec(html))) {
    pushText(html.slice(lastIndex, m.index));
    lastIndex = tagRe.lastIndex;
    const [, closing, tag, attrsRaw] = m;
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const classMatch = attrsRaw.match(/class="([^"]*)"/);
    const dirMatch = attrsRaw.match(/dir="([^"]*)"/);
    const el = new FakeElement(tag, classMatch ? classMatch[1] : '');
    if (dirMatch) el.attrs.dir = dirMatch[1];
    stack[stack.length - 1].appendChild(el);
    const selfClosing = /\/>$/.test(m[0]) || ['br', 'img', 'input'].includes(tag.toLowerCase());
    if (!selfClosing) stack.push(el);
  }
  pushText(html.slice(lastIndex));
  return root;
}
"""


def _function_source_from(source: str, name: str) -> str:
    start = source.find(f"function {name}(")
    assert start != -1, f"{name} not found"
    brace = source.find("{", start)
    assert brace != -1
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


def _const_source(name: str) -> str:
    marker = f"const {name}="
    start = UI_JS.find(marker)
    assert start != -1, f"{name} not found"
    i = start + len(marker)
    depth = 0
    in_regex = False
    while i < len(UI_JS):
        ch = UI_JS[i]
        if in_regex:
            if ch == "\\":
                i += 2
                continue
            if ch == "/":
                in_regex = False
        elif ch == "/" and UI_JS[i - 1] not in ")]" and depth == 0:
            in_regex = True
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == ";" and depth == 0:
            return UI_JS[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated const {name}")


def _run_node(harness: str) -> dict:
    node = shutil.which("node")
    if not node:  # pragma: no cover - depends on environment
        pytest.skip("node not available")
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _pipeline_harness(constructor_call: str) -> str:
    """Build/render a thinking card via the real constructor, run the real
    _applyAutomaticMessageDirections over it, and report the final dir
    attribute + text content of its <pre>.
    """
    thinking_card_html_src = _function_source_from(UI_JS, "_thinkingCardHtml")
    thinking_markup_src = _function_source_from(UI_JS, "_thinkingMarkup")
    sanitize_src = _function_source_from(UI_JS, "_sanitizeThinkingDisplayText")
    apply_dirs_src = _function_source_from(UI_JS, "_applyAutomaticMessageDirections")
    prune_src = _function_source_from(UI_JS, "_pruneAutomaticMessageDirectionObservers")
    esc_src = _const_source("esc")
    # Stub out helpers _thinkingCardHtml/_thinkingMarkup call that aren't
    # relevant to direction/escaping behavior.
    stubs = textwrap.dedent(
        """
        function t(key){ return key; }
        function li(){ return ''; }
        function _worklogDetailsExpandedDefault(){ return false; }
        function _stripXmlToolCallsDisplay(s){ return s; }
        """
    )
    return textwrap.dedent(
        f"""
        {_DOM_SHIM}
        {stubs}
        {esc_src}
        {sanitize_src}
        {thinking_card_html_src}
        {thinking_markup_src}
        const _automaticMessageDirectionObservers = new Map();
        {prune_src}
        {apply_dirs_src}
        const html = {constructor_call};
        const root = parseFragment(html);
        _applyAutomaticMessageDirections(root);
        const pre = root.querySelectorAll('pre')[0];
        console.log(JSON.stringify({{
          dir: pre ? pre.getAttribute('dir') : null,
          classes: pre ? pre.className : null,
          text: pre ? pre.children.map(c => c.text || '').join('') : null,
        }}));
        """
    )


XSS_PAYLOAD = "<script>alert(1)</script><img src=x onerror=alert(2)>"


class TestThinkingCardFinalDom:
    """Exercises the FULL pipeline (constructor + direction pass), because a
    source-substring check on the constructor alone cannot catch a later pass
    silently overwriting the attribute it just set — which is exactly what
    happened here (see module docstring)."""

    def test_cached_render_pre_stays_dir_auto_after_direction_pass(self):
        result = _run_node(_pipeline_harness("_thinkingCardHtml('שלום עולם', true)"))
        assert result["dir"] == "auto", (
            "the machine-classification pass in _applyAutomaticMessageDirections "
            "must not force the thinking-card <pre> back to dir=\"ltr\" -- it "
            "should be excluded like .csv-table is excluded from further "
            "prose-direction reprocessing"
        )
        assert "message-machine-ltr" not in (result["classes"] or "")

    def test_live_streaming_pre_stays_dir_auto_after_direction_pass(self):
        result = _run_node(_pipeline_harness("_thinkingMarkup('שלום עולם')"))
        assert result["dir"] == "auto", (
            "_thinkingMarkup() (the live/legacy streaming constructor) must "
            "also emit dir=\"auto\", matching _thinkingCardHtml(); otherwise "
            "streaming Hebrew/Arabic reasoning never gets bidi resolution "
            "even though the cached/history render path does"
        )
        assert "message-machine-ltr" not in (result["classes"] or "")

    def test_cached_render_escapes_script_injection_payload(self):
        result = _run_node(
            _pipeline_harness(f"_thinkingCardHtml({json.dumps(XSS_PAYLOAD)}, true)")
        )
        assert "<script>" not in result["text"]
        assert "<img" not in result["text"]
        assert "&lt;script&gt;" in result["text"]

    def test_live_streaming_escapes_script_injection_payload(self):
        result = _run_node(_pipeline_harness(f"_thinkingMarkup({json.dumps(XSS_PAYLOAD)})"))
        assert "<script>" not in result["text"]
        assert "<img" not in result["text"]
        assert "&lt;script&gt;" in result["text"]

    def test_machine_classification_pass_excludes_thinking_card_body(self):
        src = _function_source_from(UI_JS, "_applyAutomaticMessageDirections")
        assert "closest" in src and "thinking-card-body" in src, (
            "expected the machine-classification loop to skip nodes inside "
            ".thinking-card-body (e.g. via node.closest('.thinking-card-body')) "
            "so it cannot re-force dir=\"ltr\" onto the thinking <pre> right "
            "after dir=\"auto\" was applied to it"
        )

