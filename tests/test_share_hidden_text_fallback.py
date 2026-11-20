"""Public share page: a message the page would partly hide is shown as plain text.

``static/share.js`` renders each message with ``renderMd`` and then compares the
words of the published text with the words the rendered message shows. If any
published word is missing, the whole message is shown as plain text instead.
These tests load the real ``share.js`` under node and exercise the word counting
on small DOM trees. The end-to-end proof, with the real renderer in a real share
page plus a differential run over random messages, is
``tests/browser_share_hidden_text.py`` (run by the browser-smoke workflow).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARE_JS = ROOT / "static" / "share.js"
SHARE_HTML = ROOT / "static" / "share.html"
WORKFLOW = ROOT / ".github" / "workflows" / "browser-smoke.yml"
NODE = shutil.which("node")

# Loads share.js into a vm context (its DOMContentLoaded hook is a no-op here)
# and evaluates each request. A request is {fn, args}; a DOM argument is a
# compact tree: ["tag", {attrs}, ...children], a string is a text node and
# {"comment": "..."} is a comment node.
_DRIVER = r"""
const fs = require('fs'); const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const ctx = {console, document: {addEventListener(){}}, window: {location: {pathname: '/'}}};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const build = n => {
  if (typeof n === 'string') return {nodeType: 3, data: n};
  if (n && n.comment !== undefined) return {nodeType: 8, data: n.comment};
  const [tag, attrs, ...kids] = n;
  return {nodeType: 1, tagName: tag.toUpperCase(),
          attributes: Object.entries(attrs || {}).map(([name, value]) => ({name, value})),
          childNodes: kids.map(build)};
};
const out = JSON.parse(fs.readFileSync(0, 'utf8')).map(req => {
  const args = req.args.map(a => (a && a.dom !== undefined) ? {nodeType: 1, tagName: 'DIV', attributes: [], childNodes: a.dom.map(build)} : a);
  const t0 = Date.now();
  let value = ctx[req.fn](...args);
  if (Object.prototype.toString.call(value) === '[object Map]') value = Object.fromEntries(value);  // a Map from the vm realm
  return {value, ms: Date.now() - t0};
});
fs.writeSync(1, JSON.stringify(out));
"""


class _ShareJs:
    """Calls functions of the real share.js under node."""

    def __init__(self, node: str, driver: Path):
        self.node, self.driver = node, driver

    def many(self, requests):
        result = subprocess.run(
            [self.node, str(self.driver), str(SHARE_JS)],
            input=json.dumps(requests), capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def __call__(self, fn, *args):
        return self.many([{"fn": fn, "args": list(args)}])[0]["value"]


@pytest.fixture(scope="module")
def call(tmp_path_factory):
    node = NODE
    if node is None:
        pytest.skip("node not on PATH")
    driver = tmp_path_factory.mktemp("share_js") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    return _ShareJs(node, driver)


def _missing(call, source, *dom):
    return call("_shareMissingWords", source, {"dom": list(dom)})


# ── Counting words ────────────────────────────────────────────────────────────


def test_add_words(call):
    got = call("_shareSourceWords", "Un*believ*able snake_case `co`de AND and")
    assert got == {"unbelievable": 1, "snakecase": 1, "code": 1, "and": 2}


def test_cjk_and_accented_words_survive(call):
    got = call("_shareSourceWords", "中文**粗体**文字 naïve café")
    assert got == {"中文粗体文字": 1, "naïve": 1, "café": 1}


def test_source_drops_markup_that_never_shows_as_words(call):
    got = call(
        "_shareSourceWords",
        "&amp;lt;b&amp;gt;bold&lt;/b&gt; <br> E=mc<sup>2</sup> MEDIA:https://e.com/a.png\n"
        "- [x] done\n```python\ncode\n```",
    )
    assert "lt" not in got and "amp" not in got and "b" not in got and "br" not in got and "sup" not in got
    assert "media" not in got and "x" not in got and "python" not in got
    assert {"bold", "e", "mc2", "https", "com", "a", "png", "done", "code"} <= set(got)


def test_source_keeps_words_the_page_cannot_show(call):
    got = call(
        "_shareSourceWords",
        '<span data-note="note words" title="tip">t</span> <custom-tag>c</custom-tag> <span odd>o</span>\n'
        "```js two words\nx\n```",
    )
    # attribute values, unknown attribute and tag names, and a multi-word fence label
    assert {"note", "words", "tip", "data", "custom", "odd", "js", "two"} <= set(got)
    assert "span" not in got and "title" not in got


def test_inline_tags_join_and_block_tags_split(call):
    assert call("_shareSourceWords", "x<b>y</b>z") == {"xyz": 1}
    assert call("_shareSourceWords", "x<br>y") == {"x": 1, "y": 1}


# ── What the rendered message shows ───────────────────────────────────────────


def test_comments_are_not_shown(call):
    assert _missing(call, "a <!-- b --> c", ["p", {}, "a ", {"comment": " b "}, " c"]) == ["b"]


def test_counts_catch_a_hidden_copy_of_a_visible_word(call):
    assert _missing(call, "word <!-- word -->", ["p", {}, "word ", {"comment": " word "}]) == ["word"]


def test_block_elements_split_words_inline_elements_join_them(call):
    assert _missing(call, "- a\n- b", ["ul", {}, ["li", {}, "a"], ["li", {}, "b"]]) == []
    assert _missing(call, "**bo**ld", ["p", {}, ["strong", {}, "bo"], "ld"]) == []


def test_addresses_alt_text_and_renderer_attributes_count(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/docs", "target": "_blank", "rel": "noopener"}, "docs"], " ",
           ["img", {"src": "https://e.com/cat.png", "alt": "a cat", "class": "msg-media-img", "loading": "lazy"}]]
    src = '[docs](https://e.com/docs) <img src="https://e.com/cat.png" class="msg-media-img" alt="a cat" loading="lazy">'
    assert _missing(call, src, dom) == []


def test_a_bare_link_counts_its_address_once(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/word"}, "https://e.com/word"], " ", {"comment": " word "}]
    assert _missing(call, "https://e.com/word <!-- word -->", dom) == ["word"]


def test_percent_encoded_addresses_match_plain_ones(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/a%20b"}, "x"]]
    assert _missing(call, "[x](https://e.com/a b)", dom) == []


@pytest.mark.parametrize(
    "attrs",
    [{"data-mermaid-id": "secretword"}, {"download": "secretword"}, {"style": "secretword"},
     {"class": "language-secretword"}],
)
def test_free_text_attributes_never_count(call, attrs):
    dom = ["p", {}, ["span", attrs, "x"]]
    assert _missing(call, "x secretword", dom) == ["secretword"]


def test_ordinary_message_has_nothing_missing(call):
    src = "## Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n`Vec<T>` and $x$"
    dom = ["h2", {}, "Title"], ["table", {}, ["tr", {}, ["th", {}, "a"], ["th", {}, "b"]],
                                ["tr", {}, ["td", {}, "1"], ["td", {}, "2"]]], \
          ["p", {}, ["code", {}, "Vec<T>"], " and ", ["span", {"class": "katex-inline"}, "x"]]
    assert _missing(call, src, *dom) == []


# ── Cost ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["<a " * 100_000, "<" * 400_000, '<a x="' + "b " * 200_000, "<a b=" * 80_000, "&amp;" * 100_000,
     "- [x] \n" * 50_000, "```\n" * 100_000],
    ids=["unclosed_tags", "lt_run", "open_quote", "open_values", "entities", "task_boxes", "fences"],
)
def test_source_words_stay_linear(call, text):
    out = call.many([{"fn": "_shareSourceWords", "args": [text]}])[0]
    assert out["ms"] < 2000, out["ms"]


# ── Wiring ────────────────────────────────────────────────────────────────────


def test_share_page_checks_each_message_before_highlighting():
    # Behaviour is proven in the browser gate; this pins the exact statement so a
    # disabled or bypassed check fails the ordinary suite too.
    js = SHARE_JS.read_text(encoding="utf-8")
    render = js[js.index("function _shareRenderMessages"):]
    stmt = "if(body&&_shareMissingWords(source,body).length) _shareShowPlainText(body,source);"
    assert render.count(stmt) == 1
    assert render.index("const body=row.querySelector('.share-message-body');") < render.index(stmt)
    assert render.index(stmt) < render.index("wrap.appendChild(row);") < render.index("highlightCode(wrap)")


def test_plain_text_view_is_styled_and_uses_text_content():
    js = SHARE_JS.read_text(encoding="utf-8")
    plain = js[js.index("function _shareShowPlainText"):js.index("function _shareRenderMessages")]
    assert "pre.textContent=" in plain and "innerHTML" not in plain
    html = SHARE_HTML.read_text(encoding="utf-8")
    assert ".share-plain-note" in html and "pre.share-plain-text" in html


def test_browser_gate_runs_in_ci():
    assert "python tests/browser_share_hidden_text.py" in WORKFLOW.read_text(encoding="utf-8")
